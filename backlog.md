# xlab backlog — from a benchmark lab to a testing framework for AI agents

Written 2026-10-18 against the xlab tree as it stood that morning (core modules
present, no CLI, dashboard or reports yet; see the status snapshot). The method
and the lessons come from the lab built for xsync in `~/projects/xsyncv4`:
`experiments.md` (the plan), `benchmarks/lab/` (the harness),
`benchmarks/experiments/` (170 pre-registered specs and the decision trees) and
`experiment-results.md`. xlab is that lab with the file-sync parts taken out, so
the same discipline can be pointed at a new system under test. This backlog says
how it becomes a lab for AI agents.

Agent work stays in this file. Generic lab work that the agent theme needs is
listed too, because nothing below is believable until the lab itself is finished
and calibrated.

---

## Status snapshot

- **What exists.** `spec.py` (TOML specs: arms, grids, factors, cells, control,
  delta, kinds), `plan.py` (randomized complete block plans, seeded, interleaved
  cells, retries), `runner.py` (one run at a time, gates, fingerprints, void
  handling, resume, halt after failures), `store.py` (SQLite index with runs,
  samples, events, trees, documents), `stats.py` (paired t, Hodges-Lehmann,
  sign-flip, Holm, power, Pareto), `analyze.py` (effects, absolutes, fronts,
  verdicts), `corpora.py` (seeded generated corpora, packed real trees, pinned
  by digest), `trees.py` (decision trees and a conductor), `ai.py` and
  `insight.py` (periodic model-written synthesis from numbers only), adapters
  `command`, `compression`, `simulated`, `manual`. About 3,500 lines, standard
  library only. Every module imports under Python 3.15; the system `python3` on
  this Mac is 3.9 and has no `tomllib`, so the lab must be run with Homebrew's.
- **What `pyproject.toml` promises but the tree lacks.** `xlab = "xlab.cli:main"`
  (no `cli.py`), `xlab.dashboard` package data (no `dashboard/`), "local reports"
  (no `report.py`), and the conductor and insight loops have nothing that calls
  them on a schedule.
- **What the reference lab had that xlab does not yet.** A sentinel (standing
  runs of a short spec at an interval, giving the noise floor through the day
  and catching drift), per-host budgets (the reference charged bytes written per
  drive per 24 hours), preflight that proves the system under test is what the
  spec says before a run starts, a selftest of the statistics on synthetic data
  with known effects, the dashboard, and the public read-only site.
- **Nothing has been measured with xlab yet.** The compression adapter is the
  worked example; no run has been made.

---

## Target and scope

Today an agent change is judged by running it a few times and looking. The bar
xlab sets is the one the xsync lab set for a file-sync tool: a change to an
agent is a **pre-registered experiment**, run **paired, blocked and randomized**
against the configuration in use today, and the answer is a **verdict with an
interval**: better, worse, equivalent, or inconclusive, at a declared smallest
effect that matters, never a pass count from three tries.

The **system under test** is an agent configuration: the model, the system
prompt or `CLAUDE.md`, the skills and plugins, the tools and MCP servers
available, the effort level, the harness and its version. An **arm** is one such
configuration. A **task** is instructions plus a fixture (a repository snapshot
or files) plus a grader. A **cell** is a task suite under declared conditions
(sandbox image, tool availability, prompt-cache state, time budget). A **block**
is every arm attempting the same task back to back in random order, so that
drift in the provider's latency, load and quietly updated models cancels out of
each paired comparison.

Someone using it should be able to:

- change a skill, a prompt, a model or the harness and learn within a night
  whether the agent got better, worse, or stayed the same on the suites they
  care about, at what cost;
- keep a frozen configuration running on a frozen suite around the clock, so a
  model that changed behaviour overnight shows up as a sentinel that moved;
- write the follow-ups down before the first run as a decision tree (screen the
  variants, confirm the winner with fresh blocks, hold for a decision when
  nothing wins) and let the lab follow it;
- read a model-written summary of a night's results that cites only the numbers
  it was given, next to the tables it was written from.

Out of scope for now: building agents, hosting a leaderboard for other people,
grading by anything the lab cannot reproduce, and replacing a project's unit
tests.

The brief, in one sentence, is the first sentence of the documentation site's
front page (DS-02): *"As a researcher I want to run experiments without blowing
the budget on AI tokens, so …"*. Everything that follows it on that page, and
every budget story here (L-07, AG-09, AG-09b, AG-11a), exists to finish that
sentence honestly.

---

## Vocabulary: the xsync lab, xlab today, the agent lab

