"""The read side of the knowledge base — everything the agent's tools call.

Two kinds of lookup, and the distinction is deliberate:

  * `search()` — hybrid semantic + keyword over prose. Fuzzy question, ranked
    passages, similarity scores. Good for "what societies are there", bad for fees.
  * `fee_structure()` / `program_info()` / ... — exact SQL over the structured
    tables. Every number the agent quotes comes from here, with its currency and
    scrape date attached, so a fee can be stated but never invented.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import config
from kb import db, embed
from kb.vector_store import STORE

# ---------------------------------------------------------------- prose search

# At most this many passages from any single page, so one long page can't fill
# every result slot with near-duplicates.
MAX_CHUNKS_PER_URL = 2

_FTS_STRIP = re.compile(r'[^\w\s]')


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query. Quoted terms avoid operator
    injection from user input like 'fee AND *'."""
    words = [w for w in _FTS_STRIP.sub(" ", text).split() if len(w) > 2]
    return " OR ".join(f'"{w}"' for w in words[:12])


def search(query: str, *, top_k: int = config.DEFAULT_TOP_K,
           section: str | None = None, faculty: str | None = None) -> list[dict]:
    """Hybrid retrieval: dense vectors fused with FTS5 keyword hits.

    Vector search alone misses exact tokens (program codes like "BSSE", campus
    names, "Pharm-D"); FTS alone misses paraphrase. Reciprocal-rank fusion takes
    both rankings without needing the two score scales to be comparable.
    """
    dense: list[dict] = []
    try:
        vector = embed.embed_texts([query])[0]
        dense = STORE.search(vector, top_k=top_k * 2, section=section, faculty=faculty)
    except Exception as exc:  # noqa: BLE001 - keyword search still works offline
        print(f"  ! dense retrieval unavailable: {type(exc).__name__}: {exc}")

    sparse: list[dict] = []
    match_expression = _fts_query(query)
    if match_expression:
        conn = db.connect()
        try:
            rows = conn.execute(
                """
                SELECT c.id, c.url, c.heading, c.text, c.section, c.faculty,
                       c.campus, p.title, p.fetched_at, bm25(chunks_fts) AS rank
                  FROM chunks_fts
                  JOIN chunks c ON c.id = chunks_fts.rowid
                  JOIN pages  p ON p.url = c.url
                 WHERE chunks_fts MATCH ?
                 ORDER BY rank
                 LIMIT ?
                """,
                (match_expression, top_k * 2),
            ).fetchall()
            sparse = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            sparse = []          # malformed MATCH — fall back to dense only
        finally:
            conn.close()

    # Reciprocal rank fusion. k=60 is the standard damping constant.
    K = 60
    fused: dict[int, dict[str, Any]] = {}
    for rank, item in enumerate(dense):
        key = item["chunk_id"]
        fused[key] = {**item, "score": 1.0 / (K + rank + 1)}
    for rank, item in enumerate(sparse):
        key = item["id"]
        contribution = 1.0 / (K + rank + 1)
        if key in fused:
            fused[key]["score"] += contribution
        else:
            fused[key] = {
                "chunk_id": key, "url": item["url"], "title": item["title"],
                "heading": item["heading"], "text": item["text"],
                "section": item["section"], "faculty": item["faculty"],
                "campus": item["campus"], "fetched_at": item["fetched_at"],
                "similarity": None, "score": contribution,
            }

    # Cap chunks per source page. Without this, a long page can occupy every slot
    # with near-identical passages — which on a voice call means one page's worth
    # of coverage spent on the whole answer budget.
    ranked: list[dict[str, Any]] = []
    per_url: dict[str, int] = {}
    overflow: list[dict[str, Any]] = []
    for item in sorted(fused.values(), key=lambda d: -d["score"]):
        url = item.get("url") or ""
        if per_url.get(url, 0) < MAX_CHUNKS_PER_URL:
            per_url[url] = per_url.get(url, 0) + 1
            ranked.append(item)
        else:
            overflow.append(item)
        if len(ranked) >= top_k:
            break

    # If diversity starved the result set, backfill rather than under-deliver.
    if len(ranked) < top_k:
        ranked.extend(overflow[:top_k - len(ranked)])
    return ranked


