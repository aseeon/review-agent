# review-agent

A code review agent built on the Claude Agent SDK. Three read-only reviewers
(correctness, security, performance) run in parallel over a git repository or a commit
range. Their findings are checked against the code and merged into one report.

## Requirements

- Python 3.11+, [uv](https://docs.astral.sh/uv/) and `git` on `PATH`.
- Claude access: either set `ANTHROPIC_API_KEY`, or log in once with
  [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`). The Agent SDK
  ships its own copy of the CLI, so there is nothing else to install.
- Access to `claude-opus-5-5` and `claude-sonnet-5-5`.

## Install

```bash
uv tool install git+https://github.com/aseeon/review-agent
```

To try it without installing, use `uvx --from git+https://github.com/aseeon/review-agent review-agent`.
Upgrade with `uv tool upgrade review-agent`.

## Use

```bash
review-agent ./repo main HEAD   # review the commits in main..HEAD
review-agent ./repo main        # same; head defaults to HEAD
review-agent ./repo             # review the whole repository
review-agent . main --log runs.jsonl   # also append a one-line run record
```

The report goes to stdout and progress to stderr, so `review-agent . main > review.txt`
saves only the report. Output looks like this (numbers illustrative):

```text
review of main..HEAD: complete, severity high, 2 findings
  correctness  ok      14 tool calls, $0.41
  security     ok      22 tool calls, $1.12
  performance  ok      9 tool calls, $0.28

[high] security  app/api.py:41-44  (high)
    ...
```

- **Cost.** Each reviewer stops at its budget: security $4, correctness $2, performance $2,
  so a run costs at most $8. Override per reviewer with `REVIEW_AGENT_SECURITY_BUDGET_USD`,
  `REVIEW_AGENT_CORRECTNESS_BUDGET_USD` and `REVIEW_AGENT_PERFORMANCE_BUDGET_USD`. The
  report shows what each reviewer spent.
- **Exit code.** 0 when all three reviewers finished, 1 otherwise, so it can gate a CI
  step. An invalid budget override exits 2 before anything is spent.
- **Run log.** The `--log` record holds the outcome, counts and cost per reviewer. It
  never contains code or file names.

## How it works

- **Fixed fan-out.** The three reviewers run concurrently, and plain code merges their
  output rather than another model. Overlapping findings in the same category are
  deduplicated. The rest are ranked by severity, then confidence, and capped at 10.
- **Read-only.** Reviewers can use only `Read`, `Grep`, `Glob` and a `get_changed_files`
  diff tool: no writes, no shell, no network. The reviewed repo's `CLAUDE.md` and
  settings are not loaded, so repository content can't give the agent instructions.
- **Verified citations.** Each finding quotes the code it cites. The quote is checked
  against the file. Line numbers are corrected if the quote turns up elsewhere, and the
  reviewer gets one retry for the rest. Anything still unmatched is listed as unverified
  and doesn't count toward the overall severity.
- **Visible failures.** A reviewer that fails (budget, turns, invalid output) marks the
  run `partial` and shows the reason.

Reviewer prompts, models and default budgets are in `src/review_agent/reviewers.py`. The
turn limit is in `build_options` in `src/review_agent/review.py`.

## Develop

```bash
git clone https://github.com/aseeon/review-agent && cd review-agent
uv sync              # .venv with the package and dev tools
uv run pytest        # unit tests, no network
uv run basedpyright  # type check
uv run python evals/run.py --runs 3   # planted-defect eval, calls the model (~$0.15/run)
```

The eval builds a small repository with five planted defects (one only visible across
files) and a separate clean change, reviews both, and scores the findings against
`evals/expected.json`: a defect counts as found when a finding has the same file and
category and lands within three lines of it. Results append to `runs/evals/planted.jsonl`.

## Limitations

- A reviewer that runs out of budget contributes no findings; it doesn't return partial ones.
- Large diffs are packed a whole file at a time, in git's order. Files that don't fit are
  listed by name, and reviewers can fetch them one at a time.
- Performance review is static: nothing is executed or benchmarked.
- There is no eval harness yet.

## License

Apache-2.0
