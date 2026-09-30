from typing import cast

from review_agent.findings import Finding, ReviewerOutput
from review_agent.review import Checked, feedback, task


def test_range_task_names_the_refs_and_the_finish_line():
    text = task("abc123", None)
    assert 'base="abc123"' in text and 'head="HEAD"' in text
    assert "every file in the --stat summary" in text


def test_whole_repo_task_has_no_diff_steps():
    text = task(None, None)
    assert "get_changed_files" not in text and "every source file" in text


def test_schema_failure_gets_the_schema_message():
    message = feedback(Checked(None, [], [], ["1 validation error for ReviewerOutput"]))
    assert "didn't match the schema" in message and "1 validation error" in message
    assert "cite code" not in message


def test_bad_citations_get_the_citation_message_with_each_problem():
    output = ReviewerOutput(findings=[])
    message = feedback(Checked(output, [], [], ["a.rs:1680-1680 (the file has 1641 lines)."]))
    assert "cite code that isn't where they say" in message
    assert "- a.rs:1680-1680 (the file has 1641 lines)." in message


def test_schema_describes_every_field_the_model_fills():
    schema = cast(dict[str, dict[str, dict[str, dict[str, dict[str, object]]]]], ReviewerOutput.model_json_schema())
    props = schema["$defs"]["Finding"]["properties"]
    assert set(props) == set(Finding.model_fields) - {"category"}
    assert all("description" in p for p in props.values())
