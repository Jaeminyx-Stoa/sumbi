# sumbi design (draft 2)

sumbi seeds a sensible harness, watches the friction and waste in how coding agents work, and improves the harness itself, so that task success rises while the time and tokens per success fall. It is meant to be planted in any repository and to serve any coding agent. It generalizes two harness labs that were run by hand in 2026.

The first draft was reviewed by a model from a different family. This draft addresses its findings; see [Review record](#review-record) at the end.

## Why another tool

Close neighbours already exist:

- **Friction mining that suggests instruction edits:** Claude Code `/insights`, netresearch/retro-skill, claude-reflect.
- **Harness seeding with spec updates:**
  - mindfold-ai/Trellis (many agents; promotes learnings after lint, type checks and tests pass)
  - Junhanliu-dev/espalier-engineering (derives conventions; refreshes gated by a human)
- **Automatic skill growth:** tigerless-labs/autoharness.
- **Live observability:** simple10/agents-observe.
- **Research that searches harnesses against benchmarks:** Meta-Harness, AutoSaddler.

What we did not find combined in one place:

1. Success counted per deliverable by outside signals.
2. Cost per success that includes failures and retries.
3. Predictions written before a change and compared like with like.
4. Review by a different model family for large changes.
5. One notice delivered to several agents.
6. Invariants that never weaken approvals.

Self-improvement that is not filtered by outcomes can make things worse. Context files have been reported to add 19–23% cost with little success gain ([Gloaguen et al. 2026](https://arxiv.org/abs/2602.11988)). So the core of sumbi is **a deliverable ledger and a judging device**, not a rule generator. Instruction, skill and hook formats are reused from existing standards.

## Invariants

1. **Deliverables are fixed when work is dispatched.**
   - Each one gets an ID and acceptance criteria up front.
   - Work abandoned before any pull request existed is recorded too.
   - States: `success`, `failed`, `in_progress`, `immature` (merged, but the revert and follow-up window has not closed).
   - First-pass success and eventual success after retries are counted separately.
2. **Success uses outside signals:** merged, required checks green at merge, no revert or follow-up fix inside the window, and human acceptance where it applies. Self-reports are context only.
3. **Two cost views.**
   - *Operational:* period spend ÷ period successes, for watching trends.
   - *Intervention verdicts:* lifetime cost of a cohort of deliverables ÷ its successes, with identical follow-up windows.
   - Tokens are reported as new input, cache writes, cache reads, output and reasoning output, plus the total. "Not reported" is distinct from zero.
4. **No verdict without completeness.** Every run reports what it read and how each link was made. If unattributed spend exceeds the threshold, savings verdicts are withheld.
5. **Interventions are pre-registered.** Before a change, record:
   - the prediction
   - the acceptable success-rate drop (non-inferiority margin)
   - the sample needed and the order of decisions
   - the harness version each deliverable actually ran under

   Changes in model, effort, CLI version or runner setup during the window mark the comparison as *not comparable*.
6. **Verdicts:** adopt, reject, owner decides, or withhold.
   - Adopt only when non-inferiority holds, neither time nor tokens get worse, and at least one improves by more than 10%.
   - Cost ratios carry bootstrap intervals; success rates carry numerator, denominator and an interval.
7. **Safety.**
   - No verdict weakens an approval, a cross-family review or a money-path gate. A gate-change detector watches for it.
   - Collection is read-only.
   - Committed output holds allow-listed categories and local pseudonymous IDs. Free text stays in a local review file that is never committed automatically.
8. **Small steps.** One to three changes per round, devices before rules, and short always-loaded instructions.

## Data model and linking contract

```
project ─ deliverable (ID, acceptance, state, dispatched/closed at)
            └ attempt (retries and follow-up fixes included)
                └ session (agent, host, session ID, parent session ID, harness version)
                └ pull request · CI run
```

- **Every link carries its evidence:** a deliverable ID in the brief, a worker contract, a PR URL or branch name seen in the log. A working directory's git origin is a *candidate*, not proof.
- **Allocation:** sessions spanning several projects are allocated explicitly or left *unassigned*. Parent sessions (dispatch, review, handoff) are allocated to their deliverables.
- **Coverage report per host, every run:** sessions read, broken lines, duplicates, unknown formats, and the share of unattributed spend.
- **Time.**
  - Observed (request and tool start/stop events) is reported separately from estimated (gaps between events, with sensitivity to the idle threshold).
  - Deliverable elapsed time (dispatch to acceptance or abandonment) is split into agent, CI, human and rate-limit waiting.
  - The sum of parallel session time is kept apart from the time a person actually waited.

## Components: three layers and two kits

### 1. Collect — `sumbi collect`

| Adapter | First | Later |
|---|---|---|
| Sessions | Claude Code (`~/.claude/projects/**.jsonl`, subagents), Codex (`~/.codex/sessions/**/rollout-*.jsonl`) | More agents per the support matrix; OpenTelemetry as a transport |
| Outcomes | GitHub Actions and pull requests, tested first against recorded API responses | GitLab; manual entry for outcomes without Git or pull requests |
| Workers | Plugins for in-house orchestration logs | — |
| Hosts | Local, then (M3) SSH with a pinned zipapp, allowed hosts and read paths | — |

Windows are `[start, end)` everywhere. Errors, caps and unsupported fields are reported, not hidden. Cumulative token counters, resumed sessions and duplicates are handled explicitly.

### 2. Judge — `sumbi compare`

- **Ledger:** `deliverables.csv` in the lab is the source of truth. Rows inferred from pull requests enter only as *suggestions*.
- **Comparison:** by cohort, with breakdowns by task type.
- **Confounder flags:** model, effort and CLI mix; runner setup; exposure version; volume and difficulty shifts.
- **Verdicts are proposals.** The retro session and the owner decide.

### 3. Propagate — `sumbi notice sync`

- **Delivery:**
  - Claude Code hooks (SessionStart, UserPromptSubmit)
  - Codex hooks ([docs](https://developers.openai.com/codex/hooks); the installed version is checked)
  - an `AGENTS.md` managed block for agents without hooks
- **Scope:** origins are normalized (https and ssh forms). An explicitly out-of-scope repository never falls back to path matching. A global instructions block cannot guarantee per-repository silence, so it is optional and documented as such.
- **Installer tests:** ownership, conflicts, idempotency, atomic replacement, recovery, uninstall. Backups have defined permissions and retention.
- **Local checkouts:** a merge on the hosting service does not reach sessions that read a local checkout. Propagation includes fast-forwarding the local checkout that sessions open.

### Lab kit — `sumbi init lab`

- Contents: `AGENTS.md` (principles, safety-line slots, locations), a `CLAUDE.md` shim, config, `ledger.md`, `deliverables.csv`, `notices.md`, and the retro skill with its references.
- The skill uses the [Agent Skills](https://agentskills.io) `SKILL.md` format. Support beyond Claude Code and Codex is tracked in the support matrix.
- The lab lives where product agents do not read it.

### Product kit — `sumbi init product` (optional example)

- Contents: a short `AGENTS.md` table of contents, a map, a brief template, a handoff template, and the `Harness friction:` report line.
- Existing files are never overwritten; sumbi proposes a diff instead.

## Support matrix

"Any repository, any agent" is defined by levels. Each feature is marked *verified*, *secondary source* or *unverified*.

| Level | Meaning | First support |
|---|---|---|
| L0 manual | Spec for entering deliverables, cost and outcomes by hand | Any repository, any agent |
| L1 sessions | Tokens, time and friction from logs | Claude Code, Codex |
| L2 outcomes | CI, pull requests, reverts | GitHub |
| L3 propagation | Notices and rules through hooks, `AGENTS.md`, skills | Claude Code, Codex, agents that read `AGENTS.md` |

## Milestones and acceptance

- **M1 — measurement base.**
  - Scope: read local Claude Code and Codex logs for a fixed window. Join them with a minimal `deliverables.csv` to report the four deliverable states and total tokens by kind. Keep observed and estimated time apart. Use recorded GitHub responses. No installation, remote hosts or propagation yet.
  - Accept 1: matches hand-computed truths on synthetic fixtures.
  - Accept 2: passes the edge cases — window boundaries, resumed sessions, duplicates, broken lines, Windows paths, a failure with no pull request, a session spanning several repositories.
  - Accept 3: the coverage report exposes gaps.
  - Accept 4: no project names in core code (a test enforces it).
- **M2 — minimal judging.**
  - Scope: cohort comparison, non-inferiority margin and sample size, intervals on cost ratios, task-type breakdown, confounder flags.
  - Accept: reproduces a hand-built round table, or explains every difference.
- **M3 — real connections.**
  - Scope: live GitHub adapter, SSH hosts with a pinned zipapp, separation of committable aggregates from local free text.
  - Accept: the next lab rounds are collected with sumbi.
- **M4 — seeding and propagation.**
  - Scope: `init lab`, `init product` (example), `notice sync`, the gate-change detector.
  - Accept: in a temporary home, only in-scope sessions receive notices; install and uninstall are idempotent; existing files are never overwritten.
- **M5 — replay A/B** (costs tokens; the owner decides).
  - Rerun past work under harness A and B: randomized order, isolation, repetitions, and unseen tasks (merged pull requests alone are a survivor sample).
  - Repeats of one pull request are not independent deliverables. Replay results are judged separately from real work.
- **M6.** Move the labs fully onto sumbi, widen the support matrix, add more template languages, and release publicly.

## Risks

| Risk | Response |
|---|---|
| Gaming: avoiding hard work, splitting deliverables, straddling windows | Deliverables fixed at dispatch, cohort verdicts, immediate rejection of splitting or of dropping failures |
| Small samples | Pre-computed margins and sample sizes; honest *withhold*; M5 replay |
| Log format drift | Synthetic fixtures per agent version; unknown formats warn and count in the coverage report |
| Privacy and secrets | Allow-listed categories and pseudonymous IDs only; free text stays local |
| Remote execution | Pinned hashes, allowed hosts and paths, no credentials in arguments |
| Over-generalization | Project specifics live in plugins, config and examples; the support matrix keeps promises narrow |

## Review record

A model from a different family reviewed draft 1. Verdict: proceed with changes — 2 P0, 7 P1, 1 P2, all adopted.

| Finding | Where draft 2 addresses it |
|---|---|
| P0: no deliverable registry or observation window | Invariants 1 and 3 |
| P0: completeness of attributed spend | Invariant 4, [Data model and linking contract](#data-model-and-linking-contract) |
| P1: time definitions | "Time" under the linking contract |
| P1: parity with old collectors is not correctness | M1 acceptance (hand-computed truths) |
| P1: overstated certainty | Invariants 5 and 6, M5 |
| P1: privacy boundary | Invariant 7, [Risks](#risks) |
| P1: propagation scope and gates | [Propagate](#3-propagate--sumbi-notice-sync) |
| P1: milestone order | Measurement moved before propagation |
| P1: unfair prediction baseline | Kept in the lab ledger, outside this repository |
| P2: reuse standards, mark unverified claims | Standards reused; the support matrix marks each claim |
