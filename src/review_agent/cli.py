"""The review-agent command.

Exits 0 when all three reviewers finished, 1 when any of them didn't, 2 on an invalid
budget override or a commit that can't be checked out.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .review import Report, record, run_review
from .reviewers import REVIEWERS


class Args(argparse.Namespace):
    repo: str = "."
    base: str | None = None
    head: str | None = None
    log: Path | None = None


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        prog="review-agent",
        description="Review a git repository, or a commit range in it, for correctness, security and performance problems.",
    )
    _ = parser.add_argument("repo", nargs="?", default=".", help="repository to review (default: .)")
    _ = parser.add_argument("base", nargs="?", help="start of the commit range; omit to review the whole repository")
    _ = parser.add_argument("head", nargs="?", help="end of the commit range (default: HEAD)")
    _ = parser.add_argument("--log", type=Path, metavar="FILE", help="append a one-line run record (outcome, counts, cost) to FILE")
    return parser.parse_args(argv, namespace=Args())


def print_report(report: Report) -> None:
    print(f"\nreview of {report.range}: {report.status}, severity {report.severity}, {len(report.findings)} findings")
    for name, r in report.reviewers.items():
        detail = r.error or f"{r.tool_calls} tool calls, ${r.cost_usd:.2f}"
        print(f"  {name:<12} {r.status:<7} {detail}")
    for f in report.findings:
        print(f"\n[{f.severity}] {f.category}  {f.file}:{f.line_start}-{f.line_end}  ({f.confidence})")
        print(f"    {f.description}")
        if f.suggested_fix:
            print(f"    fix: {f.suggested_fix}")
    for f in report.unverified:
        print(f"\n[unverified: cited code not found] {f.category}  {f.file}:{f.line_start}")
        print(f"    {f.description}")
    if report.dropped_by_cap:
        print(f"\n{report.dropped_by_cap} lower-ranked findings dropped by the cap.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:  # a bad budget override must stop the run before anything is spent
        for r in REVIEWERS:
            _ = r.budget()
    except ValueError as e:
        print(f"review-agent: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        report = asyncio.run(run_review(args.repo, args.base, args.head))
    except ValueError as e:  # e.g. head isn't a commit in this repository
        print(f"review-agent: {e}", file=sys.stderr)
        return 2
    if args.log:
        record(report, args.log)
    print_report(report)
    return 0 if report.status == "complete" else 1