| Term | xsync lab | xlab today | Agent lab |
|---|---|---|---|
| Sample | One timed transfer | One `adapter.measure` | One agent attempt at one task in a fresh workspace |
| Arm | Tool, flags, revision, tunables | Arm parameters | Model, prompt, skills, tools, effort, harness version |
| Control | `rsync` | `control` arm | The configuration in use today |
| Cell | corpus × scenario × direction × client × link × cache | Factors | suite × sandbox × tool set × cache state × budget |
| Block | Every arm once in one cell, random order | Same | Every arm attempts the same task, random order |
| Corpus | A pinned tree, generated or frozen | `corpora.py`: generated kinds or a packed directory | A task suite: tasks, fixtures and graders, pinned by digest |
| Fingerprint | `xs` binary hash, rsync and OpenSSH versions | `adapter.fingerprint`, checked every block | Model id and the version string the API returns, harness version, hashes of prompt, skills, tool manifest |
| Gates | Load, temperature, dirty pages, free disk | `[gates]` in `lab.toml` | Provider health probe, rate-limit headroom, sandbox resources, spend budget |
| Void | Failed transfer, failed verify, throttle, path changed | `Void`: recorded with a reason, never a value | API error, rate limit, sandbox failure, grader crash; **never a score** |
| Verify | Destination against the corpus manifest, untimed | `finish_sample` | The grader, untimed, outside the agent's reach |
| Primary metric | `durable_s`, a positive ratio | Any positive metric, log-ratio | Pass/fail or a bounded score first; time and cost beside it |
| Sentinel | `S0`: two identical xs arms and rsync, every 6 h | Missing | A frozen configuration on a frozen micro-suite, every N hours |
| Tree | `conduct.py`: screen, promote, confirm, hold | `trees.py` | Same, with rules on pass rates and cost |
| Insight | Dashboard summaries by a model, "from the numbers below" | `insight.py` | Same; it also reads nothing an agent wrote |

---

## What the reference lab learned that carries over

Each row of `experiments.md` §1.4 was a published-then-retracted result that
became a harness gate. The agent analogue of each is a story below.

| Measurement failure in the xsync lab | Agent analogue | Story |
|---|---|---|
| Both A/B arms ran the same stale binary | Both arms used the same prompt or model because a path or alias resolved wrong; a provider silently updated the model mid-run | AG-04 |
| Failures recorded as timings | An agent that hit a rate limit or a sandbox error recorded as "failed the task" | AG-05 |
| Silent fallback to rsync | The harness fell back to another model or tool without saying; a tool the arm depends on was absent | AG-04, AG-06 |
| Cold first run 1.9× slower; cold/warm swing of 7.6× | Prompt-cache state changes latency and cost by large factors and is never declared | AG-06 |
| Pi thermal drift 1.5× | Provider latency and quality drift by hour; models change under the same name | AG-09, blocks |
| `sync` flushed the previous cell's work into the timer | Leftover state in a workspace from the previous sample | AG-03 |
| n = 2–3 with best-of | Three tries and a look. Agent outcomes are far noisier than transfer times; sample size must come from a measured noise floor | AG-11 |
| A/A and injected known effects before any result is believed | The same configuration under two labels must be equivalent; a deliberately handicapped arm must come out worse | AG-11 |
| Prose numbers drifted from live charts | Summaries written from the numbers and labelled so; no number typed by hand | L-06 |

Three things the reference did that matter even more for agents:

1. **The controller is never in the timed path.** The runner on the driver
   host runs the sample; the controller may sleep. For agents the equivalent is
   that nothing the lab does during a sample (polling, logging, grading) can
   change the agent's wall time or its context.
2. **Declared before measured.** Specs and trees are committed before the first
   run and the analysis reads only what they declare. Agents invite post-hoc
   rubrics; the lab refuses them.
3. **Verification is independent of both tools.** The corpus manifest judged
   the copy, not xs or rsync. The grader must likewise owe nothing to any arm:
   a judge model is itself a fingerprinted, calibrated component, never the arm
   being tested.

---

## Foundation already available

Extend these; do not rebuild them.

| Capability | Where | Gap for agents |
|---|---|---|
| Specs with arms, grids, factors, cells, control, delta, kinds | `spec.py` | No notion of a task suite as a factor with its own definition table; no `grader` or `budget` keys |
| Randomized block plans, seeded, interleaved, with retry orders | `plan.py` | None; blocks are exactly what agent comparisons need |
| Unattended runner: gates, fingerprint check every block, void handling, resume, halt | `runner.py` | Gates are host-shaped (load, temperature, disk); nothing about a provider or a budget. One sample at a time, which is right until AG-14 decides otherwise |
| Timed command under `wait4`, host suspension detection | `measure.py` | An agent's wall time is only one of its costs; tokens and dollars come from the harness's own output |
| Paired statistics on log-ratios, verdicts, power | `stats.py`, `analyze.py` | Only positive, ratio-scale metrics. Pass/fail and bounded scores need paired proportions and differences (AG-07) |
| Corpora pinned by digest; generated or packed | `corpora.py` | A corpus is one file; a suite is tasks, fixtures and graders (AG-02) |
| Decision trees with promote and hold | `trees.py` | Rules speak in ratios and speedups; pass-rate and cost rules are missing (AG-12) |
| Periodic model-written insight, evidence-only, validated | `ai.py`, `insight.py` | Must never see agent transcripts, only their scores (AG-13) |
| Manual adapter: tasks with instructions, recorded later | `adapters/manual.py` | Reusable as the human grader (AG-08c) |
| Command adapter: setup, before, timed command, after, teardown, `stdout-json` metrics | `adapters/command.py` | Close to an agent adapter already; what it lacks is a workspace per sample, a grader step and the cost fields (AG-01) |
| Simulated adapter with known effects | `adapters/simulated.py` | Emits only positive continuous values; needs Bernoulli and bounded outcomes for the selftest (AG-11) |

