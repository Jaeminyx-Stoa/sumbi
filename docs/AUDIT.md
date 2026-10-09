# Automatic harness audit (design proposal, draft 1)

Status: proposal. Nothing here is implemented yet. It connects the existing pieces into a loop that runs where sumbi is installed:
- measurement: `collect`, `deliver`, and goal events from [CONVERGENCE.md](CONVERGENCE.md);
- reviewed harness changes: `improve` ([IMPROVE.md](IMPROVE.md));
- judging: `register` and `compare`.

## Problem

Today a person or a sumbi maintainer reads the measurements, recognizes a failure pattern and writes the harness change by hand. Three manual case studies of stalled agent runs needed the same steps:
1. count the friction;
2. name the pattern;
3. map it to a harness device;
4. predict the effect;
5. compare before and after.

Steps 1, 2 and 5 are mechanical. Steps 3 and 4 are mechanical once the pattern is known. Writing the exact change still needs judgement in the repository's context.

## Loop

1. **Observe.** On a schedule, run `collect` (and `deliver` or goal events where configured) over a rolling window. Write the report locally.
2. **Detect.** Deterministic detectors read the report and emit findings. A finding records:
   - a detector ID;
   - the evidence pointers and their values;
   - the threshold, window and scope (project and agent);
   - how many consecutive audits have seen it.

   A finding becomes **open** only after it persists for a configured number of audits (default 2), so one noisy window does not trigger changes.
3. **Propose.** Each open finding maps to one or more remedies from a remedy catalog. sumbi writes an improve **bundle draft** with:
   - evidence references, diagnosis ID, prediction and verification declaration filled in;
   - `changes` empty.

   It also writes a short **work order** for the workspace's own agent: which harness targets to change, the mechanism to aim for, and the guide questions from `improve --guide`. Drafts and work orders stay local under `.sumbi/audit/`.
4. **Author.** The workspace's agent or owner writes the exact harness change into the bundle and runs `improve` (dry run). It follows the existing review policy and attestations. sumbi never writes the replacement text itself and never touches product source; `improve` refuses non-harness targets.
5. **Register and apply.** sumbi drafts the registration from the remedy's prediction and the default windows. The owner approves, then `improve --registration ... --apply` applies the change and records the exposure time.
6. **Judge.** When the windows mature, the scheduled audit runs `compare` and proposes a verdict. On `reject` it proposes `improve --revert`. On `owner decides` or `withhold` it reports why.
7. **Learn.** sumbi keeps a local, descriptive record of each remedy: tried, adopted, rejected or withheld. Audits rank remedies with that record before proposing.

Nothing is applied without an owner-approved bundle. sumbi automates finding, drafting, registering and judging. It does not automate authorship or approval.

## Detectors (v1)

Every detector reads fields that `collect` already reports. Thresholds are defaults that owners can override in `.sumbi/config.toml` under `[audit]`.

| ID | Evidence | Default open condition | Remedy family |
| --- | --- | --- | --- |
| `hook-friction` | `hook_denials` denials per 100 tool calls; `first_call_denials` share | ≥ 2 per 100, or first-call share ≥ 0.3 | State the accepted command forms where the agent first reads them; make a denial message name the accepted form |
| `context-bloat` | `delegation.context` share of tokens in requests above 200k | ≥ 0.5 | A context budget for dispatched work: split, summarize, or hand off at the limit |
| `model-unrequested` | `delegation.model_request` share of dispatched tokens on unrequested models | ≥ 0.3 | Require an explicit model and effort choice in dispatch guidance |
| `workaround-scripts` | `delegation.workaround_friction` scripts outside the checkout per dispatched session; tool failure rate | ≥ 1 per session, or ≥ 0.1 | Provide the missing harness tool, or document the environment trap |
| `human-wait` | `summary.counts.user_input_requests` per session | ≥ 3 | Collect prerequisites (logins, accounts, approvals) once at dispatch |
| `compaction-churn` | `summary.counts.compactions` per active hour | ≥ 1 | Bound session length: handoff and new session at a goal boundary |
| `stall` | Goal events: stall episodes over budget without escalation; otherwise, sessions with compactions ≥ 5 and no linked success in `deliver` | ≥ 1 | Goal device: a runnable check, a stall budget, and escalation to the owner |
| `authority-conflict` | Goal events: stale-version effort or unowned decisions | > 0 | One goal version referenced by every channel |

