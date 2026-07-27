"""HTML -> clean text, plus the small parsers shared by the crawlers."""
from __future__ import annotations

import hashlib
import re
import urllib.parse

from bs4 import BeautifulSoup

import config

_WS = re.compile(r"[ \t\xa0]+")
_BLANKS = re.compile(r"\n{3,}")

# Chrome that appears on every page and would otherwise dominate every chunk.
_STRIP_SELECTORS = [
    "script", "style", "noscript", "svg", "iframe",
    "nav", "header", "footer",
    ".sidebar", "#sidebar", ".breadcrumb", ".breadcrumbs",
    ".social-icons", ".cookie", ".skip-link",
]


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def clean_text(html: str, *, strip_chrome: bool = True) -> str:
    soup = soup_of(html)
    if strip_chrome:
        for selector in _STRIP_SELECTORS:
            for node in soup.select(selector):
                node.decompose()

    # Force block-level separation so headings don't glue onto body text.
    for tag in soup.find_all(["br"]):
        tag.replace_with("\n")
    for tag in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]):
        tag.append("\n")

    text = soup.get_text(" ")
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


def page_title(html: str) -> str:
    soup = soup_of(html)
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        # Site appends a suffix on most pages; drop it for cleaner headings.
        for sep in (" | ", " - ", " – "):
            if sep in title and "Riphah" in title.split(sep)[-1]:
                title = sep.join(title.split(sep)[:-1])
        return title.strip()
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def section_of(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    return path.split("/")[0] if path else "home"


def faculty_of(url: str) -> str | None:
    return config.FACULTY_SLUGS.get(section_of(url))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_money(raw: str | None) -> int | None:
    """'PKR 157,097**' -> 157097.   '-' / '' / None -> None.

    None is meaningful: it's the site's '-', i.e. this fee is not charged for this
    program. Collapsing it to 0 would let the agent claim a fee exists at zero cost.
    """
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d]", "", raw)
    return int(cleaned) if cleaned else None


_CURRENCY_RE = re.compile(r"\b(PKR|USD|EUR|GBP|Rs\.?)\b", re.IGNORECASE)


def parse_currency(raw: str | None) -> str | None:
    """Extract the unit from a fee cell. 'USD 16,500' -> 'USD'.

    Riphah prices local students in PKR and international students in USD within
    the same table, so the unit has to travel with the number.
    """
    if not raw:
        return None
    match = _CURRENCY_RE.search(raw)
    if not match:
        return None
    token = match.group(1).upper().rstrip(".")
    return "PKR" if token == "RS" else token


def parse_meta_line(text: str) -> dict[str, str]:
    """Parse the 'Intake: Fall, Seats: 100 Male, Timings: ..., Admission: Open' line."""
    fields = {"intake", "seats", "timings", "days", "admission"}
    out: dict[str, str] = {}
    # Split on ', Label:' boundaries, keeping the label.
    parts = re.split(r",\s*(?=(?:Intake|Seats|Timings|Days|Admission)\s*:)", text)
    for part in parts:
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = key.strip().lower()
        if key in fields:
            out[key] = value.strip(" ,")
    return out


def absolutize(href: str, base: str = config.BASE) -> str:
    """Riphah's markup uses root-relative hrefs without a leading slash."""
    if href.startswith(("http://", "https://")):
        return href
    return urllib.parse.urljoin(base.rstrip("/") + "/", href.lstrip("/"))


def sitemap_urls(xml: str) -> list[tuple[str, str | None]]:
    """Return [(loc, lastmod)] from a urlset, or recurse a sitemapindex."""
    soup = BeautifulSoup(xml, "xml")
    out: list[tuple[str, str | None]] = []
    for node in soup.find_all("url"):
        loc = node.find("loc")
        if not loc:
            continue
        lastmod = node.find("lastmod")
        out.append((loc.get_text(strip=True), lastmod.get_text(strip=True) if lastmod else None))
    return out


def sitemap_index_children(xml: str) -> list[str]:
    soup = BeautifulSoup(xml, "xml")
    return [
        node.find("loc").get_text(strip=True)
        for node in soup.find_all("sitemap")
        if node.find("loc")
    ]


MAX_PDF_CHARS = 200_000


def pdf_text(data: bytes) -> str:
    """Extract text from a PDF. Returns '' if PyMuPDF is unavailable or it fails.

    Output is capped: a few Riphah brochures are image-heavy PDFs whose text
    layer extracts to megabytes of repeated glyph noise. Past MAX_PDF_CHARS the
    content is not prose any more, so truncate rather than index the noise.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ""
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            pages = [page.get_text("text") for page in doc]
    except Exception:
        return ""
    text = "\n\n".join(pages)
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text).strip()
    return text[:MAX_PDF_CHARS]
