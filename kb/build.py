"""Phase 1 entry point: build or refresh the whole knowledge base.

    python -m kb.build                # everything, using cached HTML where present
    python -m kb.build --refresh      # re-fetch from riphah.edu.pk, ignoring cache
    python -m kb.build --only fees    # one stage
    python -m kb.build --skip extras  # skip the Claude-extraction stage
    python -m kb.build --status        # what's in the KB right now

Stage order matters: pages before chunks (chunks read page text), programs before
chunk-structured (synthetic chunks read offerings), chunks before embed.
"""
from __future__ import annotations

import argparse
import sys

import config
from kb import (
    chunk, crawl_extras, crawl_fees, crawl_pages, crawl_programs, db, embed,
)

STAGES = ["pages", "fees", "programs", "extras", "chunk", "embed"]


def _run_stage(name: str, *, refresh: bool, limit: int | None) -> int:
    print(f"\n=== {name} ===", flush=True)
    with db.stage(name) as result:
        if name == "pages":
            result["items"] = crawl_pages.run(refresh=refresh, limit=limit)
        elif name == "fees":
            result["items"] = crawl_fees.run(refresh=refresh)
        elif name == "programs":
            result["items"] = crawl_programs.run(refresh=refresh, limit=limit)
        elif name == "extras":
            result["items"] = crawl_extras.run_all(refresh=refresh)
        elif name == "chunk":
            result["items"] = chunk.build() + chunk.build_structured()
        elif name == "embed":
            result["items"] = embed.run()
        return result["items"]


def status() -> None:
    counts = db.counts()
    print("\nKnowledge base contents")
    print("-" * 46)
    for key, value in counts.items():
        print(f"  {key:22} {value:>8,}")

    conn = db.connect()
    try:
        print("\nLast run per stage")
        print("-" * 46)
        rows = conn.execute(
            """
            SELECT stage, status, items, finished_at, detail
              FROM crawl_log
             WHERE id IN (SELECT MAX(id) FROM crawl_log GROUP BY stage)
             ORDER BY id
            """
        ).fetchall()
        for row in rows:
            when = (row["finished_at"] or "running")[:19]
            print(f"  {row['stage']:10} {row['status']:6} {row['items']:>6}  {when}")
            if row["detail"] and row["status"] == "error":
                print(f"             ! {row['detail'][:100]}")

        stale = conn.execute(
            "SELECT MIN(fetched_at), MAX(fetched_at) FROM fees"
        ).fetchone()
        if stale and stale[0]:
            print(f"\n  fee data verified between {stale[0][:10]} and {stale[1][:10]}")
    finally:
        conn.close()

    if counts["chunks"] and not counts["chunks_embedded"]:
        print("\n  ! chunks exist but none are embedded — run: python -m kb.build --only embed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Riphah knowledge base")
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch pages instead of using the on-disk cache")
    parser.add_argument("--only", nargs="+", choices=STAGES, metavar="STAGE",
                        help=f"run only these stages ({', '.join(STAGES)})")
    parser.add_argument("--skip", nargs="+", choices=STAGES, metavar="STAGE",
                        help="run everything except these stages")
    parser.add_argument("--limit", type=int,
                        help="cap pages/programs per stage (for a smoke test)")
    parser.add_argument("--status", action="store_true", help="report and exit")
    args = parser.parse_args(argv)

    db.migrate()
    config.ensure_dirs()

    if args.status:
        status()
        return 0

    stages = args.only or [s for s in STAGES if s not in (args.skip or [])]
    failed: list[str] = []
    for name in stages:
        try:
            _run_stage(name, refresh=args.refresh, limit=args.limit)
        except Exception as exc:  # noqa: BLE001 - one stage failing shouldn't kill the rest
            failed.append(name)
            print(f"  ! stage '{name}' failed: {type(exc).__name__}: {exc}", flush=True)

    status()
    if failed:
        print(f"\nFailed stages: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
