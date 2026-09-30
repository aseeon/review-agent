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
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ToolUseBlock,
)
from pydantic import ValidationError

from .diff import GIT_TOOL, build_git_server
from .findings import Finding, ReviewerOutput, check_citation, merge, overall_severity
from .reviewers import REVIEWERS, Reviewer

log = logging.getLogger("review")

RUNS_LOG = Path(__file__).parent.parent / "runs" / "reviews.jsonl"
READ_TOOLS = ["Read", "Grep", "Glob"]

# A result subtype other than success means the reviewer didn't finish.
FAILURES = {
    "error_max_budget_usd": "budget exhausted",
    "error_max_turns": "turn limit reached",
    "error_max_structured_output_retries": "no valid structured output",
}


class ReviewerFailed(Exception):
    pass


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
        max_budget_usd=1.50,
        env={
            "API_TIMEOUT_MS": "120000",
            "CLAUDE_CODE_MAX_RETRIES": "2",
            # Pinned like the models: the CLI's default cap on tool output can change
            # under us. diff.py stays well below it.
            "MAX_MCP_OUTPUT_TOKENS": "25000",
        },
    )


def task(base: str | None, head: str | None) -> str:
    if base:
        return f"Review the changes between {base} and {head or 'HEAD'}."
    return "Review this repository."


async def collect(client: ClaudeSDKClient, name: str, stats: dict[str, Any]) -> ResultMessage:
    result = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    stats["tool_calls"] += 1
                    log.info("%s → %s", name, block.name)
        elif isinstance(message, ResultMessage):
            result = message
            # The CLI reports a running total for the session, so take the latest.
            stats["cost_usd"] = message.total_cost_usd or 0.0
            stats["turns"] = message.num_turns
    if result is None:
        raise ReviewerFailed("the session ended without a result")
    return result


def file_reader(repo_path: str, base: str | None, head: str | None) -> Callable[[str], list[str] | None]:
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


def check_output(result: ResultMessage, read: Callable[[str], list[str] | None]):
    """Validate one reviewer answer.

    Returns (output, verified, unverified, problems to send back); output is None when
    the answer failed validation.

    Budget, turn and structured-output failures raise: the reviewer didn't finish.
    """
    if result.subtype in FAILURES:
        raise ReviewerFailed(FAILURES[result.subtype])
    if result.is_error:
        raise ReviewerFailed(f"{result.subtype}: {result.errors or result.result}")
    try:
        output = ReviewerOutput.model_validate(result.structured_output)
    except ValidationError as e:
        return None, [], [], [f"Your output failed validation:\n{e}"]
    verified: list[Finding] = []
    unverified: list[Finding] = []
    problems: list[str] = []
    for f in output.findings:
        lines = read(f.file)
        status, checked = check_citation(f, lines)
        if status == "unverified":
            unverified.append(f)
            where = ("that file doesn't exist in the reviewed code" if lines is None
                     else f"the file has {len(lines)} lines" if f.line_start > len(lines)
                     else "the quote isn't on those lines")
            problems.append(f"{f.file}:{f.line_start}-{f.line_end} ({where}).")
        else:
            if status == "relocated":
                log.info("relocated %s:%d -> %d", f.file, f.line_start, checked.line_start)
            verified.append(checked)
    return output, verified, unverified, problems


FEEDBACK = (
    "Some of your output needs fixing. For each finding below, Read the file and correct "
    "line_start, line_end and quote, or drop the finding if it doesn't hold. Return the "
    "complete output again, all findings included.\n"
)


async def run_reviewer(reviewer: Reviewer, repo_path: str, base: str | None, head: str | None) -> dict[str, Any]:
    stats: dict[str, Any] = {"model": reviewer.model, "tool_calls": 0, "cost_usd": 0.0, "turns": 0}
    read = file_reader(repo_path, base, head)
    try:
        async with ClaudeSDKClient(options=build_options(reviewer, repo_path)) as client:
            await client.query(task(base, head))
            output, verified, unverified, problems = check_output(await collect(client, reviewer.name, stats), read)
            if problems:  # one round of feedback, then take what we have
                log.info("%s: sending back %d problems", reviewer.name, len(problems))
                await client.query(FEEDBACK + "\n".join(f"- {p}" for p in problems))
                output, verified, unverified, _ = check_output(await collect(client, reviewer.name, stats), read)
                if output is None:
                    raise ReviewerFailed("invalid output after one retry")
    except ReviewerFailed as e:
        return {**stats, "status": "failed", "error": str(e)}
    tag = {"category": reviewer.name}
    return {
        **stats,
        "status": "ok",
        "findings": [f.model_copy(update=tag) for f in verified],
        "unverified": [f.model_copy(update=tag) for f in unverified],
        "files_reviewed": output.files_reviewed,
        "files_skipped": [s.model_dump() for s in output.files_skipped],
    }


async def run_review(repo_path: str, base: str | None = None, head: str | None = None) -> dict[str, Any]:
    results = await asyncio.gather(
        *(run_reviewer(r, repo_path, base, head) for r in REVIEWERS),
        return_exceptions=True,  # one reviewer crashing must not erase the others
    )
    reviewers: dict[str, dict[str, Any]] = {}
    found: list[Finding] = []
    unverified: list[Finding] = []
    for reviewer, result in zip(REVIEWERS, results):
        if isinstance(result, BaseException):
            result = {"model": reviewer.model, "status": "failed", "error": repr(result)}
        found += result.pop("findings", [])
        unverified += result.pop("unverified", [])
        reviewers[reviewer.name] = result

    kept, dropped = merge(found)
    ok = sum(r["status"] == "ok" for r in reviewers.values())
    report = {
        # A failed reviewer is a visible gap, never an empty list of findings.
        "status": "complete" if ok == len(REVIEWERS) else "partial" if ok else "failed",
        "range": f"{base}..{head or 'HEAD'}" if base else "whole repo",
        "severity": overall_severity(kept),
        "findings": [f.model_dump() for f in kept],
        "dropped_by_cap": dropped,
        # Citations that still didn't match after one retry: shown, never counted.
        "unverified": [f.model_dump() for f in unverified],
        "reviewers": reviewers,
    }
    record(report)
    return report


def record(report: dict[str, Any]) -> None:
    """Append one line per run: outcome, counts and cost, never code or file names."""
    RUNS_LOG.parent.mkdir(exist_ok=True)
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "range": report["range"],
        "status": report["status"],
        "severity": report["severity"],
        "findings": len(report["findings"]),
        "unverified": len(report["unverified"]),
        "reviewers": {
            name: {k: r.get(k) for k in ("status", "model", "tool_calls", "turns", "cost_usd", "error")}
            for name, r in report["reviewers"].items()
        },
    }
    with RUNS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
