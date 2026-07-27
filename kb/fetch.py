"""Polite HTTP fetching with an on-disk cache.

The cache matters more than it looks: parsing is the part we iterate on, and a
cache means a parser fix costs zero requests to riphah.edu.pk.
"""
from __future__ import annotations

import hashlib
import time
import urllib.parse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

import config

_client: httpx.Client | None = None
_last_request_at = 0.0


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml,*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": config.BASE + "/",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
    return _client


def allowed(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    return not any(path.startswith(p) for p in config.ROBOTS_DISALLOW)


def _cache_path(url: str, suffix: str):
    digest = hashlib.sha256(url.encode()).hexdigest()[:20]
    return config.RAW_DIR / f"{digest}{suffix}"


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < config.CRAWL_DELAY:
        time.sleep(config.CRAWL_DELAY - elapsed)
    _last_request_at = time.monotonic()


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _get(url: str) -> httpx.Response:
    _throttle()
    resp = client().get(url)
    # 404s are real answers, not transport failures — don't burn retries on them.
    if resp.status_code >= 500:
        resp.raise_for_status()
    return resp


def get_text(url: str, *, use_cache: bool = True, suffix: str = ".html") -> str | None:
    """Fetch a text resource. Returns None on 4xx (page genuinely absent)."""
    if not allowed(url):
        return None
    cached = _cache_path(url, suffix)
    if use_cache and cached.exists():
        return cached.read_text(encoding="utf-8", errors="replace")

    resp = _get(url)
    if resp.status_code >= 400:
        return None
    text = resp.text
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8", errors="replace")
    return text


def get_bytes(url: str, *, use_cache: bool = True, suffix: str = ".bin") -> bytes | None:
    if not allowed(url):
        return None
    cached = _cache_path(url, suffix)
    if use_cache and cached.exists():
        return cached.read_bytes()

    resp = _get(url)
    if resp.status_code >= 400:
        return None
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(resp.content)
    return resp.content


def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
