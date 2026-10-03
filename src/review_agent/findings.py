"""What a reviewer returns, and how findings from several reviewers become one report."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
MAX_FINDINGS = 10  # per report. Each reviewer is also asked for at most 5.


class Finding(BaseModel):
    # The reviewer model gets these descriptions as part of its schema. They are the only
    # definition of each field. The prompt holds only the judgement rules.
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
    # Other reviewers whose findings on the same lines were folded into this one.
    also_flagged_by: SkipJsonSchema[list[str]] = []

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
    """Check that the quoted code is at the cited lines. Returns (status, finding).

    "ok": it's there. "relocated": the quote appears exactly once elsewhere in the file,
    so the lines are corrected. "unverified": the file is missing, or the quote is absent
    or ambiguous. Only the first non-blank line of the quote is matched.
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


CATCH_ALL = "correctness"  # in the broad sense every bug is a correctness bug


def _fold(specialist: Finding, catch_all: Finding) -> Finding:
    """Keep the specialist's finding and count the catch-all one as corroboration."""
    severity = max(specialist.severity, catch_all.severity, key=SEVERITY_RANK.__getitem__)
    return specialist.model_copy(update={
        "severity": severity,
        "also_flagged_by": [*specialist.also_flagged_by, catch_all.category],
    })


def merge(findings: list[Finding]) -> tuple[list[Finding], int]:
    """Rank, dedupe, fold and cap. Returns the kept findings and how many the cap dropped.

    - When two findings in the same category overlap, only the stronger one is kept.
      Findings are ranked first, so the stronger duplicate always wins.
    - A correctness finding that overlaps a security or performance finding is folded into
      it. The specialist's description stays, the severity is the worse of the two, and
      correctness goes into also_flagged_by. Correctness is the catch-all, so its version
      of a security or performance defect corroborates it and adds no new problem.
    - Security and performance findings on the same lines are both kept. An injectable
      query that also can't use an index is two problems.
    """
    kept: list[Finding] = []
    for f in sorted(findings, key=_rank, reverse=True):
        if any(k.category == f.category and _overlaps(k, f) for k in kept):
            continue
        if f.category == CATCH_ALL:
            i = next((i for i, k in enumerate(kept) if k.category != CATCH_ALL and _overlaps(k, f)), None)
            if i is not None:
                kept[i] = _fold(kept[i], f)
                continue
        else:
            i = next((i for i, k in enumerate(kept) if k.category == CATCH_ALL and _overlaps(k, f)), None)
            if i is not None:
                kept[i] = _fold(f, kept[i])
                continue
        kept.append(f)
    kept.sort(key=_rank, reverse=True)  # folding can raise a finding's severity
    return kept[:MAX_FINDINGS], max(0, len(kept) - MAX_FINDINGS)


def overall_severity(findings: list[Finding]) -> str:
    """The model never reports an overall severity."""
    return max((f.severity for f in findings), key=SEVERITY_RANK.__getitem__, default="none")
