"""What a reviewer returns, and how findings from several reviewers become one report."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
MAX_FINDINGS = 10  # per report; each reviewer is also asked for at most 5


class Finding(BaseModel):
    # The descriptions are part of the schema the reviewer model is given: they are the
    # single definition of each field. The prompt keeps only the judgement rules.
    file: str = Field(description="Path relative to the repository root.")
    line_start: int = Field(
        ge=1,
        description="First line of the defect, in the file as reviewed. Take it from Read output "
        + "or from the line numbers get_changed_files prints.",
    )
    line_end: int = Field(ge=1, description="Last line of the defect; equal to line_start for one line.")
    quote: str = Field(
        description="The text of line line_start, copied exactly. Put line_start on the most "
        + "specific line of the defect, one whose text appears only once in the file.",
    )
    severity: Literal["low", "medium", "high"] = Field(
        description="high: exploitable, loses data, or breaks normal use. medium: wrong behaviour "
        + "under a realistic trigger. low: real but minor.",
    )
    confidence: Literal["confirmed", "suspected"] = Field(
        description="confirmed: you read the code path that proves it. suspected: high impact but "
        + "not proven; the description says what is uncertain.",
    )
    description: str = Field(
        description="The defect and its trigger: the input, state or call path that makes it go "
        + "wrong. The location belongs in file and the line fields.",
    )
    suggested_fix: str = Field(default="", description="The smallest change that fixes it.")
    # Set by code from the reviewer that reported it, so it's not in the model's schema.
    category: SkipJsonSchema[str] = ""

    @model_validator(mode="after")
    def _range(self) -> Finding:
        if self.line_end < self.line_start:
            raise ValueError("line_end is before line_start")
        return self


class SkippedFile(BaseModel):
    file: str = Field(description="Path relative to the repository root.")
    reason: str = Field(description="Why this file was not reviewed.")


class ReviewerOutput(BaseModel):
    findings: list[Finding] = Field(description="At most 5, most important first. Empty when there is nothing to report.")
    files_reviewed: list[str] = Field(default=[], description="Every file you reviewed.")
    files_skipped: list[SkippedFile] = Field(default=[], description="Every file in scope you did not review.")


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
