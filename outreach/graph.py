"""Microsoft sign-in (OAuth) and Microsoft Graph mail API client."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from .db import utcnow

AUTH_BASE = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access User.Read Mail.Send Mail.ReadWrite"

# Immutable IDs keep the same message ID when a draft moves to Sent Items,
# which lets follow-ups reply in the same thread.
BASE_HEADERS = {"Prefer": 'IdType="ImmutableId"'}

SEND_BLOCKED_CODES = {
    "ErrorMessageSubmissionBlocked",
    "ErrorSendAsDenied",
    "ErrorAccountSuspend",
    "MailboxNotEnabledForRESTAPI",
    "ErrorQuotaExceeded",
    "ErrorExceededMessageLimit",
}


class GraphError(Exception):
    def __init__(self, status: int, code: str = "", message: str = ""):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")

    @property
    def is_throttled(self) -> bool:
        return self.status in (429, 503, 504)

    @property
    def is_send_blocked(self) -> bool:
        return self.code in SEND_BLOCKED_CODES or self.status == 403

    @property
    def is_bad_recipient(self) -> bool:
        return self.status == 400 and "recipient" in (self.message or "").lower()


class AuthError(Exception):
    """Sign-in expired or was revoked: the mailbox must be reconnected."""


# ------------------------------------------------------------------ OAuth


def authorize_url(client_id: str, tenant: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "prompt": "select_account",
    }
    return f"{AUTH_BASE}/{tenant or 'organizations'}/oauth2/v2.0/authorize?{urlencode(params)}"


def _token_request(tenant: str, data: dict) -> dict:
    url = f"{AUTH_BASE}/{tenant or 'organizations'}/oauth2/v2.0/token"
    resp = httpx.post(url, data=data, timeout=30)
    payload = resp.json() if resp.content else {}
    if resp.status_code != 200:
        desc = payload.get("error_description") or payload.get("error") or resp.text
        raise AuthError(desc.split("\r\n")[0])
    payload["expires_at"] = utcnow() + timedelta(seconds=int(payload.get("expires_in", 3600)))
    return payload


def exchange_code(client_id, client_secret, tenant, redirect_uri, code) -> dict:
    return _token_request(
        tenant,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
        },
    )


def refresh_token(client_id, client_secret, tenant, token: str) -> dict:
    return _token_request(
        tenant,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": token,
            "scope": SCOPES,
        },
    )


# ------------------------------------------------------------------ Graph client


def recipient(email: str, name: str = "") -> dict:
    addr = {"address": email}
    if name:
        addr["name"] = name
    return {"emailAddress": addr}


class GraphClient:
    def __init__(self, access_token: str):
        self.headers = {**BASE_HEADERS, "Authorization": f"Bearer {access_token}"}

    def _req(self, method: str, url: str, **kwargs) -> dict:
        if not url.startswith("http"):
            url = GRAPH + url
        try:
            resp = httpx.request(method, url, headers=self.headers, timeout=60, **kwargs)
        except httpx.HTTPError as exc:
            raise GraphError(503, "NetworkError", str(exc)) from exc
        if resp.status_code >= 400:
            code, message = "", resp.text[:500]
            try:
                err = resp.json().get("error", {})
                code, message = err.get("code", ""), err.get("message", message)
            except ValueError:
                pass
            if resp.status_code == 401:
                raise AuthError(message)
            raise GraphError(resp.status_code, code, message)
        if not resp.content:
            return {}
        return resp.json()

    def me(self) -> dict:
        return self._req("GET", "/me?$select=id,displayName,mail,userPrincipalName")

    def create_message(self, subject: str, html: str, to_email: str, to_name: str = "") -> dict:
        body = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [recipient(to_email, to_name)],
        }
        return self._req("POST", "/me/messages", json=body)

    def send(self, message_id: str) -> None:
        self._req("POST", f"/me/messages/{message_id}/send", content=b"")

    def create_reply(self, message_id: str) -> dict:
        return self._req("POST", f"/me/messages/{message_id}/createReply", json={})

    def update(self, message_id: str, patch: dict) -> dict:
        return self._req("PATCH", f"/me/messages/{message_id}", json=patch)

    def find_by_internet_id(self, folder: str, internet_message_id: str) -> dict | None:
        safe = internet_message_id.replace("'", "''")
        data = self._req(
            "GET",
            f"/me/mailFolders/{folder}/messages",
            params={"$filter": f"internetMessageId eq '{safe}'", "$select": "id,subject,conversationId"},
        )
        items = data.get("value", [])
        return items[0] if items else None

    def list_since(self, folder: str, since: datetime, max_pages: int = 10) -> list[dict]:
        params = {
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime asc",
            "$top": "50",
            "$select": "id,subject,from,receivedDateTime,conversationId,internetMessageId,"
            "bodyPreview,body,uniqueBody,internetMessageHeaders",
        }
        out: list[dict] = []
        data = self._req("GET", f"/me/mailFolders/{folder}/messages", params=params)
        for _ in range(max_pages):
            out.extend(data.get("value", []))
            nxt = data.get("@odata.nextLink")
            if not nxt:
                break
            data = self._req("GET", nxt)
        return out

    def reply(self, message_id: str, comment_html: str) -> None:
        self._req("POST", f"/me/messages/{message_id}/reply", json={"comment": comment_html})
