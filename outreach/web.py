"""Dashboard web app (FastAPI + server-rendered pages)."""

from __future__ import annotations

import math
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import available_timezones

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select

from . import engine as eng
from . import graph
from .db import (
    Campaign,
    CampaignLead,
    EventLog,
    Lead,
    Mailbox,
    Message,
    SessionLocal,
    Setting,
    Step,
    Suppression,
    add_suppression,
    get_setting,
    init_db,
    is_suppressed,
    log_event,
    set_setting,
    utcnow,
)
from .leads_import import parse_csv
from .templating import build_email_html, find_variables, lead_context, render, text_to_html

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

LABELS = {
    "interested": "Interested",
    "meeting_booked": "Meeting booked",
    "not_interested": "Not interested",
    "closed": "Closed / won",
}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIMEZONES = sorted(available_timezones() - {"localtime", "Factory"})
PAGE_SIZE = 50

oauth_states: dict[str, float] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if app.state.start_scheduler:
        eng.scheduler = eng.Scheduler()
        eng.scheduler.start()
    yield
    if eng.scheduler:
        eng.scheduler.stop()


def create_app(start_scheduler: bool = True) -> FastAPI:
    app = FastAPI(title="Outreach", lifespan=lifespan)
    app.state.start_scheduler = start_scheduler
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    register_routes(app)
    return app


# ------------------------------------------------------------------ helpers


def fmt_dt(value, tzname: str = "UTC", fmt: str = "%d %b %Y, %H:%M") -> str:
    if not value:
        return "—"
    from datetime import timezone

    return value.replace(tzinfo=timezone.utc).astimezone(eng.get_tz(tzname)).strftime(fmt)


