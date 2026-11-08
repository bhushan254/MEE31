"""The sending engine: schedules campaign emails, sends follow-ups, and reads replies/bounces."""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from . import graph
from .unsubscribe import link_html
from .db import (
    Campaign,
    CampaignLead,
    Lead,
    Mailbox,
    Message,
    SessionLocal,
    add_suppression,
    get_setting,
    is_suppressed,
    log_event,
    utcnow,
)
from .templating import build_email_html, html_to_text, lead_context, render

log = logging.getLogger("outreach")

# One lock so the background engine and "do it now" buttons never send at the same time.
engine_lock = threading.RLock()

BOUNCE_SENDERS = ("postmaster@", "mailer-daemon@", "microsoftexchange")
BOUNCE_SUBJECTS = (
    "undeliverable",
    "delivery status notification (failure)",
    "mail delivery failed",
    "returned mail",
    "delivery failure",
    "failure notice",
    "undelivered mail",
    "mail delivery subsystem",
    "message not delivered",
)
AUTO_REPLY_SUBJECTS = ("automatic reply", "auto reply", "autoreply", "auto:", "out of office", "out of the office")
UNSUB_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|stop emailing|stop sending|do not (contact|email)|"
    r"don'?t (contact|email)|opt[\s-]?out|no more emails)\b",
    re.I,
)
EMAIL_FIND_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ------------------------------------------------------------------ helpers


def get_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def day_start_utc(tz: ZoneInfo, now: datetime | None = None) -> datetime:
    now = now or utcnow()
    local = now.replace(tzinfo=timezone.utc).astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def in_window(campaign: Campaign, now: datetime) -> bool:
    local = now.replace(tzinfo=timezone.utc).astimezone(get_tz(campaign.timezone))
    return local.weekday() in campaign.send_day_list and campaign.start_hour <= local.hour < campaign.end_hour


def sent_today(session, mailbox_id: int, now: datetime | None = None) -> int:
    start = day_start_utc(get_tz(get_setting(session, "timezone")), now)
    return session.scalar(
        select(func.count(Message.id)).where(
            Message.mailbox_id == mailbox_id,
            Message.direction == "out",
            Message.kind.in_(["campaign", "manual"]),
            Message.created_at >= start,
        )
    ) or 0


def client_for(session, mailbox: Mailbox) -> graph.GraphClient:
    """Return a Graph client with a fresh access token (refreshing it when needed)."""
    if mailbox.access_token and mailbox.token_expires_at and mailbox.token_expires_at > utcnow() + timedelta(minutes=5):
        return graph.GraphClient(mailbox.access_token)
    if not mailbox.refresh_token:
        raise graph.AuthError("No sign-in stored for this mailbox")
    tok = graph.refresh_token(
        get_setting(session, "ms_client_id"),
        get_setting(session, "ms_client_secret"),
        get_setting(session, "ms_tenant"),
        mailbox.refresh_token,
    )
    mailbox.access_token = tok["access_token"]
    mailbox.refresh_token = tok.get("refresh_token", mailbox.refresh_token)
    mailbox.token_expires_at = tok["expires_at"]
    session.commit()
    return graph.GraphClient(mailbox.access_token)


def mark_reconnect(session, mailbox: Mailbox, reason: str) -> None:
    mailbox.status = "reconnect"
    mailbox.status_reason = f"Microsoft sign-in expired, please reconnect. ({reason[:200]})"
    log_event(session, f"{mailbox.email}: sign-in expired. Click Reconnect on the Mailboxes page.", "error", mailbox_id=mailbox.id)
    session.commit()


def _insert_after_body_tag(document: str, fragment: str) -> str:
    m = re.search(r"<body[^>]*>", document, re.I)
    if not m:
        return fragment + "<br>" + document
    return document[: m.end()] + fragment + "<br>" + document[m.end():]


# ------------------------------------------------------------------ sending


