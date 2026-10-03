# Design

Why the agent is built the way it is: each decision, what it replaced or ruled out, and
the measurement behind it. The README covers installing and running it.

## Shape: a fixed fan-out, merged in code

Three reviewers (correctness, security, performance) run as independent agent sessions
under `asyncio.gather`. Plain Python validates and merges their findings.

The first version had an orchestrating model that delegated to the three reviewers as
sub-agents. In all eight recorded runs, across four repositories in Python, Rust and
GDScript, it chose the same plan: one turn, all three reviewers in parallel
(`runs/orchestration.jsonl`). A plan that never varies belongs in code. The fan-out
removed a model call, a layer of prompt and a source of variance.

The orchestrator also produced the most instructive failure. The CLI starts sub-agents as
background tasks by default, so the orchestrator returned before any reviewer finished.
It reported severity "none" with no findings, which looked exactly like a clean review.
All four first runs were silently empty. Two rules came out of that:

- **A failure must never look like a clean result.** Each reviewer reports its own status,
  tool calls and cost. One failed reviewer marks the run `partial`, and the CLI exits
  non-zero unless all three finished.
- **Treat "no findings" as a claim to check, not a success.** The eval's clean change,
  where the right answer is no findings, exists for the same reason.

Ruled out: one reviewer covering everything (it can't run in parallel, and one prompt
pulled three ways), and a model merging the findings (deterministic rules are cheaper,
testable and don't drift).

## Models and budgets

| Reviewer | Model | Budget |
|---|---|---|
| security | `claude-opus-5-5` | $4 |
| correctness | `claude-sonnet-5-5` | $2 |
| performance | `claude-sonnet-5-5` | $2 |

Security gets Opus because its findings are the most expensive to miss, and the missing
check is often in a different file from the change. Performance started on Haiku and moved to
Sonnet after weak findings in early runs.

Models are pinned and there is **no fallback model**. A silent switch to another model
would make two runs of the same eval incomparable, so a model that is unavailable fails
the reviewer visibly.

Budgets are per reviewer and can be overridden from the environment, so a runaway run
costs at most $8. Measured range reviews cost $0.16 to $0.67, about $0.35 typically. The
Opus security reviewer is 50 to 75% of that (`runs/reviews.jsonl`).

## What a reviewer can see and do

- **Read-only tools.** `Read`, `Grep`, `Glob` and one custom tool, `get_changed_files`.
  `tools` limits which built-in tools exist at all, and `permission_mode="dontAsk"`
  denies anything else. There are no writes, no shell and no network, so the worst a
  prompt injection in the reviewed code can do is distort that review.
- **No instructions from the reviewed repo.** `setting_sources=[]` means the repo's
  `CLAUDE.md` and settings are never loaded. The shared rules say repository content is
  data under review, and an attempt to steer the review is reported as a finding.
- **The commit under review, not the working tree.** A range review runs in a temporary
  detached `git worktree` at the head commit, removed afterwards. Before this, the diff
  showed `base..head` but `Read` and `Grep` read the working tree. In one re-run of an
  older range, that tree was nine commits and about 4,000 lines ahead, so reviewers took
  their context from code that wasn't under review.
- **A diff tool built for citing.**
  - Every diff line carries the new file's line number. Wrong citations clustered on a
    diff cut off at 100k characters, where the model had to work out line numbers from
    hunk headers.
  - Whole files are packed under a 60k-character budget and never cut in the middle.
    Files that don't fit are listed by name, and a `path` argument fetches one.
  - The budget stays under the CLI's own cap on tool output (`MAX_MCP_OUTPUT_TOKENS`,
    pinned), so the list of left-out files is always what the reviewer sees.
  - Lockfiles and generated files are named instead of packed, so a changed `uv.lock`
    can't use up the budget.
  - Refs are validated before use (`rev-parse --verify`, no leading `-`), `git` runs by
    exec, not through a shell, and the repository path is fixed in a closure, so the model
    can't point the tool at another repository.

## Checking the output

1. **Schema.** Findings come back as structured output validated by Pydantic: file,
   lines, quote, severity, confidence, description. Field definitions live in the schema,
   so each is defined once, in the place the model reads it. The category is set by code
   from the reviewer, never by the model.
2. **Citation check.** Each finding quotes the code it cites, and the quote must appear on
   the cited lines of the reviewed version of the file. A quote found exactly once
   elsewhere corrects the line numbers. Anything else goes back to the reviewer.
3. **One retry, with feedback that fits the failure.** A schema error sends the
   validation errors; bad citations send the list to fix. Whatever still fails is
   reported as unverified and doesn't count toward the overall severity.

## Merging

- Overlapping findings in the same category are deduplicated, keeping the stronger one.
- A correctness finding on the same lines as a security or performance finding is
  **folded** into it. The specialist's framing stands, the severity is the worse of the
  two, and the report says correctness also flagged it. A second reviewer agreeing
  becomes corroboration instead of a duplicate.
- Security and performance findings on the same lines are both kept, since they describe
  different problems.
- Findings are ranked by severity, then confidence, and capped at 10. The overall
  severity is the worst finding.

## Prompts

Each reviewer's system prompt is its focus paragraph plus shared rules, filled in with
the reviewer's area. Three lessons shaped them, each found by measurement:

- **State each rule once.** The shared rules told every reviewer to report "every bug and
  security defect" while each focus prompt said security belonged elsewhere. The two
  contradicted each other, and the performance reviewer reported the eval's SQL injection
  instead of its own N+1 query. The boundary now lives only in the shared rules, as a
  template naming each reviewer's area, with the reason: a defect reported twice reaches
  the reader twice and takes a slot from the reviewer's own area. Recall went from 4/5 to
  5/5 planted defects.
