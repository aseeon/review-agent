"""Run the three reviewers in parallel and merge what they find into one report.

The plan is the same on every run (three independent reviews, then a merge), so it lives
in code. Judgement stays inside each reviewer, which reads whatever it needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NamedTuple, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ToolUseBlock,
)
from pydantic import BaseModel, Field, ValidationError

from .diff import GIT_TOOL, build_git_server
from .findings import Finding, ReviewerOutput, SkippedFile, check_citation, merge, overall_severity
from .reviewers import REVIEWERS, Reviewer

log = logging.getLogger("review")

READ_TOOLS = ["Read", "Grep", "Glob"]

# A result subtype other than success means the reviewer didn't finish.
FAILURES = {
    "error_max_budget_usd": "budget exhausted",
    "error_max_turns": "turn limit reached",
    "error_max_structured_output_retries": "no valid structured output",
}

CITATION_FEEDBACK = """\
These findings cite code that isn't where they say. For each one, Read the file and correct \
line_start, line_end and quote, or drop the finding if it doesn't hold. Return the complete \
output again, all findings included.
"""

SCHEMA_FEEDBACK = """\
Your output didn't match the schema:
{errors}
Return the complete output again, all findings included."""

LineReader = Callable[[str], "list[str] | None"]


class ReviewerFailed(Exception):
    pass


class ReviewerRun(BaseModel):
    """One reviewer's outcome. Findings travel separately into the merged report."""

    model: str
    status: Literal["ok", "failed"] = "ok"
    error: str | None = None
    tool_calls: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    files_reviewed: list[str] = []
    files_skipped: list[SkippedFile] = []
    findings: list[Finding] = Field(default=[], exclude=True)
    unverified: list[Finding] = Field(default=[], exclude=True)


class Report(BaseModel):
    # A failed reviewer is a visible gap, never an empty list of findings.
    status: Literal["complete", "partial", "failed"]
    range: str
    severity: str
    findings: list[Finding]
    dropped_by_cap: int
    # Citations that still didn't match after one retry: shown, never counted.
    unverified: list[Finding]
    reviewers: dict[str, ReviewerRun]


class Checked(NamedTuple):
    output: ReviewerOutput | None  # None when the answer failed validation
    verified: list[Finding]
    unverified: list[Finding]
    problems: list[str]  # what to send back to the reviewer


def build_options(reviewer: Reviewer, repo_path: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=reviewer.system_prompt(),
        cwd=repo_path,
        # tools decides which built-ins exist; allowed_tools which run without a prompt.
        # No write tools, no shell, no network: the worst an injected instruction can
        # do is change the report.
        tools=READ_TOOLS,
        allowed_tools=[*READ_TOOLS, GIT_TOOL],
        mcp_servers={"git": build_git_server(repo_path)},
        strict_mcp_config=True,     # ignore ambient .mcp.json / user settings
        setting_sources=[],         # the reviewed repo's CLAUDE.md never becomes instructions
        permission_mode="dontAsk",  # unattended: anything not allowed above is denied
        output_format={"type": "json_schema", "schema": ReviewerOutput.model_json_schema()},
        model=reviewer.model,
        max_turns=25,
        max_budget_usd=reviewer.budget(),
        env={
            "API_TIMEOUT_MS": "120000",
            "CLAUDE_CODE_MAX_RETRIES": "2",
            # Pinned like the models: the CLI's default cap on tool output can change
            # under us. diff.py stays well below it.
            "MAX_MCP_OUTPUT_TOKENS": "25000",
        },
    )


def task(base: str | None, head: str | None) -> str:
    """The first message: this run's steps and the finish line, which depend on the mode."""
    if base:
        head = head or "HEAD"
        return f"""\
Review the changes between {base} and {head}.
1. Call get_changed_files with base="{base}" and head="{head}". Its --stat summary lists every changed file.
2. Review each changed file. Fetch any file left out of the first result with the path argument.
3. Report defects in, or caused by, these changes.
You are done when every file in the --stat summary is in files_reviewed, or in files_skipped with the reason."""
    return """\
Review this repository.
1. Glob for the source files.
2. Start from the entry points and the most-used modules, and follow what they call.
3. Report defects anywhere in the code.
You are done when every source file is in files_reviewed, or in files_skipped with the reason."""


def feedback(checked: Checked) -> str:
    """What to send back for one retry. A schema failure and bad citations need different fixes."""
    if checked.output is None:
        return SCHEMA_FEEDBACK.format(errors=checked.problems[0])
    return CITATION_FEEDBACK + "\n".join(f"- {p}" for p in checked.problems)


async def collect(client: ClaudeSDKClient, name: str, run: ReviewerRun) -> ResultMessage:
    result = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    run.tool_calls += 1
                    log.info("%s → %s", name, block.name)
        elif isinstance(message, ResultMessage):
            result = message
            # The CLI reports a running total for the session, so take the latest.
            run.cost_usd = message.total_cost_usd or 0.0
            run.turns = message.num_turns
    if result is None:
        raise ReviewerFailed("the session ended without a result")
    return result


