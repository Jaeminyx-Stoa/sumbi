# Goal convergence (design proposal, draft 2)

Status: proposal; nothing here is implemented yet. It extends [DESIGN.md](DESIGN.md) invariant 1 (deliverables are fixed with acceptance criteria at dispatch) to long-running work that spans many sessions. Draft 1 was reviewed by a model from a different family. See [Review record](#review-record).

## Problem

Long autonomous runs can stay busy without approaching their goal. One observed run went for more than a day. Its harness constrained actions tightly: frozen command forms, read limits, run budgets and hooks. The run still produced nothing acceptable. Three failure modes combined:

1. **Two authorities.** The goal was stated in more than one place: an agent-side goal command, the brief, and later decisions by the dispatcher. They disagreed. The agent chose one and kept going.
2. **An acceptance check that could not run.** Part of the acceptance criterion needed paths the brief forbade. The criterion could never pass, whatever the agent did.
3. **No stop.** The agent did not stop when the goal was out of reach. It split the work into more and more audits, plans and repairs.

A fourth mode belongs to the same family:

4. **A runnable but invalid proxy.** A check that runs and passes without meeting the intended outcome. Examples: synthetic-only data, a stale environment, or a check the assignee can modify. sumbi's own early parser changes passed fixture-only acceptance tests and failed on the first real logs.

Action rules cannot catch any of these. They answer "may the agent do this?" and never "is the work getting closer to the goal?" Adding a rule per failure grows friction and leaves the next variant uncaught.

## Principles (harness-side devices)

1. **A goal is a runnable, representative check.**
   - The acceptance criterion is a check that the assignee can run within its own permissions and data access.
   - The check must exercise the real acceptance path on representative data. A check that only proves its collection path is a dry run, not acceptance. The assignee cannot modify the check or its data.
   - Before dispatch, the check runs once on the current state to record a baseline. If it cannot run, the goal goes back to its owner.
   - A change of environment, data or check implementation requires a new baseline.
2. **One goal, one owner, one current version.**
   - Every channel that carries the goal (agent goal commands, briefs, contracts, decision logs) refers to `goal_id@version`. A mismatch stops the assignee.
   - Only the owner revises a goal, by issuing a new version.
   - A revision never rewrites history. Work dispatched under the old version keeps that version's outcome.
3. **Progress is the check's trajectory, not the agent's report.**
   - The harness records check results over time.
   - Effort is elapsed time, tokens or sub-dispatches. When effort since the last qualifying improvement exceeds a budget, the harness stops the work and escalates to the owner with the trajectory.
   - A binary check has no intermediate trajectory. For binary checks, the budget measures effort since dispatch or since the last change of check result.

The harness implements these principles. sumbi measures whether they hold and whether adopting them improves outcomes. None of them replaces approvals, cross-family review or money-path gates. An owner's acceptance is not one of those gates.

## Relation to deliverables and outcome sources

- **A goal is a planning unit; a deliverable is the judged unit.**
  - A deliverable or attempt can name one goal version as its acceptance criterion. The link must be explicit, through the brief, the contract or a `goal_reference`.
  - Parent links between sessions are not enough.
  - One goal can have several deliverables. A deliverable has at most one goal version.
- **Goal metrics are descriptive by default.** They never override `github`, `worker-github` or `local-verify` outcomes. The `goal-check` outcome source ([Judging](#judging)) is opt-in per registration, and arms cannot mix it with other sources.

## Events (sumbi-events v1 extension)

The new record types inherit the v1 envelope and rules: `schema_version`, a unique immutable `event_id`, `session_id`, timezone-bearing `timestamp`, idempotent replay, disjoint adapter observations and conservative coverage. Every ID is opaque and pseudonymized with the existing salt policy before any report. Numeric `value` and `target` stay in local outputs unless a registration declares the measurement publishable.

| Type | Required fields | Optional fields |
| --- | --- | --- |
| `goal_declared` | `goal_id`, `version` (integer, strictly increasing per goal), `owner_id`, `check_id`, `check_digest`, `direction` (`pass`, `higher` or `lower`), `retry_policy` (`attempts`, `aggregate`: `all`, `majority` or `last`), `min_improvement` (non-negative number; 0 for `pass`) | `target` (required unless `pass`), `supersedes_version` |
| `goal_reference` | `goal_id`, `version`, `role` (`dispatch`, `resume` or `handoff`), `state` (`start` or `end`) | `deliverable_id` |
| `goal_check` | `goal_id`, `version`, `check_id`, `check_digest`, `run_id`, `attempt`, `started_at`, `completed_at`, `evaluated_state` (an opaque digest of the evaluated tree or artifact), `scope` (`baseline`, `progress` or `final`), `result` (`pass`, `fail`, `unrunnable` or `error`) | `value` (finite number), `unrunnable_class` (`permission`, `scope`, `data`, `resource`, `environment` or `other`) |
| `goal_escalation` | `goal_id`, `version`, `class` (`stalled`, `unrunnable`, `conflict`, `budget` or `other`), `last_check_event_id` | `effort_since_improvement` (seconds, tokens, dispatches) |
| `goal_decision` | `goal_id`, `from_version`, `decision` (`revise`, `continue`, `abandon` or `accept`), `actor_id` | `to_version` (required if and only if `revise`), `escalation_event_id` |

**State machine and validity**
- **Current version.** A version becomes current at its `goal_declared` timestamp and stays current until a later version is declared. Events whose time is unknown cannot select a version; they are gaps.
- **Conflicting declarations.** Two declarations of one `goal_id@version` with different contents invalidate that version's evidence, even when their event IDs differ.
- **Orphans.** References, checks and decisions that point to undeclared versions are orphans and count as gaps.
- **Check identity.** A `check_id`/`check_digest` pair on a check must match the declared one. A mismatch is a conflict.
- **Comparing values.** Values are compared only within one `check_digest`. A new digest needs a fresh baseline.
- **Pass and value.** `pass` must agree with `value` and `target` when both are present.
- **Retries.** Retries count only under the declared `retry_policy`. A result produced by extra attempts is recorded but cannot pass.
- **Freshness.** A pass certifies only the `evaluated_state` it checked. Edits after `started_at` are not covered.
- **Later results win.** A later `fail`, `error` or `unrunnable` on the current state cancels an earlier pass.
- **Decision authority.** Decisions name an `actor_id`. A decision whose actor is not the declared owner is reported as `unowned_decision`. sumbi-events still cannot authenticate emitters (a v1 limitation).

## Measurements (`sumbi collect`, `goals` section)

**Definitions**
- **Dispatch:** a goal version's first valid `goal_reference` with `role: dispatch`.
- **Readiness:** whether that dispatch was preceded by a `baseline` check of the same version and `check_digest`. The check must have completed before dispatch, inside a freshness bound (default 24 h), and must not be `unrunnable`. Goals that were never dispatched because their baseline was unrunnable are reported separately.
- **First check:** the first `progress` or `final` check after dispatch.
- **Improvement:** a value change of at least `min_improvement` in `direction`, or a change from `fail` to `pass`.
- **Stall episode:** starts at the last improvement, or at dispatch. It ends at an improvement, an escalation, a decision or the observation cutoff. Resumes and handoffs do not reset it, and neither do new versions without a fresh baseline.
- **Stale-version effort:** effort by sessions whose active reference names a version that is no longer current.
- **Escalation latency:** each escalation pairs with the next decision that names it, or with the next decision on the same goal version. Unpaired escalations stay pending and are censored at the cutoff.

**Windows and allocation**
- Cohorts are selected by dispatch time, with an explicit observation cutoff. Operational effort is clipped to the window.
- Earlier state is carried in: the current version and the last check.
- Goals open at the cutoff are censored. They are never counted as zero-cost successes or failures.
- A reference is active from `start` to `end`, or until the session's next reference.
- Effort inside a session is allocated to goals by event time. Effort that cannot be allocated to one goal stays unassigned, and is never duplicated across goals.
- Elapsed time and summed parallel effort are reported separately. Unknown tokens remain unknown.

**Figures, per goal and aggregated by project and owner pseudonym**
- Readiness rate and the classes of unrunnable baselines.
- Time and tokens to the first check.
- First pass, and effort to first pass.
- Accepted outcome. Revised versions are reported separately from original versions.
- Stall episodes and their effort.
- Share of stall episodes over budget that ended in an escalation.
- Stale-version effort.
- Escalation latency and pending escalations.
- Outcome counts: `reached`, `abandoned`, `unresolved` and censored.
- Cost per reached goal, under the existing lifetime cost rules.

**Anti-gaming**
- The enrolled units and the evaluation policy (check digest, retry policy, budgets) are frozen at dispatch.
- Abandoned and revised work keeps all of its costs. Criterion revisions are counted and shown next to outcomes.
- Check frequency is reported, so frequent checks or noise below `min_improvement` cannot manufacture convergence.

## Judging

The opt-in `goal-check` outcome source is a weaker machine-evidence source under invariant 2. The producer's label is the evidence, so registrations must say so.

- **Unit:** one dispatched goal version linked to a deliverable, fixed at dispatch.
- **Success:** a `final` check passes under the dispatched version and is the last result on the evaluated state, followed by an owner `accept` within the follow-up window.
- **Terminal non-success:** fail, abandon, a revision that replaces the version, or censoring at the cutoff, reported separately.
- **Statistics:** the existing M2 engine, with pre-registered margin and sample size, Wilson and Newcombe intervals, whole-unit bootstrap, and the ordered coverage, comparability, success and cost gates. Lifetime cost includes failures, retries and revisions.
- **Clustering:** goals that share a deliverable or session are clustered. They are not independent samples.
- **Withheld verdicts:** coverage gaps, missing events, a change of check digest within an arm, or unequal observation horizons withhold a verdict. Selective owner acceptance is a confounder that is reported.

## Milestones

- **G1: events and collect.**
  - Scope: validation of the five types, the state machine, and the `goals` section and text lines.
  - Synthetic fixtures for every failure mode above, plus window boundaries, multi-session goals, goal switches and shared costs, duplicates and conflicts, missing times, revisions, partial and flaky checks, unowned decisions and privacy.
  - Read-only, input-order-independent replay.
  - Accept: fixture outputs match hand-computed truths. A replay against one real producer reconciles every difference into a named category, with no unresolved difference. Real records and results stay outside version control.
- **G2: `goal-check` outcome source.** Accept: hand-built cases for every gate, withheld verdicts and the statistical edge cases, not only a happy-path table.
- **G3: practice catalog entries** describing the three devices. Accept: catalog validation passes, and each entry cites registered comparison evidence with provenance before it claims an effect.

## Risks

| Risk | Response |
| --- | --- |
| Invalid proxy checks | Representative data, immutable checks, a fresh baseline per digest, and reporting of check changes |
| Gaming by revisions, splitting or retries | Units and policy frozen at dispatch; revisions and retries kept with their costs; clustering |
| Over-stopping productive exploration | Budgets are the harness's choice. sumbi reports stall effort and post-escalation decisions. A `continue` decision does not prove the stop was unnecessary. |
| Emitters disagree or impersonate owners | v1 conflict rules; `unowned_decision`; unauthenticated emitters remain a stated limit |
| Sensitive values or names | Pseudonymized IDs; values local unless declared publishable |

## Review record

Draft 1 was reviewed by a model from a different family. Verdict: proceed with changes (7 P1, 1 P2), all adopted.

| Finding | Where draft 2 addresses it |
| --- | --- |
| P2: invalid proxies, binary checks, `continue` semantics | Problem mode 4; principles 1 and 3; Risks |
| P1: invariant fit, revisions, gates | Principles 2 and 3; Relation to deliverables and outcome sources |
| P1: event identity, state machine, conflicts | Events: state machine and validity |
| P1: check semantics, freshness, privacy | Events table and state machine; Events intro |
| P1: reproducible metrics, windows, allocation | Measurements: definitions, windows and allocation |
| P1: gaming | Measurements: anti-gaming |
| P1: judging contract | Judging |
| P1: acceptance | Milestones |