def pick_next(session, campaign: Campaign, mailbox: Mailbox, now: datetime) -> CampaignLead | None:
    # Follow-ups first: they belong to the mailbox that sent the first email.
    cl = session.scalar(
        select(CampaignLead)
        .where(
            CampaignLead.campaign_id == campaign.id,
            CampaignLead.status == "active",
            CampaignLead.mailbox_id == mailbox.id,
            CampaignLead.next_step > 0,
            CampaignLead.next_send_at <= now,
        )
        .order_by(CampaignLead.next_send_at)
        .limit(1)
    )
    if cl:
        return cl
    if campaign.max_new_per_day:
        start = day_start_utc(get_tz(campaign.timezone), now)
        new_today = session.scalar(
            select(func.count(Message.id)).where(
                Message.campaign_id == campaign.id,
                Message.direction == "out",
                Message.step_position == 0,
                Message.created_at >= start,
            )
        ) or 0
        if new_today >= campaign.max_new_per_day:
            return None
    return session.scalar(
        select(CampaignLead)
        .where(
            CampaignLead.campaign_id == campaign.id,
            CampaignLead.status == "active",
            CampaignLead.next_step == 0,
            or_(CampaignLead.mailbox_id.is_(None), CampaignLead.mailbox_id == mailbox.id),
            or_(CampaignLead.next_send_at.is_(None), CampaignLead.next_send_at <= now),
        )
        .order_by(CampaignLead.id)
        .limit(1)
    )


def compose(session, campaign: Campaign, step_index: int, lead: Lead, mailbox: Mailbox) -> dict:
    step = campaign.steps[step_index]
    ctx = lead_context(lead, mailbox)
    subject, miss1 = render(step.subject, ctx)
    body, miss2 = render(step.body, ctx)
    signature, _ = render(mailbox.signature or "", ctx)
    optout = render(campaign.optout_text or "", ctx)[0] if campaign.include_optout else ""
    return {
        "subject": subject.strip(),
        "body_text": body,
        "html": build_email_html(body, signature, optout, link_html(session, campaign, lead, mailbox)),
        "missing": sorted(miss1 | miss2),
    }


def _send_followup_in_thread(client: graph.GraphClient, cl: CampaignLead, html: str, lead: Lead) -> dict:
    try:
        draft = client.create_reply(cl.last_message_id)
    except graph.GraphError as exc:
        if exc.status != 404 or not cl.last_internet_message_id:
            raise
        found = client.find_by_internet_id("sentitems", cl.last_internet_message_id)
        if not found:
            raise
        draft = client.create_reply(found["id"])
    quoted = (draft.get("body") or {}).get("content", "")
    client.update(
        draft["id"],
        {
            "body": {"contentType": "HTML", "content": _insert_after_body_tag(quoted, html) if quoted else html},
            "toRecipients": [graph.recipient(lead.email, lead.full_name)],
            "ccRecipients": [],
            "bccRecipients": [],
        },
    )
    client.send(draft["id"])
    return draft


def stop_lead_everywhere(session, lead: Lead, status: str, only_stop_on_reply: bool = False) -> None:
    rows = session.scalars(
        select(CampaignLead).where(CampaignLead.lead_id == lead.id, CampaignLead.status == "active")
    ).all()
    for cl in rows:
        if only_stop_on_reply and not cl.campaign.stop_on_reply:
            continue
        cl.status = status
        cl.next_send_at = None


