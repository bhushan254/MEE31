"""Database models and session handling (SQLite for now, PostgreSQL-ready later)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    """Naive UTC timestamp. Everything in the database is stored in UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


campaign_mailboxes = Table(
    "campaign_mailboxes",
    Base.metadata,
    Column("campaign_id", ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
    Column("mailbox_id", ForeignKey("mailboxes.id", ondelete="CASCADE"), primary_key=True),
)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Mailbox(Base):
    __tablename__ = "mailboxes"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    ms_user_id: Mapped[str] = mapped_column(String(100), default="")
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=25)
    # active | paused | reconnect | error
    status: Mapped[str] = mapped_column(String(20), default="active")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    signature: Mapped[str] = mapped_column(Text, default="")
    next_send_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_inbox_check: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(200), default="")
    last_name: Mapped[str] = mapped_column(String(200), default="")
    company: Mapped[str] = mapped_column(String(300), default="")
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    list_name: Mapped[str] = mapped_column(String(200), default="General", index=True)
    # new | contacted | replied | bounced | unsubscribed
    status: Mapped[str] = mapped_column(String(20), default="new")
    # "" | interested | not_interested | meeting_booked | closed
    label: Mapped[str] = mapped_column(String(30), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in [self.first_name, self.last_name] if p).strip()


