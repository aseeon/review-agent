# review-agent

A code review agent built on the Claude Agent SDK. Three reviewers look at a git
repository in parallel, one each for correctness, security and performance. The agent
checks every finding against the code and prints one report.

## Install

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and `git`.

```bash
uv tool install git+https://github.com/aseeon/review-agent
```

For Claude access, either set `ANTHROPIC_API_KEY` or log in once with
[Claude Code](https://docs.claude.com/en/docs/claude-code). Your account needs access to
`claude-opus-5-5` and `claude-sonnet-5-5`.

## Use

Review the last 5 commits:

```bash
review-agent ./repo HEAD~5
```

Review only the last commit:

```bash
review-agent ./repo HEAD~1
```

Review the whole repository:

```bash
review-agent ./repo
```

The report goes to stdout and progress goes to stderr, so `> review.txt` saves just the
report. It looks like this:

```text
review of HEAD~5..HEAD: complete, severity high, 2 findings
  correctness  ok      14 tool calls, $0.41
  security     ok      22 tool calls, $1.12
  performance  ok      9 tool calls, $0.28

[high] security  app/api.py:41-44  (confirmed)
    ...
```

## Cost and exit codes

- A run costs at most $8. Each reviewer stops at its own budget: $4 for security and $2
  each for correctness and performance. To change one, set
  `REVIEW_AGENT_SECURITY_BUDGET_USD`, `REVIEW_AGENT_CORRECTNESS_BUDGET_USD` or
  `REVIEW_AGENT_PERFORMANCE_BUDGET_USD`.
- The exit code is 0 when all three reviewers finished and 1 when any of them failed, so
  you can use it in CI. A bad budget value exits with 2 before anything is spent.
- Add `--log FILE` to append a one-line record of each run, with the outcome and the cost
  per reviewer. The record never contains code or file names.

## How it works

- The three reviewers run at the same time, and plain Python merges what they find. It
  removes duplicates, ranks findings by severity and keeps the top 10.
- Reviewers can only read. They can't write files, run commands or reach the network, and
  the reviewed repository's `CLAUDE.md` is never loaded.
- Every finding quotes the code it points at. If the quote isn't on the cited lines, the
  reviewer gets one retry. Findings that still don't match are marked unverified.
- If a reviewer fails, the report says which one and why.

The prompts, models and budgets are in `src/review_agent/reviewers.py`.
[docs/design.md](docs/design.md) explains each decision and the measurements behind it.

## Develop

```bash
git clone https://github.com/aseeon/review-agent && cd review-agent
uv sync
uv run pytest
uv run basedpyright
uv run python evals/run.py --runs 3
```

The last command runs the eval against the real model, at about $0.15 a run. It builds a
small repository with five planted bugs and one clean change, reviews both, and counts
how many bugs were found. Results go to `runs/evals/planted.jsonl`.

## Limitations

- A reviewer that runs out of budget returns no findings.
- Large diffs are sent one whole file at a time, in git's order. Files that don't fit are
  listed, and reviewers can fetch them separately.
- The performance reviewer only reads code. Nothing is run or benchmarked.

## License

Apache-2.0
