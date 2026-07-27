"""Stage 3: the program catalog (184 programs) and where each one is offered.

The catalog is not exposed as an API — it lives in the <select name='p'> on
/admissions/dates/, where each <option> carries Riphah's own program id and a
`data-chained` attribute naming the program type:

    <option value='16' data-chained='ALL U'>BS Software Engineering  (BSSE)</option>

With the ids in hand, /programs/program-finder-admissions.php?df=0&pt=<type>&p=<id>
returns a `table#po-list` of every campus offering that program, and
/programs/detail/?p=<id> holds eligibility and curriculum prose.
"""
from __future__ import annotations

import json
import re
from typing import Any

from bs4 import Tag

import config
from kb import db, fetch, parse

_ABBREV_RE = re.compile(r"\(([^()]+)\)\s*$")
_OPTION_RE = re.compile(
    r"<option\s+value='(?P<id>\d+)'\s+data-chained='(?P<chained>[^']*)'\s*>(?P<label>.*?)</option>",
    re.IGNORECASE | re.DOTALL,
)

# Detail pages carry stable anchor ids (`<div id='programEligibilityCriteria'>`),
# which are far more reliable than matching on heading text. Each anchor marks
# the start of a section; the section runs until the next anchor.
DETAIL_ANCHORS = {
    "programOverview": "overview",
    "programEligibilityCriteria": "eligibility",
    "programSelectionCriteria": "selection_criteria",
    "programMeritLists": "merit_lists",
    "programAwardingCriteria": "awarding_criteria",
    "programAdmissionsFee": "admissions_fee",   # fees come from crawl_fees; kept for context
}

# Most anchors are empty spacer <div>s sitting immediately before their content,
# so walking next_siblings works. `programOverview` is the exception: it is an
# <h2> on the page title used purely as a link target, and the overview prose
# lives further down under its own <h3>. These labels are the fallback: when the
# sibling walk comes back empty, find the heading with this text instead.
SECTION_HEADINGS = {
    "overview": "Program Overview",
    "eligibility": "Eligibility Criteria",
    "selection_criteria": "Merit / Selection Criteria",
    "merit_lists": "Merit Lists",
    "awarding_criteria": "Degree Awarding Criteria",
    "admissions_fee": "Admissions & Fee Structure",
}

# Columns on `programs` that a parsed section can populate directly.
SECTION_COLUMNS = {"overview", "eligibility", "selection_criteria"}

# The overview renders as a flat run of label/value pairs with no punctuation
# between them ("... Total Credit Hours 136 Credit Hours Program Duration 4 Years
# (8 Semesters) Program Offerings 4 Locations ..."). To read one value you have to
# stop at the next label, so the label vocabulary is the delimiter.
_OVERVIEW_LABELS = [
    "Program Type", "System", "Program Modality", "Total Credit Hours",
    "Program Duration", "Programme Duration", "Duration", "Program Offerings",
    "Offered by", "Eligibility", "Accreditation", "Degree Awarded",
]


def _labelled_value(text: str, label: str, *, max_len: int = 60) -> str | None:
    """Value following `label`, cut at whichever other label comes next."""
    match = re.search(rf"\b{re.escape(label)}\b\s*[:\-]?\s*", text, re.IGNORECASE)
    if not match:
        return None
    tail = text[match.end():]

    stop = len(tail)
    for other in _OVERVIEW_LABELS:
        if other.lower() == label.lower():
            continue
        found = re.search(rf"\b{re.escape(other)}\b", tail, re.IGNORECASE)
        if found:
            stop = min(stop, found.start())
    value = tail[:stop].strip(" .,;–-")
    return value[:max_len].strip() or None


_CREDITS_RE = re.compile(r"([\d]{1,3})\s*Credit Hours", re.IGNORECASE)


def _clean(node: Tag | None) -> str:
    return node.get_text(" ", strip=True) if node else ""