---

## Priorities

P0 finishes the lab so that a result can exist. P1 is the agent adapter and the
statistics it needs; nothing in it is believable before P2's calibration has
passed. P3 is what the lab does with results once it has them. P4 widens it.
Stories are open unless marked otherwise. Sizes: **S** an hour or two, **M** a
day, **L** several days, **XL** a week or more.

### P0 — A lab that runs (generic, blocking everything below)

| ID | Item | Done when | Size |
|---|---|---|---|
| L-01 | **`cli.py`.** `pyproject.toml` names `xlab.cli:main`; there is none. The reference's `plan.py` verbs are the model: `init`, `plan`, `enqueue [--front] [--seed]`, `queue`, `run` (the runner loop), `cancel`, `analyze [--control] [--looks]`, `report`, `record`, `import`, `tasks`, `tree validate|run|status`, `insight`, `doctor`. | Every verb has a `--help` and a test; `xlab run` works the queue unattended; `xlab doctor` names the Python in use and refuses one without `tomllib` | M |
| L-02 | **Conductor and insight on a schedule.** `trees.Conductor` and `insight.tick` exist but nothing calls them. A `conductor` loop (every `[conductor] every_s`) advances trees and, when due, writes reports and insights; it runs beside the runner or inside its idle wait. | A tree with two nodes screens, promotes and confirms with no one watching; the insight is written at the window's end | M |
| L-03 | **Reports.** `[reports] every_hours` is configured; no `report.py` exists. Markdown and JSON per experiment from `analyze`, and a periodic lab report (what ran, what was decided, what is queued), stored through `store.add_document`. | `xlab report EXPERIMENT` renders effects, absolutes, fronts, void reasons and the runs it covers, with no number not in the analysis | M |
| L-04 | **Selftest of the statistics.** The reference's `selftest.py` simulated experiments with known ratios and heavy drift and checked interval coverage (95.2%, 94.8%, 95.4% at 8, 12, 24 blocks), coverage under heavy tails, wrong-verdict rate, power and Holm's family-wise error. xlab has `simulated` but no selftest. | `xlab selftest` passes and runs in under two minutes; `analyze.py` is not changed without it | M |
| L-05 | **Preflight.** The runner fingerprints the system under test but only once a run has started. A preflight before `enqueue` and at `run` start checks the adapter can see everything the spec names (tools installed, corpora buildable, suite digests pinned) and prints one `PROBLEM` line per failure. | A spec naming a missing tool is refused at enqueue, not voided at the first block | S |
| L-06 | **Sentinel runs.** `standing SPEC --every H`: one run of a short spec every H hours whatever is queued. Its identical arms give the noise floor at that hour; its ratio to the control should not move. Shown separately in reports. | A sentinel that drifts past its own interval raises an event | M |
| L-07 | **Budgets.** The reference charged bytes written per host per 24 hours and passed over runs that would exceed it. Generalise: an adapter declares what a plan will spend (`bytes`, `seconds`, `usd`) per resource; `[budgets]` in `lab.toml` caps each per 24 hours; a run that does not fit waits. | A plan costing more than the day's allowance runs only when nothing has been charged for a day | M |
| L-08 | **Tests for the lab.** The reference had `test_lab.py` (80 KB). xlab has none. Unit tests for spec normalisation, plan determinism from seed, runner resume after a kill mid-block, void and retry, store queries, tree rule evaluation, insight validation. | `python -m unittest` passes; a crashed runner resumes at its first unfinished block in a test | L |
| L-09 | **Dashboard.** `pyproject` ships `xlab.dashboard/static`; nothing exists. The reference's dashboard is a separate backlog (`~/projects/xsyncv4/backlog-benchmark-site.md`) that was already aiming at reuse by another lab; xlab is that other lab. Local first: runner state, queue, runs with samples, analysis with forest plots, trees, tasks for manual samples, insights. | The dashboard shows a running compression experiment live from `store` with no xlab-specific code in the page beyond a config file | XL |
| L-10 | **README and a worked example.** The lab's own README in the style of the reference's `benchmarks/lab/README.md`: layout, specs, running around the clock, analysis, calibration. | A newcomer runs the simulated spec, a compression spec and reads the report without asking | S |

### P1 — The agent adapter

