# Design

This document explains why the agent is built the way it is. For each decision it gives
what the decision replaced or ruled out, and the measurement behind it. The README covers
installing and running the tool.

## A fixed fan-out, merged in code

Three reviewers (correctness, security and performance) run as separate agent sessions
under `asyncio.gather`. Plain Python then validates and merges their findings.

The first version had an orchestrating model that handed the work to the three reviewers
as sub-agents. I recorded eight runs across four repositories in Python, Rust and
GDScript, and the orchestrator chose the same plan every time. It ran all three reviewers
in parallel in one turn (`runs/orchestration.jsonl`). If a model makes the same decision
every time, code can make it instead. Moving the plan into code removed a model call, a
layer of prompt and one source of variation between runs.

The orchestrator also caused the most useful failure. The CLI starts sub-agents as
background tasks by default, so the orchestrator returned before any reviewer finished.
It reported severity "none" with no findings, which looked exactly like a clean review.
All four of the first runs were empty in this way. Two rules came out of that.

- A failure should never look like a clean result. Each reviewer reports its own status,
  tool calls and cost. If one reviewer fails, the run is marked `partial`, and the CLI
  exits with an error unless all three finished.
- An empty report needs testing too. That is why the eval includes a clean change, where
  the right answer is no findings.

I also considered two other designs and decided against them. One reviewer covering all
three areas can't run in parallel, and its single prompt would pull in three directions.
A model merging the findings would cost more, be harder to test and could change its
behaviour over time, while fixed rules in code do the same thing every run.

## Models and budgets

| Reviewer | Model | Budget |
|---|---|---|
| security | `claude-opus-5-5` | $4 |
| correctness | `claude-sonnet-5-5` | $2 |
| performance | `claude-sonnet-5-5` | $2 |

Security gets Opus because its findings are the most expensive to miss, and the missing
check is often in a different file from the change. Performance started on Haiku and
moved to Sonnet because Haiku's findings in the early runs were weak.

Each model is pinned, and there is no fallback model. If the agent quietly switched to
another model, two runs of the same eval could no longer be compared. When a model is
unavailable, that reviewer fails and the report says so.

Each reviewer has its own budget, which can be overridden from the environment. A run
that goes wrong costs at most $8. Measured range reviews cost between $0.16 and $0.67,
and about $0.35 is typical. The Opus security reviewer accounts for 50 to 75% of that
(`runs/reviews.jsonl`).

## What a reviewer can see and do

- Reviewers can only read. They get `Read`, `Grep`, `Glob` and one custom tool,
  `get_changed_files`. The `tools` option limits which built-in tools exist at all, and
  `permission_mode="dontAsk"` denies everything else. They can't write files, run
  commands or reach the network, so a prompt injection in the reviewed code can at worst
  spoil that one review.
- The reviewed repository can't give the agent instructions. With `setting_sources=[]`,
  its `CLAUDE.md` and settings are never loaded. The shared rules tell reviewers that
  repository content is data under review, and that any attempt to steer the review should
  be reported as a finding.
- Reviewers read the commit under review. A range review runs in a temporary detached
  `git worktree` at the head commit, which is removed afterwards. Before this change, the
  diff showed `base..head` while `Read` and `Grep` read the working tree. In one re-run of
  an older range, the working tree was nine commits and about 4,000 lines ahead, so the
  reviewers were reading code that wasn't under review.
- The diff tool is built so findings can cite exact lines.
  - Every diff line carries the line number from the new file. Before this, wrong
    citations clustered on one diff that had been cut off at 100k characters, where the
    model had to work out line numbers from the hunk headers.
  - Whole files are packed into a budget of 60k characters and never cut in the middle.
    Files that don't fit are listed by name, and the tool's `path` argument fetches one at
    a time.
  - The budget stays below the CLI's own limit on tool output (`MAX_MCP_OUTPUT_TOKENS`,
    which is pinned), so the reviewer always sees the list of files that were left out.
  - Lockfiles and generated files are listed by name and left out of the packed diff, so a
    changed `uv.lock` can't use up the budget.
  - The tool checks git refs before using them (`rev-parse --verify`, and no leading `-`).
    It runs `git` directly without a shell, and the repository path is fixed when the tool
    is built, so the model can't point it at another repository.

