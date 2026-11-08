"""Tests for the Hello box update: unsubscribe link, templates, safe database upgrade."""

import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from outreach import engine as eng
from outreach import unsubscribe
from outreach.db import Campaign, CampaignLead, Lead, Mailbox, SessionLocal, Template, init_db, is_suppressed, set_setting
from outreach.web import create_app

from .conftest import NOW
from .test_engine import inbound


# ---------------------------------------------------------------- unsubscribe link


def test_email_contains_mailto_unsubscribe_link(session, fake, setup_campaign):
    setup_campaign(n_leads=1, n_mailboxes=1)
    eng.run_send_tick(session, NOW)
    html = fake.sent[0]["html"]
    assert 'href="mailto:me0@mydomain0.com?subject=Unsubscribe&amp;body=' in html
    assert ">Unsubscribe</a>" in html


def test_unsubscribe_link_can_be_turned_off(session, fake, setup_campaign):
    c, _, _ = setup_campaign(n_leads=1, n_mailboxes=1)
    c.include_unsub_link = False
    session.commit()
    eng.run_send_tick(session, NOW)
    assert "<a " not in fake.sent[0]["html"]


def test_clicking_mailto_link_unsubscribes(session, fake, setup_campaign):
    setup_campaign(n_leads=1, n_mailboxes=1)
    eng.run_send_tick(session, NOW)
    # The mailto link creates a brand-new email (new conversation) with subject "Unsubscribe".
    fake.inbox = [inbound("u1", "new-conv", "lead0@client0.com", subject="Unsubscribe",
                          text="Please unsubscribe me from your emails.")]
    eng.run_inbox_tick(session, NOW, force=True)
    assert session.scalar(select(CampaignLead)).status == "unsubscribed"
    assert is_suppressed(session, "lead0@client0.com")
    assert eng.run_send_tick(session, NOW + timedelta(days=3)) == 0


def test_normal_reply_is_not_unsubscribe():
    assert not eng.is_unsubscribe_request("Re: Quick question", "Yes, interested. Let's talk.")
    assert eng.is_unsubscribe_request("Re: Quick question", "Stop")
    assert eng.is_unsubscribe_request("Unsubscribe", "")


def test_public_link_page_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB_URL", f"sqlite:///{tmp_path / 'u.db'}")
    with TestClient(create_app(start_scheduler=False)) as client:
        with SessionLocal() as db:
            set_setting(db, "public_url", "https://hello.example.com")
            mb = Mailbox(email="me@mine.com")
            lead = Lead(email="x@lead.com")
            db.add_all([mb, lead])
            db.commit()
            url = unsubscribe.unsubscribe_url(db, lead, mb)
            assert url.startswith("https://hello.example.com/u/")
        path = url.replace("https://hello.example.com", "")
        assert "Unsubscribe?" in client.get(path).text  # opening the link alone does nothing
        with SessionLocal() as db:
            assert not is_suppressed(db, "x@lead.com")
        assert "unsubscribed" in client.post(path).text
        with SessionLocal() as db:
            assert is_suppressed(db, "x@lead.com")
        assert "not valid" in client.get(path[:-3] + "abc").text


# ---------------------------------------------------------------- templates


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    with TestClient(create_app(start_scheduler=False)) as c:
        yield c


def test_templates_create_edit_use(client):
    assert client.get("/templates").status_code == 200
    r = client.post("/templates", data={"name": "Dentist copy"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/templates/")[1].split("?")[0])
    r = client.post(f"/templates/{tid}", data={
        "name": "Dentist copy v2", "subject": ["Hi {{first_name}}", "", ""],
        "body": ["Email one", "Follow one", "Follow two"], "delay_days": ["0", "2", "5"]})
    assert "Template saved" in r.text
    with SessionLocal() as db:
        t = db.get(Template, tid)
        assert t.name == "Dentist copy v2" and [s["delay_days"] for s in t.steps] == [0, 2, 5]
    assert "Dentist copy v2" in client.get("/templates").text
    with SessionLocal() as db:
        db.add(Mailbox(email="me@mine.com", display_name="Me"))
        db.commit()
    pv = client.post("/templates/preview", json={"subject": "Hi {{first_name}}", "body": "Hello"})
    assert pv.status_code == 200 and "Unsubscribe" in pv.json()["html"]

    # Add to a NEW campaign
    r = client.post(f"/templates/{tid}/use", data={"campaign_id": "new", "campaign_name": "From tpl"})
    assert "New campaign created" in r.text
    with SessionLocal() as db:
        c = db.scalar(select(Campaign).where(Campaign.name == "From tpl"))
        assert [s.body for s in c.steps] == ["Email one", "Follow one", "Follow two"]
        cid = c.id

    # Add to an EXISTING campaign (replaces its emails)
    other = client.post("/campaigns", data={"name": "Existing"}, follow_redirects=False)
    oid = int(other.headers["location"].split("/campaigns/")[1].split("?")[0])
    r = client.post(f"/templates/{tid}/use", data={"campaign_id": str(oid)})
    assert "added to this campaign" in r.text
    with SessionLocal() as db:
        assert len(db.get(Campaign, oid).steps) == 3

    # Save a campaign's emails as a template
    r = client.post(f"/campaigns/{cid}/save-template", data={"name": "Saved from campaign"})
    assert "Saved as template" in r.text
    assert client.get(f"/campaigns/{cid}").status_code == 200

    client.post(f"/templates/{tid}/duplicate")
    client.post(f"/templates/{tid}/delete")
    with SessionLocal() as db:
        names = sorted(t.name for t in db.scalars(select(Template)).all())
        assert names == ["Dentist copy v2 (copy)", "Saved from campaign"]


def test_rebrand(client):
    assert "Hello box" in client.get("/").text


# ---------------------------------------------------------------- database upgrade


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    init_db(f"sqlite:///{path}")
    with SessionLocal() as s:
        s.add(Campaign(name="Kept"))
        s.commit()
    # Simulate a database created by the previous version (no new columns / tables).
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE campaigns DROP COLUMN include_unsub_link")
    con.execute("ALTER TABLE campaigns DROP COLUMN unsub_link_text")
    con.execute("DROP TABLE templates")
    con.commit()
    con.close()

    init_db(f"sqlite:///{path}")  # starting the new version
    with SessionLocal() as s:
        c = s.scalar(select(Campaign))
        assert c.name == "Kept" and c.include_unsub_link is True and c.unsub_link_text == "Unsubscribe"
        s.add(Template(name="t", steps=[]))
        s.commit()
