"""
Review Agent — reviews a git repository, or a commit range in it, for correctness,
security and performance problems.

An orchestrator delegates to three read-only reviewer sub-agents, one per concern,
each on its own model, and merges their findings into one structured report.

Run:
    python3 -m venv .venv && source .venv/bin/activate
    pip install claude-agent-sdk
    export ANTHROPIC_API_KEY=sk-ant-...
    python review_agent.py ./some/repo [base [head]]

Every run appends the orchestrator's delegation plan to runs/orchestration.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolAnnotations,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("agent")


# ---------------------------------------------------------------------- models
# Pinned, not aliases: eval numbers are only comparable if the model under them
# doesn't move between runs. Each reviewer's model is a hypothesis the eval tests.
#
# No fallback_model on purpose: a silent model swap mid-run makes results
# incomparable. Transient overloads are retried (CLAUDE_CODE_MAX_RETRIES); past
# that, a failed review you can rerun beats a finished one you can't trust.

ORCHESTRATOR_MODEL = "claude-sonnet-5-5"
SECURITY_MODEL = "claude-opus-5-5"             # cross-file reasoning: what is *missing*
CORRECTNESS_MODEL = "claude-sonnet-5-5"        # logic against intent, edge cases
PERFORMANCE_MODEL = "claude-sonnet-5-5"        # static analysis: tracing hot paths across calls

RUNS_LOG = Path(__file__).parent / "runs" / "orchestration.jsonl"


# ---------------------------------------------------------------- custom tools

GIT_TOOL = "mcp__git__get_changed_files"
MAX_DIFF_CHARS = 100_000  # keep one tool result from eating the context window


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


async def _git(repo_path: str, *args: str) -> tuple[int, str, str]:
    # Exec, not shell: refs never pass through a shell parser.
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=repo_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def build_git_server(repo_path: str):
    """The tool closes over repo_path so the model can't point it at another repo."""

    @tool(
        "get_changed_files",
        "Get the diff for a commit range in the repository under review: a --stat summary "
        "followed by the full patch for base..head. Both arguments are git refs (SHA, branch, "
        "tag, or expressions like HEAD~3). Use this to scope a review to what changed; use "
        "Read for full file context.",
        {"base": str, "head": str},
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def get_changed_files(args: dict[str, Any]) -> dict[str, Any]:
        # Validate instead of indexing: a KeyError here would crash the tool call
        # instead of telling the model how to fix it. This text becomes its next prompt.
        refs: dict[str, str] = {}
        for name in ("base", "head"):
            value = args.get(name)
            if not isinstance(value, str) or not value.strip():
                return _text(
                    f"Missing '{name}'. Pass both base and head as git refs, e.g. "
                    "base='main', head='HEAD' or base='HEAD~1', head='HEAD'."
                )
            value = value.strip()
            if value.startswith("-"):
                return _text(f"'{name}' must be a git ref, not an option: {value!r}.")
            refs[name] = value

        for name, ref in refs.items():
            code, _, _ = await _git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            if code != 0:
                _, branches, _ = await _git(repo_path, "branch", "--format=%(refname:short)")
                known = ", ".join(branches.split()[:20]) or "(none)"
                return _text(
                    f"'{name}' ref {ref!r} does not resolve to a commit. Local branches: "
                    f"{known}. Use a branch name, a SHA, or a relative ref like HEAD~1."
                )

        commit_range = f"{refs['base']}..{refs['head']}"
        code, stat, err = await _git(repo_path, "diff", "--stat", commit_range)
        if code != 0:
            return _text(f"git diff failed for {commit_range}: {err.strip()}")
        if not stat.strip():
            return _text(f"No changes between {refs['base']} and {refs['head']}.")

        code, patch, err = await _git(repo_path, "diff", commit_range)
        if code != 0:
            return _text(f"git diff failed for {commit_range}: {err.strip()}")
        body = f"{stat}\n{patch}"
        if len(body) > MAX_DIFF_CHARS:
            body = (
                body[:MAX_DIFF_CHARS]
                + f"\n\n[truncated at {MAX_DIFF_CHARS} chars. The --stat summary at the top "
                "lists every changed file; Read those files, or narrow the range.]"
            )
        return _text(body)

    return create_sdk_mcp_server(name="git", version="1.0.0", tools=[get_changed_files])


# ---------------------------------------------------------------------- hooks


async def audit_every_tool_call(
    input_data: HookInput, tool_use_id: str | None, context: HookContext
) -> HookJSONOutput:
    """Observability. Ships every tool call to your logs / tracing backend."""
    # agent_type is only present when the call comes from inside a sub-agent.
    log.info(
        "tool_call agent=%s tool=%s id=%s",
        input_data.get("agent_type", "orchestrator"),
        input_data.get("tool_name"),
        tool_use_id,
    )
    return {}


# ------------------------------------------------------------------- subagents

# Every reviewer gets the same read-only toolset. Sub-agents never get the Agent
# tool: a reviewer that can spawn reviewers has no bounded cost.
REVIEWER_TOOLS = ["Read", "Grep", "Glob", GIT_TOOL]

# The diff is the anchor, not the boundary. Most real defects are only visible
# from code the diff doesn't touch: the missing auth check, the caller in a loop.
CONTEXT_RULES = (
    " When given a commit range, call get_changed_files first and start from the "
    "changed hunks. Then Read or Grep whatever the change depends on (callers, "
    "decorators, validators, models, config) before deciding. Report only issues "
    "in or caused by the change. Cite file:line for each finding. If you find "
    "nothing, say so plainly — do not invent findings to seem useful."
)

AGENTS = {
    "correctness-reviewer": AgentDefinition(
        description=(
            "Reviews code for correctness bugs: logic errors, off-by-one, wrong conditions, "
            "unhandled edge cases (empty, None, boundaries), broken error handling."
        ),
        prompt=(
            "You are a correctness reviewer. Check whether the code does what its names, "
            "docstrings and callers expect. Leave security and performance to the other "
            "reviewers." + CONTEXT_RULES
        ),
        tools=REVIEWER_TOOLS,
        model=CORRECTNESS_MODEL,
        maxTurns=20,  # camelCase — AgentDefinition differs from ClaudeAgentOptions
    ),
    "security-reviewer": AgentDefinition(
        description=(
            "Reviews code for security issues: auth handling, injection, secrets in source, "
            "unsafe deserialization."
        ),
        prompt=(
            "You are a security reviewer. Report only concrete, exploitable issues. "
            "Absence matters as much as presence: look for the check, sanitiser or "
            "permission that should be there and isn't." + CONTEXT_RULES
        ),
        tools=REVIEWER_TOOLS,
        model=SECURITY_MODEL,
        maxTurns=20,
    ),
    "perf-reviewer": AgentDefinition(
        description=(
            "Reviews code for performance problems: N+1 queries, unbounded loops, "
            "missing indexes, synchronous I/O in hot paths."
        ),
        prompt=(
            "You are a performance reviewer. This is static analysis: you cannot run "
            "or benchmark code, so reason from the code itself. Find the hot paths "
            "(request handlers, loops over collections, queries) and quantify impact "
            "where possible (e.g. 'O(n) queries per request')." + CONTEXT_RULES
        ),
        tools=REVIEWER_TOOLS,
        model=PERFORMANCE_MODEL,
        maxTurns=20,
    ),
}


# -------------------------------------------------------------- output schema

REVIEW_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["none", "low", "medium", "high"]},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["correctness", "security", "performance"],
                        },
                        "location": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["category", "location", "description"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["severity", "findings", "summary"],
    },
}


