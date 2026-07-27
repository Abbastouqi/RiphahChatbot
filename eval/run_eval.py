"""Run the regression set against the text agent.

    python eval/run_eval.py                # all cases
    python eval/run_eval.py --retrieval    # retrieval only, no model calls (free)
    python eval/run_eval.py --id fee-mbbs-currency-trap
    python eval/run_eval.py --out results.json

`--retrieval` is the one to run while iterating on the knowledge base: it checks
that the structured lookups return the right facts without spending tokens on the
agent loop.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kb import retrieve  # noqa: E402

CASES_PATH = Path(__file__).parent / "test_cases.json"


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text())["cases"]


def check(case: dict, answer: str, trace: list[dict]) -> tuple[bool, list[str]]:
    """Return (passed, failure reasons).

    Supports substring assertions (`expect_contains` / `must_not_contain`) and
    regex ones (`expect_matches` / `must_not_match`). The regex forms exist
    because substrings kept flagging correct answers: banning "roughly PKR" also
    bans legitimate rounding for speech, and banning "per year" stops the agent
    quoting the user's phrase in order to refuse it. What actually needs
    forbidding — "an amount attached to an annual claim" — is a pattern, not a
    substring.
    """
    problems: list[str] = []
    lowered = answer.lower()

    expected_tool = case.get("expect_tool")
    if expected_tool:
        used = [step["tool"] for step in trace]
        if expected_tool not in used:
            problems.append(f"expected tool {expected_tool}, used {used or 'none'}")

    for needle in case.get("expect_contains", []):
        if needle.lower() not in lowered:
            problems.append(f"missing expected text {needle!r}")

    for needle in case.get("must_not_contain", []):
        if needle.lower() in lowered:
            problems.append(f"contains forbidden text {needle!r}")

    for pattern in case.get("expect_matches", []):
        if not re.search(pattern, answer, re.IGNORECASE | re.DOTALL):
            problems.append(f"no match for expected pattern {pattern!r}")

    for pattern in case.get("must_not_match", []):
        found = re.search(pattern, answer, re.IGNORECASE | re.DOTALL)
        if found:
            problems.append(f"matched forbidden pattern {pattern!r} at {found.group(0)[:70]!r}")

    return (not problems), problems


def retrieval_only() -> int:
    """Assert the knowledge base can answer the fact-bearing cases at all.

    This bypasses the model entirely: if a fee isn't in the DB, no prompt will
    save the answer, so failures here are always the crawler's problem.
    """
    checks = [
        ("BSSE fee at I-14",
         lambda: retrieve.fee_structure("BS Software Engineering", campus="I-14"),
         lambda r: r["found"] and "213,878" in json.dumps(r)),
        ("MBBS local fee is PKR",
         lambda: retrieve.fee_structure("MBBS", applies_to="Pakistani"),
         lambda r: r["found"] and any(f["currency"] == "PKR" for f in r["fees"])),
        ("MBBS international fee is USD",
         lambda: retrieve.fee_structure("MBBS", applies_to="International"),
         lambda r: r["found"] and any(f["currency"] == "USD" for f in r["fees"])),
        ("no bare amounts leak (every amount carries a unit)",
         lambda: retrieve.fee_structure("MBBS"),
         lambda r: all(
             (v is None) or v.split()[0] in ("PKR", "USD", "EUR", "GBP")
             for f in r["fees"]
             for v in [f["first_semester_total"], *f["breakdown"].values()]
         )),
        ("BSSE eligibility present",
         lambda: retrieve.program_info("BS Software Engineering"),
         lambda r: r["found"] and bool(r["programs"][0].get("eligibility"))),
        ("BSSE offered at multiple campuses",
         lambda: retrieve.program_info("BS Software Engineering"),
         lambda r: r["found"] and len(r["programs"][0]["offered_at"]) >= 2),
        ("PhD catalog non-empty",
         lambda: retrieve.list_programs(level="doctoral"),
         lambda r: r["count"] > 0),
        ("Lahore campus has programs",
         lambda: retrieve.campus_offerings("Lahore"),
         lambda r: r["count"] > 0),
        ("unknown program returns found=False",
         lambda: retrieve.fee_structure("BS Astrophysics"),
         lambda r: r["found"] is False),
        ("prose search reaches faculty pages",
         lambda: retrieve.search("Faculty of Computing dean message", top_k=4),
         lambda r: len(r) > 0),
        ("prose search finds scholarships",
         lambda: retrieve.search("scholarships and financial assistance", top_k=4),
         lambda r: len(r) > 0),
        ("contact info always available",
         lambda: retrieve.contact_info(),
         lambda r: bool(r.get("apply_online"))),
    ]

    passed = 0
    print("\nRetrieval checks (no model calls)")
    print("=" * 66)
    for label, produce, predicate in checks:
        try:
            result = produce()
            ok = bool(predicate(result))
        except Exception as exc:  # noqa: BLE001
            ok = False
            result = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"          -> {json.dumps(result, ensure_ascii=False)[:220]}")
        passed += ok

    print("-" * 66)
    print(f"  {passed}/{len(checks)} retrieval checks passed")
    return 0 if passed == len(checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval", action="store_true",
                        help="run retrieval checks only (no API calls)")
    parser.add_argument("--id", help="run a single case by id")
    parser.add_argument("--out", help="write full results as JSON")
    args = parser.parse_args(argv)

    if args.retrieval:
        return retrieval_only()

    from agent.text_agent import answer as ask

    cases = load_cases()
    if args.id:
        cases = [c for c in cases if c["id"] == args.id]
        if not cases:
            print(f"no case with id {args.id!r}")
            return 2

    results, passed = [], 0
    print(f"\nAgent evaluation — {len(cases)} cases")
    print("=" * 78)
    for case in cases:
        try:
            response = ask(case["question"])
            answer_text = response.get("answer", "")
            trace = response.get("trace", [])
        except Exception as exc:  # noqa: BLE001
            answer_text, trace = f"ERROR: {type(exc).__name__}: {exc}", []

        ok, problems = check(case, answer_text, trace)
        passed += ok
        results.append({**case, "answer": answer_text, "trace": trace,
                        "passed": ok, "problems": problems})

        print(f"\n  [{'PASS' if ok else 'FAIL'}] {case['id']}")
        print(f"        Q: {case['question']}")
        print(f"        tools: {[s['tool'] for s in trace] or 'none'}")
        print(f"        A: {answer_text[:220]}{'…' if len(answer_text) > 220 else ''}")
        for problem in problems:
            print(f"        ! {problem}")

    print("\n" + "=" * 78)
    print(f"  {passed}/{len(cases)} cases passed")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"  wrote {args.out}")

    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