def file_reader(repo_path: str, base: str | None, head: str | None) -> LineReader:
    """Lines of a file as reviewed: at head for a commit range, else the working tree."""
    root = Path(repo_path).resolve()
    cache: dict[str, list[str] | None] = {}

    def read(path: str) -> list[str] | None:
        path = path.replace("\\", "/").removeprefix("./")
        if path not in cache:
            if base:
                r = subprocess.run(["git", "show", f"{head or 'HEAD'}:{path}"], cwd=root, capture_output=True)
                cache[path] = r.stdout.decode(errors="replace").splitlines() if r.returncode == 0 else None
            else:
                file = (root / path).resolve()
                ok = file.is_relative_to(root) and file.is_file()
                cache[path] = file.read_text(errors="replace").splitlines() if ok else None
        return cache[path]

    return read


def check_output(result: ResultMessage, read: LineReader) -> Checked:
    """Validate one reviewer answer: the schema, then every citation against the file.

    Budget, turn and structured-output failures raise: the reviewer didn't finish.
    """
    if result.subtype in FAILURES:
        raise ReviewerFailed(FAILURES[result.subtype])
    if result.is_error:
        raise ReviewerFailed(f"{result.subtype}: {result.errors or result.result}")
    try:
        output = ReviewerOutput.model_validate(cast(object, result.structured_output))
    except ValidationError as e:
        return Checked(None, [], [], [str(e)])
    checked = Checked(output, [], [], [])
    for f in output.findings:
        lines = read(f.file)
        status, located = check_citation(f, lines)
        if status == "unverified":
            checked.unverified.append(f)
            where = (
                "that file doesn't exist in the reviewed code" if lines is None
                else f"the file has {len(lines)} lines" if f.line_start > len(lines)
                else "the quote isn't on those lines"
            )
            checked.problems.append(f"{f.file}:{f.line_start}-{f.line_end} ({where}).")
        else:
            if status == "relocated":
                log.info("relocated %s:%d -> %d", f.file, f.line_start, located.line_start)
            checked.verified.append(located)
    return checked


async def run_reviewer(reviewer: Reviewer, repo_path: str, base: str | None, head: str | None) -> ReviewerRun:
    run = ReviewerRun(model=reviewer.model)
    read = file_reader(repo_path, base, head)
    try:
        async with ClaudeSDKClient(options=build_options(reviewer, repo_path)) as client:
            await client.query(task(base, head))
            checked = check_output(await collect(client, reviewer.name, run), read)
            if checked.problems:  # one round of feedback, then take what we have
                log.info("%s: sending back %d problems", reviewer.name, len(checked.problems))
                await client.query(feedback(checked))
                retry = check_output(await collect(client, reviewer.name, run), read)
                # An invalid retry must not throw away a valid first answer.
                if retry.output is not None or checked.output is None:
                    checked = retry
            return finish(run, reviewer.name, checked)
    except ReviewerFailed as e:
        run.status, run.error = "failed", str(e)
        return run
    # Only reachable if the client swallowed an exception: never report that as clean.
    run.status, run.error = "failed", "session closed without an answer"
    return run


def finish(run: ReviewerRun, category: str, checked: Checked) -> ReviewerRun:
    if checked.output is None:
        run.status, run.error = "failed", "invalid output after one retry"
        return run
    tag = {"category": category}
    run.findings = [f.model_copy(update=tag) for f in checked.verified]
    run.unverified = [f.model_copy(update=tag) for f in checked.unverified]
    run.files_reviewed = checked.output.files_reviewed
    run.files_skipped = checked.output.files_skipped
    return run


async def run_review(repo_path: str, base: str | None = None, head: str | None = None) -> Report:
    results = await asyncio.gather(
        *(run_reviewer(r, repo_path, base, head) for r in REVIEWERS),
        return_exceptions=True,  # one reviewer crashing must not erase the others
    )
    runs: dict[str, ReviewerRun] = {}
    for reviewer, result in zip(REVIEWERS, results):
        if isinstance(result, BaseException):
            result = ReviewerRun(model=reviewer.model, status="failed", error=repr(result))
        runs[reviewer.name] = result

    kept, dropped = merge([f for r in runs.values() for f in r.findings])
    ok = sum(r.status == "ok" for r in runs.values())
    return Report(
        status="complete" if ok == len(REVIEWERS) else "partial" if ok else "failed",
        range=f"{base}..{head or 'HEAD'}" if base else "whole repo",
        severity=overall_severity(kept),
        findings=kept,
        dropped_by_cap=dropped,
        unverified=[f for r in runs.values() for f in r.unverified],
        reviewers=runs,
    )


def record(report: Report, path: Path) -> None:
    """Append one line per run: outcome, counts and cost, never code or file names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "range": report.range,
        "status": report.status,
        "severity": report.severity,
        "findings": len(report.findings),
        "unverified": len(report.unverified),
        "reviewers": {
            name: r.model_dump(include={"status", "model", "tool_calls", "turns", "cost_usd", "error"})
            for name, r in report.reviewers.items()
        },
    }
    with path.open("a", encoding="utf-8") as f:
        _ = f.write(json.dumps(entry) + "\n")
