"""python -m review_agent <repo> [base [head]]

Exits 0 when all three reviewers finished, 1 when any of them didn't.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .review import run_review


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    base = sys.argv[2] if len(sys.argv) > 2 else None
    head = sys.argv[3] if len(sys.argv) > 3 else None
    report = asyncio.run(run_review(repo, base, head))

    print(f"\nreview of {report['range']}: {report['status']}, severity {report['severity']}, "
          f"{len(report['findings'])} findings")
    for name, r in report["reviewers"].items():
        detail = r.get("error") or f"{r['tool_calls']} tool calls, ${r['cost_usd']:.2f}"
        print(f"  {name:<12} {r['status']:<7} {detail}")
    for f in report["findings"]:
        print(f"\n[{f['severity']}] {f['category']}  {f['file']}:{f['line_start']}-{f['line_end']}"
              f"  ({f['confidence']})\n    {f['description']}")
        if f["suggested_fix"]:
            print(f"    fix: {f['suggested_fix']}")
    for f in report["unverified"]:
        print(f"\n[unverified: cited code not found] {f['category']}  {f['file']}:{f['line_start']}"
              f"\n    {f['description']}")
    if report["dropped_by_cap"]:
        print(f"\n{report['dropped_by_cap']} lower-ranked findings dropped by the cap.")
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