class Suppression(Base):
    """Blocklist: emails or whole domains that must never be emailed."""

    __tablename__ = "suppressions"
    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String(320), unique=True)
    kind: Mapped[str] = mapped_column(String(10), default="email")  # email | domain
    reason: Mapped[str] = mapped_column(String(50), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # draft | active | paused | completed
    status: Mapped[str] = mapped_column(String(20), default="draft")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    send_days: Mapped[str] = mapped_column(String(20), default="0,1,2,3,4")  # Mon=0
    start_hour: Mapped[int] = mapped_column(Integer, default=9)
    end_hour: Mapped[int] = mapped_column(Integer, default=17)
    min_gap_minutes: Mapped[int] = mapped_column(Integer, default=10)
    max_gap_minutes: Mapped[int] = mapped_column(Integer, default=25)
    max_new_per_day: Mapped[int] = mapped_column(Integer, default=0)  # 0 = no extra limit
    stop_on_reply: Mapped[bool] = mapped_column(Boolean, default=True)
    include_optout: Mapped[bool] = mapped_column(Boolean, default=True)
    optout_text: Mapped[str] = mapped_column(
        Text,
        default="P.S. If this isn't relevant for you, just reply \"unsubscribe\" and I won't email you again.",
    )
    bounce_pause_pct: Mapped[float] = mapped_column(Float, default=5.0)
    include_unsub_link: Mapped[bool] = mapped_column(Boolean, default=True)
    unsub_link_text: Mapped[str] = mapped_column(String(100), default="Unsubscribe")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    steps: Mapped[list["Step"]] = relationship(
        back_populates="campaign", order_by="Step.position", cascade="all, delete-orphan"
    )
    mailboxes: Mapped[list[Mailbox]] = relationship(secondary=campaign_mailboxes)

    @property
    def send_day_list(self) -> list[int]:
        return [int(d) for d in self.send_days.split(",") if d.strip().isdigit()]


class Step(Base):
    __tablename__ = "steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    delay_days: Mapped[int] = mapped_column(Integer, default=3)  # days after previous step
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    campaign: Mapped[Campaign] = relationship(back_populates="steps")


class Template(Base):
    """A saved email copy: first email + follow-ups, reusable in any campaign."""

    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # [{"subject": str, "body": str, "delay_days": int}, ...]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CampaignLead(Base):
    __tablename__ = "campaign_leads"
    __table_args__ = (UniqueConstraint("campaign_id", "lead_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    mailbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="SET NULL"), nullable=True
    )
    next_step: Mapped[int] = mapped_column(Integer, default=0)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=utcnow)
    # active | completed | replied | bounced | unsubscribed | stopped | failed
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    last_message_id: Mapped[str] = mapped_column(Text, default="")
    last_internet_message_id: Mapped[str] = mapped_column(Text, default="")
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    campaign: Mapped[Campaign] = relationship()
    lead: Mapped[Lead] = relationship()
    mailbox: Mapped[Mailbox | None] = relationship()


class Message(Base):
    """Every email sent by the tool and every relevant email received."""

    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    campaign_lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_leads.id", ondelete="SET NULL"), nullable=True
    )
    step_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str] = mapped_column(String(3))  # out | in
    # campaign | manual | test | reply | auto_reply | bounce
    kind: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    preview: Mapped[str] = mapped_column(Text, default="")
    from_email: Mapped[str] = mapped_column(String(320), default="")
    to_email: Mapped[str] = mapped_column(String(320), default="")
    graph_id: Mapped[str] = mapped_column(Text, default="", index=True)
    internet_message_id: Mapped[str] = mapped_column(Text, default="")
    conversation_id: Mapped[str] = mapped_column(Text, default="", index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    mailbox: Mapped[Mailbox | None] = relationship()
    lead: Mapped[Lead | None] = relationship()
    campaign: Mapped[Campaign | None] = relationship()


class EventLog(Base):
    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | warn | error
    message: Mapped[str] = mapped_column(Text)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mailbox_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------------------------------------------------------------- engine/session

engine = None
SessionLocal = sessionmaker(expire_on_commit=False)


def default_db_url() -> str:
    data_dir = Path(os.environ.get("OUTREACH_DATA_DIR", Path.cwd() / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'outreach.db'}"


def init_db(url: str | None = None):
    """Create the engine and tables. Safe to call more than once."""
    global engine
    url = url or os.environ.get("OUTREACH_DB_URL") or default_db_url()
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    Base.metadata.create_all(engine)
    upgrade_schema(engine)
    SessionLocal.configure(bind=engine)
    return engine


def _sql_literal(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def upgrade_schema(eng) -> list[str]:
    """Add columns that newer versions introduced to an existing database.

    This is what lets you install an update and keep all your data: new tables are
    created by create_all, and new columns on existing tables are added here.
    """
    added = []
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                sql = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=eng.dialect)}'
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                if default is not None:
                    sql += f" DEFAULT {_sql_literal(default)}"
                conn.execute(text(sql))
                added.append(f"{table.name}.{col.name}")
    return added


# ---------------------------------------------------------------- settings helpers

DEFAULT_SETTINGS = {
    "ms_client_id": "",
    "ms_client_secret": "",
    "ms_tenant": "organizations",
    "timezone": "UTC",
    "inbox_check_minutes": "5",
    "sending_paused": "0",
    "base_url": "http://localhost:8000",
    "public_url": "",
    "app_secret": "",
}


def get_setting(session, key: str) -> str:
    row = session.get(Setting, key)
    if row is None:
        return DEFAULT_SETTINGS.get(key, "")
    return row.value


def set_setting(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def is_suppressed(session, email: str) -> bool:
    email = email.strip().lower()
    domain = email.split("@")[-1]
    hit = session.scalar(
        select(Suppression.id).where(Suppression.value.in_([email, domain])).limit(1)
    )
    return hit is not None


def add_suppression(session, value: str, reason: str = "manual") -> bool:
    """Add an email or domain to the blocklist. Returns False if already present."""
    value = value.strip().lower()
    if not value:
        return False
    if value.startswith("@"):
        value = value[1:]
    kind = "email" if "@" in value else "domain"
    if session.scalar(select(Suppression.id).where(Suppression.value == value)):
        return False
    session.add(Suppression(value=value, kind=kind, reason=reason))
    return True


def log_event(session, message: str, level: str = "info", campaign_id=None, mailbox_id=None):
    session.add(EventLog(message=message, level=level, campaign_id=campaign_id, mailbox_id=mailbox_id))
