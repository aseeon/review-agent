import pytest
from pydantic import ValidationError

from types import SimpleNamespace

from review_agent.findings import (
    MAX_FINDINGS, Finding, ReviewerOutput, check_citation, merge, overall_severity,
)
from review_agent.review import check_output, file_reader

FILE = ["import os", "", "def load(path):", "    return open(path).read()", "", "x = 1"]


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


def cited(start, quote, end=None):
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
    assert check_citation(cited(6, ""), FILE)[0] == "unverified"         # nothing quoted
    assert check_citation(cited(2, "path"), FILE)[0] == "unverified"     # on lines 3 and 4
    assert check_citation(cited(1, "import os"), None)[0] == "unverified"  # no such file


def test_invalid_output_asks_for_a_retry_instead_of_crashing():
    result = SimpleNamespace(subtype="success", is_error=False,
                             structured_output={"findings": [{"file": "a.py"}]})
    output, verified, unverified, problems = check_output(result, lambda path: FILE)
    assert output is None and "validation" in problems[0]


def test_file_reader_keeps_dotted_directories(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("on: push\n")
    read = file_reader(str(tmp_path), None, None)
    assert read("./.github/ci.yml") == ["on: push"]
    assert read("../outside.txt") is None
