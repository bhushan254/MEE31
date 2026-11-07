import itertools
from datetime import datetime, timedelta

import pytest

from outreach import db as dbmod
from outreach import engine as eng
from outreach.db import Campaign, CampaignLead, Lead, Mailbox, SessionLocal, Step, init_db, set_setting


class FakeGraph:
    """Stands in for Microsoft Graph: records what would be sent and serves a fake inbox."""

    def __init__(self):
        self.sent = []  # dicts: id, to, subject, html, reply_to
        self.drafts = {}
        self.inbox = []
        self.errors = []  # GraphError instances raised on next send
        self._ids = itertools.count(1)

    def _new(self, **kw):
        n = next(self._ids)
        d = {"id": f"msg{n}", "conversationId": kw.pop("conv", f"conv{n}"), "internetMessageId": f"<m{n}@test>", **kw}
        self.drafts[d["id"]] = d
        return d

    def create_message(self, subject, html, to_email, to_name=""):
        return dict(self._new(subject=subject, html=html, to=to_email))

    def create_reply(self, message_id):
        orig = self.drafts[message_id]
        return dict(self._new(subject="RE: " + orig["subject"], conv=orig["conversationId"], to="self",
                              body={"content": "<html><body><hr>quoted</body></html>"}, reply_to=message_id))

    def update(self, message_id, patch):
        d = self.drafts[message_id]
        d["html"] = patch["body"]["content"]
        d["to"] = patch["toRecipients"][0]["emailAddress"]["address"]
        return d

    def send(self, message_id):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(dict(self.drafts[message_id]))

    def find_by_internet_id(self, folder, imid):
        return None

    def list_since(self, folder, since):
        return list(self.inbox) if folder == "inbox" else []

    def reply(self, message_id, html):
        self.sent.append({"reply_to": message_id, "html": html})


@pytest.fixture
def fake(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(eng, "client_for", lambda session, mailbox: g)
    return g


@pytest.fixture
def session(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    with SessionLocal() as s:
        yield s


# Wednesday 10:00 UTC: inside a Mon–Fri 9–17 UTC window.
NOW = datetime(2026, 11, 4, 10, 0, 0)


@pytest.fixture
def setup_campaign(session):
    def make(n_leads=3, n_mailboxes=2, steps=2, daily_limit=25):
        set_setting(session, "timezone", "UTC")
        mbs = [Mailbox(email=f"me{i}@mydomain{i}.com", display_name=f"Sam Sender{i}", daily_limit=daily_limit,
                       created_at=NOW - timedelta(days=1)) for i in range(n_mailboxes)]
        leads = [Lead(email=f"lead{i}@client{i}.com", first_name=f"Lead{i}", company=f"Co{i}",
                      fields={"city": "Pune"}) for i in range(n_leads)]
        c = Campaign(name="Test", status="active", timezone="UTC", min_gap_minutes=1, max_gap_minutes=1)
        c.mailboxes = mbs
        c.steps = [Step(position=0, delay_days=0, subject="Hi {{first_name}}", body="Hello {{first_name}} from {{city}}")]
        for i in range(1, steps):
            c.steps.append(Step(position=i, delay_days=2, subject="", body=f"Follow-up {i}"))
        session.add_all(mbs + leads + [c])
        session.flush()
        for lead in leads:
            session.add(CampaignLead(campaign_id=c.id, lead_id=lead.id, next_send_at=NOW - timedelta(minutes=1)))
        session.commit()
        return c, mbs, leads

    return make
