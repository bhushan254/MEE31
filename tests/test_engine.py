from datetime import timedelta

from sqlalchemy import select

from outreach import engine as eng
from outreach import graph
from outreach.db import CampaignLead, Lead, Message, Suppression, is_suppressed, set_setting
from outreach.templating import render

from .conftest import NOW


def test_render_variables_fallback_and_spintax():
    text, missing = render("{Hi|Hi} {{first_name|there}}, {{company}} {{nope}}", {"first_name": "", "company": "Acme"})
    assert text == "Hi there, Acme "
    assert missing == {"nope"}


def test_sends_first_step_rotating_mailboxes(session, fake, setup_campaign):
    c, mbs, leads = setup_campaign(n_leads=3, n_mailboxes=2)
    assert eng.run_send_tick(session, NOW) == 2  # one per mailbox per tick
    assert {s["to"] for s in fake.sent} == {"lead0@client0.com", "lead1@client1.com"}
    assert fake.sent[0]["subject"] == "Hi Lead0"
    assert "Hello Lead0 from Pune" in fake.sent[0]["html"]
    # Gap: same tick time again sends nothing.
    assert eng.run_send_tick(session, NOW) == 0
    assert eng.run_send_tick(session, NOW + timedelta(minutes=2)) == 1
    cls = session.scalars(select(CampaignLead)).all()
    assert all(cl.next_step == 1 for cl in cls)
    assert all(cl.next_send_at == cl.last_sent_at + timedelta(days=2) for cl in cls)


def test_followup_is_threaded_reply_from_same_mailbox(session, fake, setup_campaign):
    c, mbs, leads = setup_campaign(n_leads=1, n_mailboxes=2)
    eng.run_send_tick(session, NOW)
    first = fake.sent[0]
    later = NOW + timedelta(days=2, minutes=5)
    assert eng.run_send_tick(session, later) == 1
    follow = fake.sent[1]
    assert follow["reply_to"] == first["id"]
    assert follow["to"] == "lead0@client0.com"
    assert follow["conversationId"] == first["conversationId"]
    assert follow["html"].index("Follow-up 1") < follow["html"].index("quoted")
    cl = session.scalar(select(CampaignLead))
    assert cl.status == "completed"
    msgs = session.scalars(select(Message).where(Message.direction == "out")).all()
    assert len({m.mailbox_id for m in msgs}) == 1


def test_respects_window_and_daily_limit(session, fake, setup_campaign):
    setup_campaign(n_leads=5, n_mailboxes=1, daily_limit=2)
    saturday = NOW + timedelta(days=3)
    assert eng.run_send_tick(session, saturday) == 0
    night = NOW.replace(hour=20)
    assert eng.run_send_tick(session, night) == 0
    t = NOW
    total = 0
    for _ in range(6):
        total += eng.run_send_tick(session, t)
        t += timedelta(minutes=5)
    assert total == 2


def test_global_pause(session, fake, setup_campaign):
    setup_campaign()
    set_setting(session, "sending_paused", "1")
    session.commit()
    assert eng.run_send_tick(session, NOW) == 0


def inbound(fake_msg_id, conv, sender, subject="Re: Hi", text="Sounds good, let's talk", headers=None):
    return {
        "id": fake_msg_id, "conversationId": conv, "internetMessageId": f"<{fake_msg_id}@x>",
        "from": {"emailAddress": {"address": sender}}, "subject": subject,
        "receivedDateTime": "2026-11-04T11:00:00Z", "bodyPreview": text,
        "uniqueBody": {"content": f"<div>{text}</div>"}, "body": {"content": f"<p>{text}</p>"},
        "internetMessageHeaders": headers or [],
    }


