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
- **Every citation is checked in code.** Each finding quotes the line it cites. Code
  looks for that text at those lines in the reviewed version of the file. If it's there,
  fine; if it appears exactly once elsewhere, the line numbers are corrected; otherwise the
  reviewer is told what didn't match and gets one retry. What still doesn't match is shown
  as unverified and doesn't count towards the severity. A string match can't hallucinate,
  so this is done deterministically rather than by asking a model to re-check.
- **Line numbers are printed, not computed.** `get_changed_files` puts the new file's line
  number on every diff line, so reviewers read numbers instead of working them out from
  hunk headers, which is where wrong citations came from.
- **Large diffs are packed by whole file.** Files are included until a size budget, never
  cut in the middle, and the ones left out are listed by name; a `path` argument fetches
  one file's diff. The budget sits well under the CLI's own cap on tool output, which is
  set explicitly (`MAX_MCP_OUTPUT_TOKENS`) rather than left to a default.
- **Invalid output gets one retry, then fails loudly.** Schema errors and unmatched
  citations are sent back once. A reviewer that still can't produce valid output, or runs
  out of budget or turns, is marked failed with the reason.
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

- A reviewer's findings arrive only when it finishes, so one that runs out of budget
  contributes nothing; the run is marked partial rather than showing partial findings.
- A citation is checked against its first non-empty quoted line; a quote that is common
  code (e.g. `return None`) can only be verified at its exact line, never relocated.
- Packing is in git's file order, so on a very large change the files left out are
  simply the later ones, not the least relevant ones.
- There is no eval harness yet.
