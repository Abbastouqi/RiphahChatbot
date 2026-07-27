"""Stage 1: crawl every URL in sitemap.xml (plus the news index) into `pages`.

584 URLs at last count, of which a handful are PDFs. Nothing here needs a
headless browser — only the News block on the homepage is JS-rendered, and that
is deliberately skipped (news is not what an admissions bot gets asked about).
"""
from __future__ import annotations

import urllib.parse

import config
from kb import db, fetch, parse

# Pages that add noise without answering questions.
SKIP_PATTERNS = (
    "/campus-life/",   # ~20 event recap pages
    "/news/",
)


def _is_pdf(url: str) -> bool:
    """True for PDF resources, including the ones served behind a query string.

    Riphah's program brochures live at `/programs/brochure/?file=program-16.pdf`
    — the *path* has no extension, so checking only the path decodes a PDF as
    text and yields megabytes of mojibake. Check the whole URL.
    """
    parsed = urllib.parse.urlparse(url.lower())
    return parsed.path.endswith(".pdf") or ".pdf" in parsed.query


def _looks_binary(text: str) -> bool:
    """Guard against anything that decoded as text but isn't.

    Cheap belt-and-braces: a real HTML page has almost no NUL bytes or
    replacement characters, and always contains a tag.
    """
    sample = text[:4000]
    if "\x00" in sample or sample.count("�") > 20:
        return True
    return "<" not in sample and "%PDF" in text[:1024]


def _should_skip(url: str, *, include_events: bool) -> bool:
    if not fetch.allowed(url):
        return True
    if include_events:
        return False
    path = urllib.parse.urlparse(url).path
    return any(p in path for p in SKIP_PATTERNS)


def discover(*, include_news: bool = False) -> list[tuple[str, str | None]]:
    """Return [(url, lastmod)] from the main sitemap, optionally plus news."""
    urls: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    main = fetch.get_text(config.SITEMAP, suffix=".xml")
    if main:
        for url, lastmod in parse.sitemap_urls(main):
            if url not in seen:
                seen.add(url)
                urls.append((url, lastmod))

    if include_news:
        index = fetch.get_text(config.NEWS_SITEMAP, suffix=".xml")
        for child in parse.sitemap_index_children(index or ""):
            child_xml = fetch.get_text(child, suffix=".xml")
            for url, lastmod in parse.sitemap_urls(child_xml or ""):
                if url not in seen:
                    seen.add(url)
                    urls.append((url, lastmod))

    return urls


def _store(conn, url: str, lastmod: str | None, text: str, kind: str) -> bool:
    """Upsert a page. Returns True when the text changed (so chunks need rebuilding)."""
    digest = parse.content_hash(text)
    row = conn.execute("SELECT content_hash FROM pages WHERE url = ?", (url,)).fetchone()
    if row and row["content_hash"] == digest:
        return False

    title = parse.page_title(text) if kind == "html" else urllib.parse.unquote(
        urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    )
    body = parse.clean_text(text) if kind == "html" else text

    conn.execute(
        """
        INSERT INTO pages (url, title, section, faculty, content_type,
                           text, char_count, content_hash, lastmod, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title, section=excluded.section, faculty=excluded.faculty,
            content_type=excluded.content_type, text=excluded.text,
            char_count=excluded.char_count, content_hash=excluded.content_hash,
            lastmod=excluded.lastmod, fetched_at=excluded.fetched_at
        """,
        (
            url, title, parse.section_of(url), parse.faculty_of(url), kind,
            body, len(body), digest, lastmod, db.now(),
        ),
    )
    # Text changed -> old chunks are stale.
    conn.execute("DELETE FROM chunks WHERE url = ?", (url,))
    return True


def run(*, limit: int | None = None, include_news: bool = False,
        include_events: bool = False, refresh: bool = False) -> int:
    """Crawl and store pages. Returns the number of pages whose content changed."""
    urls = discover(include_news=include_news)
    urls = [(u, m) for u, m in urls if not _should_skip(u, include_events=include_events)]
    if limit:
        urls = urls[:limit]

    changed = 0
    conn = db.connect()
    try:
        for i, (url, lastmod) in enumerate(urls, 1):
            try:
                if _is_pdf(url):
                    raw = fetch.get_bytes(url, use_cache=not refresh, suffix=".pdf")
                    if not raw:
                        continue
                    text = parse.pdf_text(raw)
                    if len(text) < 200:      # scanned or empty — nothing to index
                        continue
                    kind = "pdf"
                else:
                    text = fetch.get_text(url, use_cache=not refresh)
                    if not text:
                        continue
                    if _looks_binary(text):
                        # Mislabelled binary — retry it as a PDF rather than
                        # indexing decoded noise.
                        raw = fetch.get_bytes(url, use_cache=not refresh, suffix=".pdf")
                        text = parse.pdf_text(raw) if raw else ""
                        if len(text) < 200:
                            continue
                        kind = "pdf"
                    else:
                        kind = "html"

                if _store(conn, url, lastmod, text, kind):
                    changed += 1
                if i % 25 == 0:
                    conn.commit()
                    print(f"  pages: {i}/{len(urls)} ({changed} changed)", flush=True)
            except Exception as exc:  # noqa: BLE001 - one bad page must not abort the crawl
                print(f"  ! {url}: {type(exc).__name__}: {exc}", flush=True)
        conn.commit()
    finally:
        conn.close()

    print(f"  pages: {len(urls)} fetched, {changed} changed", flush=True)
    return changed


if __name__ == "__main__":
    db.migrate()
    config.ensure_dirs()
    with db.stage("pages") as result:
        result["items"] = run()
