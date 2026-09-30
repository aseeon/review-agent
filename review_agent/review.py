"""Run the three reviewers in parallel and merge what they find into one report.

The plan is the same on every run (three independent reviews, then a merge), so it lives
in code. Judgement stays inside each reviewer, which reads whatever it needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
from .findings import ReviewerOutput, merge, overall_severity
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


def parse(result: ResultMessage) -> ReviewerOutput:
    if result.subtype in FAILURES:
        raise ReviewerFailed(FAILURES[result.subtype])
    if result.is_error:
        raise ReviewerFailed(f"{result.subtype}: {result.errors or result.result}")
    try:
        return ReviewerOutput.model_validate(result.structured_output)
    except ValidationError as e:
        raise ReviewerFailed(f"invalid output: {e.error_count()} validation errors") from e


async def run_reviewer(reviewer: Reviewer, repo_path: str, base: str | None, head: str | None) -> dict[str, Any]:
    stats: dict[str, Any] = {"model": reviewer.model, "tool_calls": 0, "cost_usd": 0.0, "turns": 0}
    try:
        async with ClaudeSDKClient(options=build_options(reviewer, repo_path)) as client:
            await client.query(task(base, head))
            output = parse(await collect(client, reviewer.name, stats))
    except ReviewerFailed as e:
        return {**stats, "status": "failed", "error": str(e)}
    findings = [f.model_copy(update={"category": reviewer.name}) for f in output.findings]
    return {
        **stats,
        "status": "ok",
        "findings": findings,
        "files_reviewed": output.files_reviewed,
        "files_skipped": [s.model_dump() for s in output.files_skipped],
    }


async def run_review(repo_path: str, base: str | None = None, head: str | None = None) -> dict[str, Any]:
    results = await asyncio.gather(
        *(run_reviewer(r, repo_path, base, head) for r in REVIEWERS),
        return_exceptions=True,  # one reviewer crashing must not erase the others
    )
    reviewers: dict[str, dict[str, Any]] = {}
    found = []
    for reviewer, result in zip(REVIEWERS, results):
        if isinstance(result, BaseException):
            result = {"model": reviewer.model, "status": "failed", "error": repr(result)}
        found += result.pop("findings", [])
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
        "reviewers": {
            name: {k: r.get(k) for k in ("status", "model", "tool_calls", "turns", "cost_usd", "error")}
            for name, r in report["reviewers"].items()
        },
    }
    with RUNS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
