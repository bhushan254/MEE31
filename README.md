# Outreach: personal cold email dashboard for Microsoft 365

A self-hosted cold outreach tool, similar to ManyReach and Instantly, that you run on your own Windows PC.
It sends personalized email sequences from your Microsoft 365 email IDs, rotates across your mailboxes,
follows up automatically, stops when someone replies, and handles bounces and unsubscribes for you.

**You don't need to know any code to use it.**

## What it does

- **Mailboxes:** connect all your Microsoft email IDs with the official "Sign in with Microsoft". Each one gets its own daily limit and signature.
- **Leads:** upload CSV files. Every extra column (City, Website, Icebreaker…) becomes a `{{variable}}` you can use in emails.
- **Campaigns:** first email plus any number of follow-ups. Follow-ups are sent as replies in the same thread.
- **Personalization:** `{{first_name}}`, fallbacks `{{first_name|there}}`, and spintax `{Hi|Hello|Hey}`.
- **Rotation:** emails are spread across your mailboxes. Follow-ups always come from the mailbox that sent the first email.
- **Human-like sending:** only in business hours in your leads' time zone, with a random wait between emails.
- **Unified inbox:** see every reply from every mailbox in one place, reply from the dashboard and label leads (Interested, Meeting booked…).
- **Safety:** stops on reply, detects bounces and blocklists them, detects "unsubscribe" replies, auto-pauses a campaign when the bounce rate is too high, pauses a mailbox if Microsoft blocks it, and has a global **Pause all sending** button.
- **Activity log:** a record of everything the tool did.

## Install on Windows (one time)

1. **Install Python.** Download it from <https://www.python.org/downloads/> and run the installer.
   On the first screen, **tick "Add python.exe to PATH"**, then click *Install Now*.
2. **Download this tool.** On the GitHub page, click the green **Code** button, then **Download ZIP**.
   Unzip it somewhere easy to find, for example `Documents\Outreach`.
3. **Start it.** Open the folder and double-click **`start.bat`**.
   - The first start takes 1–2 minutes while it sets itself up.
   - If Windows shows "Windows protected your PC", click *More info*, then *Run anyway*.
   - Your browser opens the dashboard at **http://localhost:8000**.
4. **Follow the Setup guide** inside the dashboard (left menu). It walks you through connecting Microsoft
   (about 15 minutes, done once), uploading leads and starting your first campaign.

From then on, double-click `start.bat` whenever you want to use it.

## Daily use: important to know

- **Emails are only sent while the black `start.bat` window is open and your PC is awake.**
  During sending hours, keep it open and set Windows sleep to "Never"
  (*Settings → System → Power*). If the PC is off, nothing is lost: sending continues the next time you start it.
- To stop the tool, close the black window.
- All your data (leads, campaigns, emails) lives in the `data` folder next to `start.bat`.
  **Back up that folder** now and then. If you download a newer version of the tool, copy your `data` folder into it.

## Recommended limits (how you already worked)

- 20–25 emails per mailbox per day, including follow-ups.
- Verify your lead lists before uploading, and keep bounces under 2–3%.
- SPF, DKIM and DMARC set up for every domain in Microsoft 365.

## Privacy and security

- The dashboard only accepts connections from your own computer (`127.0.0.1`).
- The tool never sees your Microsoft passwords. You sign in on Microsoft's own page, and access can be
  removed anytime from your Microsoft account.
- Sign-in tokens and the app secret are stored in `data/outreach.db` on your PC. Don't share that file.

## For developers

- Stack: Python 3.10+, FastAPI, SQLAlchemy (SQLite), Jinja2 templates, Microsoft Graph API.
- Run: `pip install -r requirements.txt` then `python -m outreach`
- Tests: `pip install pytest` then `python -m pytest`
- Code map:
  - `outreach/engine.py`: scheduler, sending, follow-ups, reply/bounce/unsubscribe detection
  - `outreach/graph.py`: Microsoft OAuth and Graph mail API
  - `outreach/web.py`: dashboard routes; `outreach/templates/`: pages
  - `outreach/db.py`: database models
- Env vars: `OUTREACH_PORT` (default 8000), `OUTREACH_DATA_DIR`, `OUTREACH_DB_URL`, `OUTREACH_NO_BROWSER=1`.

### Roadmap to going live (multi-user)

User accounts and login, PostgreSQL, encrypted token storage, a multi-tenant Microsoft app registration,
deployment to a server (Docker), and billing.