def test_reply_stops_sequence(session, fake, setup_campaign):
    c, mbs, leads = setup_campaign(n_leads=1, n_mailboxes=1)
    eng.run_send_tick(session, NOW)
    fake.inbox = [inbound("in1", fake.sent[0]["conversationId"], "lead0@client0.com")]
    eng.run_inbox_tick(session, NOW, force=True)
    cl = session.scalar(select(CampaignLead))
    assert cl.status == "replied"
    assert session.get(Lead, leads[0].id).status == "replied"
    # processing again doesn't duplicate
    eng.run_inbox_tick(session, NOW, force=True)
    assert session.scalar(select(Message).where(Message.direction == "in")).kind == "reply"
    assert len(session.scalars(select(Message).where(Message.direction == "in")).all()) == 1
    assert eng.run_send_tick(session, NOW + timedelta(days=3)) == 0


def test_auto_reply_does_not_stop(session, fake, setup_campaign):
    setup_campaign(n_leads=1, n_mailboxes=1)
    eng.run_send_tick(session, NOW)
    fake.inbox = [inbound("in1", fake.sent[0]["conversationId"], "lead0@client0.com", subject="Automatic reply: Hi")]
    eng.run_inbox_tick(session, NOW, force=True)
    assert session.scalar(select(CampaignLead)).status == "active"
    assert session.scalar(select(Message).where(Message.direction == "in")).kind == "auto_reply"


def test_unsubscribe_reply_blocklists(session, fake, setup_campaign):
    setup_campaign(n_leads=1, n_mailboxes=1)
    eng.run_send_tick(session, NOW)
    fake.inbox = [inbound("in1", fake.sent[0]["conversationId"], "lead0@client0.com", text="Please remove me from your list")]
    eng.run_inbox_tick(session, NOW, force=True)
    assert session.scalar(select(CampaignLead)).status == "unsubscribed"
    assert is_suppressed(session, "lead0@client0.com")


def test_bounce_detected_by_body_and_campaign_autopauses(session, fake, setup_campaign):
    c, mbs, leads = setup_campaign(n_leads=10, n_mailboxes=1)
    c.min_gap_minutes = c.max_gap_minutes = 1
    t = NOW
    for _ in range(10):
        eng.run_send_tick(session, t)
        t += timedelta(minutes=2)
    assert len(fake.sent) == 10
    fake.inbox = [
        inbound("ndr1", "other-conv", "postmaster@mydomain0.com", subject="Undeliverable: Hi Lead3",
                text="Delivery has failed to these recipients: lead3@client3.com"),
    ]
    eng.run_inbox_tick(session, t, force=True)
    lead3 = session.scalar(select(Lead).where(Lead.email == "lead3@client3.com"))
    assert lead3.status == "bounced"
    assert session.scalar(select(Suppression).where(Suppression.value == "lead3@client3.com")).reason == "bounce"
    assert session.get(type(c), c.id).status == "paused"  # 1/10 = 10% > 5%


def test_suppressed_lead_is_skipped(session, fake, setup_campaign):
    from outreach.db import add_suppression

    setup_campaign(n_leads=1, n_mailboxes=1)
    add_suppression(session, "client0.com")
    session.commit()
    eng.run_send_tick(session, NOW)
    assert fake.sent == []
    assert session.scalar(select(CampaignLead)).status == "stopped"


def test_send_blocked_pauses_mailbox(session, fake, setup_campaign):
    c, mbs, leads = setup_campaign(n_leads=2, n_mailboxes=1)
    fake.errors = [graph.GraphError(403, "ErrorMessageSubmissionBlocked", "blocked")]
    eng.run_send_tick(session, NOW)
    assert session.get(type(mbs[0]), mbs[0].id).status == "error"
    assert session.scalar(select(CampaignLead).order_by(CampaignLead.id)).next_step == 0


def test_campaign_completes(session, fake, setup_campaign):
    c, mbs, leads = setup_campaign(n_leads=1, n_mailboxes=1, steps=1)
    eng.run_send_tick(session, NOW)
    eng.run_send_tick(session, NOW + timedelta(minutes=5))
    assert session.get(type(c), c.id).status == "completed"


def test_is_bounce_ignores_delays():
    assert eng.is_bounce("postmaster@x.com", "Undeliverable: Hello")
    assert not eng.is_bounce("postmaster@x.com", "Delivery delayed: Hello")
    assert eng.is_bounce("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)")
