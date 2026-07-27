"""Stage 6: admission dates and campus contacts, extracted with Claude.

Fees and programs come out of endpoints that return tables, so they are parsed
deterministically. Dates and contacts do not: they are hand-written prose spread
across /admissions/dates/, /contact/, /about/campuses/ and the campus pages, with
inconsistent labelling. That is exactly the shape where an LLM extractor beats a
regex — but only under a strict schema, so the model can normalise wording
without being free to invent a phone number.

Two rules keep it honest:
  * `additionalProperties: false` on a required-field schema, so the model must
    fill declared fields or omit the record entirely.
  * every value is copied, never reformatted — a date stays "Jun - Sep".
"""
from __future__ import annotations

import os
import re
from typing import Any

import config
from kb import db, fetch, parse

# Pages worth extracting from. Kept small and explicit: contacts and dates live
# in known places, and pointing the extractor at the whole site would be waste.
DATE_SOURCES = [
    "/admissions/dates/",
    "/admissions/",
    "/admissions/process/",
    "/academics/academic-calendar/",
]

CONTACT_SOURCES = [
    "/contact/",
    "/about/campuses/",
    "/admissions/",
]

DATES_SCHEMA = {
    "type": "object",
    "properties": {
        "dates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "intake": {
                        "type": "string",
                        "description": "Intake or campaign this applies to, e.g. "
                                       "'Fall 2026', 'Spring Campaign'. Use '' if unstated.",
                    },
                    "event": {
                        "type": "string",
                        "description": "What happens, e.g. 'Application submission window'.",
                    },
                    "date_text": {
                        "type": "string",
                        "description": "The date EXACTLY as printed on the page, e.g. "
                                       "'Jun - Sep', '15 August 2026'. Never reformat "
                                       "or infer a year that is not written.",
                    },
                    "applies_to": {
                        "type": "string",
                        "description": "Programs/faculties it applies to, or '' for all.",
                    },
                },
                "required": ["intake", "event", "date_text", "applies_to"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["dates"],
    "additionalProperties": False,
}

CONTACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string",
                              "description": "Office or campus name as printed."},
                    "campus": {"type": "string", "description": "Campus name, or ''."},
                    "city": {"type": "string", "description": "City, or ''."},
                    "address": {"type": "string",
                                "description": "Postal address as printed, or ''."},
                    "phone": {"type": "string",
                              "description": "Phone exactly as printed, including the "
                                             "'-5' style range suffix. '' if absent."},
                    "email": {"type": "string", "description": "Email, or ''."},
                },
                "required": ["label", "campus", "city", "address", "phone", "email"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["contacts"],
    "additionalProperties": False,
}

EXTRACT_SYSTEM = """You extract structured records from Riphah International \
University web pages for a factual question-answering system.

Rules, in priority order:
1. Copy values verbatim from the page. Never reformat, translate, normalise, or \
complete them. A date written "Jun - Sep" stays "Jun - Sep". A phone written \
"+92-51-5912890 -5" stays exactly that.
2. Never infer. If a year, city, or campus is not written on the page, use "" \
for that field rather than deducing it from context.
3. Extract only records the page actually states. An empty array is the correct \
answer for a page that has none. Do not pad the output.
4. Ignore site chrome: navigation, search boxes, "CONNECT WITH US", WhatsApp and \
Messenger link lists, cookie notices, and footers.
5. Do not extract per-program fee amounts or seat counts — other pipeline stages \
own those."""


def _claude():
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set — add it to .env")
    return anthropic.Anthropic()


def _extract(text: str, schema: dict[str, Any], instruction: str) -> dict[str, Any]:
    """One structured-extraction call. Returns {} when the model yields nothing.

    Note: no `temperature` — Claude Opus 5 rejects sampling parameters with a 400.
    """
    import json

    client = _claude()
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=8000,
        system=EXTRACT_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{
            "role": "user",
            "content": f"{instruction}\n\n--- PAGE TEXT ---\n{text[:60000]}",
        }],
    )
    if response.stop_reason == "refusal":
        print("  ! extraction refused by safety classifier; skipping page")
        return {}
    payload = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        return {}


def run_dates(*, refresh: bool = False) -> int:
    conn = db.connect()
    stored = 0
    try:
        conn.execute("DELETE FROM important_dates")
        for path in DATE_SOURCES:
            url = config.BASE + path
            html = fetch.get_text(url, use_cache=not refresh)
            if not html:
                continue
            text = parse.clean_text(html)
            if len(text) < 200:
                continue

            data = _extract(
                text, DATES_SCHEMA,
                "Extract every admission deadline, campaign window, or academic "
                "calendar date stated on this page.",
            )
            for record in data.get("dates", []):
                if not record.get("date_text") or not record.get("event"):
                    continue
                conn.execute(
                    "INSERT INTO important_dates (intake, event, date_text, applies_to, "
                    "source_url, fetched_at) VALUES (?,?,?,?,?,?)",
                    (record.get("intake") or None, record["event"], record["date_text"],
                     record.get("applies_to") or None, url, db.now()),
                )
                stored += 1
            print(f"  dates: {path} -> {len(data.get('dates', []))}", flush=True)
        conn.commit()
    finally:
        conn.close()
    print(f"  dates: {stored} rows", flush=True)
    return stored