def send_step(session, campaign: Campaign, cl: CampaignLead, mailbox: Mailbox, now: datetime) -> bool:
    lead = cl.lead
    if is_suppressed(session, lead.email) or lead.status in ("bounced", "unsubscribed"):
        cl.status = "unsubscribed" if lead.status == "unsubscribed" else "stopped"
        cl.error = "On blocklist / bounced / unsubscribed"
        cl.next_send_at = None
        session.commit()
        return False
    if campaign.stop_on_reply and lead.status == "replied":
        cl.status = "replied"
        cl.next_send_at = None
        session.commit()
        return False
    if cl.next_step >= len(campaign.steps):
        cl.status = "completed"
        cl.next_send_at = None
        session.commit()
        return False

    mail = compose(session, campaign, cl.next_step, lead, mailbox)
    step_no = cl.next_step + 1
    try:
        client = client_for(session, mailbox)
        threaded = cl.next_step > 0 and not mail["subject"] and cl.last_message_id
        if threaded:
            sent = _send_followup_in_thread(client, cl, mail["html"], lead)
        else:
            subject = mail["subject"] or ("Re: " + compose(session, campaign, 0, lead, mailbox)["subject"])
            sent = client.create_message(subject, mail["html"], lead.email, lead.full_name)
            client.send(sent["id"])
            sent.setdefault("subject", subject)
    except graph.AuthError as exc:
        mark_reconnect(session, mailbox, str(exc))
        return False
    except graph.GraphError as exc:
        if exc.is_throttled:
            mailbox.next_send_after = now + timedelta(minutes=10)
            log_event(session, f"{mailbox.email}: Microsoft asked us to slow down, retrying in 10 minutes.", "warn", campaign.id, mailbox.id)
        elif exc.is_send_blocked:
            mailbox.status = "error"
            mailbox.status_reason = f"Microsoft blocked sending: {exc.message[:300]}"
            log_event(session, f"{mailbox.email}: Microsoft blocked sending ({exc.code}). Mailbox paused. Check it in Outlook / Microsoft 365 admin.", "error", campaign.id, mailbox.id)
        elif exc.status == 400:
            cl.status = "failed"
            cl.error = exc.message[:500]
            cl.next_send_at = None
            log_event(session, f"Could not send to {lead.email}: {exc.message[:200]}", "error", campaign.id, mailbox.id)
        else:
            mailbox.next_send_after = now + timedelta(minutes=10)
            log_event(session, f"{mailbox.email}: send error {exc.status} {exc.code}, retrying in 10 minutes.", "error", campaign.id, mailbox.id)
        session.commit()
        return False

    session.add(
        Message(
            mailbox_id=mailbox.id,
            lead_id=lead.id,
            campaign_id=campaign.id,
            campaign_lead_id=cl.id,
            step_position=cl.next_step,
            direction="out",
            kind="campaign",
            subject=sent.get("subject") or mail["subject"],
            body_html=mail["html"],
            preview=mail["body_text"][:240],
            from_email=mailbox.email,
            to_email=lead.email,
            graph_id=sent.get("id", ""),
            internet_message_id=sent.get("internetMessageId", "") or "",
            conversation_id=sent.get("conversationId", "") or "",
            is_read=True,
            created_at=now,
        )
    )
    cl.mailbox_id = mailbox.id
    cl.last_message_id = sent.get("id", "")
    cl.last_internet_message_id = sent.get("internetMessageId", "") or ""
    cl.last_sent_at = now
    cl.error = ""
    cl.next_step += 1
    if cl.next_step >= len(campaign.steps):
        cl.status = "completed"
        cl.next_send_at = None
    else:
        cl.next_send_at = now + timedelta(days=max(0, campaign.steps[cl.next_step].delay_days))
    if lead.status == "new":
        lead.status = "contacted"
    lo, hi = sorted([max(1, campaign.min_gap_minutes), max(1, campaign.max_gap_minutes)])
    mailbox.next_send_after = now + timedelta(minutes=random.uniform(lo, hi))
    log_event(session, f"Sent step {step_no} to {lead.email} from {mailbox.email}", "info", campaign.id, mailbox.id)
    session.commit()
    return True


def run_send_tick(session, now: datetime | None = None) -> int:
    """Send at most one email per mailbox per campaign. Returns number of emails sent."""
    now = now or utcnow()
    if get_setting(session, "sending_paused") == "1":
        return 0
    sent = 0
    campaigns = session.scalars(select(Campaign).where(Campaign.status == "active")).all()
    for campaign in campaigns:
        if not campaign.steps:
            continue
        remaining = session.scalar(
            select(func.count(CampaignLead.id)).where(
                CampaignLead.campaign_id == campaign.id, CampaignLead.status == "active"
            )
        )
        if not remaining:
            total = session.scalar(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == campaign.id))
            if total:
                campaign.status = "completed"
                log_event(session, f"Campaign '{campaign.name}' finished: every lead has completed the sequence.", "info", campaign.id)
                session.commit()
            continue
        if not in_window(campaign, now):
            continue
        for mailbox in campaign.mailboxes:
            if mailbox.status != "active":
                continue
            if mailbox.next_send_after and mailbox.next_send_after > now:
                continue
            if sent_today(session, mailbox.id, now) >= mailbox.daily_limit:
                continue
            cl = pick_next(session, campaign, mailbox, now)
            if cl and send_step(session, campaign, cl, mailbox, now):
                sent += 1
    return sent


# ------------------------------------------------------------------ inbox processing


def parse_graph_time(value: str | None) -> datetime:
    if not value:
        return utcnow()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return utcnow()


def is_bounce(from_email: str, subject: str) -> bool:
    s = subject.lower().strip()
    if "delayed" in s or "delay" in s.split(":")[0]:
        return False
    return from_email.startswith(BOUNCE_SENDERS[:2]) or BOUNCE_SENDERS[2] in from_email or s.startswith(BOUNCE_SUBJECTS)


def is_auto_reply(subject: str, headers: list | None) -> bool:
    if subject.lower().strip().startswith(AUTO_REPLY_SUBJECTS):
        return True
    for h in headers or []:
        name = (h.get("name") or "").lower()
        value = (h.get("value") or "").lower()
        if name == "auto-submitted" and value and value != "no":
            return True
        if name in ("x-autoreply", "x-autorespond") or (name == "precedence" and value in ("auto_reply", "bulk", "junk")):
            return True
    return False


