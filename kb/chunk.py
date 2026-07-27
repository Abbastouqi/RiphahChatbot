"""Stage 4: split page text into retrievable chunks.

Chunking is heading-aware: a chunk keeps its nearest heading and is prefixed with
"<page title> — <heading>" before embedding. That prefix is what lets a query
like "computing faculty dean" match a chunk whose body never says "computing".

Before splitting, site-wide boilerplate is removed by *frequency* rather than by
CSS selector. Riphah's template repeats a mega-menu, a search overlay, a
WhatsApp/Messenger block and a full 11-campus footer on every page. Chasing those
with selectors is guesswork against a template we don't control; counting how many
pages a line appears on is not. Any line present on more than
BOILERPLATE_THRESHOLD of pages is chrome by definition, and dropping it cuts the
corpus by roughly two thirds without losing a single unique fact.
"""
from __future__ import annotations

import collections
import re

import config
from kb import db

# A line on more than this fraction of pages is template chrome, not content.
BOILERPLATE_THRESHOLD = 0.25
# Only consider short lines: a long paragraph repeated across pages is usually a
# genuine shared description (e.g. a faculty blurb) and worth keeping once.
BOILERPLATE_MAX_LEN = 120

# Lines that look like headings in our cleaned text: short, title-ish, no
# terminal punctuation.
_HEADING_RE = re.compile(r"^(?=.{3,90}$)(?![a-z])[^.!?:;]+$")

# Boilerplate that survives chrome-stripping on some pages.
_NOISE_PREFIXES = (
    "Apply Now", "Explore", "Read More", "Download", "Loading...",
    "Skip to content", "Toggle navigation",
)


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return False
    if stripped.endswith((".", "!", "?", ",")):
        return False
    words = stripped.split()
    if len(words) > 14:
        return False
    # Mostly-capitalised or title-case lines read as headings.
    caps = sum(1 for w in words if w[:1].isupper())
    return caps >= max(1, len(words) - 1) and bool(_HEADING_RE.match(stripped))


def _drop_noise(line: str) -> bool:
    stripped = line.strip()
    return (not stripped) or stripped in _NOISE_PREFIXES or len(stripped) <= 2


def split(text: str, *, target: int = config.CHUNK_TARGET_CHARS,
          overlap: int = config.CHUNK_OVERLAP_CHARS) -> list[tuple[str | None, str]]:
    """Return [(heading, chunk_text)]. Splits on blank lines, packs to ~target."""
    blocks: list[tuple[str | None, str]] = []
    heading: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if len(body) >= 40:            # a heading with no body isn't worth a chunk
            blocks.append((heading, body))
        buffer = []

    for raw_line in text.splitlines():
        if _drop_noise(raw_line):
            continue
        line = raw_line.strip()
        if _is_heading(line):
            # A new heading closes the previous section only if we have content.
            if sum(len(b) for b in buffer) > 0:
                flush()
            heading = line
            continue
        buffer.append(line)
        if sum(len(b) + 1 for b in buffer) >= target:
            flush()
            # Carry the tail forward so a fact split across the boundary is still
            # findable from either side.
            if overlap and blocks:
                tail = blocks[-1][1][-overlap:]
                cut = tail.find(" ")
                buffer = [tail[cut + 1:] if cut != -1 else tail]
    flush()
    return blocks


def boilerplate_lines(conn) -> set[str]:
    """Lines that appear on more than BOILERPLATE_THRESHOLD of HTML pages.

    Counted once per page (a set per page), so a line repeated ten times on one
    page doesn't look site-wide.
    """
    rows = conn.execute(
        "SELECT text FROM pages WHERE content_type = 'html' AND char_count > 0"
    ).fetchall()
    if len(rows) < 20:            # too small a sample to infer a template
        return set()

    counts: collections.Counter[str] = collections.Counter()
    for row in rows:
        unique = {
            line.strip() for line in row["text"].splitlines()
            if 0 < len(line.strip()) <= BOILERPLATE_MAX_LEN
        }
        counts.update(unique)

    cutoff = len(rows) * BOILERPLATE_THRESHOLD
    return {line for line, count in counts.items() if count >= cutoff}


