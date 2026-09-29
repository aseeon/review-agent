"""
Review Agent — a single runnable file demonstrating the Claude Agent SDK patterns
that matter for production work.

Run:
    python3 -m venv .venv && source .venv/bin/activate
    pip install claude-agent-sdk
    export ANTHROPIC_API_KEY=sk-ant-...
    python review_agent.py ./some/repo [base [head]]
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolAnnotations,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("agent")


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

        _, patch, _ = await _git(repo_path, "diff", commit_range)
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


async def audit_every_tool_call(input_data, tool_use_id, context):
    """Observability. Ships every tool call to your logs / tracing backend."""
    log.info("tool_call tool=%s id=%s", input_data.get("tool_name"), tool_use_id)
    return {}


# ---------------------------------------------------------------- permissions


async def permission_handler(
    tool_name: str, input_data: dict, context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    """Only fires when the permission flow falls through to a prompt.

    Calls already approved by allowed_tools / permission_mode never reach here —
    a PreToolUse hook is the place to inspect those.
    """
    path = input_data.get("file_path", "")

    if tool_name == "Write" and (path.startswith("/etc/") or path.startswith("/usr/")):
        return PermissionResultDeny(message="System directory write denied", interrupt=True)

    # Redirect rather than refuse: rewrite the call into a sandbox path.
    if tool_name in ("Write", "Edit") and "secrets" in path:
        return PermissionResultAllow(updated_input={**input_data, "file_path": f"./sandbox/{path}"})

    return PermissionResultAllow(updated_input=input_data)


# ------------------------------------------------------------------- subagents

AGENTS = {
    "security-reviewer": AgentDefinition(
        description=(
            "Reviews code for security issues: auth handling, injection, secrets in source, "
            "unsafe deserialization. Use when the task involves reviewing a diff or a module."
        ),
        prompt=(
            "You are a security reviewer. Report only concrete, exploitable issues. "
            "Cite file:line for each finding. If you find nothing, say so plainly — "
            "do not invent findings to seem useful."
        ),
        tools=["Read", "Grep", "Glob", GIT_TOOL],  # note: never include Task
        model="sonnet",
        maxTurns=20,  # camelCase — AgentDefinition differs from ClaudeAgentOptions
    ),
    "perf-reviewer": AgentDefinition(
        description=(
            "Reviews code for performance problems: N+1 queries, unbounded loops, "
            "missing indexes, synchronous I/O in hot paths."
        ),
        prompt=(
            "You are a performance reviewer. Quantify impact where possible "
            "(e.g. 'O(n) queries per request'). Cite file:line."
        ),
        tools=["Read", "Grep", "Glob", GIT_TOOL],
        model="sonnet",
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
                        "category": {"type": "string", "enum": ["security", "performance"]},
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
            "You are a code review orchestrator. Delegate security analysis to the "
            "security-reviewer subagent and performance analysis to the perf-reviewer "
            "subagent, then synthesise their findings into one report. Do not review "
            "the code yourself — your job is orchestration and synthesis."
        ),
        cwd=repo_path,
        # Task is REQUIRED or subagents silently never spawn.
        allowed_tools=["Task", "Read", "Grep", "Glob", GIT_TOOL],
        # allowed_tools does not restrict — deny rules do. These survive bypassPermissions.
        disallowed_tools=["Bash(rm *)", "Bash(git push *)", "WebFetch"],
        agents=AGENTS,
        mcp_servers={"git": build_git_server(repo_path)},
        strict_mcp_config=True,   # ignore ambient .mcp.json / user settings
        setting_sources=[],       # reproducible: no filesystem settings
        permission_mode="dontAsk",  # unattended: fail closed
        can_use_tool=permission_handler,
        hooks={
            "PreToolUse": [HookMatcher(hooks=[audit_every_tool_call])]
        },
        output_format=REVIEW_SCHEMA,
        model="claude-sonnet-5",
        fallback_model="claude-haiku-4-5-20251001",
        max_turns=40,
        max_budget_usd=2.00,   # set this on day one, not after the first bill
        env={
            "API_TIMEOUT_MS": "120000",
            "CLAUDE_CODE_MAX_RETRIES": "2",
        },
    )


async def review(repo_path: str, base: str | None = None, head: str | None = None) -> dict[str, Any] | None:
    options = build_options(repo_path)
    result: dict[str, Any] | None = None

    async with ClaudeSDKClient(options=options) as client:
        if base:
            scope = (
                f"Review the changes between {base} and {head or 'HEAD'} for security and "
                "performance issues. Reviewers should call get_changed_files for that range. "
            )
        else:
            scope = "Review this repository for security and performance issues. "
        await client.query(scope + "Delegate to both reviewer subagents, then synthesise.")

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
                    elif isinstance(block, ToolUseBlock):
                        log.info("→ %s", block.name)

            elif isinstance(message, ResultMessage):
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
