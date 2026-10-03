"""The get_changed_files tool. It returns what changed in a commit range, sized to fit in one tool result."""

from __future__ import annotations

import asyncio
import re
from fnmatch import fnmatchcase

from claude_agent_sdk import McpSdkServerConfig, ToolAnnotations, create_sdk_mcp_server, tool

GIT_TOOL = "mcp__git__get_changed_files"
# Stays well under the CLI's cap on MCP tool output (MAX_MCP_OUTPUT_TOKENS, pinned in
# review.py), so the CLI never cuts off the note that lists the files left out.
MAX_DIFF_CHARS = 60_000
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# Lockfiles and generated code are large, written by tools and rarely hold a defect.
# One changed uv.lock could use the whole budget, so these files are listed by name and
# their diffs are left out.
GENERATED = (
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum",
    "*.min.js", "*.min.css", "*_pb2.py", "*_pb2_grpc.py", "*.pb.go",
)

DESCRIPTION = """\
Get the diff for a commit range in the repository under review: a --stat summary, then \
the patch for base..head with the new file's line number on every line. Both refs are git \
refs (SHA, branch, tag, or expressions like HEAD~3). Large diffs include whole files until \
a size budget and list the files left out; pass path to get one file's diff. Cite the line \
numbers it prints. Use Read for full file context."""

ToolResult = dict[str, object]


def number_lines(patch: str) -> str:
    """Prefix every line that exists in the new file with its line number.

    The model can then read line numbers directly. Working them out from hunk headers
    caused wrong citations. Removed lines get no number.
    """
    out: list[str] = []
    n: int | None = None
    for line in patch.splitlines():
        hunk = HUNK_RE.match(line)
        if line.startswith("diff --git "):
            n = None
            out.append(line)
        elif hunk:
            n = int(hunk.group(1))
            out.append(line)
        elif n is None or line.startswith("\\"):
            out.append(line)  # file header, or "\ No newline at end of file"
        elif line.startswith("-"):
            out.append(f"{'':>6} {line}")
        else:
            out.append(f"{n:>6} {line}")
            n += 1
    return "\n".join(out)


def split_files(patch: str) -> list[tuple[str, str]]:
    """Split a patch into (path, diff) pairs, in the order git printed them."""
    files: list[tuple[str, str]] = []
    for chunk in re.split(r"(?m)^(?=diff --git )", patch):
        if chunk.startswith("diff --git "):
            path = chunk.split("\n", 1)[0].split(" b/", 1)[-1]
            files.append((path, chunk.rstrip("\n")))
    return files


def is_generated(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(name, pattern) for pattern in GENERATED)


def pack(files: list[tuple[str, str]], budget: int = MAX_DIFF_CHARS) -> tuple[str, list[str]]:
    """Add whole files until the budget is spent. Returns (text, paths left out).

    A file that doesn't fit is left out whole, and smaller files after it can still fit.
    """
    parts: list[str] = []
    left_out: list[str] = []
    used = 0
    for path, text in files:
        if used + len(text) <= budget:
            parts.append(text)
            used += len(text) + 1
        else:
            left_out.append(path)
    return "\n".join(parts), left_out


def parse_args(args: dict[str, object]) -> tuple[dict[str, str], str | None] | str:
    """Return ({"base": ref, "head": ref}, path or None), or a message telling the model what to fix.

    Indexing would raise KeyError and crash the tool call, so a missing key returns the
    message. Values starting with "-" are rejected because git would read them as options.
    """
    refs: dict[str, str] = {}
    for name in ("base", "head"):
        value = args.get(name)
        if not isinstance(value, str) or not value.strip():
            return f"Missing '{name}'. Pass both base and head as git refs, e.g. base='main', head='HEAD' or base='HEAD~1', head='HEAD'."
        value = value.strip()
        if value.startswith("-"):
            return f"'{name}' must be a git ref, not an option: {value!r}."
        refs[name] = value
    raw_path = args.get("path")
    if raw_path is not None and (not isinstance(raw_path, str) or not raw_path.strip() or raw_path.startswith("-")):
        return "'path' must be a file path from the --stat summary, or omitted."
    return refs, raw_path.strip() if isinstance(raw_path, str) else None


def _text(text: str) -> ToolResult:
    return {"content": [{"type": "text", "text": text}]}


async def _git(repo_path: str, *args: str) -> tuple[int, str, str]:
    # No shell, so refs never pass through a shell parser.
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=repo_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    code = await proc.wait()  # returncode is typed int | None. wait() on an exited process returns an int.
    return code, out.decode(errors="replace"), err.decode(errors="replace")


def build_git_server(repo_path: str) -> McpSdkServerConfig:
    """The tool closes over repo_path so the model can't point it at another repo."""

    @tool(
        "get_changed_files",
        DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "base": {"type": "string"},
                "head": {"type": "string"},
                "path": {"type": "string", "description": "Optional: one changed file's path."},
            },
            "required": ["base", "head"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def get_changed_files(args: dict[str, object]) -> ToolResult:
        parsed = parse_args(args)
        if isinstance(parsed, str):
            return _text(parsed)
        refs, path = parsed

        for name, ref in refs.items():
            code, _, _ = await _git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            if code != 0:
                _, branches, _ = await _git(repo_path, "branch", "--format=%(refname:short)")
                known = ", ".join(branches.split()[:20]) or "(none)"
                return _text(f"'{name}' ref {ref!r} does not resolve to a commit. Local branches: {known}. Use a branch name, a SHA, or a relative ref like HEAD~1.")

        commit_range = f"{refs['base']}..{refs['head']}"
        scope = ["--", path] if path else []
        code, stat, err = await _git(repo_path, "diff", "--stat", commit_range, *scope)
        if code != 0:
            return _text(f"git diff failed for {commit_range}: {err.strip()}")
        if not stat.strip():
            where = f" in {path}" if path else ""
            return _text(f"No changes between {refs['base']} and {refs['head']}{where}.")

        code, patch, err = await _git(repo_path, "diff", commit_range, *scope)
        if code != 0:
            return _text(f"git diff failed for {commit_range}: {err.strip()}")
        numbered = number_lines(patch)

        if path:
            if len(numbered) > MAX_DIFF_CHARS:
                numbered = f"{numbered[:MAX_DIFF_CHARS]}\n\n[cut at {MAX_DIFF_CHARS} chars; Read {path} for the rest.]"
            return _text(f"{stat}\n{numbered}")

        files = split_files(numbered)
        generated = [p for p, _ in files if is_generated(p)]
        body, left_out = pack([(p, text) for p, text in files if not is_generated(p)])
        if generated:
            names = ", ".join(generated)
            body += f"\n\n[Not included, lockfile or generated: {names}. Call get_changed_files with path set to one of them if the change could matter.]"
        if left_out:
            names = ", ".join(left_out)
            body += f"\n\n[Not included, over the size budget: {names}. Call get_changed_files again with path set to one of them, or Read the file.]"
        return _text(f"{stat}\n{body}")

    return create_sdk_mcp_server(name="git", version="1.0.0", tools=[get_changed_files])