# ----------------------------------------------------------- structured lookups

def _fmt_money(amount: int | None, currency: str) -> str | None:
    return None if amount is None else f"{currency} {amount:,}"


def _match_programs(conn, name: str, limit: int = 8) -> list[sqlite3.Row]:
    """Find programs by name, abbreviation, or loose token match.

    Voice transcription mangles program names ("B S computer science"), so this
    goes from strict to loose rather than requiring an exact string.
    """
    needle = name.strip()
    if not needle:
        return []

    exact = conn.execute(
        "SELECT * FROM programs WHERE LOWER(name) = LOWER(?) "
        "OR LOWER(abbreviation) = LOWER(?) LIMIT ?",
        (needle, needle, limit),
    ).fetchall()
    if exact:
        return exact

    like = conn.execute(
        "SELECT * FROM programs WHERE name LIKE ? OR abbreviation LIKE ? LIMIT ?",
        (f"%{needle}%", f"%{needle}%", limit),
    ).fetchall()
    if like:
        return like

    # Loose: require every token of >2 chars to appear somewhere in the name.
    tokens = [t for t in re.split(r"\W+", needle.lower()) if len(t) > 2]
    if not tokens:
        return []
    clauses = " AND ".join("LOWER(name) LIKE ?" for _ in tokens)
    return conn.execute(
        f"SELECT * FROM programs WHERE {clauses} LIMIT ?",
        [f"%{t}%" for t in tokens] + [limit],
    ).fetchall()


def fee_structure(program: str, *, campus: str | None = None,
                  applies_to: str | None = None) -> dict[str, Any]:
    """Exact first-semester fee rows for a program.

    Returns amounts pre-formatted **with their currency** — Riphah prices
    international students in USD and locals in PKR in the same table, so the
    agent must never see a bare integer.
    """
    conn = db.connect()
    try:
        sql = ["SELECT * FROM fees WHERE 1=1"]
        params: list[Any] = []

        matches = _match_programs(conn, program)
        if matches:
            names = [m["name"] for m in matches]
            sql.append(f"AND program_name IN ({', '.join('?' for _ in names)})")
            params.extend(names)
        else:
            sql.append("AND program_name LIKE ?")
            params.append(f"%{program.strip()}%")

        if campus:
            sql.append("AND campus LIKE ?")
            params.append(f"%{campus.strip()}%")
        if applies_to:
            sql.append("AND applies_to LIKE ?")
            params.append(f"%{applies_to.strip()}%")

        sql.append("ORDER BY program_name, campus, applies_to LIMIT 25")
        rows = conn.execute(" ".join(sql), params).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"found": False, "query": {"program": program, "campus": campus},
                "message": "No fee record in the knowledge base for that program."}

    out = []
    for r in rows:
        currency = r["currency"] or "PKR"
        entry = {
            "program": r["program_name"],
            "campus": r["campus"],
            "faculty": r["faculty"],
            "academic_unit": r["academic_unit"],
            "applies_to": r["applies_to"],
            "currency": currency,
            "first_semester_total": _fmt_money(r["total_fee"], currency),
            "breakdown": {
                k: _fmt_money(r[col], currency)
                for k, col in (
                    ("admission", "admission_fee"),
                    ("registration", "registration_fee"),
                    ("id_card", "card_fee"),
                    ("tuition", "tuition_fee"),
                    ("exam", "exam_fee"),
                    ("enrollment", "enrollment_fee"),
                    ("lab", "lab_fee"),
                )
                if r[col] is not None
            },
            "credit_hours": r["credit_hours"],
            "per_credit_hour": _fmt_money(r["per_credit_hour"], currency),
            "intake": r["intake"],
            "seats": r["seats"],
            "timings": r["timings"],
            "admission_status": r["admission_status"],
            "notes": r["notes"],
            "source_url": r["source_url"],
            "last_verified": r["fetched_at"][:10],
        }
        if r["other_fees"]:
            entry["additional_charges"] = json.loads(r["other_fees"])
        out.append(entry)

    return {
        "found": True,
        "count": len(out),
        "fees": out,
        "disclaimer": (
            "First-semester figures as published on riphah.edu.pk on "
            f"{out[0]['last_verified']}. Tuition, exam, enrollment and lab fees "
            "recur each semester. Fees exclude taxes and levies, exclude hostel "
            "charges, and the university may revise them at any time. Confirm with "
            "the admissions office before making a financial decision."
        ),
    }