## Checking the output

1. The schema comes first. Findings come back as structured output, which Pydantic
   validates. Each finding has a file, lines, a quote, a severity, a confidence and a
   description. The field definitions live in the schema itself, so each one is written
   once, where the model reads it. Code sets the category from the reviewer that produced
   the finding.
2. The citation check comes next. Each finding quotes the code it cites, and the quote has
   to appear on the cited lines of the reviewed version of the file. If the quote appears
   exactly once somewhere else in the file, the line numbers are corrected. Anything else
   goes back to the reviewer.
3. The reviewer gets one retry, with a message that matches the failure. A schema failure
   sends the validation errors, and bad citations send the list to fix. Anything that
   still fails is reported as unverified and doesn't count toward the overall severity.

## Merging

- When two findings in the same category overlap, the stronger one is kept.
- When a correctness finding covers the same lines as a security or performance finding,
  it is folded into that finding. The specialist's description stays, the severity is the
  worse of the two, and the report notes that correctness flagged it as well. So a second
  reviewer agreeing adds weight to the finding without showing up as a duplicate.
- Security and performance findings on the same lines are both kept, because they
  describe different problems.
- Findings are ranked by severity, then by confidence, and the report keeps the top 10.
  The overall severity is that of the worst finding.

## Prompts

Each reviewer's system prompt is its own focus paragraph plus a set of shared rules, with
the reviewer's area filled in. Three lessons shaped these prompts, and the eval found each
of them.

- Each rule should appear once. The shared rules told every reviewer to report "every bug
  and security defect", while each reviewer's own prompt said security belonged to
  someone else. Because of that contradiction, the performance reviewer reported the
  eval's SQL injection and missed its own N+1 query. The rule now appears only in the
  shared rules, as a template that names each reviewer's area. It also gives the reason,
  which is that a defect reported twice reaches the reader twice and takes a place from
  the reviewer's own area. With this change the eval found all 5 planted defects instead
  of 4.
- The correctness reviewer's area is defined by the caller it assumes. In a broad sense
  every bug is a correctness bug, so this reviewer kept describing security defects by
  their symptoms ("a name with a quote breaks the search"). Its prompt now says its area
  is the right result for "a well-meaning caller with legitimate input". It also says that
  if fixing a security or performance problem would also fix the finding, the finding
  belongs to that reviewer. In a test of the correctness reviewer alone, with
  four runs per prompt version, findings outside its area fell from 12 to 1. In the full
  eval it reported no security defects in three runs, down from one to three per run. The
  fold in the merge stays as a safety net.
- The rules say what to aim for and why. Every finding is a defect with a named trigger.
  A reviewer reads a changed function's callers before calling it safe. A finding of lower
  severity needs certainty. A serious finding that can't be fully proven is reported as
  "suspected", together with what is uncertain.

## Evaluation

### Planted defects

The eval in `evals/` builds a small repository with five planted defects and a separate
clean change. It reviews both with the real agent and scores the findings with a script.
A defect counts as found when a finding has the same file and category and lands within
three lines of it. The five defects are SQL injection, an N+1 query, an off-by-one, a
hardcoded secret, and a missing authorization check that only shows up when you look at
another file. Results for each prompt version are kept in `runs/evals/`.

| Prompt version | Defects found per run | Extra findings per run | Findings on the clean change |
|---|---|---|---|
| contradictory scope rules | 4, 4, 4 | 6 | 0 |
| each area named once in the shared rules | 5, 5, 5 | 2 to 4 | 0 |
| plus the fold in the merge | 5, 5, 5 | 0 | 0 |
| plus correctness defined by its caller (current) | 5, 5, 5 | 0 or 1 | 0 |

Before the fold, nearly every extra finding was one reviewer reporting another reviewer's
defect. The one extra that still appears is a fair performance point, since a `LIKE`
pattern that starts with a wildcard can't use an index. A run costs about $0.14.

Every result records a short fingerprint of the prompts it ran with. I added this after a
mistake. An edit had failed without my noticing, so one check run measured the old prompt,
and the result made it look as if the new prompt had failed.

