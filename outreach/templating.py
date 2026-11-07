"""Email personalization: {{variables}}, {{variable|fallback}} and {spin|tax}."""

from __future__ import annotations

import html
import random
import re

VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*(?:\|([^{}]*?))?\s*\}\}")
SPIN_RE = re.compile(r"(?<!\{)\{([^{}]*\|[^{}]*)\}(?!\})")
TAG_RE = re.compile(r"<[^>]+>")


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def lead_context(lead, mailbox=None) -> dict:
    ctx: dict[str, str] = {}
    for k, v in (lead.fields or {}).items():
        ctx[normalize_key(k)] = "" if v is None else str(v)
    ctx.update(
        {
            "first_name": lead.first_name or "",
            "last_name": lead.last_name or "",
            "full_name": lead.full_name,
            "email": lead.email,
            "company": lead.company or "",
        }
    )
    if mailbox is not None:
        name = (mailbox.display_name or "").strip()
        ctx["sender_name"] = name
        ctx["sender_first_name"] = name.split(" ")[0] if name else ""
        ctx["sender_email"] = mailbox.email
    return ctx


def fill_variables(text: str, ctx: dict, missing: set | None = None) -> str:
    def rep(m: re.Match) -> str:
        key = m.group(1).lower()
        fallback = m.group(2)
        val = ctx.get(key)
        if val is None or str(val).strip() == "":
            if fallback is not None:
                return fallback.strip()
            if missing is not None:
                missing.add(key)
            return ""
        return str(val).strip()

    return VAR_RE.sub(rep, text or "")


def spin(text: str, rng=random) -> str:
    for _ in range(50):  # innermost groups first, supports nesting
        new = SPIN_RE.sub(lambda m: rng.choice(m.group(1).split("|")), text)
        if new == text:
            break
        text = new
    return text


def render(template: str, ctx: dict, rng=random) -> tuple[str, set]:
    """Returns (rendered text, set of variables that were empty with no fallback)."""
    missing: set = set()
    text = fill_variables(template or "", ctx, missing)
    text = spin(text, rng)
    return text, missing


def text_to_html(text: str) -> str:
    return html.escape(text.strip()).replace("\r\n", "\n").replace("\n", "<br>\n")


def build_email_html(body: str, signature: str = "", optout: str = "") -> str:
    parts = [text_to_html(body)]
    if signature and signature.strip():
        parts.append(text_to_html(signature))
    if optout and optout.strip():
        parts.append(text_to_html(optout))
    inner = "<br><br>\n".join(parts)
    return f'<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#000">{inner}</div>'


def html_to_text(content: str) -> str:
    content = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", content or "")
    content = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", content)
    text = html.unescape(TAG_RE.sub(" ", content))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def find_variables(template: str) -> set:
    return {m.group(1).lower() for m in VAR_RE.finditer(template or "")}
