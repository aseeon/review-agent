"""Planted-defect eval: does the reviewer find known bugs, and stay quiet on a clean change?

Builds a throwaway git repo from fixture/ with three commits:
  base  -> head   plants the defects listed in expected.json
  head  -> clean  adds a correct new module, where the right answer is no findings
then reviews both ranges with the real agent, N times, and scores the findings:
a defect counts as found when a finding has the same file and category and its lines
come within 3 of the defect's line.

    uv run python evals/run.py [--runs N] [--out FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from review_agent.findings import Finding
from review_agent.review import Report, run_review

HERE = Path(__file__).parent
TOLERANCE = 3


class Defect(TypedDict):
    id: str
    category: str
    file: str
    at: str


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def build_repo(root: Path) -> tuple[str, str, str]:
    """Commit fixture/base, fixture/head and fixture/clean in order. Returns their SHAs."""
    _ = git(root, "init", "-q")
    _ = git(root, "config", "user.email", "eval@example.com")
    _ = git(root, "config", "user.name", "eval")
    shas: list[str] = []
    for stage in ("base", "head", "clean"):
        _ = shutil.copytree(HERE / "fixture" / stage, root, dirs_exist_ok=True)
        _ = git(root, "add", "-A")
        _ = git(root, "commit", "-q", "-m", stage)
        shas.append(git(root, "rev-parse", "HEAD"))
    return shas[0], shas[1], shas[2]


def defect_line(root: Path, defect: Defect) -> int:
    lines = (root / defect["file"]).read_text().splitlines()
    hits = [i for i, line in enumerate(lines, 1) if defect["at"] in line]
    if len(hits) != 1:
        raise ValueError(f"{defect['id']}: expected one line containing {defect['at']!r}, found {len(hits)}")
    return hits[0]


def matches(f: Finding, defect: Defect, line: int) -> bool:
    return (
        f.file.replace("\\", "/").removeprefix("./") == defect["file"]
        and f.category == defect["category"]
        and f.line_start - TOLERANCE <= line <= f.line_end + TOLERANCE
    )


def score(report: Report, defects: list[Defect], lines: dict[str, int]) -> dict[str, object]:
    found = {d["id"]: any(matches(f, d, lines[d["id"]]) for f in report.findings) for d in defects}
    extras = [f for f in report.findings if not any(matches(f, d, lines[d["id"]]) for d in defects)]
    return {"found": found, "extras": [f"{f.category} {f.file}:{f.line_start} {f.description[:80]}" for f in extras]}


def cost(report: Report) -> float:
    return round(sum(r.cost_usd for r in report.reviewers.values()), 4)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    _ = parser.add_argument("--runs", type=int, default=3)
    _ = parser.add_argument("--out", type=Path, default=Path("runs/evals/planted.jsonl"))
    args = parser.parse_args()
    runs = int(args.runs)  # pyright: ignore[reportAny]
    out = Path(args.out)  # pyright: ignore[reportAny]

    defects: list[Defect] = json.loads((HERE / "expected.json").read_text())["defects"]
    root = Path(tempfile.mkdtemp(prefix="review-eval-"))
    try:
        base, head, clean = build_repo(root)
        _ = git(root, "checkout", "-q", head)
        lines = {d["id"]: defect_line(root, d) for d in defects}
        _ = git(root, "checkout", "-q", clean)
        out.parent.mkdir(parents=True, exist_ok=True)
        for n in range(1, runs + 1):
            # One at a time: each range review creates its own git worktree in the same repo.
            planted = await run_review(str(root), base, head)
            quiet = await run_review(str(root), head, clean)
            result = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "run": n,
                "status": [planted.status, quiet.status],
                **score(planted, defects, lines),
                "unverified": len(planted.unverified) + len(quiet.unverified),
                "clean_findings": [f"{f.category} {f.file}:{f.line_start} {f.description[:80]}" for f in quiet.findings],
                "cost_usd": cost(planted) + cost(quiet),
            }
            with out.open("a", encoding="utf-8") as fh:
                _ = fh.write(json.dumps(result) + "\n")
            found = result["found"]
            assert isinstance(found, dict)
            print(f"run {n}: found {sum(found.values())}/{len(defects)} {found}, "  # pyright: ignore[reportUnknownArgumentType]
                  f"extras {len(result['extras'])}, clean findings {len(result['clean_findings'])}, ${result['cost_usd']:.2f}")  # pyright: ignore[reportArgumentType]
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
