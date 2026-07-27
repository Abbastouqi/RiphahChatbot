"""SQLite access. One connection factory, one migration call, a few helpers."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import config

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate() -> None:
    """Idempotent — every statement in schema.sql is CREATE ... IF NOT EXISTS,
    plus additive column migrations for databases created before a column existed.
    Column additions run *first*: schema.sql indexes reference the new columns."""
    with connect() as conn:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "conversations" in tables:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
            if "user_id" not in cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN user_id TEXT")
        conn.executescript(SCHEMA_PATH.read_text())


@contextmanager
def stage(name: str) -> Iterator[dict[str, Any]]:
    """Record a crawl stage in crawl_log, so `build.py --status` can report it.

    Yield a dict; set ``result["items"]`` inside the block to log a count.
    """
    started = now()
    conn = connect()
    cur = conn.execute(
        "INSERT INTO crawl_log (stage, status, started_at) VALUES (?, 'running', ?)",
        (name, started),
    )
    log_id = cur.lastrowid
    conn.commit()
    result: dict[str, Any] = {"items": 0, "detail": None}
    try:
        yield result
    except Exception as exc:  # noqa: BLE001 - we re-raise after logging
        conn.execute(
            "UPDATE crawl_log SET status='error', detail=?, finished_at=? WHERE id=?",
            (f"{type(exc).__name__}: {exc}"[:2000], now(), log_id),
        )
        conn.commit()
        conn.close()
        raise
    else:
        conn.execute(
            "UPDATE crawl_log SET status='ok', items=?, detail=?, finished_at=? WHERE id=?",
            (result["items"], result["detail"], now(), log_id),
        )
        conn.commit()
        conn.close()


def counts() -> dict[str, int]:
    tables = [
        "pages", "chunks", "programs", "offerings",
        "fees", "important_dates", "contacts",
    ]
    with connect() as conn:
        out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        out["chunks_embedded"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()[0]
    return out