def is_unsubscribe_request(subject: str, text: str) -> bool:
    """A reply saying unsubscribe/stop, or the email created by clicking the unsubscribe link."""
    subj = re.sub(r"^\s*((re|aw|sv|fw|fwd)\s*:\s*)+", "", subject or "", flags=re.I).strip()
    if UNSUB_RE.search(subj) and not (subject or "").lower().startswith(("re:", "aw:")):
        return True
    if subj.lower().rstrip(".!") in ("unsubscribe", "stop", "remove"):
        return True
    return bool(UNSUB_RE.search((text or "")[:600])) or (text or "").strip().lower().rstrip(".!") in ("stop", "remove")


def unsubscribe_lead(session, lead: Lead, how: str, campaign_id=None, mailbox_id=None) -> None:
    lead.status = "unsubscribed"
    add_suppression(session, lead.email, "unsubscribed")
    stop_lead_everywhere(session, lead, "unsubscribed")
    log_event(session, f"{lead.email} unsubscribed ({how}). Added to blocklist.", "warn", campaign_id, mailbox_id)


def bounce_check(session, campaign: Campaign) -> None:
    contacted = session.scalar(
        select(func.count(CampaignLead.id)).where(
            CampaignLead.campaign_id == campaign.id, CampaignLead.last_sent_at.is_not(None)
        )
    ) or 0
    bounced = session.scalar(
        select(func.count(CampaignLead.id)).where(
            CampaignLead.campaign_id == campaign.id, CampaignLead.status == "bounced"
        )
    ) or 0
    if contacted >= 10 and campaign.status == "active" and bounced * 100.0 / contacted > campaign.bounce_pause_pct:
        campaign.status = "paused"
        log_event(
            session,
            f"Campaign '{campaign.name}' auto-paused: bounce rate {bounced * 100.0 / contacted:.1f}% is above your "
            f"{campaign.bounce_pause_pct:g}% limit. Clean the lead list before resuming.",
            "error",
            campaign.id,
        )


def handle_inbound(session, mailbox: Mailbox, m: dict) -> str | None:
    """Process one received email. Returns what it was (reply/auto_reply/bounce) or None if unrelated."""
    gid = m.get("id", "")
    imid = m.get("internetMessageId") or ""
    if session.scalar(select(Message.id).where(Message.mailbox_id == mailbox.id, Message.graph_id == gid)):
        return None
    if imid and session.scalar(select(Message.id).where(Message.direction == "in", Message.internet_message_id == imid)):
        return None
    from_email = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
    if not from_email or from_email == mailbox.email.lower():
        return None
    subject = m.get("subject") or ""
    conv = m.get("conversationId") or ""
    body_html = (m.get("body") or {}).get("content", "")
    unique_text = html_to_text((m.get("uniqueBody") or {}).get("content", "")) or (m.get("bodyPreview") or "")
    received = parse_graph_time(m.get("receivedDateTime"))

    out = None
    if conv:
        out = session.scalar(
            select(Message)
            .where(Message.mailbox_id == mailbox.id, Message.direction == "out", Message.conversation_id == conv, Message.lead_id.is_not(None))
            .order_by(Message.id.desc())
            .limit(1)
        )

    if is_bounce(from_email, subject):
        lead = out.lead if out else None
        if lead is None:
            text = html_to_text(body_html).lower()
            for addr in dict.fromkeys(a.lower() for a in EMAIL_FIND_RE.findall(text)):
                if addr in (mailbox.email.lower(), from_email):
                    continue
                cand = session.scalar(select(Lead).where(Lead.email == addr))
                if cand and session.scalar(
                    select(Message.id).where(Message.lead_id == cand.id, Message.mailbox_id == mailbox.id, Message.direction == "out")
                ):
                    lead = cand
                    break
                    # (if the bounced address is not one of our leads we ignore the bounce)
        if lead is None:
            return None
        last_out = out or session.scalar(
            select(Message).where(Message.lead_id == lead.id, Message.mailbox_id == mailbox.id, Message.direction == "out").order_by(Message.id.desc()).limit(1)
        )
        session.add(
            Message(
                mailbox_id=mailbox.id, lead_id=lead.id, campaign_id=last_out.campaign_id if last_out else None,
                campaign_lead_id=last_out.campaign_lead_id if last_out else None, direction="in", kind="bounce",
                subject=subject, body_html=body_html, preview=(m.get("bodyPreview") or "")[:240],
                from_email=from_email, to_email=mailbox.email, graph_id=gid, internet_message_id=imid,
                conversation_id=conv, is_read=False, created_at=received,
            )
        )
        lead.status = "bounced"
        add_suppression(session, lead.email, "bounce")
        stop_lead_everywhere(session, lead, "bounced")
        session.flush()
        log_event(session, f"Bounce: {lead.email} (removed from all campaigns and blocklisted)", "warn",
                  last_out.campaign_id if last_out else None, mailbox.id)
        if last_out and last_out.campaign:
            bounce_check(session, last_out.campaign)
        session.commit()
        return "bounce"

    lead = out.lead if out else session.scalar(select(Lead).where(Lead.email == from_email))
    if lead is None:
        return None
    kind = "auto_reply" if is_auto_reply(subject, m.get("internetMessageHeaders")) else "reply"
    if out is None:
        out = session.scalar(
            select(Message).where(Message.lead_id == lead.id, Message.direction == "out").order_by(Message.id.desc()).limit(1)
        )
        if out is None:
            return None  # a lead we never emailed: not a campaign reply
    session.add(
        Message(
            mailbox_id=mailbox.id, lead_id=lead.id, campaign_id=out.campaign_id, campaign_lead_id=out.campaign_lead_id,
            direction="in", kind=kind, subject=subject, body_html=body_html, preview=unique_text[:240],
            from_email=from_email, to_email=mailbox.email, graph_id=gid, internet_message_id=imid,
            conversation_id=conv, is_read=False, created_at=received,
        )
    )
    if kind == "reply":
        if is_unsubscribe_request(subject, unique_text):
            unsubscribe_lead(session, lead, "by email", out.campaign_id, mailbox.id)
        else:
            if lead.status not in ("unsubscribed", "bounced"):
                lead.status = "replied"
            stop_lead_everywhere(session, lead, "replied", only_stop_on_reply=True)
            log_event(session, f"New reply from {lead.email}", "info", out.campaign_id, mailbox.id)
    session.commit()
    return kind


