import pytest
from pydantic import ValidationError

from review_agent.findings import MAX_FINDINGS, Finding, ReviewerOutput, merge, overall_severity


def finding(category="security", file="a.py", start=10, end=None, severity="low", confidence="confirmed"):
    return Finding(file=file, line_start=start, line_end=end or start, quote="x = 1",
                   severity=severity, confidence=confidence, description="d", category=category)


def test_same_category_overlap_keeps_the_stronger_finding():
    kept, _ = merge([finding(start=10, end=12, severity="low"), finding(start=11, severity="high")])
    assert [f.severity for f in kept] == ["high"]


def test_different_categories_on_the_same_lines_are_both_kept():
    kept, _ = merge([finding("security", start=5), finding("correctness", start=5)])
    assert {f.category for f in kept} == {"security", "correctness"}


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
    assert overall_severity([finding(severity="low"), finding(severity="medium")]) == "medium"
    assert overall_severity([]) == "none"


def test_schema_rejects_what_the_prompt_forbids():
    with pytest.raises(ValidationError):
        finding(severity="very high")
    with pytest.raises(ValidationError):
        finding(start=10, end=9)


def test_category_is_set_by_code_not_by_the_model():
    assert "category" not in str(ReviewerOutput.model_json_schema())