def redirect(url: str, ok: str = "", err: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if ok:
        url += f"{sep}ok={quote(ok)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


def page(request: Request, db, name: str, **ctx) -> HTMLResponse:
    tzname = get_setting(db, "timezone")
    ctx.update(
        request=request,
        tz=tzname,
        dt=lambda v, f="%d %b %Y, %H:%M": fmt_dt(v, tzname, f),
        unread=db.scalar(
            select(func.count(Message.id)).where(Message.direction == "in", Message.kind == "reply", Message.is_read.is_(False))
        ) or 0,
        sending_paused=get_setting(db, "sending_paused") == "1",
        labels=LABELS,
        ok=request.query_params.get("ok", ""),
        err=request.query_params.get("err", ""),
    )
    return templates.TemplateResponse(request, name, ctx)


def to_int(value, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def campaign_stats(db, campaign_id: int) -> dict:
    def count_cl(*conds):
        return db.scalar(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == campaign_id, *conds)) or 0

    total = count_cl()
    contacted = count_cl(CampaignLead.last_sent_at.is_not(None))
    sent = db.scalar(
        select(func.count(Message.id)).where(Message.campaign_id == campaign_id, Message.direction == "out", Message.kind == "campaign")
    ) or 0
    replied_leads = db.scalar(
        select(func.count(func.distinct(Message.lead_id))).where(
            Message.campaign_id == campaign_id, Message.direction == "in", Message.kind == "reply"
        )
    ) or 0
    bounced = count_cl(CampaignLead.status == "bounced")
    unsub = count_cl(CampaignLead.status == "unsubscribed")
    interested = db.scalar(
        select(func.count(CampaignLead.id))
        .join(Lead, Lead.id == CampaignLead.lead_id)
        .where(CampaignLead.campaign_id == campaign_id, Lead.label.in_(["interested", "meeting_booked", "closed"]))
    ) or 0
    return {
        "total": total,
        "active": count_cl(CampaignLead.status == "active"),
        "completed": count_cl(CampaignLead.status == "completed"),
        "contacted": contacted,
        "sent": sent,
        "replied": replied_leads,
        "reply_rate": (replied_leads * 100.0 / contacted) if contacted else 0.0,
        "bounced": bounced,
        "bounce_rate": (bounced * 100.0 / contacted) if contacted else 0.0,
        "unsubscribed": unsub,
        "interested": interested,
    }


def redirect_uri(db) -> str:
    return get_setting(db, "base_url").rstrip("/") + "/auth/callback"


def ms_configured(db) -> bool:
    return bool(get_setting(db, "ms_client_id") and get_setting(db, "ms_client_secret"))


def list_counts(db) -> list[tuple[str, int]]:
    return db.execute(select(Lead.list_name, func.count(Lead.id)).group_by(Lead.list_name).order_by(Lead.list_name)).all()


# ------------------------------------------------------------------ routes


def register_routes(app: FastAPI) -> None:  # noqa: C901 (one place for all pages)
    # -------------------------------------------------------- overview
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        with SessionLocal() as db:
            now = utcnow()
            mailboxes = db.scalars(select(Mailbox).order_by(Mailbox.email)).all()
            mb_rows = [(m, eng.sent_today(db, m.id, now)) for m in mailboxes]
            week = now - timedelta(days=7)
            month = now - timedelta(days=30)
            sent_7 = db.scalar(select(func.count(Message.id)).where(Message.direction == "out", Message.kind == "campaign", Message.created_at >= week)) or 0
            replies_7 = db.scalar(select(func.count(Message.id)).where(Message.direction == "in", Message.kind == "reply", Message.created_at >= week)) or 0
            sent_30 = db.scalar(select(func.count(Message.id)).where(Message.direction == "out", Message.kind == "campaign", Message.created_at >= month)) or 0
            bounces_30 = db.scalar(select(func.count(Message.id)).where(Message.kind == "bounce", Message.created_at >= month)) or 0
            setup = {
                "ms": ms_configured(db),
                "timezone": db.get(Setting, "timezone") is not None,
                "mailbox": bool(mailboxes),
                "leads": bool(db.scalar(select(func.count(Lead.id)))),
                "campaign": bool(db.scalar(select(func.count(Campaign.id)))),
            }
            recent_replies = db.scalars(
                select(Message).where(Message.direction == "in", Message.kind == "reply").order_by(Message.created_at.desc()).limit(6)
            ).all()
            events = db.scalars(select(EventLog).order_by(EventLog.id.desc()).limit(12)).all()
            active_campaigns = db.scalars(select(Campaign).where(Campaign.status == "active")).all()
            sched = eng.scheduler
            return page(
                request, db, "overview.html",
                mb_rows=mb_rows,
                sent_today_total=sum(s for _, s in mb_rows),
                capacity=sum(m.daily_limit for m in mailboxes if m.status == "active"),
                sent_7=sent_7, replies_7=replies_7,
                reply_rate_7=(replies_7 * 100.0 / sent_7) if sent_7 else 0.0,
                bounce_rate_30=(bounces_30 * 100.0 / sent_30) if sent_30 else 0.0,
                setup=setup, recent_replies=recent_replies, events=events,
                active_campaigns=active_campaigns,
                engine_running=bool(sched and sched.is_alive()),
                last_tick=sched.last_tick if sched else None,
            )

    @app.post("/sending/toggle")
    def toggle_sending():
        with SessionLocal() as db:
            paused = get_setting(db, "sending_paused") == "1"
            set_setting(db, "sending_paused", "0" if paused else "1")
            log_event(db, "All sending resumed." if paused else "All sending paused (global pause button).", "warn")
            db.commit()
        return redirect("/", ok="Sending resumed" if paused else "All sending paused")

    # -------------------------------------------------------- settings
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        with SessionLocal() as db:
            return page(
                request, db, "settings.html",
                client_id=get_setting(db, "ms_client_id"),
                has_secret=bool(get_setting(db, "ms_client_secret")),
                tenant=get_setting(db, "ms_tenant"),
                inbox_minutes=get_setting(db, "inbox_check_minutes"),
                redirect_uri=redirect_uri(db),
                timezones=TIMEZONES,
            )

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        with SessionLocal() as db:
            set_setting(db, "ms_client_id", str(form.get("ms_client_id", "")).strip())
            set_setting(db, "ms_tenant", str(form.get("ms_tenant", "")).strip() or "organizations")
            secret = str(form.get("ms_client_secret", "")).strip()
            if secret:
                set_setting(db, "ms_client_secret", secret)
            tzname = str(form.get("timezone", "UTC"))
            if tzname in TIMEZONES:
                set_setting(db, "timezone", tzname)
            set_setting(db, "inbox_check_minutes", str(to_int(form.get("inbox_check_minutes"), 5, 1, 60)))
            db.commit()
        return redirect("/settings", ok="Settings saved")

    # -------------------------------------------------------- mailboxes + Microsoft sign-in
    @app.get("/mailboxes", response_class=HTMLResponse)
    def mailboxes_page(request: Request):
        with SessionLocal() as db:
            now = utcnow()
            rows = [(m, eng.sent_today(db, m.id, now)) for m in db.scalars(select(Mailbox).order_by(Mailbox.email)).all()]
            return page(request, db, "mailboxes.html", rows=rows, ms_ready=ms_configured(db))

    @app.get("/mailboxes/connect")
    def mailbox_connect():
        with SessionLocal() as db:
            if not ms_configured(db):
                return redirect("/settings", err="First add your Microsoft app details (step 1 of the setup guide).")
            state = secrets.token_urlsafe(24)
            oauth_states[state] = utcnow().timestamp()
            url = graph.authorize_url(get_setting(db, "ms_client_id"), get_setting(db, "ms_tenant"), redirect_uri(db), state)
        return RedirectResponse(url, status_code=303)

    @app.get("/auth/callback")
    def auth_callback(request: Request):
        q = request.query_params
        if q.get("error"):
            return redirect("/mailboxes", err=f"Microsoft sign-in failed: {q.get('error_description', q.get('error'))[:300]}")
        state = q.get("state", "")
        if state not in oauth_states:
            return redirect("/mailboxes", err="Sign-in link expired. Please click Connect again.")
        oauth_states.pop(state, None)
        with SessionLocal() as db:
            try:
                tok = graph.exchange_code(
                    get_setting(db, "ms_client_id"), get_setting(db, "ms_client_secret"),
                    get_setting(db, "ms_tenant"), redirect_uri(db), q.get("code", ""),
                )
                me = graph.GraphClient(tok["access_token"]).me()
            except (graph.AuthError, graph.GraphError) as exc:
                return redirect("/mailboxes", err=f"Could not connect: {str(exc)[:300]}")
            email = (me.get("mail") or me.get("userPrincipalName") or "").lower()
            mb = db.scalar(select(Mailbox).where(Mailbox.email == email))
            is_new = mb is None
            if is_new:
                mb = Mailbox(email=email, display_name=me.get("displayName") or "")
                db.add(mb)
            mb.ms_user_id = me.get("id", "")
            mb.access_token = tok["access_token"]
            mb.refresh_token = tok.get("refresh_token", "")
            mb.token_expires_at = tok["expires_at"]
            if mb.status in ("reconnect", "error") or is_new:
                mb.status = "active"
                mb.status_reason = ""
            log_event(db, f"Mailbox connected: {email}")
            db.commit()
        return redirect("/mailboxes", ok=f"{email} connected")

    @app.post("/mailboxes/{mid}/update")
    async def mailbox_update(mid: int, request: Request):
        form = await request.form()
        with SessionLocal() as db:
            mb = db.get(Mailbox, mid)
            if mb:
                mb.display_name = str(form.get("display_name", mb.display_name)).strip()
                mb.daily_limit = to_int(form.get("daily_limit"), 25, 1, 200)
                mb.signature = str(form.get("signature", ""))
                db.commit()
        return redirect("/mailboxes", ok="Mailbox saved")

    @app.post("/mailboxes/{mid}/toggle")
    def mailbox_toggle(mid: int):
        with SessionLocal() as db:
            mb = db.get(Mailbox, mid)
            if not mb:
                return redirect("/mailboxes")
            if mb.status == "active":
                mb.status = "paused"
            elif mb.status in ("paused", "error"):
                mb.status, mb.status_reason = "active", ""
            db.commit()
            return redirect("/mailboxes", ok=f"{mb.email} is now {mb.status}")

    @app.post("/mailboxes/{mid}/delete")
    def mailbox_delete(mid: int):
        with SessionLocal() as db:
            mb = db.get(Mailbox, mid)
            if mb:
                db.delete(mb)
                db.commit()
        return redirect("/mailboxes", ok="Mailbox removed")

    @app.post("/mailboxes/{mid}/test")
    async def mailbox_test(mid: int, request: Request):
        form = await request.form()
        to = str(form.get("to", "")).strip()
        with eng.engine_lock, SessionLocal() as db:
            mb = db.get(Mailbox, mid)
            if not mb or "@" not in to:
                return redirect("/mailboxes", err="Enter an email address for the test")
            sig = render(mb.signature or "", {"sender_name": mb.display_name, "sender_email": mb.email})[0]
            html = build_email_html("Hi,\n\nThis is a test email from your outreach dashboard. If you can read this, the mailbox is connected correctly.", sig)
            try:
                client = eng.client_for(db, mb)
                msg = client.create_message("Test email from your outreach dashboard", html, to)
                client.send(msg["id"])
            except graph.AuthError as exc:
                eng.mark_reconnect(db, mb, str(exc))
                return redirect("/mailboxes", err="Sign-in expired. Click Reconnect.")
            except graph.GraphError as exc:
                return redirect("/mailboxes", err=f"Test failed: {exc.message[:300]}")
            db.add(Message(mailbox_id=mb.id, direction="out", kind="test", subject="Test email", body_html=html,
                           from_email=mb.email, to_email=to, graph_id=msg.get("id", ""), is_read=True))
            log_event(db, f"Test email sent from {mb.email} to {to}", mailbox_id=mb.id)
            db.commit()
        return redirect("/mailboxes", ok=f"Test email sent to {to}. Check that inbox (and its spam folder).")

    @app.post("/mailboxes/check")
    def mailboxes_check():
        with eng.engine_lock, SessionLocal() as db:
            eng.run_inbox_tick(db, force=True)
        return redirect("/inbox", ok="Inboxes checked")

    # -------------------------------------------------------- leads
    @app.get("/leads", response_class=HTMLResponse)
    def leads_page(request: Request):
        q = request.query_params
        list_name, status, search = q.get("list", ""), q.get("status", ""), q.get("q", "").strip()
        pg = to_int(q.get("page"), 1, 1)
        with SessionLocal() as db:
            stmt = select(Lead)
            if list_name:
                stmt = stmt.where(Lead.list_name == list_name)
            if status in LABELS:
                stmt = stmt.where(Lead.label == status)
            elif status:
                stmt = stmt.where(Lead.status == status)
            if search:
                like = f"%{search.lower()}%"
                stmt = stmt.where(or_(func.lower(Lead.email).like(like), func.lower(Lead.first_name).like(like),
                                      func.lower(Lead.last_name).like(like), func.lower(Lead.company).like(like)))
            total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            leads = db.scalars(stmt.order_by(Lead.id.desc()).offset((pg - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
            return page(request, db, "leads.html", leads=leads, total=total, page_no=pg,
                        pages=max(1, math.ceil(total / PAGE_SIZE)), lists=list_counts(db),
                        f_list=list_name, f_status=status, f_q=search)

    @app.post("/leads/import")
    async def leads_import(request: Request):
        form = await request.form()
        upload = form.get("file")
        list_name = str(form.get("list_name", "")).strip() or "General"
        if upload is None or not getattr(upload, "filename", ""):
            return redirect("/leads", err="Choose a CSV file first")
        raw = await upload.read()
        rows, info = parse_csv(raw)
        if not info["email_column"]:
            return redirect("/leads", err="Couldn't find an 'email' column in that file. Make sure the first row has column names like Email, First Name, Company.")
        added = dup = blocked = 0
        with SessionLocal() as db:
            existing = set(db.scalars(select(Lead.email)).all())
            for r in rows:
                if r["email"] in existing:
                    dup += 1
                    continue
                if is_suppressed(db, r["email"]):
                    blocked += 1
                    continue
                db.add(Lead(list_name=list_name, **r))
                existing.add(r["email"])
                added += 1
            log_event(db, f"Imported {added} leads into list '{list_name}'")
            db.commit()
        msg = f"Imported {added} leads into '{list_name}'."
        if dup:
            msg += f" {dup} already existed (skipped)."
        if blocked:
            msg += f" {blocked} are on your blocklist (skipped)."
        if info["invalid"]:
            msg += f" {info['invalid']} rows had an invalid email (skipped)."
        if info["custom_fields"]:
            msg += " Extra columns you can use as variables: " + ", ".join("{{" + c + "}}" for c in info["custom_fields"][:15])
        return redirect(f"/leads?list={quote(list_name)}", ok=msg)

    @app.get("/leads/{lid}", response_class=HTMLResponse)
    def lead_detail(lid: int, request: Request):
        with SessionLocal() as db:
            lead = db.get(Lead, lid)
            if not lead:
                return redirect("/leads", err="Lead not found")
            msgs = db.scalars(select(Message).where(Message.lead_id == lid).order_by(Message.created_at)).all()
            cls = db.scalars(select(CampaignLead).where(CampaignLead.lead_id == lid)).all()
            return page(request, db, "lead_detail.html", lead=lead, msgs=msgs, cls=cls, suppressed=is_suppressed(db, lead.email))

    @app.post("/leads/{lid}/update")
    async def lead_update(lid: int, request: Request):
        form = await request.form()
        back = str(form.get("back", "")) or f"/leads/{lid}"
        with SessionLocal() as db:
            lead = db.get(Lead, lid)
            if lead:
                if "label" in form:
                    label = str(form.get("label", ""))
                    lead.label = label if label in LABELS else ""
                    if lead.label:  # a human handled this lead: stop automated follow-ups
                        eng.stop_lead_everywhere(db, lead, "replied")
                if "notes" in form:
                    lead.notes = str(form.get("notes", ""))
                for f in ("first_name", "last_name", "company"):
                    if f in form:
                        setattr(lead, f, str(form.get(f, "")).strip())
                db.commit()
        return redirect(back, ok="Lead updated")

    @app.post("/leads/{lid}/delete")
    def lead_delete(lid: int):
        with SessionLocal() as db:
            lead = db.get(Lead, lid)
            if lead:
                db.delete(lead)
                db.commit()
        return redirect("/leads", ok="Lead deleted")

    @app.post("/leads/{lid}/block")
    def lead_block(lid: int):
        with SessionLocal() as db:
            lead = db.get(Lead, lid)
            if lead:
                add_suppression(db, lead.email, "manual")
                lead.status = "unsubscribed"
                eng.stop_lead_everywhere(db, lead, "unsubscribed")
                db.commit()
        return redirect(f"/leads/{lid}", ok="Added to blocklist and removed from campaigns")

    @app.post("/lists/delete")
    async def list_delete(request: Request):
        form = await request.form()
        name = str(form.get("list_name", ""))
        with SessionLocal() as db:
            ids = db.scalars(select(Lead.id).where(Lead.list_name == name)).all()
            db.execute(delete(CampaignLead).where(CampaignLead.lead_id.in_(ids)))
            db.execute(delete(Lead).where(Lead.id.in_(ids)))
            db.commit()
        return redirect("/leads", ok=f"Deleted list '{name}' ({len(ids)} leads)")

    # -------------------------------------------------------- campaigns
    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns_page(request: Request):
        with SessionLocal() as db:
            camps = db.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
            rows = [(c, campaign_stats(db, c.id)) for c in camps]
            return page(request, db, "campaigns.html", rows=rows)

    @app.post("/campaigns")
    async def campaign_create(request: Request):
        form = await request.form()
        name = str(form.get("name", "")).strip() or "New campaign"
        with SessionLocal() as db:
            mailboxes = db.scalars(select(Mailbox).where(Mailbox.status == "active")).all()
            c = Campaign(name=name, timezone=get_setting(db, "timezone"))
            c.mailboxes = list(mailboxes)
            c.steps = [
                Step(position=0, delay_days=0, subject="{Quick question|Question} about {{company|your company}}",
                     body="Hi {{first_name|there}},\n\n[Write your first email here.]\n\nBest,\n{{sender_first_name}}"),
                Step(position=1, delay_days=3, subject="",
                     body="Hi {{first_name|there}},\n\nJust following up on my last email. [Short follow-up.]\n\nThanks,\n{{sender_first_name}}"),
            ]
            db.add(c)
            db.commit()
            return redirect(f"/campaigns/{c.id}", ok="Campaign created. Edit the emails below, then add leads.")

    @app.get("/campaigns/{cid}", response_class=HTMLResponse)
    def campaign_detail(cid: int, request: Request):
        q = request.query_params
        tab = q.get("tab", "sequence")
        pg = to_int(q.get("page"), 1, 1)
        f_status = q.get("status", "")
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            if not c:
                return redirect("/campaigns", err="Campaign not found")
            stmt = select(CampaignLead).where(CampaignLead.campaign_id == cid)
            if f_status:
                stmt = stmt.where(CampaignLead.status == f_status)
            total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            cls = db.scalars(stmt.order_by(CampaignLead.id).offset((pg - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
            sample_leads = db.scalars(
                select(Lead).join(CampaignLead, CampaignLead.lead_id == Lead.id).where(CampaignLead.campaign_id == cid).limit(20)
            ).all() or db.scalars(select(Lead).limit(20)).all()
            used_vars = set()
            for s in c.steps:
                used_vars |= find_variables(s.subject) | find_variables(s.body)
            field_keys = set()
            for ld in sample_leads:
                field_keys |= set((ld.fields or {}).keys())
            events = db.scalars(select(EventLog).where(EventLog.campaign_id == cid).order_by(EventLog.id.desc()).limit(30)).all()
            return page(
                request, db, "campaign_detail.html",
                c=c, tab=tab, stats=campaign_stats(db, cid), cls=cls, total=total, page_no=pg,
                pages=max(1, math.ceil(total / PAGE_SIZE)), f_status=f_status,
                lists=list_counts(db), all_mailboxes=db.scalars(select(Mailbox).order_by(Mailbox.email)).all(),
                sample_leads=sample_leads, days=DAYS, timezones=TIMEZONES,
                variables=sorted({"first_name", "last_name", "full_name", "company", "email", "sender_name",
                                  "sender_first_name"} | field_keys),
                used_vars=used_vars, events=events,
                in_window=eng.in_window(c, utcnow()),
            )

    @app.post("/campaigns/{cid}/steps")
    async def campaign_steps(cid: int, request: Request):
        form = await request.form()
        subjects, bodies, delays = form.getlist("subject"), form.getlist("body"), form.getlist("delay_days")
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            if not c:
                return redirect("/campaigns")
            new_steps = []
            for i, (s, b) in enumerate(zip(subjects, bodies)):
                if not str(b).strip() and i > 0:
                    continue
                new_steps.append(Step(position=len(new_steps), subject=str(s).strip(), body=str(b),
                                      delay_days=0 if not new_steps else to_int(delays[i] if i < len(delays) else 3, 3, 0, 60)))
            if not new_steps or not new_steps[0].subject:
                return redirect(f"/campaigns/{cid}?tab=sequence", err="The first email needs a subject and a body.")
            # Leads that already finished the old sequence may have new steps now.
            old_len = len(c.steps)
            c.steps = new_steps
            db.flush()
            if len(new_steps) > old_len:
                for cl in db.scalars(select(CampaignLead).where(CampaignLead.campaign_id == cid, CampaignLead.status == "completed")).all():
                    cl.status = "active"
                    cl.next_send_at = (cl.last_sent_at or utcnow()) + timedelta(days=new_steps[cl.next_step].delay_days)
                if c.status == "completed":
                    c.status = "paused"
            db.commit()
        return redirect(f"/campaigns/{cid}?tab=sequence", ok="Sequence saved")

    @app.post("/campaigns/{cid}/preview")
    async def campaign_preview(cid: int, request: Request):
        data = await request.json()
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            lead = db.get(Lead, to_int(data.get("lead_id"))) if data.get("lead_id") else None
            mailbox = c.mailboxes[0] if c and c.mailboxes else db.scalar(select(Mailbox).limit(1))
            if lead is None:
                lead = db.scalar(
                    select(Lead).join(CampaignLead, CampaignLead.lead_id == Lead.id).where(CampaignLead.campaign_id == cid).limit(1)
                ) or db.scalar(select(Lead).limit(1))
            if lead is None:
                lead = Lead(email="jane@example.com", first_name="Jane", last_name="Doe", company="Example Inc", fields={})
            ctx = lead_context(lead, mailbox)
            subject, m1 = render(str(data.get("subject", "")), ctx)
            body, m2 = render(str(data.get("body", "")), ctx)
            sig = render(mailbox.signature or "", ctx)[0] if mailbox else ""
            optout = render(c.optout_text, ctx)[0] if c and c.include_optout else ""
            return JSONResponse({
                "subject": subject or "(same thread: Re: previous subject)",
                "html": build_email_html(body, sig, optout),
                "missing": sorted(m1 | m2),
                "from": f"{mailbox.display_name} <{mailbox.email}>" if mailbox else "(no mailbox connected yet)",
                "to": lead.email,
            })

    @app.post("/campaigns/{cid}/settings")
    async def campaign_settings(cid: int, request: Request):
        form = await request.form()
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            if not c:
                return redirect("/campaigns")
            c.name = str(form.get("name", c.name)).strip() or c.name
            ids = {to_int(x) for x in form.getlist("mailbox_ids")}
            c.mailboxes = [m for m in db.scalars(select(Mailbox)).all() if m.id in ids]
            tzname = str(form.get("timezone", c.timezone))
            c.timezone = tzname if tzname in TIMEZONES else c.timezone
            days = sorted({to_int(d, -1) for d in form.getlist("send_days")} & set(range(7)))
            c.send_days = ",".join(str(d) for d in days) or "0,1,2,3,4"
            c.start_hour = to_int(form.get("start_hour"), 9, 0, 23)
            c.end_hour = to_int(form.get("end_hour"), 17, 1, 24)
            if c.end_hour <= c.start_hour:
                c.end_hour = min(24, c.start_hour + 1)
            c.min_gap_minutes = to_int(form.get("min_gap_minutes"), 10, 1, 240)
            c.max_gap_minutes = max(c.min_gap_minutes, to_int(form.get("max_gap_minutes"), 25, 1, 480))
            c.max_new_per_day = to_int(form.get("max_new_per_day"), 0, 0, 1000)
            c.stop_on_reply = form.get("stop_on_reply") == "on"
            c.include_optout = form.get("include_optout") == "on"
            c.optout_text = str(form.get("optout_text", c.optout_text))
            c.bounce_pause_pct = float(to_int(form.get("bounce_pause_pct"), 5, 1, 50))
            db.commit()
        return redirect(f"/campaigns/{cid}?tab=settings", ok="Settings saved")

    @app.post("/campaigns/{cid}/leads/add")
    async def campaign_add_leads(cid: int, request: Request):
        form = await request.form()
        list_name = str(form.get("list_name", ""))
        skip_contacted = form.get("skip_contacted") == "on"
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            if not c:
                return redirect("/campaigns")
            stmt = select(Lead).where(Lead.status.not_in(["bounced", "unsubscribed"]))
            if list_name != "__all__":
                stmt = stmt.where(Lead.list_name == list_name)
            if skip_contacted:
                stmt = stmt.where(Lead.status == "new").where(~Lead.id.in_(select(CampaignLead.lead_id)))
            in_campaign = set(db.scalars(select(CampaignLead.lead_id).where(CampaignLead.campaign_id == cid)).all())
            added = skipped = 0
            for lead in db.scalars(stmt).all():
                if lead.id in in_campaign:
                    continue
                if is_suppressed(db, lead.email):
                    skipped += 1
                    continue
                db.add(CampaignLead(campaign_id=cid, lead_id=lead.id, next_send_at=utcnow()))
                added += 1
            if added and c.status == "completed":
                c.status = "paused"
            db.commit()
        msg = f"Added {added} leads to the campaign."
        if skipped:
            msg += f" {skipped} skipped (blocklist)."
        return redirect(f"/campaigns/{cid}?tab=leads", ok=msg)

    @app.post("/campaigns/{cid}/leads/{clid}/remove")
    def campaign_remove_lead(cid: int, clid: int):
        with SessionLocal() as db:
            cl = db.get(CampaignLead, clid)
            if cl and cl.campaign_id == cid:
                db.delete(cl)
                db.commit()
        return redirect(f"/campaigns/{cid}?tab=leads", ok="Lead removed from campaign")

    @app.post("/campaigns/{cid}/start")
    def campaign_start(cid: int):
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            if not c:
                return redirect("/campaigns")
            problems = []
            if not c.steps or not c.steps[0].subject.strip() or not c.steps[0].body.strip():
                problems.append("write the first email (subject and body)")
            if not any(m.status == "active" for m in c.mailboxes):
                problems.append("choose at least one active mailbox in Settings tab")
            active_leads = db.scalar(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == cid, CampaignLead.status == "active"))
            if not active_leads:
                problems.append("add leads (Leads tab)")
            if any("[" in s.body and "]" in s.body and "Write your" in s.body for s in c.steps) or any("[Short follow-up.]" in s.body for s in c.steps):
                problems.append("replace the [placeholder] text in your emails")
            if problems:
                return redirect(f"/campaigns/{cid}", err="Before starting: " + "; ".join(problems) + ".")
            c.status = "active"
            log_event(db, f"Campaign '{c.name}' started", campaign_id=cid)
            db.commit()
            msg = "Campaign started."
            if not eng.in_window(c, utcnow()):
                msg += " It's outside the sending hours right now, so the first emails go out when the window opens."
            if get_setting(db, "sending_paused") == "1":
                msg += " Note: global sending is PAUSED (top bar)."
        return redirect(f"/campaigns/{cid}", ok=msg)

    @app.post("/campaigns/{cid}/pause")
    def campaign_pause(cid: int):
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            if c:
                c.status = "paused"
                log_event(db, f"Campaign '{c.name}' paused", campaign_id=cid)
                db.commit()
        return redirect(f"/campaigns/{cid}", ok="Campaign paused")

    @app.post("/campaigns/{cid}/duplicate")
    def campaign_duplicate(cid: int):
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            if not c:
                return redirect("/campaigns")
            n = Campaign(name=c.name + " (copy)")
            for f in ("timezone", "send_days", "start_hour", "end_hour", "min_gap_minutes", "max_gap_minutes",
                      "max_new_per_day", "stop_on_reply", "include_optout", "optout_text", "bounce_pause_pct"):
                setattr(n, f, getattr(c, f))
            n.mailboxes = list(c.mailboxes)
            n.steps = [Step(position=s.position, delay_days=s.delay_days, subject=s.subject, body=s.body) for s in c.steps]
            db.add(n)
            db.commit()
            return redirect(f"/campaigns/{n.id}", ok="Campaign copied (without leads)")

    @app.post("/campaigns/{cid}/delete")
    def campaign_delete(cid: int):
        with SessionLocal() as db:
            c = db.get(Campaign, cid)
            if c:
                db.execute(delete(CampaignLead).where(CampaignLead.campaign_id == cid))
                db.delete(c)
                db.commit()
        return redirect("/campaigns", ok="Campaign deleted")

    # -------------------------------------------------------- inbox
    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(request: Request):
        q = request.query_params
        kind = q.get("kind", "reply")
        pg = to_int(q.get("page"), 1, 1)
        with SessionLocal() as db:
            stmt = select(Message).where(Message.direction == "in")
            if kind in ("reply", "auto_reply", "bounce"):
                stmt = stmt.where(Message.kind == kind)
            if q.get("unread"):
                stmt = stmt.where(Message.is_read.is_(False))
            total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            msgs = db.scalars(stmt.order_by(Message.created_at.desc()).offset((pg - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
            return page(request, db, "inbox.html", msgs=msgs, kind=kind, total=total, page_no=pg,
                        pages=max(1, math.ceil(total / PAGE_SIZE)))

    @app.get("/inbox/{mid}", response_class=HTMLResponse)
    def inbox_message(mid: int, request: Request):
        with SessionLocal() as db:
            msg = db.get(Message, mid)
            if not msg:
                return redirect("/inbox", err="Message not found")
            if not msg.is_read:
                msg.is_read = True
                db.commit()
            thread = []
            if msg.lead_id:
                thread = db.scalars(select(Message).where(Message.lead_id == msg.lead_id).order_by(Message.created_at)).all()
            return page(request, db, "inbox_message.html", msg=msg, thread=thread or [msg])

    @app.post("/inbox/{mid}/reply")
    async def inbox_reply(mid: int, request: Request):
        form = await request.form()
        text = str(form.get("text", "")).strip()
        if not text:
            return redirect(f"/inbox/{mid}", err="Write a reply first")
        with eng.engine_lock, SessionLocal() as db:
            msg = db.get(Message, mid)
            if not msg or not msg.mailbox:
                return redirect("/inbox", err="Mailbox for this message is no longer connected")
            mb = msg.mailbox
            lead = msg.lead
            ctx = lead_context(lead, mb) if lead else {}
            sig = render(mb.signature or "", ctx)[0]
            html = text_to_html(text) + ("<br><br>" + text_to_html(sig) if sig.strip() else "")
            try:
                eng.client_for(db, mb).reply(msg.graph_id, html)
            except graph.AuthError as exc:
                eng.mark_reconnect(db, mb, str(exc))
                return redirect(f"/inbox/{mid}", err="Sign-in expired for this mailbox. Reconnect it on the Mailboxes page.")
            except graph.GraphError as exc:
                return redirect(f"/inbox/{mid}", err=f"Reply failed: {exc.message[:300]}")
            db.add(Message(mailbox_id=mb.id, lead_id=msg.lead_id, campaign_id=msg.campaign_id, direction="out", kind="manual",
                           subject="Re: " + (msg.subject or ""), body_html=html, preview=text[:240], from_email=mb.email,
                           to_email=msg.from_email, conversation_id=msg.conversation_id, is_read=True))
            log_event(db, f"You replied to {msg.from_email} from {mb.email}", mailbox_id=mb.id)
            db.commit()
        return redirect(f"/inbox/{mid}", ok="Reply sent")

    @app.post("/inbox/mark-all-read")
    def inbox_mark_read():
        with SessionLocal() as db:
            for m in db.scalars(select(Message).where(Message.direction == "in", Message.is_read.is_(False))).all():
                m.is_read = True
            db.commit()
        return redirect("/inbox", ok="All marked as read")

    # -------------------------------------------------------- blocklist
    @app.get("/blocklist", response_class=HTMLResponse)
    def blocklist_page(request: Request):
        q = request.query_params.get("q", "").strip().lower()
        with SessionLocal() as db:
            stmt = select(Suppression)
            if q:
                stmt = stmt.where(Suppression.value.like(f"%{q}%"))
            rows = db.scalars(stmt.order_by(Suppression.id.desc()).limit(500)).all()
            total = db.scalar(select(func.count(Suppression.id))) or 0
            return page(request, db, "blocklist.html", rows=rows, total=total, f_q=q)

    @app.post("/blocklist/add")
    async def blocklist_add(request: Request):
        form = await request.form()
        values = str(form.get("values", "")).replace(",", "\n").split()
        added = 0
        with SessionLocal() as db:
            for v in values:
                if add_suppression(db, v, "manual"):
                    added += 1
                    db.flush()
                    email = v.strip().lower().lstrip("@")
                    leads = db.scalars(
                        select(Lead).where(or_(Lead.email == email, Lead.email.like(f"%@{email}")))
                    ).all() if email else []
                    for lead in leads:
                        lead.status = "unsubscribed"
                        eng.stop_lead_everywhere(db, lead, "unsubscribed")
            db.commit()
        return redirect("/blocklist", ok=f"Added {added} entries to the blocklist")

    @app.post("/blocklist/{sid}/delete")
    def blocklist_delete(sid: int):
        with SessionLocal() as db:
            row = db.get(Suppression, sid)
            if row:
                db.delete(row)
                db.commit()
        return redirect("/blocklist", ok="Removed from blocklist")

    # -------------------------------------------------------- activity
    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request):
        level = request.query_params.get("level", "")
        with SessionLocal() as db:
            stmt = select(EventLog)
            if level:
                stmt = stmt.where(EventLog.level == level)
            events = db.scalars(stmt.order_by(EventLog.id.desc()).limit(300)).all()
            return page(request, db, "activity.html", events=events, level=level)

    @app.get("/guide", response_class=HTMLResponse)
    def guide_page(request: Request):
        with SessionLocal() as db:
            return page(request, db, "guide.html", redirect_uri=redirect_uri(db))