_PHONE_RE = re.compile(
    r"(?P<label>(?:Tel|Landline|Mobile|Phone|Fax|UAN)\s*:?\s*)"
    r"(?P<number>\+?92[\d\s\-,()]{7,40})",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")


def run_contacts_regex(*, refresh: bool = False) -> int:
    """Deterministic contact extraction — no API key required.

    The escalation path is the fallback for every other failure mode ("I don't
    have that, here's who to call"), so it must not itself depend on an API key
    being present. Riphah prints phone numbers in a consistent
    "Landline: +92-51-..." form, which a regex handles fine. `run_contacts()`
    (Claude-based) still runs when a key is available and yields richer records
    with addresses attached to the right campus.
    """
    conn = db.connect()
    stored = 0
    seen: set[str] = set()
    try:
        for path in CONTACT_SOURCES:
            url = config.BASE + path
            html = fetch.get_text(url, use_cache=not refresh)
            if not html:
                continue
            text = parse.clean_text(html)

            lines = text.splitlines()
            for index, line in enumerate(lines):
                match = _PHONE_RE.search(line)
                if not match:
                    continue
                number = re.sub(r"\s{2,}", " ", match.group("number")).strip(" ,")
                if number in seen:
                    continue
                seen.add(number)

                # The campus name is the nearest preceding line ending in
                # "Campus" or naming a known city.
                label = "Riphah International University"
                for back in range(index - 1, max(-1, index - 7), -1):
                    candidate = lines[back].strip()
                    if candidate.endswith("Campus") or candidate in config.CITIES.values():
                        label = candidate
                        break

                # City is rarely in the label — "Raiwind Campus" is in Lahore but
                # never says so. Resolve it from the campus map, then the address,
                # so "the Lahore campus number" is answerable.
                city = next((c for c in config.CITIES.values() if c in label), None)
                if city is None:
                    for name, campus_city in config.CAMPUSES.values():
                        if name and name.lower() in label.lower():
                            city = campus_city
                            break
                if city is None:
                    for satellite in config.SATELLITE_CAMPUSES:
                        if satellite.lower() in label.lower():
                            city = satellite.replace(" Campus", "")
                            break

                address = None
                for back in range(index - 1, max(-1, index - 4), -1):
                    candidate = lines[back].strip()
                    if "," in candidate and not candidate.endswith("Campus"):
                        address = candidate
                        break

                conn.execute(
                    "INSERT INTO contacts (label, campus, city, address, phone, email, "
                    "source_url, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                    (label, label if label.endswith("Campus") else None, city,
                     address, number, None, url, db.now()),
                )
                stored += 1

            for email in set(_EMAIL_RE.findall(text)):
                if email in seen:
                    continue
                seen.add(email)
                conn.execute(
                    "INSERT INTO contacts (label, email, source_url, fetched_at) "
                    "VALUES (?,?,?,?)",
                    (f"Email ({path.strip('/') or 'general'})", email, url, db.now()),
                )
                stored += 1
        conn.commit()
    finally:
        conn.close()
    print(f"  contacts (regex): {stored} rows", flush=True)
    return stored


def run_contacts(*, refresh: bool = False) -> int:
    conn = db.connect()
    stored = 0
    seen: set[tuple[str, str]] = set()
    try:
        conn.execute("DELETE FROM contacts")
        for path in CONTACT_SOURCES:
            url = config.BASE + path
            html = fetch.get_text(url, use_cache=not refresh)
            if not html:
                continue
            text = parse.clean_text(html)
            if len(text) < 200:
                continue

            data = _extract(
                text, CONTACTS_SCHEMA,
                "Extract every campus address, office phone number, and contact "
                "email stated on this page.",
            )
            for record in data.get("contacts", []):
                label = (record.get("label") or "").strip()
                phone = (record.get("phone") or "").strip()
                if not label or not (phone or record.get("email") or record.get("address")):
                    continue
                key = (label.lower(), phone)
                if key in seen:
                    continue
                seen.add(key)
                conn.execute(
                    "INSERT INTO contacts (label, campus, city, address, phone, email, "
                    "source_url, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                    (label, record.get("campus") or None, record.get("city") or None,
                     record.get("address") or None, phone or None,
                     record.get("email") or None, url, db.now()),
                )
                stored += 1
            print(f"  contacts: {path} -> {len(data.get('contacts', []))}", flush=True)
        conn.commit()
    finally:
        conn.close()
    print(f"  contacts: {stored} rows", flush=True)
    return stored


def run_all(*, refresh: bool = False) -> int:
    """Dates + contacts. Uses Claude when a key is present; always populates
    contacts, because the escalation path must work without one."""
    total = 0
    if os.getenv("ANTHROPIC_API_KEY", "").strip().startswith("sk-") and \
            len(os.getenv("ANTHROPIC_API_KEY", "")) > 25:
        total += run_dates(refresh=refresh)
        total += run_contacts(refresh=refresh)
    else:
        print("  extras: no ANTHROPIC_API_KEY — dates skipped, "
              "contacts via regex fallback", flush=True)
        conn = db.connect()
        conn.execute("DELETE FROM contacts")
        conn.commit()
        conn.close()
    # Regex pass always runs: it costs nothing and fills numbers the LLM pass
    # may have phrased differently.
    total += run_contacts_regex(refresh=refresh)
    return total


if __name__ == "__main__":
    db.migrate()
    config.ensure_dirs()
    with db.stage("extras") as result:
        result["items"] = run_all()
