import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from outreach.db import Campaign, CampaignLead, Lead, Mailbox, SessionLocal
from outreach.web import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB_URL", f"sqlite:///{tmp_path / 'web.db'}")
    with TestClient(create_app(start_scheduler=False)) as c:
        yield c


def test_all_pages_render(client):
    for path in ["/", "/campaigns", "/leads", "/inbox", "/mailboxes", "/blocklist", "/activity", "/settings", "/guide"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_full_flow_import_campaign_start(client):
    csv = "Email,First Name,Company,City\njohn@acme.com,john,Acme,Pune\nbad-email,x,y,z\njane@beta.io,Jane,Beta,Delhi\n"
    r = client.post("/leads/import", data={"list_name": "Test list"}, files={"file": ("leads.csv", csv, "text/csv")})
    assert r.status_code == 200 and "Imported 2 leads" in r.text and "{{city}}" in r.text
    with SessionLocal() as db:
        john = db.scalar(select(Lead).where(Lead.email == "john@acme.com"))
        assert john.first_name == "John" and john.fields == {"city": "Pune"}
        db.add(Mailbox(email="me@mine.com", display_name="Me"))
        db.commit()

    r = client.post("/campaigns", data={"name": "My first"}, follow_redirects=False)
    cid = int(r.headers["location"].split("/campaigns/")[1].split("?")[0])
    page = client.get(f"/campaigns/{cid}")
    assert "My first" in page.text

    # Placeholders must be replaced before starting
    r = client.post(f"/campaigns/{cid}/leads/add", data={"list_name": "Test list", "skip_contacted": "on"})
    assert "Added 2 leads" in r.text
    r = client.post(f"/campaigns/{cid}/start")
    assert "placeholder" in r.text

    r = client.post(f"/campaigns/{cid}/steps", data={
        "subject": ["Hi {{first_name}}", ""], "body": ["Hello from {{city}}", "Bump"], "delay_days": ["0", "4"]})
    assert "Sequence saved" in r.text
    preview = client.post(f"/campaigns/{cid}/preview", json={"subject": "Hi {{first_name}}", "body": "In {{city}} {{zip}}"})
    assert preview.json()["missing"] == ["zip"]

    r = client.post(f"/campaigns/{cid}/settings", data={
        "name": "My first", "mailbox_ids": ["1"], "timezone": "Asia/Kolkata", "send_days": ["0", "1", "2"],
        "start_hour": "9", "end_hour": "18", "min_gap_minutes": "10", "max_gap_minutes": "20",
        "max_new_per_day": "0", "stop_on_reply": "on", "include_optout": "on", "optout_text": "reply stop",
        "bounce_pause_pct": "5"})
    assert "Settings saved" in r.text
    r = client.post(f"/campaigns/{cid}/start")
    assert "Campaign started" in r.text
    with SessionLocal() as db:
        c = db.get(Campaign, cid)
        assert c.status == "active" and c.timezone == "Asia/Kolkata" and c.steps[1].delay_days == 4
        assert db.scalar(select(CampaignLead).where(CampaignLead.campaign_id == cid)) is not None
    for path in [f"/campaigns/{cid}?tab=leads", f"/campaigns/{cid}?tab=settings", f"/campaigns/{cid}?tab=activity", "/leads/1"]:
        assert client.get(path).status_code == 200


def test_blocklist_domain_stops_leads(client):
    client.post("/leads/import", data={"list_name": "L"}, files={"file": ("l.csv", "email\na@x.com\nb@y.com\n", "text/csv")})
    r = client.post("/blocklist/add", data={"values": "x.com"})
    assert "Added 1" in r.text
    with SessionLocal() as db:
        assert db.scalar(select(Lead).where(Lead.email == "a@x.com")).status == "unsubscribed"
        assert db.scalar(select(Lead).where(Lead.email == "b@y.com")).status == "new"


def test_connect_requires_settings(client):
    r = client.get("/mailboxes/connect")
    assert "Microsoft app details" in r.text