- **Define an area by the world it assumes, not by symptoms.** In the broad sense every
  bug is a correctness bug, so the correctness reviewer kept re-describing security
  defects by their symptom ("a name with a quote breaks the search"). It is now defined by
  a well-meaning caller with legitimate input, plus an ownership test: if fixing a
  security or performance problem would also fix the finding, it belongs to that
  reviewer. In a correctness-only experiment, four runs per variant, findings outside its
  area fell from 12 to 1. In the full eval, it re-reports no security defect in three
  runs, down from one to three per run. The merge fold stays as a safety net.
- **Rules state the target, and say why.** Every finding is a defect with a named trigger.
  Callers are read before a changed function is called safe. Lower-severity findings need
  certainty, while a high-impact finding that can't be fully proven is reported as
  "suspected" with what is uncertain.

## Evaluation

**Planted defects** (`evals/`). The eval builds a small repository with five planted
defects and a separate clean change, reviews both with the real agent, and scores the
findings deterministically: same file, same category, within three lines. The defects are
SQL injection, an N+1 query, an off-by-one, a hardcoded secret, and a missing
authorization check visible only across files. Results are kept per prompt version in
`runs/evals/`:

| Prompt version | Defects found per run | Extra findings per run | Clean change |
|---|---|---|---|
| contradictory scope rules | 4, 4, 4 | 6 | 0 |
| areas named once in shared rules | 5, 5, 5 | 2 to 4 | 0 |
| + fold in the merge | 5, 5, 5 | 0 | 0 |
| + correctness by assumed caller (current) | 5, 5, 5 | 0 or 1 | 0 |

Before the fold, nearly every extra was one reviewer reporting another's defect. The one extra left is a fair performance point (a leading-wildcard `LIKE` can't use an index). A run costs about $0.14. Every result records a short fingerprint of the prompts it ran
with. That came from an error: one "confirmation" run silently measured the old prompt,
because an edit had failed. Without the fingerprint, the result looked like the new
prompt had failed.

**Labelled real findings.** The planted eval checks recall on known bugs. Precision needs
real code, so I reviewed four of my own repositories and labelled every finding as real,
noise or false. The labels stay outside this repository because the findings quote that
code.

- First full run: 28 findings, 10 real, 16 noise, 2 false, so 36% precision. The problem
  was noise, not invented bugs.
- After the citation, scope and worktree fixes: 7 real of 11 distinct, so 64%. No false
  findings and no unverified citations.
- Current prompt, on the two repositories with known bugs, three runs: 13 real of the 16
  labelled findings (81%), about 4.3 known-real bugs found per run.

**A change I reverted.** One sentence telling reviewers what isn't worth reporting
(unreachable triggers, harm a later check already stops, naming and hardening notes,
negligible costs) was built from the noise labels. It passed the planted eval at 5/5. An
A/B on the labelled repositories, three runs per prompt, showed the trade:

| | Without the sentence | With it |
|---|---|---|
| Known-real bugs found per run | 4.3 | 2.3 |
| Precision on labelled findings | 81% | 88% |

It cut real low-severity bugs and still let some of its target noise through, so it was
reverted (`320aef5`). The planted eval alone would have approved it: recall on five
planted bugs can't see the loss of narrow real ones.

**Caveats.** Three runs per variant, small fixtures, one person labelling. Run-to-run
variance is large: on one repository, runs found anywhere from 0 to 3 real bugs. The
numbers show direction, not significance.

## Known limits

- A reviewer that runs out of budget contributes no findings, not partial ones.
- Large diffs are packed in git's order, not by relevance, and the budget is in
  characters, not tokens.
- Performance review is static: nothing is run or benchmarked.
- The retry-with-feedback path is covered by unit tests on its parts and by live runs,
  but not by an end-to-end test without the API.

## What I would add next

In order of expected value.

1. **A stubbed end-to-end test.** A fake SDK client that returns scripted results, so the
   whole run can be tested in CI without an API key: the fan-out, one reviewer failing,
   and a bad citation followed by a corrected one. Today the parts have unit tests, and
   the orchestration has only been exercised live. pr-agent runs its whole pipeline in CI
   against a deterministic fake model in the same way.
2. **A public recall set.** The labelled findings can't be published, so rebuild the idea
   from public repositories: take commits that fixed a bug, review the commit that
   introduced it, and score whether the fix's lines were flagged. That gives real, narrow
   bugs at scale, the class the planted eval missed when it approved the reverted change.
3. **A model override per reviewer** from the environment, as budgets already have. It is
   also the seam for running on Bedrock or Vertex, where model IDs differ.
4. **Triage before the fan-out.** Skip or shorten reviews of docs-only changes and
   dependency bumps, and after the first review of a pull request, review only the new
   commits.
5. **A second pass that scores each finding.** pr-agent has one model write the
   suggestions and a second call score each one, with caps such as "a suggestion that only
   asks to verify something scores at most 7". Worth trying if noise becomes the main
   complaint again, measured on the recall set, since the reverted sentence shows how
   easily a noise filter cuts real findings.
6. **Report cost per real finding** instead of cost per review, plus the prompt cache hit
   rate, which the SDK reports and nothing here records yet.
7. **Running as a pull request bot.** Inline comments, stable fingerprints so a re-review
   doesn't repeat a finding, and previous findings fed back so the reviewer can mark them
   resolved.
8. **A repository map keyed to the commit**, only if evals show misses from not finding
   related code. So far grep and read have been enough: the cross-file defect was found
   in every eval run.
