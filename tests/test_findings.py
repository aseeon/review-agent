from typing import Literal

import pytest
from pydantic import ValidationError

from review_agent.findings import MAX_FINDINGS, Finding, check_citation, merge, overall_severity

FILE = ["import os", "", "def load(path):", "    return open(path).read()", "", "x = 1"]

Severity = Literal["low", "medium", "high"]


def finding(
    category: str = "security",
    file: str = "a.py",
    start: int = 10,
    end: int | None = None,
    severity: Severity = "low",
    confidence: Literal["confirmed", "suspected"] = "confirmed",
) -> Finding:
    return Finding(file=file, line_start=start, line_end=end or start, quote="x = 1",
                   severity=severity, confidence=confidence, description="d", category=category)


def test_same_category_overlap_keeps_the_stronger_finding():
    kept, _ = merge([finding(start=10, end=12, severity="low"), finding(start=11, severity="high")])
    assert [f.severity for f in kept] == ["high"]


def test_correctness_on_a_specialists_lines_is_folded_in_as_corroboration():
    kept, _ = merge([finding("security", start=5), finding("correctness", start=5)])
    assert [(f.category, f.also_flagged_by) for f in kept] == [("security", ["correctness"])]


def test_fold_keeps_the_specialist_and_the_worse_severity_whichever_comes_first():
    kept, _ = merge([finding("correctness", start=5, severity="high"), finding("performance", start=5, severity="low")])
    assert [(f.category, f.severity, f.also_flagged_by) for f in kept] == [("performance", "high", ["correctness"])]


def test_two_specialists_on_the_same_lines_are_both_kept():
    kept, _ = merge([finding("security", start=24), finding("performance", start=24)])
    assert {f.category for f in kept} == {"security", "performance"}


def test_ranked_by_severity_then_confidence():
    kept, _ = merge([
        finding(file="a.py", severity="low"),
        finding(file="b.py", severity="high", confidence="suspected"),
        finding(file="c.py", severity="high"),
    ])
    assert [f.file for f in kept] == ["c.py", "b.py", "a.py"]


def test_cap_keeps_the_top_ranked_and_counts_the_rest():
    many = [finding(file=f"{i}.py", severity="high" if i == 0 else "low") for i in range(MAX_FINDINGS + 3)]
    kept, dropped = merge(many)
    assert len(kept) == MAX_FINDINGS and dropped == 3 and kept[0].file == "0.py"


def test_overall_severity_is_the_worst_finding():
    # high and low, because alphabetically "low" > "high": a plain max() would get it wrong
    assert overall_severity([finding(severity="high"), finding(severity="low")]) == "high"
    assert overall_severity([]) == "none"


def test_line_range_cannot_end_before_it_starts():
    with pytest.raises(ValidationError, match="line_end is before line_start"):
        _ = Finding.model_validate({**finding().model_dump(), "line_start": 10, "line_end": 9})


def cited(start: int, quote: str, end: int | None = None) -> Finding:
    return finding(start=start, end=end).model_copy(update={"quote": quote})


def test_citation_found_at_the_cited_line():
    assert check_citation(cited(4, "return open(path).read()"), FILE)[0] == "ok"


def test_citation_relocated_when_the_quote_is_unique_elsewhere():
    status, f = check_citation(cited(1, "def load(path):", end=2), FILE)
    assert status == "relocated" and (f.line_start, f.line_end) == (3, 4)


def test_citation_past_the_end_of_the_file_is_relocated_or_unverified():
    assert check_citation(cited(1680, "def load(path):"), FILE)[0] == "relocated"
    assert check_citation(cited(1680, "not in the file"), FILE)[0] == "unverified"


def test_ambiguous_or_missing_citations_are_unverified():
    assert check_citation(cited(6, ""), FILE)[0] == "unverified"           # nothing quoted
    assert check_citation(cited(2, "path"), FILE)[0] == "unverified"       # on lines 3 and 4
    assert check_citation(cited(1, "import os"), None)[0] == "unverified"  # no such file