def check_mailbox(session, mailbox: Mailbox, now: datetime | None = None) -> dict:
    now = now or utcnow()
    counts = {"reply": 0, "auto_reply": 0, "bounce": 0}
    since = (mailbox.last_inbox_check or mailbox.created_at) - timedelta(minutes=15)
    try:
        client = client_for(session, mailbox)
        for folder in ("inbox", "junkemail"):
            try:
                messages = client.list_since(folder, since)
            except graph.GraphError as exc:
                if folder == "inbox":
                    raise
                log.warning("junk folder check failed for %s: %s", mailbox.email, exc)
                continue
            for m in messages:
                kind = handle_inbound(session, mailbox, m)
                if kind:
                    counts[kind] += 1
    except graph.AuthError as exc:
        mark_reconnect(session, mailbox, str(exc))
        return counts
    except graph.GraphError as exc:
        log_event(session, f"{mailbox.email}: could not read inbox ({exc.status} {exc.code})", "warn", mailbox_id=mailbox.id)
        session.commit()
        return counts
    mailbox.last_inbox_check = now
    session.commit()
    return counts


def run_inbox_tick(session, now: datetime | None = None, force: bool = False) -> None:
    now = now or utcnow()
    try:
        interval = max(1, int(get_setting(session, "inbox_check_minutes") or 5))
    except ValueError:
        interval = 5
    for mailbox in session.scalars(select(Mailbox).where(Mailbox.status != "reconnect")).all():
        if not force and mailbox.last_inbox_check and mailbox.last_inbox_check > now - timedelta(minutes=interval):
            continue
        check_mailbox(session, mailbox, now)


# ------------------------------------------------------------------ background thread


class Scheduler(threading.Thread):
    TICK_SECONDS = 30

    def __init__(self):
        super().__init__(daemon=True, name="outreach-scheduler")
        self.stop_event = threading.Event()
        self.last_tick: datetime | None = None
        self.last_error: str = ""

    def run(self) -> None:
        time.sleep(3)
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(self.TICK_SECONDS)

    def tick(self) -> None:
        with engine_lock, SessionLocal() as session:
            for fn in (run_inbox_tick, run_send_tick):
                try:
                    fn(session)
                except Exception as exc:  # never let the loop die
                    session.rollback()
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("scheduler error")
                    try:
                        log_event(session, f"Engine error: {self.last_error[:300]}", "error")
                        session.commit()
                    except Exception:
                        session.rollback()
        self.last_tick = utcnow()

    def stop(self) -> None:
        self.stop_event.set()


scheduler: Scheduler | None = None