| ID | Item | Done when | Size |
|---|---|---|---|
| AG-01 | **`agent` adapter.** One sample: make a fresh workspace from the task's fixture, run the arm's harness command in it with the task's instructions, under `measure.run_timed`, with a timeout; collect the harness's structured output; run the grader; return metrics. Arms are harness commands with placeholders (`{workspace}`, `{task.instructions}`, `{arm.model}`) exactly as `command` fills them, so any harness that can be started from a shell works. First profile: Claude Code non-interactive (`claude -p … --output-format json`), whose result carries tokens, cost, turns and duration. Second: an Agent SDK script. Third: any command printing the same JSON. | A two-arm spec (two prompts, one model) completes eight blocks on a three-task suite unattended, every sample graded, every workspace removed | L |
| AG-02 | **Task suites as corpora.** A suite is a directory: `suite.toml` (name, tasks, grader defaults), one directory per task with `task.toml` (instructions, grader, time budget, expected outputs), and its fixture (files, or a packed repository snapshot). `corpora.ensure` learns to pin a suite the way it pins a packed tree: sorted members, neutral ownership, SHA-256 in every record, a `sha256 =` pin in the spec refuses a drifted suite. Generated suites too (seeded, deterministic, like `text-32m`): synthetic coding tasks with a known solution, for the calibration in AG-11. | The same suite name gives the same digest on two machines; a changed task text is refused | M |
| AG-03 | **Workspace isolation.** Every sample gets a workspace nobody has touched: a copy of the fixture under the run's scratch (or a `git worktree` of a snapshot, or a container from a declared image). The agent never sees the lab, the other arms' workspaces, the suite's graders or expected outputs. The workspace is deleted after grading unless `keep_workspaces` is set for a run under investigation. Environment is blanked the way the reference blanked `XSYNC_CONFIG`: no user `CLAUDE.md`, no user settings, no ambient API keys beyond the one the arm names. | A test plants a file in a previous sample's workspace and the next sample cannot see it; a grader file is unreadable from inside the workspace | M |
| AG-04 | **Fingerprint for agents.** `adapter.fingerprint` returns: the harness version (`claude --version`), the model id as resolved and the model version string the API returns on a one-token probe, SHA-256 of the system prompt, `CLAUDE.md`, each skill and plugin, the MCP and tool manifest, and the sandbox image digest. Checked every block as the runner does now: a change mid-run fails the run with both fingerprints in the event. Preflight (L-05) refuses an arm whose model probe returns a different model than named, or whose prompt file is missing. | Editing a skill while a run is in progress fails that run at the next block with the two hashes named | M |
| AG-05 | **Void taxonomy.** The line between "the run could not take the sample" (void, no score) and "the agent did badly" (a score of 0) is the central design decision and is written down once, in the adapter's docstring and the README. Void: provider 5xx, 429 and overload, timeout before the first token, harness crash, sandbox or fixture setup failure, grader crash, fingerprint change. A score: the agent gave up, said it cannot, ran out of its turn or time budget, produced wrong output, broke the build. Every void carries the provider's own error text; a void streak with no valid sample fails the run as the runner does now. | A test suite of recorded harness outputs classifies each the documented way; the dashboard's void reasons are the raw ones | S |
| AG-06 | **Agent gates and declared conditions.** Gates before each sample: a provider health probe (one tiny request; its latency against a ceiling), rate-limit headroom read from the probe's headers, free disk for workspaces, and the budget (L-07). Declared cell factors, never accidents: `cache = "cold" \| "warm"` (warm: a priming request with the arm's prefix within the cache TTL, untimed, immediately before; cold: outside the TTL, or a nonce in the prefix), `tools = "full" \| "none" \| a named set`, `network = "on" \| "off"` for the sandbox. The runner records the state before and after as it records host snapshots now. | A warm/cold factor shows the expected cost difference in a two-cell smoke run; a provider outage voids samples with "host did not settle"-style reasons rather than failing them | M |
| AG-07 | **Statistics for pass/fail and bounded scores.** Everything in `stats.py` assumes positive ratio-scale metrics. Agents' primary metric is usually a Bernoulli outcome per task or a score in [0, 1]. Add a metric `kind`: `ratio` (today), `difference` (paired mean difference with its t and Hodges-Lehmann intervals, delta in metric units), `proportion` (paired binary outcomes per block: exact McNemar and a Newcombe interval for the difference in pass rates, delta in percentage points; `pass@k` as a derived metric when a task has several attempts in a block). Verdicts and `blocks_needed` for each kind. Metric declares `kind`, `better`, and `bounds`. | `selftest` covers all three kinds with known effects; `analyze` chooses the method from the metric's kind and the report says which | L |
| AG-08 | **Graders.** A grader runs after the sample, untimed, with the workspace read-only and the agent gone. Each returns `{score, passed, detail}` and raises `Void` only for its own failure. **AG-08a `tests`:** run a command in the workspace (the task's hidden tests), exit 0 passes; partial credit from a `stdout-json` line. **AG-08b `expected`:** compare named files or a command's output with the task's expected output, byte-wise or by a declared normaliser. **AG-08c `manual`:** the sample becomes a task for a person on the dashboard, reusing `adapters/manual.py` and `runs.record_manual`; the run is `awaiting` until graded. **AG-08d `judge`:** a pinned model with a pinned rubric scores the workspace or transcript through `ai.complete`; the judge model and rubric hash go into the fingerprint; the judge is descriptive only until AG-11d passes. | Each grader has a test with a known-good and known-bad workspace; a grader that crashes voids the sample with its own error, never a zero | L |
| AG-09 | **Cost as a first-class metric.** From the harness output: input, output, cache-read and cache-write tokens, turns, tool calls, and dollars computed from a price table **pinned in the spec** (`[prices.MODEL]`), never fetched, so a result is reproducible after prices change. `usd` is a positive ratio-scale metric (log-ratio applies); `tokens_out` too. Declared Pareto pairs `[["usd", "pass_rate"]]` give the cost/quality front per cell. The plan declares its expected spend so L-07 can charge it. | A report shows the cost/quality front; a run that would exceed the day's `usd` budget waits | S |
| AG-09b | **Spend estimate before a run.** The front page promises experiments that do not blow the token budget, so the lab must say what a run will cost before it starts. `xlab plan` prints planned samples × the per-sample estimate (from `[prices]` and either a declared `tokens_per_sample` or the suite's last measured mean) as tokens and dollars, beside the day's remaining budget (L-07). `enqueue` refuses a plan over the remaining budget unless `--over-budget` is given, and says by how much. The estimate and the actual spend are both in the run's record and the report, so the estimator can be judged. | A plan over budget is refused with both numbers; after eight blocks the report shows estimate against actual | S |
| AG-10 | **The record.** Beside metrics, `details` carries what a later question needs and the analysis never reads: the harness's full JSON result, transcript path, the grader's detail, the resolved argv and environment (keys redacted), the fingerprint, the workspace digest after the agent finished. Transcripts go to the run directory, one file per sample, named by unit; `samples.jsonl` holds only the path. Nothing a model wrote is ever read by `insight.py`. | A sample's transcript opens from its unit id; `grep` for the API key across the lab finds nothing | S |

### P1 — The agent-facing surface: clone the repo, ask your LLM

The intended use is that someone clones xlab and tells their coding agent "I
want to start an experiment involving …", and the agent knows how to run it
properly. What must travel with the clone is the method (hypothesis, control,
delta, cost before spend, verdicts rather than pass counts), which is a
document, not a server. So: a CLI an LLM can drive blind, a project skill that
teaches the workflow, root files that point at it, and MCP only when a client
without a shell or live resources needs it.

| ID | Item | Done when | Size |
|---|---|---|---|
| AU-01 | **The CLI as the agent UI.** Every verb of L-01 takes `--json` and prints one object; `xlab plan` prints samples, blocks and the token and dollar estimate (AG-09b) before anything runs; `xlab doctor` names the Python, the adapters, the budgets and what is missing; errors say what to do next ("over budget by $4.20: raise `[budgets] usd` or pass `--over-budget`"); exit codes separate refused (budget, preflight, dirty fingerprint) from failed. `xlab explain RUN` renders a run's verdicts in plain language for a model to relay. | A scripted agent session goes from spec to verdict using only `--json` output and never parses prose | M |
| AU-02 | **The `xlab-experiment` skill.** `.claude/skills/xlab-experiment/SKILL.md` in the Agent Skills format (frontmatter `name`, `description`; body; `references/`; `scripts/`), so Claude Code loads it from a clone and other agents read the same file. The description triggers on "start an experiment", "compare", "is X better than Y", "A/B", "benchmark", "which model/prompt/skill". The body is the workflow: interview for the question, hypothesis, arms, control, delta, suite or corpus and budget; choose the adapter (command, compression, agent, manual); write the spec; run `xlab plan` and show the cost; enqueue only after the user sees it; wait for the runner; `xlab analyze`; explain the verdict and its interval; refuse post-hoc rubrics, judge primaries without a calibration pin, and runs before AG-11a on a new suite. `references/` holds the spec reference, the statistics explained, void versus score, and worked specs for each adapter; these are the same sources DS-03 generates manual pages from, so skill and site cannot drift. `scripts/` holds nothing the CLI already does. | A fresh clone plus "start an experiment comparing zstd 3 and 9 on logs" yields a valid spec, a shown cost estimate and a queued run without the user naming a verb; the same for a two-prompt agent comparison | M |
| AU-03 | **`CLAUDE.md` and `AGENTS.md` at the root.** Short and identical in substance: what xlab is, where the skill is, which Python to use, and the one rule that no run is queued without its planned spend shown. Nothing that the skill or `--help` already says. | Under forty lines each; a model that reads only these can find the skill and the CLI | S |
| AU-04 | **MCP, when a client needs it.** A thin `python -m xlab.mcp` over the same functions the CLI calls, registered by a project-scoped `.mcp.json` so it is discovered from a clone: tools for plan, enqueue, status, analyze and explain, and resources for runner state, the queue and new verdicts so a model is told rather than polls. Built only when a chat client without a shell must drive the lab, or live resources are wanted; the skill stays the source of the method and the MCP never duplicates its text. | `.mcp.json` works from a clone in Claude Code; the tool results are the CLI's `--json` objects unchanged | M |
| AU-05 | **Plugin packaging, later.** When xlab is installed as a package rather than cloned, bundle AU-02 and AU-04 as a plugin for a marketplace. The in-repo skill remains the source; the plugin is a build artefact of it. | A build step produces the plugin from the repo's skill with no hand-copied text | S |

### P2 — Calibration: nothing above is believed until this passes

| ID | Item | Done when | Size |
|---|---|---|---|
| AG-11a | **A/A.** One configuration under two arm labels, on each suite and cache state, at the planned block count, must come out *equivalent* on the primary metric in every cell. This is also the noise-floor measurement: σ for pass rate, time and cost per cell, published in the README as the reference published its σ_d table. Expect the honest result to be that a 5-point pass-rate question on a 10-task suite needs far more blocks than a night gives; that number decides what is testable and is the first thing to publish. | A table of blocks needed per (suite, metric, delta) exists and the spec defaults follow it | M |
| AG-11b | **Known effect.** An arm with an injected handicap (a system prompt that forbids one needed tool; a truncated instruction; a `sleep` in the harness for 2δ of the control's time) must be called *worse* at the planned block count; a handicap of δ must give an interval excluding equality without earning a verdict. | Both outcomes recorded in the lab, as the reference's E0b was | S |
| AG-11c | **Synthetic selftest for agent metrics.** Extend `simulated` with Bernoulli and bounded outcomes (per-task difficulty, per-block shared drift, per-arm true pass rates) and check AG-07's coverage, wrong-verdict rate and power. | Part of `xlab selftest` | M |
| AG-11d | **Judge calibration.** The judge grader on known-good and known-bad workspaces and against the `tests` grader on the same samples: agreement, bias by arm (a judge that prefers its own model's prose is a confound), and stability across repeated calls. Until this passes the judge is descriptive only; afterwards a spec may name it primary with the calibration run cited. | Agreement table in the lab; a spec with a judge primary is refused without a `calibration =` pin | M |

### P3 — Running the agent lab

| ID | Item | Done when | Size |
|---|---|---|---|
| AG-12 | **Tree rules for agents.** `trees.py` rules speak in ratios and speedups. Add `pass_rate_at_least`, `pass_rate_below`, `difference_at_least`, `usd_below`, `on_front`, with the same fnmatch picking by arm, metric and factors. A standard tree shape to ship as an example: screen the variants (6 blocks) → promote the winners to a 12-block confirmatory run → hold if nothing wins; a regression guard node that keeps the whole parent matrix (`all_corpora = true` in the reference). | The example tree runs end to end on the simulated adapter and on a real two-prompt comparison | S |
| AG-13 | **Insights for agent experiments.** `insight.py` gets the agent-specific evidence (pass rates with intervals, cost fronts, void taxonomy counts, sentinel drift) and the rule that it never receives transcripts or any text an agent produced. The reference's dashboard labelled every summary "written by Claude from the numbers below"; keep that line. | An insight citing a run not in the evidence is rejected (already) and a transcript path in the evidence is a test failure | S |
| AG-14 | **Concurrency decision and implementation.** The reference ran one sample at a time, which made drift cancel and kept the host quiet. Agent samples are mostly waiting on a provider, so parallel samples are tempting; they also share rate limits and make the provider's state a within-block confound. Decide (see Decisions) and, if parallel is allowed, run **blocks** in parallel across providers or accounts, never arms of one block against each other, and record the concurrency level in every sample. | Written decision; a `concurrency` setting with its effect measured in an A/A run | M |
| AG-15 | **Sentinel for model drift.** A frozen configuration on a frozen micro-suite (three tasks, `tests` grader, both cache states) every 6 hours, through L-06. Its pass rate, time and cost against its own first week are the drift detector; the model version string from AG-04 is recorded with each, so a drift can be matched to a version change or shown to have none. | A sentinel that moves raises an event and the insight names it; the version string history is a report table | S |
| AG-16 | **Dashboard views for agents.** On top of L-09: a sample's transcript beside the control's for the same task and block; the void taxonomy as its own panel; cost/quality fronts; the sentinel's drift chart with version changes marked. | A failing sample can be opened to its transcript from the forest plot in two clicks | M |
| AG-17 | **Importing other evaluation formats.** `claude plugin eval` suites and the skill-creator's evals already describe tasks and expected outcomes; an importer turns one into an xlab suite (AG-02) so the same tasks get paired, blocked statistics instead of a pass count. Export a verdict table back in their report shape. | One existing eval suite imports, runs as a two-arm experiment and exports its table | M |
| AG-18 | **Operator guide.** How to pin a suite, write a two-arm spec, read a verdict, when to trust a judge, what a void means, and how to run the lab on a spare machine the way the reference ran on `neo`: `@reboot` runner, a key file with mode 600, no ambient credentials. | A second person runs a comparison on their own machine from the guide alone | S |

### P3 — Documentation site

Modelled on `~/projects/xsyncv4/sites/xsync/`: a product page served from
`dist/`, a manual under `/docs/` built with VitePress 1.6.4 on the default theme
restyled grayscale and sans serif with HoloCSS entrances vendored in, pages
generated by a Python script from curated content plus the executable's own
captured `--help`, recordings of real terminal output replayed on the page, a
`sources.json` of fingerprints for the documents the manual was reviewed
against, and static hosting from `dist/` through the same `.openai/hosting.json`
shape. The site lives in `sites/xlab/` in this repository and builds offline.

| ID | Item | Done when | Size |
|---|---|---|---|
| DS-01 | **Site skeleton.** `sites/xlab/` with the xsync site's layout: `dist/` product page, `docs/` VitePress manual with `.vitepress/config.mjs`, `sidebar.json` and the grayscale theme, `scripts/generate-docs.py` and `docs_content.py`, `scripts/finish-docs-build.mjs`, `docs-inputs/`, `docs/public/`. `npm run build` generates the manuals, builds VitePress and copies it into `dist/docs/`; `npm run preview` serves `dist/` locally. HoloCSS vendored from `~/projects/holocss/dist/` with its licence; no HoloGPU fluid canvas (see Decisions). | Builds with no network after `npm ci`; the product page and `/docs/` both serve from one `dist/` | M |
| DS-02 | **Front page.** Its first sentence is fixed and begins: *"As a researcher I want to run experiments without blowing the budget on AI tokens, so"*. The full sentence, settled on 2026-10-18: *"As a researcher I want to run experiments without blowing the budget on AI tokens, so every run is planned, priced and paired before the first token is spent, and stops by itself when the day's budget is gone."* The page then makes three promises, each linked to the manual page that keeps it: the spend is known before a run starts and capped per day (AG-09b, L-07); the answer is a verdict with an interval from paired blocks, not a pass count (AG-07, AG-11a); the lab runs unattended and follows the tree you wrote before it started (L-02, AG-12). Below that, a short tour with the recordings (DS-04). No number appears on the page that was not produced by a lab run, and each one names its run id, as the reference learned when its prose drifted from its charts. | The settled sentence is byte-exact on the page; every figure on the page cites a run; the three promise links resolve to manual pages | S |
| DS-03 | **Manual generated from the CLI.** `docs-inputs/xlab-help.txt` captured from `xlab --help` and every verb's `--help` (depends on L-01); `generate-docs.py` turns it into the reference page and checks it against the installed CLI the way the xsync site retains `xsync-help.txt`. Curated pages in `docs_content.py`, in the manpage shape the xsync site uses (NAME, SYNOPSIS, DESCRIPTION, EXAMPLES, SEE ALSO): `xlab(1)` index, getting started, the lab directory and `lab.toml`, specs, suites and corpora, adapters (command, compression, simulated, manual, agent), running around the clock (queue, sentinel, budgets, halts), analysis and verdicts, decision trees, insights, calibration, the record format, development. Sidebar groups MANUAL, WORKFLOWS, REFERENCE, PROJECT. | Thirteen or so pages build from the script; a stale help capture fails the build with the diff | L |
| DS-04 | **Recordings of real output.** A `scripts/capture-output.py` like the xsync site's: it makes a throwaway lab, runs the simulated spec and the compression smoke spec, captures `xlab plan`, `xlab run`, `xlab analyze` and a report with control escapes stripped and the original kept, and the product page replays and scrubs them. The page says the recordings come from the simulated adapter and are not a benchmark. | Recordings regenerate from one command; the originals are downloadable; the caption states their source | M |
| DS-05 | **Budget page.** A manual page on what an experiment costs: how the per-sample estimate is made, the day's budget, the sentinel's share, and the blocks-needed table from AG-11a. With a small client-side calculator: σ (from the published table or typed in), delta and the price table in, blocks, tokens and dollars out, from `docs/public/noise.json` that the lab exports. It is the page the front page's first sentence links to. | The calculator's inputs come only from an exported file; changing the lab's table changes the page on the next build | S |
| DS-06 | **Sources fingerprints.** `docs/public/sources.json` with SHA-256 of the README, this backlog, `config.DEFAULTS` and the CLI help, plus the reviewed date; the generator warns when any differs from the recorded one, so the manual is revised when the thing it describes changes. | A README edit makes the next build warn with the file named | S |
| DS-07 | **Hosting.** The same static host and `.openai/hosting.json` shape as the xsync site, `dist/` as the directory, `/docs/` as the VitePress base. The dashboard's public site (L-09, P4) stays separate and is linked from the nav, never embedded. | The site deploys from `dist/` with no build step on the host | S |

### P4 — Widening

- **Beyond coding agents.** Conversational agents with a simulated user (the
  simulator is a pinned, fingerprinted component like a judge); computer-use
  agents with the iOS Simulator or a browser as the workspace; multi-agent
  workflows where the arm is the orchestration. Each is a new fixture kind and
  grader, not a new lab.
- **Several providers in one experiment.** Arms across providers need per-provider
  gates, budgets and fingerprints; the fingerprint's "model version string" has
  no common form.
- **A public read-only site**, as the reference's `dashboard.mk7s.dev`: push-only
  ingest, scrubbing on the lab side, nothing private leaving the machine. Reuse
  the reference's relay rather than writing one.
- **Cross-lab comparison.** The same suite, run by two labs on two machines,
  should agree; a `host` factor and the record's provenance already allow it.

---

## An agent spec, as xlab would read it

Illustrative; keys named here are what AG-01 to AG-09 add.

```toml
id = "SK2-review-skill"
question = "Does the rewritten code-review skill find more of the planted bugs at no more cost?"
hypothesis = "v2 raises the pass rate by at least 10 points on review-suite and costs no more"
kind = "screening"
adapter = "agent"
primary = "passed"
control = "v1"
blocks = 8
delta = 0.10                        # percentage points, since `passed` is a proportion
pareto = [["usd", "passed"]]

[options]
harness = "claude"                  # fills the command template below
timeout_s = 900
grader = "tests"                    # the suite's default; a task may override

[prices.claude-opus-5-5]
input = 15.0
output = 75.0
cache_read = 1.5
cache_write = 18.75

[factors]
suite = ["review-suite"]
cache = ["warm", "cold"]

[corpora.review-suite]
path = "~/projects/research/suites/review-suite"
sha256 = "…"

[arms.v1]
model = "claude-opus-5-5"
skills = ["skills/code-review-v1"]
command = "claude -p {task.instructions} --output-format json --model {arm.model} --add-dir {workspace}"

[arms.v2]
model = "claude-opus-5-5"
skills = ["skills/code-review-v2"]
command = "claude -p {task.instructions} --output-format json --model {arm.model} --add-dir {workspace}"
```

The grader, the expected outputs and the planted bugs live in the suite, not the
spec, and the agent's workspace cannot see them.

---

## Decisions needed

1. **First harness.** Claude Code non-interactive through the `command`-shaped
   adapter (recommended: it exists, prints structured results with cost, and is
   what the skills under test run in), the Agent SDK, or a generic command
   contract only.
2. **Void or zero.** The boundary in AG-05. Recommended as written: anything the
   lab or the provider did is void; anything the agent did is a score.
3. **Concurrency.** Serial (recommended to start, as the reference) or parallel
   blocks across providers. Decide after AG-11a shows what the noise floor costs
   in nights.
4. **Judge as a primary metric.** Never, or only with a cited calibration run
   (recommended).
5. **Prices.** Pinned in the spec (recommended) or a lab-wide table with a date.
6. **Where suites live.** In the lab (`LAB/suites/`) or a sibling repository
   pinned by commit. Recommended: a sibling, since suites are shared between
   labs and contain fixtures that do not belong in a lab's data directory.
7. **Standard library only.** The reference kept it so that `neo` ran unchanged.
   The agent adapter shells out and `ai.py` uses `urllib`, so nothing forces a
   dependency. Recommended: keep it.
8. **The dashboard.** Build xlab's own (L-09) or finish the reference's
   reusable-dashboard backlog and point it at xlab. The latter was that
   backlog's stated goal.
9. **The front page's sentence.** Settled 2026-10-18; the full sentence is in
   DS-02. The three promises under it follow from it.
10. **Skill or MCP first.** Skill first, MCP when a client needs it (as in
    AU-01 to AU-04). Recommended as written; the alternative, MCP first, costs
    a server and a config entry before the model has learned the method.
11. **Product-page motion.** The xsync site mounts HoloGPU's fluid solver on
    its front page. Recommended: not for xlab; HoloCSS entrances only, so the
    page says "budget" and not "spectacle".

---

## Suggested order

1. **L-01, L-04, L-08, L-05**: a CLI, a selftest, tests and preflight, so the
   lab can be driven and trusted on the simulated and compression adapters.
2. **AG-02, AG-03, AG-01, AG-08a, AG-05, AG-10**: suites, workspaces, the
   adapter, the tests grader, the void boundary and the record. First real
   samples.
3. **AG-07, AG-11c**: statistics for proportions, with their selftest, before any
   agent verdict is read.
4. **AG-04, AG-06, AG-09, AG-09b, L-07**: fingerprints, gates and cache state,
   cost, the spend estimate and budgets. The lab can now run a night unattended
   without surprises, and says what the night will cost first.
4a. **AU-01, AU-03, AU-02**: `--json` and the cost-first CLI, the root files,
   then the skill. From here a clone plus a prompt runs an experiment.
5. **AG-11a, AG-11b**: A/A and known effect. Publish the blocks-needed table.
   This is the moment the lab is allowed to say "better".
6. **L-06, AG-15**: the sentinel.
7. **L-02, L-03, AG-12, AG-13**: trees, reports and insights for agents.
8. **AG-08d, AG-11d**: the judge and its calibration.
9. **DS-01, DS-03, DS-04, DS-02, DS-06, DS-07**: the documentation site, once
   the CLI it documents is stable; the front page last, so its numbers exist.
10. **DS-05** after AG-11a has produced the blocks-needed table it shows.
11. **L-09, AG-16, AG-17, AG-18**: dashboard, importers, the guide (AG-18
    becomes manual pages in DS-03 rather than a separate document).
12. **AU-04, AU-05** only when a shell-less client or a package install asks
    for them.
