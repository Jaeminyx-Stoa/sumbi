# Goal convergence (design proposal, draft 1)

Status: proposal for review. Nothing here is implemented yet. It extends [DESIGN.md](DESIGN.md) invariant 1 (deliverables are fixed with acceptance criteria at dispatch) from long-running, multi-session work.

## Problem

Long autonomous runs can stay busy without approaching their goal. One observed run went for more than a day. Its harness constrained actions tightly: frozen command forms, read limits, run budgets and hooks. The run still produced nothing acceptable. Three failure modes combined:

1. **Two authorities.** The goal was stated in more than one place: an agent-side goal command, the brief, and later decisions by the dispatcher. They disagreed. The agent chose one and kept going.
2. **An acceptance check that could not run.** Part of the acceptance criterion needed paths the brief forbade. The criterion could never pass, whatever the agent did.
3. **No stop.** The agent did not stop when the goal was out of reach. It split the work into more and more audits, plans and repairs.

Action rules cannot catch any of these. They answer "may the agent do this?" and never "is the work getting closer to the goal?" Adding a rule per failure, such as a brief-template sentence, grows friction and leaves the next variant uncaught. sumbi's own early parser work showed the same pattern at small scale. Each change passed its acceptance tests, which used synthetic fixtures only, and still failed on the first real logs. No check on real data was part of the goal.

## Principles

These constrain how goals are expressed and observed, not what agents may do.

1. **A goal is a runnable check, not prose.**
   - The acceptance criterion is a check that the assignee can run within its own permissions and data access. It returns pass/fail and, where useful, a numeric value.
   - Before dispatch, the check runs once on the current state to record a baseline. If it cannot run, the goal is not dispatchable and goes back to its owner.
   - A check that needs a scarce or paid resource is first exercised in a free dry run that proves the collection path.
2. **One goal, one owner, one current version.**
   - A goal has an ID, a version and an owner. Every channel that carries the goal (agent goal commands, briefs, contracts, decision logs) refers to `goal_id@version` instead of restating it.
   - A mismatch stops the assignee.
   - Only the owner changes a goal, by issuing a new version. The assignee never narrows or widens the criterion on its own.
3. **Progress is the check's trajectory, not the agent's report.**
   - The harness records check results over time.
   - If effort since the last improvement exceeds a budget, the harness stops the work and escalates to the owner with the trajectory. Effort here means elapsed time, tokens or sub-dispatches.
   - The agent's own judgement of futility is not relied on.

The harness implements these as devices: a dispatch-time check run, version matching and a convergence monitor. sumbi does not enforce them. It measures whether they hold and whether adopting them improves outcomes.

## What sumbi adds

### Events (sumbi-events v1 extension)

New record types in [EVENTS.md](EVENTS.md) format. They carry no free text: IDs are opaque strings, results are fixed labels or numbers, and reasons are fixed classes.

| Type | Fields | Emitted when |
| --- | --- | --- |
| `goal_declared` | `goal_id`, `version`, `owner_id`, `check_id`, `direction` (`pass` or `higher` or `lower`), optional numeric `target` | The owner creates or revises a goal |
| `goal_reference` | `goal_id`, `version` | A session starts work under a goal: dispatch, resume or handoff |
| `goal_check` | `goal_id`, `version`, `check_id`, `result` (`pass`, `fail`, `unrunnable`), optional `value`, optional `unrunnable_class` (`permission`, `scope`, `data`, `resource`, `environment`, `other`), `scope` (`baseline` or `progress` or `final`) | The check runs |
| `goal_escalation` | `goal_id`, `version`, `class` (`stalled`, `unrunnable`, `conflict`, `budget`, `other`) | The harness or agent stops and hands the goal to the owner |
| `goal_decision` | `goal_id`, `from_version`, `decision` (`revise`, `continue`, `abandon`, `accept`), optional `to_version` | The owner answers an escalation or closes the goal |

Sessions remain the existing `session_id`. A goal can span many sessions and agents through `goal_reference` and the existing parent links.

Native adapters can infer a weak subset without these events, for example verification executions as unlabeled checks, as `local-verify` does today. Inferred checks are reported separately and never establish goal identity or versions.

### Measurements (`sumbi collect`, new `goals` section)

Per goal, then aggregated by project and owner pseudonym:

- **Dispatch readiness:**
  - share of goals whose first `goal_reference` was preceded by a runnable `baseline` check;
  - `unrunnable` baselines by class.
- **Time and tokens to first check** after dispatch.
- **Convergence:**
  - best value and pass state over time;
  - effort (time, tokens, sessions, sub-dispatches) from dispatch to first pass.
- **Stall effort:** effort spent since the last improvement.
  - `stall_episodes`: effort beyond a configured budget without improvement.
  - The share of stall episodes that ended in a `goal_escalation`, against those that continued.
- **Authority conflicts:**
  - sessions working under a non-current version, and the effort they spent;
  - references to undeclared goals.
- **Escalation latency:** time from `goal_escalation` to `goal_decision`, and the effort spent meanwhile.
- **Outcome:** `reached`, `abandoned` (owner decision), `unresolved`, and cost per reached goal under the existing cost rules.

All figures are counts, durations, token kinds and pseudonymous IDs. Check commands, criteria text and values' meaning stay with the harness.

### Judging

A later milestone adds an outcome source, `goal-check`. A goal succeeds when its final check passes at a version the owner accepted, and the owner's decision record exists. The source follows invariant 2: it is machine evidence, declared up front, and never mixed between arms. Registrations can then judge harness changes, such as adopting a dispatch-time check or a convergence monitor, by reached-goal rate and cost per reached goal. Stall effort and authority-conflict effort are pre-registered secondary metrics.

Until then, comparisons use the existing outcome sources. Goal metrics serve as registered predictions and are reported descriptively.

## Milestones

- **G1: events and collect.**
  - Scope: the five event types, adapter validation, the `goals` section and text lines, synthetic fixtures for every failure mode above (two authorities, unrunnable baseline, stall without escalation, escalation with decision, revision by the owner).
  - Accept 1: hand-computed truths on fixtures.
  - Accept 2: unknown or conflicting goal events become coverage gaps, never favourable cohorts.
  - Accept 3: a replay against one real harness that emits these events reproduces that harness's own records, or explains every difference.
- **G2: `goal-check` outcome source and registrations.** Accept: a hand-built round table is reproduced.
- **G3: practice catalog entries** describing the three devices, with references to measured effects once registered comparisons exist. The entries are devices for harness owners, not instruction text for agents.

## Risks

| Risk | Response |
| --- | --- |
| Goals gamed by easy checks | Owners declare checks; sumbi reports check changes across versions and judges reached goals only under owner-accepted versions |
| Emitters disagree on IDs | Same rules as sessions: conflicting events invalidate both versions |
| Over-stopping productive exploration | Budgets are the harness's choice; sumbi reports both stall effort and post-escalation decisions (`continue` shows a stop that was not needed) |
| Free text leaking through IDs | IDs are opaque; reports use pseudonyms; validation rejects text-like fields |
