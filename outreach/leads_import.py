"""Read lead lists from CSV files (exported from Excel, Google Sheets, Apollo, etc.)."""

from __future__ import annotations

import csv
import io
import re

from .templating import normalize_key

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

ALIASES = {
    "email": ["email", "email_address", "e_mail", "mail", "work_email", "business_email", "emails"],
    "first_name": ["first_name", "firstname", "first", "fname", "given_name"],
    "last_name": ["last_name", "lastname", "last", "lname", "surname", "family_name"],
    "company": ["company", "company_name", "organization", "organisation", "account_name", "business_name"],
}
FULL_NAME_KEYS = ["name", "full_name", "contact_name", "contact"]


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv(raw: bytes) -> tuple[list[dict], dict]:
    """Returns (rows, info). Each row: email, first_name, last_name, company, fields."""
    text = decode(raw)
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    norm = {h: normalize_key(h) for h in headers if h is not None}

    def find(target: str) -> str | None:
        for alias in ALIASES[target]:
            for original, n in norm.items():
                if n == alias:
                    return original
        return None

    cols = {t: find(t) for t in ALIASES}
    full_name_col = None
    if not cols["first_name"]:
        for original, n in norm.items():
            if n in FULL_NAME_KEYS:
                full_name_col = original
                break

    info = {
        "headers": headers,
        "email_column": cols["email"],
        "invalid": 0,
        "custom_fields": [n for o, n in norm.items() if o not in cols.values() and o != full_name_col and n],
    }
    if not cols["email"]:
        return [], info

    used = {c for c in cols.values() if c} | ({full_name_col} if full_name_col else set())
    rows: list[dict] = []
    seen: set = set()
    for rec in reader:
        email = (rec.get(cols["email"]) or "").strip().strip("<>").lower()
        if not EMAIL_RE.match(email):
            if any((v or "").strip() for v in rec.values() if isinstance(v, str)):
                info["invalid"] += 1
            continue
        if email in seen:
            continue
        seen.add(email)
        first = (rec.get(cols["first_name"]) or "").strip() if cols["first_name"] else ""
        last = (rec.get(cols["last_name"]) or "").strip() if cols["last_name"] else ""
        if full_name_col and not first:
            parts = (rec.get(full_name_col) or "").strip().split()
            if parts:
                first, last = parts[0], " ".join(parts[1:])
        fields = {}
        for original, n in norm.items():
            if original in used or not n:
                continue
            val = rec.get(original)
            if isinstance(val, str) and val.strip():
                fields[n] = val.strip()
        rows.append(
            {
                "email": email,
                "first_name": first.title() if first.isupper() or first.islower() else first,
                "last_name": last,
                "company": (rec.get(cols["company"]) or "").strip() if cols["company"] else "",
                "fields": fields,
            }
        )
    return rows, info