def dedupe_chunks(conn, *, min_pages: int = 5) -> int:
    """Collapse chunks whose body text repeats verbatim across many pages.

    Line-frequency filtering (above) is a blunt instrument: it works on a
    percentage threshold, and Riphah has template *sections* that sit just under
    it. The "Scholarships & Financial Assistance" blurb appears on 123 of 549
    pages — 22%, under a 25% cutoff — so it survived as 123 near-identical
    chunks. Dense retrieval then ranks those 123 above the actual scholarships
    pages for any scholarship-adjacent question, because sheer duplicate mass
    beats a single better match.

    Rather than chase the threshold, key on the body text itself. Text repeated
    verbatim on `min_pages` or more distinct pages is a template block by
    definition; keep one representative (the shortest URL, i.e. the most
    canonical page) and drop the rest. Genuine content does not repeat word for
    word across five different pages.
    """
    rows = conn.execute(
        "SELECT id, url, text FROM chunks WHERE url NOT LIKE 'structured://%'"
    ).fetchall()

    groups: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for row in rows:
        # Drop the injected "<title> — <heading>" prefix before hashing: it is
        # per-page by construction and would make every duplicate look unique.
        body = row["text"].split("\n\n", 1)[-1]
        key = re.sub(r"\W+", " ", body.lower()).strip()
        if len(key) < 60:          # too short to judge; leave alone
            continue
        groups[key].append((row["id"], row["url"]))

    removed = 0
    for members in groups.values():
        distinct_urls = {url for _, url in members}
        if len(distinct_urls) < min_pages:
            continue
        keep_url = min(distinct_urls, key=lambda u: (len(u), u))
        doomed = [cid for cid, url in members if url != keep_url]
        # Keep one chunk on the canonical page even if it has several copies.
        keepers = [cid for cid, url in members if url == keep_url]
        doomed.extend(keepers[1:])
        if doomed:
            conn.executemany("DELETE FROM chunks WHERE id = ?",
                             [(cid,) for cid in doomed])
            removed += len(doomed)

    conn.commit()
    return removed


def build(*, rebuild: bool = False) -> int:
    """Chunk every page that has no chunks yet (or all pages when rebuild=True)."""
    conn = db.connect()
    written = 0
    try:
        if rebuild:
            conn.execute("DELETE FROM chunks")
            conn.commit()

        chrome = boilerplate_lines(conn)
        print(f"  chunks: {len(chrome)} boilerplate lines identified", flush=True)

        pages = conn.execute(
            """
            SELECT p.url, p.title, p.section, p.faculty, p.text
              FROM pages p
             WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.url = p.url)
            """
        ).fetchall()

        for page in pages:
            body_text = page["text"]
            if chrome:
                body_text = "\n".join(
                    line for line in body_text.splitlines()
                    if line.strip() not in chrome
                )
            for ordinal, (heading, body) in enumerate(split(body_text)):
                # Prefix gives the embedding the page's topic, which short chunks
                # otherwise lack entirely.
                label = " — ".join(x for x in (page["title"], heading) if x)
                embed_text = f"{label}\n\n{body}" if label else body
                conn.execute(
                    """
                    INSERT OR IGNORE INTO chunks
                        (url, ordinal, heading, text, section, faculty, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (page["url"], ordinal, heading, embed_text,
                     page["section"], page["faculty"], db.now()),
                )
                written += 1
        conn.commit()

        # Dedup runs over the whole table, not just this batch, so it is reported
        # separately rather than netted off `written`.
        removed = dedupe_chunks(conn)
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()

    print(f"  chunks: {written} written from {len(pages)} pages, "
          f"{removed} duplicate template chunks collapsed, {total} in store",
          flush=True)
    return total


def build_structured() -> int:
    """Also chunk the structured tables, so semantic search can reach them.

    A student asking "which campus teaches cyber security" should hit an offerings
    row even though that sentence appears on no page. These synthetic chunks live
    under a `structured://` URL so they never collide with crawled pages.
    """
    conn = db.connect()
    written = 0
    try:
        synthetic_url = "structured://offerings"
        conn.execute(
            "INSERT OR IGNORE INTO pages (url, title, section, content_type, text, "
            "char_count, content_hash, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            (synthetic_url, "Program Offerings by Campus", "programs", "synthetic",
             "", 0, "synthetic", db.now()),
        )
        conn.execute("DELETE FROM chunks WHERE url = ?", (synthetic_url,))

        rows = conn.execute(
            """
            SELECT o.program_name, o.campus, o.city, o.academic_unit, o.intake,
                   o.seats, o.timings, o.days, o.admission_status,
                   p.program_type, p.type_label, p.faculty
              FROM offerings o
              LEFT JOIN programs p ON p.program_id = o.program_id
             ORDER BY o.program_name, o.campus
            """
        ).fetchall()

        for ordinal, row in enumerate(rows):
            sentence = (
                f"{row['program_name']} is offered at {row['campus']}"
                + (f" by {row['academic_unit']}" if row["academic_unit"] else "")
                + ". "
                + (f"It is a {row['type_label']}. " if row["type_label"] else "")
                + (f"Faculty: {row['faculty']}. " if row["faculty"] else "")
                + (f"Intake: {row['intake']}. " if row["intake"] else "")
                + (f"Seats: {row['seats']}. " if row["seats"] else "")
                + (f"Timings: {row['timings']}" if row["timings"] else "")
                + (f", {row['days']}. " if row["days"] else ". ")
                + (f"Admission status: {row['admission_status']}."
                   if row["admission_status"] else "")
            )
            conn.execute(
                "INSERT OR IGNORE INTO chunks (url, ordinal, heading, text, section, "
                "faculty, campus, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                (synthetic_url, ordinal, row["program_name"], sentence,
                 "programs", row["faculty"], row["campus"], db.now()),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()

    print(f"  chunks: {written} synthetic offering chunks", flush=True)
    return written


if __name__ == "__main__":
    db.migrate()
    with db.stage("chunk") as result:
        result["items"] = build() + build_structured()
