"""Stage 2: fee structures, via /programs/program-finder-fee-structure.php

Nine GETs (one per faculty, `au=0&pc=0&cl=0` = every unit, every campus) return
the complete fee table for the university. Each program/campus pair renders as:

    <div class='nav-link-card ...'>  academic unit + faculty
    <div class='txt-center txt-bold'>I-14 Campus (Islamabad)</div>
    <h4><a href='programs/detail/?p=16'>BS Software Engineering (BSSE)</a></h4>
    <p ...>Intake: Fall, Seats: 100 Male, Timings: ..., Admission: Open</p>
    <table class='responsive-table'>  ...the money...
    <div ...>** Tuition Fee For 17 Credit Hours @ PKR 9,241 Per Credit Hour</div>
    <h5>Notes/Instructions For Pakistani Nationals</h5><ul><li>...</li></ul>

Columns vary by faculty (some programs have a Lab fee, some don't), so headers
are read from <thead> and matched by position rather than assumed.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator

from bs4 import Tag

import config
from kb import db, fetch, parse

# thead label -> fees column. Anything unmatched lands in fees.other_fees as JSON.
COLUMN_MAP = {
    "for": "applies_to",
    "admission": "admission_fee",
    "registration": "registration_fee",
    "card": "card_fee",
    "id card": "card_fee",
    "tuition": "tuition_fee",
    "exam": "exam_fee",
    "examination": "exam_fee",
    "enrollment": "enrollment_fee",
    "enrolment": "enrollment_fee",
    "lab": "lab_fee",
    "laboratory": "lab_fee",
    "total": "total_fee",
}

MONEY_COLUMNS = {
    "admission_fee", "registration_fee", "card_fee", "tuition_fee",
    "exam_fee", "enrollment_fee", "lab_fee", "total_fee",
}

_CREDIT_RE = re.compile(
    r"Tuition Fee For\s*(?P<hours>[\d.]+)\s*Credit Hours?\s*@\s*PKR\s*(?P<rate>[\d,]+)",
    re.IGNORECASE,
)
_PROGRAM_ID_RE = re.compile(r"[?&]p=(\d+)")
_CAMPUS_RE = re.compile(r"(Campus|Location)", re.IGNORECASE)


def _clean(node: Tag | None) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _between(start: Tag, stop: Tag | None) -> Iterator[Tag]:
    """Yield elements after `start` up to (not including) `stop`."""
    for node in start.find_all_next():
        if stop is not None and node is stop:
            return
        yield node


def _find_context(table: Tag) -> dict[str, Any]:
    """Walk backwards from a fee table to recover program / campus / unit."""
    ctx: dict[str, Any] = {}

    heading = table.find_previous("h4")
    if heading is not None:
        ctx["program_name"] = _clean(heading)
        link = heading.find("a", href=True)
        if link:
            match = _PROGRAM_ID_RE.search(link["href"])
            if match:
                ctx["program_id"] = int(match.group(1))

        # Campus sits in the bold div immediately above the program heading.
        for sibling in heading.previous_siblings:
            if isinstance(sibling, Tag):
                text = _clean(sibling)
                if text and _CAMPUS_RE.search(text) and len(text) < 80:
                    ctx["campus"] = text
                    break
                if sibling.name == "h4":   # walked into the previous program block
                    break

    card = table.find_previous(
        "div", class_=lambda c: c and "nav-link-card" in " ".join(c if isinstance(c, list) else [c])
    )
    if card is not None:
        ctx["academic_unit"] = _clean(card.find("h4"))
        faculty_div = card.find("div")
        ctx["faculty"] = _clean(faculty_div)

    meta_p = None
    for sibling in table.previous_siblings:
        if isinstance(sibling, Tag) and "Intake" in sibling.get_text():
            meta_p = sibling
            break
    if meta_p is None:
        candidate = table.find_previous("p")
        if candidate is not None and "Intake" in candidate.get_text():
            meta_p = candidate
    if meta_p is not None:
        ctx.update(parse.parse_meta_line(_clean(meta_p)))

    return ctx


def _find_trailing(table: Tag, next_table: Tag | None) -> dict[str, Any]:
    """Credit-hour rate and the notes list that follow a fee table."""
    out: dict[str, Any] = {}
    notes: list[str] = []
    for node in _between(table, next_table):
        text = node.get_text(" ", strip=True)
        if "credit_hours" not in out:
            match = _CREDIT_RE.search(text)
            if match:
                out["credit_hours"] = match.group("hours")
                out["per_credit_hour"] = parse.parse_money(match.group("rate"))
        if node.name == "li":
            item = text.strip()
            if item and item not in notes:
                notes.append(item)
    if notes:
        out["notes"] = "\n".join(f"- {n}" for n in notes)
    return out


def _headers_of(table: Tag) -> list[str]:
    head = table.find("thead")
    scope = head or table
    cells = scope.find_all("th")
    if not cells:                       # some tables use a <tr> of <td> as the header
        first_row = table.find("tr")
        cells = first_row.find_all("td") if first_row else []
    return [_clean(c).lower().rstrip("*").strip() for c in cells]


def parse_fee_page(html: str, source_url: str) -> list[dict[str, Any]]:
    soup = parse.soup_of(html)
    tables = soup.select("table.responsive-table") or soup.find_all("table")
    rows: list[dict[str, Any]] = []

    for index, table in enumerate(tables):
        next_table = tables[index + 1] if index + 1 < len(tables) else None
        headers = _headers_of(table)
        if not headers:
            continue

        ctx = _find_context(table)
        if not ctx.get("program_name") or not ctx.get("campus"):
            continue                     # not a fee table (e.g. a stray layout table)
        ctx.update(_find_trailing(table, next_table))

        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 3:
                continue
            record: dict[str, Any] = {
                "program_id": ctx.get("program_id"),
                "program_name": ctx["program_name"],
                "campus": ctx["campus"],
                "city": next(
                    (c for c in config.CITIES.values() if c in ctx["campus"]), None
                ),
                "faculty": ctx.get("faculty"),
                "academic_unit": ctx.get("academic_unit"),
                "applies_to": "Pakistani Nationals",
                "currency": "PKR",
                "intake": ctx.get("intake"),
                "seats": ctx.get("seats"),
                "timings": ctx.get("timings"),
                "days": ctx.get("days"),
                "admission_status": ctx.get("admission"),
                "credit_hours": ctx.get("credit_hours"),
                "per_credit_hour": ctx.get("per_credit_hour"),
                "notes": ctx.get("notes"),
                "source_url": source_url,
            }
            extras: dict[str, str] = {}
            currencies: set[str] = set()

            for position, cell in enumerate(cells):
                label = headers[position] if position < len(headers) else f"col{position}"
                column = COLUMN_MAP.get(label)
                raw = _clean(cell)
                if column == "applies_to":
                    record["applies_to"] = raw or "Pakistani Nationals"
                elif column in MONEY_COLUMNS:
                    record[column] = parse.parse_money(raw)
                    unit = parse.parse_currency(raw)
                    if unit:
                        currencies.add(unit)
                elif raw and raw != "-":
                    extras[label] = raw

            # One row is quoted in one unit. If the site ever mixes them, keep the
            # ambiguity visible instead of silently picking one.
            if len(currencies) == 1:
                record["currency"] = currencies.pop()
            elif len(currencies) > 1:
                record["currency"] = "/".join(sorted(currencies))

            if extras:
                record["other_fees"] = json.dumps(extras, ensure_ascii=False)
            if record.get("total_fee") is None and record.get("tuition_fee") is None:
                continue                 # header row or an empty layout row
            rows.append(record)

    return rows


def run(*, refresh: bool = False) -> int:
    conn = db.connect()
    stored = 0
    try:
        conn.execute("DELETE FROM fees")   # full replace: fees are revised wholesale
        for faculty_id, faculty_name in config.FACULTIES.items():
            url = f"{config.FEE_ENDPOINT}?f={faculty_id}&au=0&pc=0&cl=0"
            html = fetch.get_text(url, use_cache=not refresh, suffix=f".fee{faculty_id}.html")
            if not html:
                print(f"  ! no response for {faculty_name}", flush=True)
                continue

            rows = parse_fee_page(html, url)
            for row in rows:
                columns = list(row.keys()) + ["fetched_at"]
                placeholders = ", ".join("?" for _ in columns)
                updates = ", ".join(
                    f"{c}=excluded.{c}" for c in columns
                    if c not in ("program_name", "campus", "applies_to")
                )
                # Upsert on (program_name, campus, applies_to) — see idx_fees_unique.
                conn.execute(
                    f"INSERT INTO fees ({', '.join(columns)}) VALUES ({placeholders}) "
                    f"ON CONFLICT(program_name, campus, applies_to) DO UPDATE SET {updates}",
                    list(row.values()) + [db.now()],
                )
            print(f"  fees: {faculty_name} -> {len(rows)} rows parsed", flush=True)
        conn.commit()
        stored = conn.execute("SELECT COUNT(*) FROM fees").fetchone()[0]
    finally:
        conn.close()

    print(f"  fees: {stored} unique rows stored", flush=True)
    return stored


if __name__ == "__main__":
    db.migrate()
    config.ensure_dirs()
    with db.stage("fees") as result:
        result["items"] = run()
