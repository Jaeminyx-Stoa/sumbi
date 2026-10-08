# Goal convergence (design proposal, draft 4)

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
   - A visible, immutable check can still be overfitted, or can miss requirements it does not measure. Representativeness is the owner's responsibility: the owner reassesses it at every revision, and the check is validated independently of the assignee.
2. **One goal, one owner, one current version.**
   - Every channel that carries the goal (agent goal commands, briefs, contracts, decision logs) refers to `goal_id@version`. A mismatch stops the assignee.
   - Only the owner revises a goal, by issuing a new version.
   - A revision never rewrites history. Work dispatched under the old version keeps that version's outcome.
3. **Progress is the check's trajectory, not the agent's report.**
   - The harness records check results over time.
   - Effort is elapsed time, tokens or sub-dispatches. When effort since the last qualifying improvement exceeds a budget, the harness stops the work and escalates to the owner with the trajectory.
   - A binary check has no intermediate trajectory. For binary checks, the budget measures effort since dispatch or since the first pass. A regression followed by a repeated pass does not restart it.
   - Escalation needs an owner who answers. Each goal declares an owner-response deadline and a cumulative budget across `continue` decisions. Effort after an escalation stays observable, because an escalation record alone does not prove that work stopped.

The harness implements these principles. sumbi measures whether they hold and whether adopting them improves outcomes. None of them replaces approvals, cross-family review or money-path gates. An owner's acceptance is not one of those gates.

## Relation to deliverables and outcome sources

- **A goal is a planning unit; a deliverable is the judged unit.**
  - The judging link is made at dispatch: a `goal_reference` with `role: dispatch`, `state: start` and a `deliverable_id`. Each deliverable links exactly one goal version, fixed at dispatch.
  - Parent links between sessions and references without a `deliverable_id` support descriptive goal metrics only.
  - A revision makes new attempts carry a new `deliverable_id`. The original deliverable keeps its own outcome and costs, so a revision can never add a success to the same deliverable.
  - Costs belong to deliverables through the existing event-time allocation. Effort that cannot be allocated stays unassigned and is never duplicated.