def program_info(program: str) -> dict[str, Any]:
    """Eligibility, duration, faculty, and where a program is offered."""
    conn = db.connect()
    try:
        matches = _match_programs(conn, program, limit=5)
        if not matches:
            return {"found": False,
                    "message": f"No program in the knowledge base matches '{program}'."}

        results = []
        for row in matches:
            offerings = conn.execute(
                "SELECT campus, city, academic_unit, intake, seats, timings, days, "
                "admission_status FROM offerings WHERE program_id = ? ORDER BY campus",
                (row["program_id"],),
            ).fetchall()
            fee_campuses = conn.execute(
                "SELECT DISTINCT campus FROM fees WHERE program_id = ?",
                (row["program_id"],),
            ).fetchall()

            results.append({
                "program": row["name"],
                "abbreviation": row["abbreviation"],
                "level": row["type_label"],
                "faculty": row["faculty"],
                "academic_unit": row["academic_unit"],
                "duration": row["duration"],
                "credit_hours": row["credit_hours"],
                "eligibility": row["eligibility"],
                "selection_criteria": row["selection_criteria"],
                "overview": (row["overview"] or "")[:1200] or None,
                "offered_at": [dict(o) for o in offerings],
                "fee_data_available_for": [c["campus"] for c in fee_campuses],
                "detail_url": row["detail_url"],
                "last_verified": row["fetched_at"][:10],
            })
    finally:
        conn.close()

    return {"found": True, "count": len(results), "programs": results}


def list_programs(*, level: str | None = None, faculty: str | None = None,
                  campus: str | None = None, limit: int = 40) -> dict[str, Any]:
    """Browse the catalog. `level` accepts a code (U/M/D/CD/AD) or a label."""
    level_code = None
    if level:
        needle = level.strip().lower()
        for code, label in config.PROGRAM_TYPES.items():
            if needle == code.lower() or needle in label.lower():
                level_code = code
                break
        else:
            # Common phrasings the site doesn't use verbatim.
            aliases = {
                "bachelor": "U", "bachelors": "U", "undergrad": "U", "bs": "U",
                "master": "M", "masters": "M", "ms": "M", "graduate": "M",
                "phd": "D", "doctorate": "D", "doctoral": "D",
                "diploma": "CD", "certificate": "CD", "associate": "AD",
            }
            level_code = aliases.get(needle)

    conn = db.connect()
    try:
        sql = [
            "SELECT DISTINCT p.program_id, p.name, p.abbreviation, p.type_label, p.faculty",
            "FROM programs p",
        ]
        params: list[Any] = []
        if campus:
            sql.append("JOIN offerings o ON o.program_id = p.program_id")
        sql.append("WHERE 1=1")
        if level_code:
            sql.append("AND p.program_type = ?")
            params.append(level_code)
        if faculty:
            sql.append("AND p.faculty LIKE ?")
            params.append(f"%{faculty.strip()}%")
        if campus:
            sql.append("AND o.campus LIKE ?")
            params.append(f"%{campus.strip()}%")
        sql.append("ORDER BY p.name LIMIT ?")
        params.append(limit)

        rows = conn.execute(" ".join(sql), params).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
    finally:
        conn.close()

    return {
        "found": bool(rows),
        "count": len(rows),
        "catalog_total": total,
        "filters": {"level": level, "faculty": faculty, "campus": campus},
        "programs": [
            {"name": r["name"], "abbreviation": r["abbreviation"],
             "level": r["type_label"], "faculty": r["faculty"]}
            for r in rows
        ],
    }


