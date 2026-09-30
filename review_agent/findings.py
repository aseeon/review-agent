"""What a reviewer returns, and how findings from several reviewers become one report."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
MAX_FINDINGS = 10  # per report; each reviewer is also asked for at most 5


class Finding(BaseModel):
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str
    severity: Literal["low", "medium", "high"]
    confidence: Literal["confirmed", "suspected"]
    description: str
    suggested_fix: str = ""
    # Set by code from the reviewer that reported it, so it's not in the model's schema.
    category: SkipJsonSchema[str] = ""

    @model_validator(mode="after")
    def _range(self) -> Finding:
        if self.line_end < self.line_start:
            raise ValueError("line_end is before line_start")
        return self


class SkippedFile(BaseModel):
    file: str
    reason: str


class ReviewerOutput(BaseModel):
    findings: list[Finding]
    files_reviewed: list[str] = []
    files_skipped: list[SkippedFile] = []


def check_citation(f: Finding, lines: list[str] | None) -> tuple[str, Finding]:
    """Is the quoted code at the cited lines? Returns (status, finding).

    "ok": it's there. "relocated": the quote appears exactly once elsewhere in the file,
    so the lines are corrected. "unverified": the file is missing, or the quote is absent
    or ambiguous. A string match either finds the code or it doesn't; it can't guess.
    """
    quote = next((line.strip() for line in f.quote.splitlines() if line.strip()), "")
    if lines is None or not quote:
        return "unverified", f
    if any(quote in line for line in lines[f.line_start - 1 : f.line_end]):
        return "ok", f
    hits = [i for i, line in enumerate(lines, 1) if quote in line]
    if len(hits) == 1:
        shift = hits[0] - f.line_start
        return "relocated", f.model_copy(update={"line_start": hits[0], "line_end": f.line_end + shift})
    return "unverified", f


def _rank(f: Finding) -> tuple[int, bool]:
    return SEVERITY_RANK[f.severity], f.confidence == "confirmed"


def _overlaps(a: Finding, b: Finding) -> bool:
    return a.file == b.file and a.line_start <= b.line_end and b.line_start <= a.line_end


def merge(findings: list[Finding]) -> tuple[list[Finding], int]:
    """Rank, dedupe and cap. Returns the kept findings and how many the cap dropped.

    Same file, overlapping lines and same category: only the stronger finding is kept.
    Different categories on the same lines are both kept (an inverted permission check is
    a bug and a hole). Ranking comes first, so the stronger duplicate always wins.
    """
    kept: list[Finding] = []
    for f in sorted(findings, key=_rank, reverse=True):
        if not any(k.category == f.category and _overlaps(k, f) for k in kept):
            kept.append(f)
    return kept[:MAX_FINDINGS], max(0, len(kept) - MAX_FINDINGS)


def overall_severity(findings: list[Finding]) -> str:
    """The worst finding decides. The model never reports an overall severity."""
    return max((f.severity for f in findings), key=SEVERITY_RANK.__getitem__, default="none")