### Labelled findings on real code

The planted eval measures how many known bugs the agent finds. Precision has to be
measured on real code, so I reviewed four of my own repositories and labelled every
finding as real, noise or false. The labels are kept outside this repository because the
findings quote that code.

- The first full run produced 28 findings. 10 were real, 16 were noise and 2 were false,
  so precision was 36%. Most of the bad findings were noise, and only two described bugs
  that didn't exist.
- After the fixes to citations, scope and the working tree, 7 of 11 distinct findings were
  real, so precision was 64%. There were no false findings and no unverified citations.
- With the current prompt, on the two repositories with known bugs, three runs produced 18
  findings. 14 of them were real, so precision was 78%.

### A change I reverted

I added one sentence telling reviewers what isn't worth reporting, based on the findings I
had labelled as noise. It listed triggers that real input never produces, harm that a
later check already stops, notes about naming and hardening, and costs too small to
matter. The planted eval still found all five defects. I then compared the two prompts on
the labelled repositories, with three runs each.

| | Without the sentence | With it |
|---|---|---|
| Real bugs found, 3 runs | 14 | 7 |
| Precision | 14/18 = 78% | 7/9 = 78% |

The sentence halved the real bugs found and didn't improve precision. It removed some
noise, but about as much noise still got through, including one finding that repeated
another in the same report. I reverted it in `320aef5`. That commit message gives 81%
against 88%, because three findings hadn't been labelled yet when I wrote it. With every
finding labelled, precision is the same either way.

The planted eval alone would have kept the sentence. Five planted bugs can't show the loss
of small real bugs like these.

### Caveats

These numbers come from three runs per prompt version, small fixtures and one person
labelling. Results also vary a lot between runs. On one repository, runs found anywhere
from 0 to 3 real bugs. The numbers show which way a change moves things, and the sample is
too small to say more than that.

## Known limits

- A reviewer that runs out of budget contributes no findings at all.
- Large diffs are packed in git's order instead of by relevance, and the budget is
  counted in characters instead of tokens.
- The performance reviewer only reads code. Nothing is run or benchmarked.
- Unit tests cover the parts of the retry path, and live runs have exercised it, but no
  test runs the whole thing without the API.

## What I would add next

These are in order of expected value.

1. A stubbed end-to-end test. A fake SDK client would return scripted results, so CI could
   test a whole run without an API key. It would cover the fan-out, one reviewer failing,
   and a bad citation followed by a corrected one. Today the parts have unit tests, and
   the orchestration has only been tested with real API calls. pr-agent runs its whole
   pipeline in CI against a fake model in the same way.
2. A public set of real bugs. My labelled findings can't be published, so I would rebuild
   the idea from public repositories. Take a commit that fixed a bug, review the commit
   that introduced it, and check whether the agent flagged the lines the fix changed. That
   would give many real, narrow bugs, which is the kind of loss the planted eval missed
   when it approved the reverted sentence.
3. A model override for each reviewer from the environment, as the budgets already have.
   This would also make it easier to run on Bedrock or Vertex, where the model IDs differ.
4. Skipping small changes before the fan-out. Docs-only changes and dependency bumps could
   get a shorter review or none, and after the first review of a pull request, the agent
   would only review new commits.
5. A second pass that scores each finding. In pr-agent, one model writes the suggestions
   and a second call scores each one, with limits such as "a suggestion that only asks to
   verify something scores at most 7". I would try this if noise becomes the main
   complaint again, and test it on the set of real bugs, since the reverted sentence shows
   how easily a noise filter removes real findings.
6. Reporting cost per real finding instead of cost per review, along with the prompt cache
   hit rate. The SDK reports the cache numbers, and nothing here records them yet.
7. Running as a pull request bot. That needs inline comments, stable fingerprints so a
   second review doesn't repeat a finding, and earlier findings fed back so the reviewer
   can mark them resolved.
8. A map of the repository tied to the commit, but only if evals show findings missed
   because a reviewer didn't find related code. So far grep and read have been enough,
   and the eval's cross-file defect was found in every run.