def campus_offerings(campus: str, *, level: str | None = None,
                     limit: int = 60) -> dict[str, Any]:
    """Everything a given campus offers — the 'what can I study in Lahore' query."""
    conn = db.connect()
    try:
        sql = [
            "SELECT o.program_name, o.academic_unit, o.intake, o.seats,",
            "       o.admission_status, p.type_label",
            "  FROM offerings o LEFT JOIN programs p ON p.program_id = o.program_id",
            " WHERE (o.campus LIKE ? OR o.city LIKE ?)",
        ]
        params: list[Any] = [f"%{campus.strip()}%", f"%{campus.strip()}%"]
        if level:
            sql.append("AND p.type_label LIKE ?")
            params.append(f"%{level.strip()}%")
        sql.append("ORDER BY o.program_name LIMIT ?")
        params.append(limit)
        rows = conn.execute(" ".join(sql), params).fetchall()
    finally:
        conn.close()

    return {
        "found": bool(rows),
        "campus": campus,
        "count": len(rows),
        "programs": [dict(r) for r in rows],
    }


def admission_dates(*, intake: str | None = None) -> dict[str, Any]:
    conn = db.connect()
    try:
        if intake:
            rows = conn.execute(
                "SELECT * FROM important_dates WHERE intake LIKE ? ORDER BY id LIMIT 40",
                (f"%{intake.strip()}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM important_dates ORDER BY id LIMIT 40"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {
            "found": False,
            "message": "No dated schedule captured in the knowledge base.",
            "fallback_url": f"{config.BASE}/admissions/dates/",
        }
    return {
        "found": True,
        "count": len(rows),
        "dates": [
            {"intake": r["intake"], "event": r["event"], "date": r["date_text"],
             "applies_to": r["applies_to"], "source_url": r["source_url"],
             "last_verified": r["fetched_at"][:10]}
            for r in rows
        ],
        "disclaimer": "Deadlines move. Verify against riphah.edu.pk/admissions/dates/.",
    }


def contact_info(*, campus: str | None = None) -> dict[str, Any]:
    conn = db.connect()
    try:
        if campus:
            rows = conn.execute(
                "SELECT * FROM contacts WHERE campus LIKE ? OR city LIKE ? OR label LIKE ? "
                "LIMIT 15",
                (f"%{campus}%", f"%{campus}%", f"%{campus}%"),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM contacts LIMIT 15").fetchall()
    finally:
        conn.close()

    return {
        "found": bool(rows),
        "contacts": [
            {"label": r["label"], "campus": r["campus"], "city": r["city"],
             "address": r["address"], "phone": r["phone"], "email": r["email"],
             "source_url": r["source_url"]}
            for r in rows
        ],
        "apply_online": "https://admissions.riphah.edu.pk",
        "main_site": config.BASE,
    }


def log_query(question: str, *, language: str | None = None,
              normalized: str | None = None, tool: str | None = None,
              hit: bool = False, top_similarity: float | None = None) -> None:
    """Record what was asked. Misses here are the input to the next KB improvement."""
    try:
        conn = db.connect()
        conn.execute(
            "INSERT INTO query_log (asked_at, language, question, normalized, "
            "tool_used, hit, top_similarity) VALUES (?,?,?,?,?,?,?)",
            (db.now(), language, question, normalized, tool, int(hit), top_similarity),
        )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 - logging must never break an answer
        pass
