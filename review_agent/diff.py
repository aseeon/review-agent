"""The get_changed_files tool: what changed in a commit range, sized to fit a tool result."""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

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