- **Goal metrics are descriptive by default.** They never override `github`, `worker-github` or `local-verify` outcomes. The `goal-check` outcome source ([Judging](#judging)) is opt-in per registration, and arms cannot mix it with other sources.

## Events (sumbi-events v1 extension)

The new record types inherit the v1 envelope and rules. A record's `timestamp` is the moment it describes; for a check, that is its completion. The rules: `schema_version`, a unique immutable `event_id`, `session_id`, timezone-bearing `timestamp`, idempotent replay, disjoint adapter observations and conservative coverage. Every ID is opaque and pseudonymized with the existing salt policy before any report. Numeric `value` and `target` stay in local outputs unless a registration declares the measurement publishable.

| Type | Required fields | Optional fields |
| --- | --- | --- |
| `goal_declared` | `goal_id`, `version` (integer, strictly increasing per goal), `owner_id`, `check_id`, `check_digest` (covers check implementation, data selection and environment pin), `direction` (`pass`, `higher` or `lower`), `retry_policy` (`attempts` per evaluated state, `aggregate`: `all`, `majority` or `last`), `min_improvement` (non-negative), `stall_budget` (value and unit), `cumulative_budget` (value and unit), `response_deadline_seconds`, `baseline_freshness_seconds` | `target` (required unless `pass`), `supersedes_version` |
| `goal_reference` | `goal_id`, `version`, `role` (`dispatch`, `resume` or `handoff`), `state` (`start` or `end`) | `deliverable_id` |
| `goal_check` | `goal_id`, `version`, `check_id`, `check_digest`, `run_id`, `aggregate_id`, `attempt`, `started_at`, `completed_at`, `evaluated_state` (an opaque digest of the evaluated tree or artifact), `scope` (`baseline`, `progress` or `final`), `completeness` (`complete` or `partial`), `result` (`pass`, `fail`, `unrunnable` or `error`) | `value` (finite number, required when `direction` is not `pass`), `unrunnable_class` (`permission`, `scope`, `data`, `resource`, `environment` or `other`) |
| `goal_escalation` | `goal_id`, `version`, `class` (`stalled`, `unrunnable`, `conflict`, `budget` or `other`), `last_check_event_id` (null before any check) | `effort_since_improvement` (seconds, tokens, dispatches) |
| `goal_decision` | `goal_id`, `from_version`, `decision` (`revise`, `continue`, `abandon` or `accept`), `actor_id` | `to_version` (required if and only if `revise`), `escalation_event_id` (required when answering an escalation), `accepted_aggregate_id` (required for `accept`) |

**State machine and validity**
- **Current version.** A version becomes current at its `goal_declared` timestamp and stays current until a later version is declared. Events whose time is unknown cannot select a version; they are gaps.
- **Conflicting declarations.** Two declarations of one `goal_id@version` with different contents invalidate that version's evidence, even when their event IDs differ.
- **Orphans.** References, checks and decisions that point to undeclared versions are orphans and count as gaps.
- **Check identity.** A `check_id`/`check_digest` pair on a check must match the declared one. A mismatch is a conflict.
- **Comparing values.** Values are compared only within one `check_digest`. A new digest needs a fresh baseline.
- **Pass and value.** `pass` must agree with `value` and `target` when both are present.
- **Retries.** Retries count only under the declared `retry_policy`. A result produced by extra attempts is recorded but cannot pass.
- **Freshness.** A pass certifies only the `evaluated_state` it checked. Edits after `started_at` are not covered.
- **Attempts and aggregates.** A check run consists of attempt events. Attempts that share one `aggregate_id` form one aggregate evaluation of one evaluated state.
  - The aggregate is complete when it has the declared number of `complete` attempts and none of them is `error` or `unrunnable`. Otherwise it is incomplete: it has no result and cannot pass.
  - The aggregate result follows `retry_policy.aggregate`: `all` requires every attempt to pass; `majority` requires more passes than fails; `last` takes the last attempt.
  - The aggregate value is the worst attempt value for `all`, the median for `majority`, and the last attempt's value for `last`.
  - Readiness, progress, invalidation and acceptance all act on complete aggregates, never on single attempts.
- **Later results win.** A later complete aggregate on the current goal version that does not pass cancels an earlier passing aggregate.
- **Decision authority.** Decisions name an `actor_id`. A decision whose actor is not the declared owner is invalid: it changes no state, cannot establish success, and counts as `unowned_decision`. sumbi-events still cannot authenticate emitters (a v1 limitation).
- **IDs and ordering.** `goal_id` is unique in a log home; `run_id` is unique per goal version; owner and actor IDs share one namespace per home. Events with equal timestamps whose order changes a state are ambiguous. Ambiguous orderings and illegal transitions (dispatch before declaration, a reference `end` without `start`, a decision without a valid escalation or check link) are gaps and never resolve in the favourable direction.
- **Aggregates.** Attempts are bounded per evaluated state and version, across runs; a new `run_id` on the same state does not reset them. An aggregate result needs `complete` attempts on one evaluated state; missing or `error` attempts make it incomplete.
- **Acceptance binding.** An `accept` binds to one complete `final` aggregate and its evaluated state. It is valid only if no later complete aggregate on that goal version fails to pass.

## Measurements (`sumbi collect`, `goals` section)

**Definitions**
- **Dispatch:** a goal version's first valid `goal_reference` with `role: dispatch`.
- **Readiness:** whether the latest `baseline` check before dispatch, of the same version and `check_digest`, was `complete` with result `pass` or `fail`, and completed within the declared `baseline_freshness_seconds`. `error`, `partial` and `unrunnable` baselines do not count. Goals that were never dispatched because their baseline was unrunnable are reported separately.
- **First check:** the first `progress` or `final` check after dispatch.
- **Improvement:** a result strictly better than the best earlier comparable result (same `check_digest`) by more than `min_improvement`, or the first `pass`. Oscillation and repeated equal results do not qualify.
- **Stall episode:** starts at the last improvement, or at dispatch. It ends only at an improvement or the observation cutoff. Escalations and `continue` decisions do not end it; effort after them is reported separately. Resumes, handoffs and revisions do not reset it. The cumulative effort of the goal and of the original deliverable is kept across all of them.
- **Stale-version effort:** effort by sessions whose active reference names a version that is no longer current.
- **Escalation latency:** an escalation pairs only with a decision that names it in `escalation_event_id`. Unpaired escalations stay pending and are censored at the cutoff. Decisions after `response_deadline_seconds` are reported as late.
- **Outcomes:**
  - `reached`: a valid `accept`.
  - `abandoned`: a valid `abandon`.
  - `superseded`: a revision replaced the version before acceptance.
  - `unresolved`: none of these by the cutoff. Unresolved goals are censored, never zero-cost failures or successes.
  - Cost and time endpoints are the deciding event, or the cutoff for censored goals.

**Windows and allocation**
- Cohorts are selected by dispatch time, with an explicit observation cutoff. Operational effort is clipped to the window.
- Earlier state is carried in: declarations, active references, dispatches, the best comparable result and the last improvement, terminal decisions and pending escalations.
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
- Share of stall episodes over budget that were escalated after exceeding the budget.
- Stale-version effort.
- Escalation latency and pending escalations.
- Outcome counts: `reached`, `abandoned` and `superseded`, with censored goals counted separately.
- Cost per reached goal, under the existing lifetime cost rules.

**Anti-gaming**
- The enrolled units and the evaluation policy (check digest, retry policy, budgets) are frozen at dispatch.
- Abandoned and revised work keeps all of its costs. Criterion revisions are counted and shown next to outcomes.
- Check frequency is reported, so frequent checks or noise below `min_improvement` cannot manufacture convergence.

## Judging

The opt-in `goal-check` outcome source is a weaker machine-evidence source under invariant 2. The producer's label is the evidence, so registrations must say so.

- **Unit:** one deliverable with its dispatch-fixed goal version. To keep units independent, a registration judges one deliverable per goal: the first one dispatched. Other deliverables of the same goal stay descriptive. Cluster-aware inference is out of scope until M2 supports it.
- **Success:** a valid owner `accept` bound to a `final` pass on the dispatched version, within the follow-up window. The window is anchored at dispatch, like M2 cohorts.
- **Terminal non-success:** `abandoned` or `superseded`. Censored (`unresolved`) units stay pending; like M2's pending-work gate, they withhold the final verdict.
- **Statistics:** the existing M2 engine, with pre-registered margin and sample size, Wilson and Newcombe intervals, whole-unit bootstrap, and the ordered coverage, comparability, success and cost gates. Lifetime cost includes failures, retries and revisions.
- **Dependence:** goals that share a deliverable or a session are not independent samples. Until cluster-aware inference exists, comparisons containing such units are withheld; they are never fed to Wilson, Newcombe or the independent bootstrap.
- **Withheld verdicts:** any of these withhold a verdict:
  - coverage gaps and missing events;
  - a change of check digest within an arm;
  - unequal observation horizons;
  - a change of the owner's acceptance policy between arms;
  - check classes, targets or retry policies that are not comparable between arms. Registrations name the check classes they compare.

  The existing all-session exposure, metadata and coverage gates apply unchanged.

## Milestones

- **G1: events and collect.**
  - Scope: validation of the five types, the state machine, and the `goals` section and text lines.
  - Synthetic fixtures for every failure mode above, plus window boundaries, multi-session goals, goal switches and shared costs, duplicates and conflicts, missing times, revisions, partial and flaky checks, unowned decisions and privacy.
  - Read-only, input-order-independent replay.
  - Accept 1: fixture outputs match hand-computed truths, including these cases: error baselines, tied transitions, incomplete aggregate runs, edits after a pass, non-owner accepts, repeated runs on one state, and stall episodes that continue from before the window.
  - Accept 2: malformed, conflicting, orphaned or unknown-time evidence creates visible gaps. It never establishes readiness or success, and it blocks affected verdicts. Missing cost evidence stays incomplete.
  - Accept 3: a replay against one real producer reconciles every difference into a named category, with no unresolved difference. The replay independently traces baseline and acceptance evidence through the representative path under assignee permissions; matching producer labels is not a correctness oracle. Real records and results stay outside version control.
- **G2: `goal-check` outcome source.** Accept: hand-built cases for every gate, withheld verdicts (including censoring and several deliverables per goal) and the statistical edge cases, not only a happy-path table.
- **G3: practice catalog entries** describing the three devices. Accept: catalog validation passes, and each entry cites registered comparison evidence with provenance before it claims an effect.

## Risks

| Risk | Response |
| --- | --- |
| Invalid proxy checks | Representative data, immutable checks, a fresh baseline per digest, and reporting of check changes |
| Gaming by revisions, splitting or retries | Units and policy frozen at dispatch; revisions and retries kept with their costs; clustering |
| Over-stopping productive exploration | Budgets are the harness's choice. sumbi reports stall effort and post-escalation decisions. A `continue` decision does not prove the stop was unnecessary. |
| Emitters disagree or impersonate owners | v1 conflict rules; `unowned_decision`; unauthenticated emitters remain a stated limit |
| Sensitive values or names | Pseudonymized IDs; values, targets and `min_improvement` local unless declared publishable |

## Review record

Draft 1 was reviewed by a model from a different family. Verdict: proceed with changes (7 P1, 1 P2), all adopted in draft 2. The second review requested changes (6 P1, 2 P2), all adopted in draft 3:
- owner deadlines and cumulative budgets;
- the deliverable unit and its dispatch link;
- invalid non-owner decisions, ID namespaces and illegal transitions;
- check digests, aggregates and acceptance binding;
- strict readiness, outcomes and carried state;
- best-result improvement and cumulative stall effort;
- censoring as pending, one judged deliverable per goal, and comparability gates;
- the G1 and G2 acceptance cases.

The third review requested one change (P1): define whether invalidation and acceptance act on attempts or on aggregates. Draft 4 defines aggregate evaluations and binds both to them. Its P2 details go to the G1 implementation specification. These include exact field validation, metric denominators, switch-boundary allocation, accounting of revision costs, digest pseudonymization and the additional hand-computed G1 cases.

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