# ------------------------------------------------------------------ the agent


def build_options(repo_path: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=(
            "You are a code review orchestrator. Delegate correctness analysis to the "
            "correctness-reviewer subagent, security analysis to the security-reviewer "
            "subagent and performance analysis to the perf-reviewer subagent, then "
            "synthesise their findings into one report. Do not review the code "
            "yourself — your job is orchestration and synthesis."
        ),
        cwd=repo_path,
        # tools decides which built-ins exist at all; allowed_tools only decides
        # which run without a prompt. Bash, Write, Edit and WebFetch are not
        # denied here, they are absent: nothing to guard, no network egress.
        # Agent (formerly Task) is required or subagents silently never spawn.
        tools=["Agent", "Read", "Grep", "Glob"],
        allowed_tools=["Agent", "Read", "Grep", "Glob", GIT_TOOL],
        agents=AGENTS,
        mcp_servers={"git": build_git_server(repo_path)},
        strict_mcp_config=True,   # ignore ambient .mcp.json / user settings
        setting_sources=[],       # reproducible: no filesystem settings
        permission_mode="dontAsk",  # unattended: anything not allowed above is denied
        hooks={
            "PreToolUse": [HookMatcher(hooks=[audit_every_tool_call])]
        },
        output_format=REVIEW_SCHEMA,
        model=ORCHESTRATOR_MODEL,
        max_turns=40,
        # Set on day one, not after the first bill. Raised from $2 for the Opus reviewer.
        max_budget_usd=3.00,
        env={
            "API_TIMEOUT_MS": "120000",
            "CLAUDE_CODE_MAX_RETRIES": "2",
        },
    )


