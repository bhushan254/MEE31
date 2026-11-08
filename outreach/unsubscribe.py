"""Unsubscribe links.

While Hello box runs on your PC (not reachable from the internet), the link is a
"mailto:" link: clicking it opens an email to you with the subject "Unsubscribe",
which the inbox check detects and blocklists automatically.

When Hello box is hosted online, set "Public web address" in Settings and the link
becomes a real web page (/u/...) where the lead confirms with one click.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
from urllib.parse import quote

from .db import get_setting, set_setting

MAILTO_SUBJECT = "Unsubscribe"
MAILTO_BODY = "Please unsubscribe me from your emails."


def app_secret(session) -> str:
    secret = get_setting(session, "app_secret")
    if not secret:
        secret = secrets.token_hex(32)
        set_setting(session, "app_secret", secret)
        session.commit()
    return secret


def sign(session, lead_id: int, mailbox_id: int) -> str:
    msg = f"{lead_id}:{mailbox_id}".encode()
    return hmac.new(app_secret(session).encode(), msg, hashlib.sha256).hexdigest()[:20]


def verify(session, lead_id: int, mailbox_id: int, signature: str) -> bool:
    return hmac.compare_digest(sign(session, lead_id, mailbox_id), signature or "")


def unsubscribe_url(session, lead, mailbox) -> str:
    public = (get_setting(session, "public_url") or "").strip().rstrip("/")
    if public and lead.id and mailbox.id:
        return f"{public}/u/{lead.id}/{mailbox.id}/{sign(session, lead.id, mailbox.id)}"
    return f"mailto:{mailbox.email}?subject={quote(MAILTO_SUBJECT)}&body={quote(MAILTO_BODY)}"


def link_html(session, campaign, lead, mailbox) -> str:
    if not campaign.include_unsub_link or mailbox is None:
        return ""
    text = (campaign.unsub_link_text or "Unsubscribe").strip() or "Unsubscribe"
    url = unsubscribe_url(session, lead, mailbox)
    return f'<a href="{html.escape(url)}" style="color:#666666">{html.escape(text)}</a>'
