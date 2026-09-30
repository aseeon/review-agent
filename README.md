# review-agent

A code review agent built on the Claude Agent SDK. It reviews a git repository, or a
commit range in it, with three specialised reviewers — correctness, security and
performance — and merges their findings into one structured report.

A reference implementation built to learn the SDK properly, then hardened. Not a
production system.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install claude-agent-sdk
export ANTHROPIC_API_KEY=sk-ant-...
python -m review_agent ./some/repo main HEAD
```

It exits non-zero unless all three reviewers finished. Tests: `pip install pytest && pytest`.

## Decisions so far

- **A fixed fan-out, not an orchestrating model.** The first version let a model delegate
  to three sub-agents and merge their output. Across its first eight real runs it chose the
  same plan every time (one turn, all three reviewers in parallel; see
  `runs/orchestration.jsonl`). A plan that never varies belongs in code, so the three
  reviewers now run with `asyncio.gather` and their findings are merged in Python. The
  model does the reviewing, not the deciding to review.
- **Merging is code.** Findings on overlapping lines in the same category are
  deduplicated, keeping the stronger one; the same lines flagged under different categories
  are both kept. Findings are ranked by severity then confidence and capped at 10. The
  overall severity is the worst finding, computed rather than asked for.
- **A failed reviewer is visible.** Each reviewer's status, tool calls and cost are part
  of the report. A reviewer that didn't finish makes the run `partial`; it never shows up
  as a reviewer with no findings.
- **Reviewers are told to prefer silence to noise.** Lower-severity findings need a
  concrete trigger scenario, style and hardening notes are out, and an empty list is a
  valid answer. The first real runs were mostly true-but-not-worth-fixing findings.
- **Three reviewers, one per concern.** One reviewer asked to check everything makes a
  shallow pass on each. Separate sessions also keep each reviewer's context clean.
  This costs more total tokens than one prompt; it is a quality trade, not a saving.
- **A model per reviewer.** Security runs on Opus, because it has to reason about what is
  *missing*, often across files. Correctness and performance run on Sonnet. A smaller model
  for performance was considered and rejected: without being able to run code, the reviewer
  has to follow hot paths through callers, which is reasoning, not pattern-matching. These
  are hypotheses for the eval harness to confirm or reject.
- **Model IDs are pinned, not aliases,** so eval runs stay comparable over time.
- **No fallback model.** A fallback swaps the model silently mid-run, which breaks that
  comparability. Transient overloads are retried; beyond that the run fails and can be
  rerun. For a batch review tool, a failed run you can rerun is better than a finished one
  you can't trust. A live, user-facing agent would justify the opposite choice.
- **Read-only reviewers.** Every reviewer gets `Read`, `Grep`, `Glob` and one git tool.
  None can write, run commands or reach the network. Repository content is treated as
  data: the reviewed repo's own `CLAUDE.md` is never loaded as instructions
  (`setting_sources=[]`), and an attempt to steer the review is reported as a finding.
- **The diff is the anchor, not the boundary.** Reviewers start from the changed hunks
  and read whatever the change depends on, because most real defects (a missing auth
  check, a query inside a caller's loop) are invisible in the diff alone.
- **Performance review is static analysis.** The reviewer cannot execute code. Running
  benchmarks would need a sandboxed execution path, and read-only was chosen on purpose.

## Removed

- **A demo `helpdesk` MCP server** that fetched fake support tickets. It existed to show
  off the SDK, not to review code. Replaced by `get_changed_files(base, head)`.
- **Two guard rails that guarded nothing.** A Bash command denylist (Bash was never
  enabled) and a permission callback that sandboxed `Write`/`Edit` calls (no agent has
  those tools, and in `dontAsk` mode the callback is never consulted). Instead, the
  built-in toolset is restricted with `tools=[...]`: tools that don't exist need no rail.

## Known limitations

- Line numbers in findings are taken on trust; nothing checks them against the file yet.
- A diff larger than 100k characters is truncated.
- There is no eval harness yet.