Detectors work only on the categories and counts already in reports. A detector whose evidence is missing does not fire; it reports `unobserved`.

## Remedy catalog

Remedies extend the existing practice catalog (`sumbi/catalog/`). A remedy holds these fields, and never product changes:
- `id` and `detector`;
- `targets`: harness target kinds, such as instruction files, agent configuration, automations and owner-declared harness tools;
- `mechanism`: a device-first description;
- `prediction`: metric, direction, rough size;
- `verification`: what must be checked before apply;
- `cautions`: gates the change must not weaken.

Device remedies come before instruction text, following [DESIGN.md](DESIGN.md) invariant 8.

## Evidence pointers

`improve` currently accepts summary pointers only. The audit adds read-only pointers for the fields the detectors use:
- `/hook_denials/summary/...`;
- `/delegation/summary/...`;
- goal-event aggregates once G1 of CONVERGENCE.md lands.

Pointers stay allow-listed and numeric.

## Running by itself

Opt-in at install time. `sumbi install --apply` with an `audit-loop` practice adds three things:
- a scheduler entry for the host: an OS scheduled task or cron line, or an agent automation where the host supports one. It runs the audit at a configured interval (default daily), read-only and local.
- a short instruction for the workspace's agent: when `.sumbi/audit/` holds an open work order, complete it with `improve` before unrelated harness edits, and stop when the owner declines.
- `[audit]` defaults in `.sumbi/config.toml`.

The scheduler entry is removable with `install --revert`, like any applied practice.

## Command surface

To be decided by the owner. The working assumption is flags on the existing `improve` command:
- `--audit`: observe, detect and draft;
- `--judge`: compare registered changes and propose verdicts.

This avoids a new top-level command.

## Privacy and safety

- Findings, drafts and work orders contain categories, counts, pseudonymous IDs and target kinds. Work orders may name repository-relative harness paths and stay local.
- No product targets, no automatic apply, no automatic approval, no new runtime dependencies.
- An audit never changes approvals, money-path gates or review requirements.

## Milestones

- **A1: detectors and report.** Run `--audit` against a collect report, with the v1 detectors, persistence and the local findings file. Accept: hand-computed findings on synthetic reports, including unobserved evidence, boundary values and persistence across two audits.
- **A2: drafts.** Remedy catalog entries, bundle drafts, work orders and registration drafts. Accept: a draft passes `improve --dry-run` once a synthetic change is filled in, and registration drafts validate.
- **A3: running by itself.** The `audit-loop` practice and scheduler entries for Windows and POSIX, with idempotent install and revert. Accept: an audit runs on schedule in a temporary home, and revert removes it.
- **A4: judging.** `--judge` runs `compare` for matured registrations and proposes verdicts and reverts. Accept: hand-built registrations reach the expected proposals.
- **A5: first real installation.** Install in one workspace that has a documented stalled run and confirm that the audit finds the patterns found by hand. Real data stays outside the repository; only summary numbers go into the report.

## Risks

| Risk | Response |
| --- | --- |
| Noisy findings trigger needless changes | Persistence across audits; owner-tunable thresholds; one to three interventions per round |
| Remedies that only add instruction text | Device-first catalog; the `improve --guide` questions; judging can reject and revert |
| Silent product edits | `improve` refuses non-harness targets |
| Scheduler side effects | Opt-in; read-only audit; removable through revert |