def catalog(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Read the master program list off the admissions-dates page."""
    html = fetch.get_text(f"{config.BASE}/admissions/dates/", use_cache=not refresh,
                          suffix=".dates.html")
    if not html:
        return []

    # Scope to the program select so we don't pick up the faculty/campus selects.
    match = re.search(r"<select[^>]*name='p'[^>]*>(.*?)</select>", html, re.DOTALL | re.IGNORECASE)
    if not match:
        return []

    programs: list[dict[str, Any]] = []
    seen: set[int] = set()
    for option in _OPTION_RE.finditer(match.group(1)):
        program_id = int(option.group("id"))
        if program_id in seen:
            continue
        seen.add(program_id)

        label = parse.clean_text(option.group("label"), strip_chrome=False)
        label = re.sub(r"\s+", " ", label).strip()
        # data-chained is "ALL <TYPE>"; the trailing token is the program type.
        tokens = [t for t in option.group("chained").split() if t != "ALL"]
        ptype = tokens[-1] if tokens else None

        abbrev_match = _ABBREV_RE.search(label)
        programs.append({
            "program_id": program_id,
            "name": label,
            "abbreviation": abbrev_match.group(1).strip() if abbrev_match else None,
            "program_type": ptype,
            "type_label": config.PROGRAM_TYPES.get(ptype or ""),
            "detail_url": f"{config.PROGRAM_DETAIL}?p={program_id}",
        })
    return programs


def parse_offerings(html: str, program_name: str, program_id: int,
                    source_url: str) -> list[dict[str, Any]]:
    soup = parse.soup_of(html)
    table = soup.find("table", id="po-list") or soup.find("table")
    if table is None:
        return []

    out: list[dict[str, Any]] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 6:
            continue

        location_cell = cells[0]
        unit_div = location_cell.find("div")
        academic_unit = _clean(unit_div).lstrip("By").strip() if unit_div else None
        if unit_div:
            unit_div.extract()          # leave only the campus name behind
        campus = _clean(location_cell)

        out.append({
            "program_id": program_id,
            "program_name": program_name,
            "campus": campus,
            "city": next((c for c in config.CITIES.values() if c in campus), None),
            "academic_unit": academic_unit,
            "intake": _clean(cells[1]) or None,
            "seats": _clean(cells[2]) or None,
            "timings": _clean(cells[3]) or None,
            "days": _clean(cells[4]) or None,
            "admission_status": _clean(cells[5]) or None,
            "source_url": source_url,
        })
    return out


def _section_text(anchor: Tag, program_name: str) -> str:
    """Text of the block a detail-page anchor introduces, up to the next anchor.

    Each anchor is an empty spacer <div>; the content is in the siblings that
    follow it. The section repeats the program name as a subtitle under its
    heading — dropped here so it doesn't get mistaken for the section body.
    """
    parts: list[str] = []
    for sibling in anchor.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if sibling.get("id", "") in DETAIL_ANCHORS:
            break
        text = _clean(sibling)
        if text:
            parts.append(text)

    # Collapse whitespace first: the page renders the program name with double
    # spaces ("BS Software Engineering  (BSSE)") while the catalog stores it with
    # single ones, so a raw replace never matches.
    body = re.sub(r"\s+", " ", "\n".join(parts)).strip()

    normalized = re.sub(r"\s+", " ", program_name).strip()
    if normalized:
        body = body.replace(normalized, " ")
    # Drop the section's own heading — it repeats the anchor we already know.
    for heading in ("Eligibility Criteria", "Program Overview",
                    "Merit / Selection Criteria", "Merit Lists",
                    "Degree Awarding Criteria", "Admissions & Fee Structure"):
        if body.startswith(heading):
            body = body[len(heading):]
            break
    return re.sub(r"\s{2,}", " ", body).strip(" \n-")


def parse_detail(html: str, program_name: str = "") -> dict[str, Any]:
    """Pull structured sections out of a program detail page via its anchor ids."""
    soup = parse.soup_of(html)
    found: dict[str, Any] = {}
    sections: dict[str, str] = {}

    for anchor_id, key in DETAIL_ANCHORS.items():
        text = ""
        anchor = soup.find(id=anchor_id)
        if anchor is not None:
            text = _section_text(anchor, program_name)

        if not text:
            # Fall back to the section's visible heading (see SECTION_HEADINGS).
            label = SECTION_HEADINGS.get(key)
            heading = soup.find(
                ["h2", "h3", "h4"],
                string=lambda s, want=label: bool(s) and want and want in s.strip(),
            ) if label else None
            if heading is not None:
                text = _section_text(heading, program_name)

        if not text:
            continue
        sections[key] = text[:6000]
        if key in SECTION_COLUMNS:
            found[key] = text[:4000]

    if sections:
        found["sections"] = json.dumps(sections, ensure_ascii=False)

    # Duration and credit hours are stated in overview prose, not as labelled
    # fields. Only scrape the overview: eligibility and merit text also contain
    # "N Credit Hours" (deficiency courses, prior-degree requirements), and
    # matching those yields a confidently wrong number. When the overview is
    # absent, leave these NULL — fees.credit_hours carries the authoritative
    # figure, taken from the fee-table footnote.
    overview = sections.get("overview", "")
    for label in ("Program Duration", "Programme Duration", "Duration"):
        value = _labelled_value(overview, label)
        if value:
            found["duration"] = value
            break

    hours = _labelled_value(overview, "Total Credit Hours", max_len=30)
    if hours:
        found["credit_hours"] = hours
    else:
        credits = _CREDITS_RE.search(overview)
        if credits:
            found["credit_hours"] = f"{credits.group(1)} Credit Hours"

    body = parse.clean_text(html)
    if body:
        found["description"] = body[:4000]
    return found


def run(*, refresh: bool = False, with_details: bool = True,
        limit: int | None = None) -> int:
    programs = catalog(refresh=refresh)
    if limit:
        programs = programs[:limit]
    if not programs:
        print("  ! program catalog empty — the <select name='p'> markup may have changed")
        return 0

    conn = db.connect()
    offering_count = 0
    try:
        conn.execute("DELETE FROM offerings")
        for index, program in enumerate(programs, 1):
            pid = program["program_id"]
            ptype = program["program_type"] or "ALL"

            if with_details:
                detail_html = fetch.get_text(
                    program["detail_url"], use_cache=not refresh, suffix=f".prog{pid}.html"
                )
                if detail_html:
                    program.update(parse_detail(detail_html, program["name"]))

            columns = list(program.keys()) + ["fetched_at"]
            placeholders = ", ".join("?" for _ in columns)
            updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "program_id")
            conn.execute(
                f"INSERT INTO programs ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(program_id) DO UPDATE SET {updates}",
                list(program.values()) + [db.now()],
            )

            dates_url = f"{config.DATES_ENDPOINT}?df=0&pt={ptype}&p={pid}"
            dates_html = fetch.get_text(dates_url, use_cache=not refresh,
                                        suffix=f".off{pid}.html")
            if dates_html:
                for row in parse_offerings(dates_html, program["name"], pid, dates_url):
                    cols = list(row.keys()) + ["fetched_at"]
                    conn.execute(
                        f"INSERT INTO offerings ({', '.join(cols)}) "
                        f"VALUES ({', '.join('?' for _ in cols)})",
                        list(row.values()) + [db.now()],
                    )
                    offering_count += 1

            if index % 20 == 0:
                conn.commit()
                print(f"  programs: {index}/{len(programs)} "
                      f"({offering_count} offerings)", flush=True)
        conn.commit()

        # Backfill faculty/unit onto programs from the fee table, which carries
        # authoritative faculty attribution straight out of the fee HTML.
        conn.execute(
            """
            UPDATE programs SET
                faculty = COALESCE(faculty, (
                    SELECT f.faculty FROM fees f
                     WHERE f.program_id = programs.program_id AND f.faculty IS NOT NULL
                     LIMIT 1)),
                academic_unit = COALESCE(academic_unit, (
                    SELECT f.academic_unit FROM fees f
                     WHERE f.program_id = programs.program_id AND f.academic_unit IS NOT NULL
                     LIMIT 1))
            """
        )
        conn.commit()
    finally:
        conn.close()

    print(f"  programs: {len(programs)} stored, {offering_count} offerings", flush=True)
    return len(programs)


if __name__ == "__main__":
    db.migrate()
    config.ensure_dirs()
    with db.stage("programs") as result:
        result["items"] = run()
