import asyncio
from pathlib import Path
from typing import cast

import pytest
from claude_agent_sdk import ResultMessage

from review_agent import review
from review_agent.findings import Finding, ReviewerOutput
from review_agent.review import check_output, feedback, file_reader, run_reviewer, task
from review_agent.reviewers import REVIEWERS


def result(output: object) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0, is_error=False,
                         num_turns=1, session_id="s", structured_output=output)


def cite(file: str, line: int, quote: str) -> dict[str, object]:
    return {"file": file, "line_start": line, "line_end": line, "quote": quote,
            "severity": "high", "confidence": "confirmed", "description": "d"}


INVALID = {"findings": [{"file": "a.py"}]}


def test_range_task_names_the_refs():
    assert 'base="abc123"' in task("abc123", None) and 'head="HEAD"' in task("abc123", None)
    assert 'head="feature"' in task("abc123", "feature")


def test_whole_repo_task_has_no_diff_steps():
    text = task(None, None)
    assert "get_changed_files" not in text and "every source file" in text


def test_invalid_output_asks_for_a_retry_instead_of_crashing():
    checked = check_output(result(INVALID), lambda _path: None)
    assert checked.output is None and "validation" in checked.problems[0]


def test_schema_failure_gets_the_schema_message():
    message = feedback(check_output(result(INVALID), lambda _path: None))
    assert "didn't match the schema" in message and "validation errors for ReviewerOutput" in message
    assert "cite code" not in message


def test_bad_citations_get_the_citation_message_with_each_problem():
    lines = ["x = 1"] * 1641
    checked = check_output(result({"findings": [cite("a.rs", 1680, "y = 2"), cite("b.rs", 1, "x")]}),
                           lambda path: lines if path == "a.rs" else None)
    message = feedback(checked)
    assert "cite code that isn't where they say" in message
    assert "- a.rs:1680-1680 (the file has 1641 lines)." in message
    assert "- b.rs:1-1 (that file doesn't exist in the reviewed code)." in message


def test_schema_describes_every_field_the_model_fills():
    schema = cast(dict[str, dict[str, dict[str, dict[str, dict[str, object]]]]], ReviewerOutput.model_json_schema())
    props = schema["$defs"]["Finding"]["properties"]
    assert set(props) == set(Finding.model_fields) - {"category", "also_flagged_by"}  # set by code
    assert all("description" in p for p in props.values())


def test_file_reader_keeps_dotted_directories(tmp_path: Path):
    (tmp_path / ".github").mkdir()
    _ = (tmp_path / ".github" / "ci.yml").write_text("on: push\n")
    read = file_reader(str(tmp_path), None, None)
    assert read("./.github/ci.yml") == ["on: push"]
    assert read("../outside.txt") is None


def fake_client(*outputs: object) -> type:
    """Stands in for ClaudeSDKClient: each query gets the next output as its result."""
    answers = iter(outputs)

    class Client:
        def __init__(self, options: object): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *_exc: object): return False
        async def query(self, _prompt: str): ...
        async def receive_response(self):
            yield result(next(answers))

    return Client


def run_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *outputs: object) -> review.ReviewerRun:
    _ = (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(review, "ClaudeSDKClient", fake_client(*outputs))
    return asyncio.run(run_reviewer(REVIEWERS[0], str(tmp_path), None, None))


def test_invalid_retry_keeps_the_valid_first_answer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    first = {"findings": [cite("a.py", 1, "x = 1"), cite("gone.py", 1, "y")]}  # one bad citation
    run = run_with(monkeypatch, tmp_path, first, INVALID)
    assert run.status == "ok"
    assert [f.file for f in run.findings] == ["a.py"] and [f.file for f in run.unverified] == ["gone.py"]


def test_valid_retry_replaces_an_invalid_first_answer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    run = run_with(monkeypatch, tmp_path, INVALID, {"findings": [cite("a.py", 1, "x = 1")]})
    assert run.status == "ok" and [f.file for f in run.findings] == ["a.py"]


def test_two_invalid_answers_fail_the_reviewer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    run = run_with(monkeypatch, tmp_path, INVALID, INVALID)
    assert run.status == "failed" and run.error == "invalid output after one retry"