def record_plan(
    base: str | None, head: str | None, plan: dict[str, list[str]], result: ResultMessage
) -> None:
    """Append what the orchestrator decided to do on this run.

    Each inner list is one assistant turn; reviewers in the same list were
    delegated in parallel. If this is identical on every run, the model is
    not adding anything by planning it.
    """
    RUNS_LOG.parent.mkdir(exist_ok=True)
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "range": f"{base}..{head or 'HEAD'}" if base else "whole repo",
        "plan": list(plan.values()),
        "subtype": result.subtype,
        "turns": result.num_turns,
        "cost_usd": result.total_cost_usd,
    }
    with RUNS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    log.info("plan=%s (appended to %s)", entry["plan"], RUNS_LOG.name)


async def review(repo_path: str, base: str | None = None, head: str | None = None) -> dict[str, Any] | None:
    options = build_options(repo_path)
    result: dict[str, Any] | None = None
    # Orchestrator delegations, grouped by assistant turn. The CLI streams each
    # content block as its own message, so parallel calls share a message_id.
    plan: dict[str, list[str]] = {}

    async with ClaudeSDKClient(options=options) as client:
        if base:
            scope = (
                f"Review the changes between {base} and {head or 'HEAD'} for correctness, "
                "security and performance issues. Reviewers should call get_changed_files "
                "for that range. "
            )
        else:
            scope = "Review this repository for correctness, security and performance issues. "
        await client.query(scope + "Delegate to all three reviewer subagents, then synthesise.")

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
                    elif isinstance(block, ToolUseBlock):
                        log.info("→ %s", block.name)
                        subagent = block.input.get("subagent_type")
                        if message.parent_tool_use_id is None and subagent:
                            turn = message.message_id or block.id
                            plan.setdefault(turn, []).append(subagent)

            elif isinstance(message, ResultMessage):
                record_plan(base, head, plan, message)
                # Full accounting. usage covers the main loop only — subagent
                # tokens live in model_usage.
                log.info(
                    "done subtype=%s reason=%s turns=%s cost=$%.4f",
                    message.subtype,
                    message.terminal_reason,
                    message.num_turns,
                    message.total_cost_usd or 0.0,
                )
                if message.model_usage:
                    for model, usage in message.model_usage.items():
                        log.info(
                            "  %s in=%s out=%s cost=$%.4f",
                            model,
                            usage.get("inputTokens"),
                            usage.get("outputTokens"),
                            usage.get("costUSD", 0.0),
                        )
                if message.is_error:
                    log.error("errors=%s api_status=%s", message.errors, message.api_error_status)
                result = message.structured_output

    return result


def main() -> None:
    repo_path = sys.argv[1] if len(sys.argv) > 1 else "."
    base = sys.argv[2] if len(sys.argv) > 2 else None
    head = sys.argv[3] if len(sys.argv) > 3 else None
    report = asyncio.run(review(repo_path, base, head))
    if report:
        print("\n\n=== structured report ===")
        print(f"severity: {report.get('severity')}")
        for finding in report.get("findings", []):
            print(f"  [{finding['category']}] {finding['location']}: {finding['description']}")


if __name__ == "__main__":
    main()
