# Evidence-driven local improvements

`improve` connects an existing collect report to a concrete, reviewed harness
change. It can address a new harness defect outside the seed catalog. Product
source stays with the project's own agent and owner: targets are limited to the
execution harness (see [Targets](#targets)). It does not infer causality, generate code, fetch internet candidates,
execute verification commands, or apply a comparison verdict automatically.
An owner or coding agent authors the diagnosis and exact change bundle.

```console
sumbi improve --guide
sumbi improve --guide --json
sumbi improve --guide --evidence collect.json
sumbi improve --evidence collect.json
sumbi improve --root . --evidence collect.json --bundle local-bundle.json
sumbi improve --root . --evidence collect.json --bundle local-bundle.json --json
sumbi improve --root . --evidence collect.json --bundle local-bundle.json --apply --reviewed-sha256 BUNDLE_SHA256
sumbi improve --root . --revert BACKUP_ID
sumbi improve --evidence collect.json --json-out out/observations.json
sumbi improve --root . --register-external ID --applied-at UTC --change LABEL \
  --predict METRIC:DIRECTION:PERCENT --revert-if METRIC:DIRECTION:PERCENT \
  --before-days N --after-days N
sumbi improve --root . --check ID --before before.json --after after.json [--signals FILE] [--project-key KEY]
```

With evidence alone, output is finite numeric observations: sessions, tool/API
errors, compactions, reported tokens and their reported components (new input,
cache writes, cache reads, output, and reasoning output as an output subset),
summed wall time, estimated active time, and unattributed spend share when present. Missing metrics remain missing.
Collect supplies no success outcome or causal diagnosis; even an empty report
can be inspected, but it cannot establish that a change will improve outcomes.
The collect JSON is bounded to 16 MiB and must use the current schema version.

The default bundle action is a read-only proposal. Its JSON summary contains
labels, counts, predictions and hashes. Text mode additionally prints a **local
review diff** containing source/configuration text and relative target paths;
keep that output private. `--json` prints only the allow-listed summary. Bundle
hypotheses, rationale, verification declarations and replacement text stay out
of public summary and apply ledger. Keep bundle/evidence files outside tracked
source or under an explicit local ignore; they are never committed by sumbi.

## Default work approach

Start an improvement round with `sumbi improve --guide`. The installed guide
asks concrete questions about the user outcome, failure-generating decision
structure, reuse/removal, measured constraints, transfer, proportionate safety,
coherent scope and fixed outcome evidence. Its generic example distinguishes a
hardcoded memory-stop patch from evidence-calibrated execution with visible
real-deliverable progress. The same judgment yields a resource pilot only where
needed, a direct diff for a small static edit, and preserved money/security gates.
It adds no mandatory bundle fields or blanket pilot/review procedure.

Use `--guide --json` for public structured guidance. With `--guide --evidence`,
JSON contains `guide` and `observations`; with a bundle, it contains `guide` and
`proposal`. Omitting `--guide` preserves the existing output. The guide reads
no repository or logs by itself and makes no causal diagnosis automatically.

## Bundle v1

This draft example uses placeholders for hashes. Replace them with lowercase
64-character SHA-256 values over exact file bytes, including newlines. Use null
for `before_sha256` only when a target does not exist. Bundle size is at most
1 MiB. JSON must have unique keys and exactly the documented fields.

```json
{
  "version": 1,
  "intervention_id": "capacity-fix",
  "evidence_sha256": "COLLECT_SHA256",
  "evidence_refs": [{"pointer": "/summary/counts/tool_errors", "value": 3}],
  "diagnosis": {
    "id": "capacity",
    "hypothesis": "A bounded capacity mismatch caused the observed failures.",
    "rationale": "Reviewed local failure evidence supports testing this hypothesis."
  },
  "prediction": {
    "metric": "tokens_per_success",
    "direction": "decrease",
    "rough_size_percent": 10
  },
  "verification": {
    "id": "capacity-regression",
    "status": "pending",
    "evidence_sha256": null,
    "declaration": "Verify the proposed capacity change and unchanged private-data guard."
  },
  "review": {
    "author_family": "family-a",
    "reviewer_family": null,
    "status": "pending",
    "safety_gates_preserved": null
  },
  "changes": [{
    "path": "tools/checks/capacity.py",
    "before_sha256": "BEFORE_SHA256",
    "after_sha256": "AFTER_SHA256",
    "after_text": "CAPACITY = 2\n"
  }]
}
```

Evidence references must match numeric values at supported pointers exactly:
`/summary/sessions`, `/summary/counts/tool_errors`, `/summary/counts/api_errors`,
`/summary/counts/compactions`, `/summary/tokens/total`,
`/summary/tokens/new_input`, `/summary/tokens/cache_write`,
`/summary/tokens/cache_read`, `/summary/tokens/output`,
`/summary/tokens/reasoning_output` (an output subset),
`/summary/time/active_seconds/value`, `/summary/time/wall_span_seconds/value`,
and `/coverage/unattributed_share`. Hypothesis and rationale are local reviewed
text, not conclusions computed from those counts.

Collect's [usage profile](MEASUREMENT.md#usage-profile) can also be cited, under
the same exact-number rule, so a bundle can point at one project's workers
rather than home-wide totals. A profile pointer is
`/profile/<scope>/roles/<role>/<leaf>` or `/profile/<scope>/fan_out/<leaf>`, where
`<scope>` is `summary`, `by_agent/<agent>`, `projects/<ref>` or
`projects/<ref>/agents/<agent>`, `<role>` is `orchestrator` or `worker`, and
`<ref>` is the report's project key (or `other`/`unassigned`). Leaves are the
profile's numeric fields: `sessions`, `sessions_per_day`, `tokens/<kind>`,
`tokens_per_day/<kind>`, `session_total_tokens_median`, `counts/<kind>`,
`tool_calls_per_day`, `tool_error_rate`, `compactions_per_day`,
`cache_read_per_tool_call`, `active_seconds`, `active_seconds_per_day`,
`tool_seconds_per_paired_call`, `compaction/<field>`, `long_tail/<field>`,
`concentration/<field>` and `tool_latency/<class>/<field>`, plus fan-out
`parents`, `children`, `children_max` and `children_median`. Model rows are
lists and cannot be cited. A null value cannot be cited. A bundle cites at most
32 pointers.

IDs and family labels use `[a-z][a-z0-9-]{0,63}`. Prediction metrics are
`tokens_per_success`, `time_per_success` or `success_rate`, with direction
`increase` or `decrease` and a finite nonnegative percentage. Verification and
review can stay pending for proposal inspection. Before apply, set verification
to `passed` with its local evidence hash, and review to `approved` with
`safety_gates_preserved: true`. Review defaults to cross-family and requires
distinct author/reviewer family labels. The only optional review field is
`"policy": "owner"` or `"policy": "cross-family"`; owner review permits the same
family or an `owner` reviewer label only where the repository's existing policy
already allows owner review. The tool does not classify a change as low risk.
Required cross-family, money, security, permission and approval gates cannot be
downgraded by selecting owner review. Pending reviews retain null reviewer and
safety fields. Then inspect
and acknowledge the SHA-256 of this final bundle with `--reviewed-sha256`.
These are **owner-local attestations**, not authenticated independent review or
proof of safety. The reviewer evaluates the changed mechanism, transfer to
materially different contexts, reuse/removal of existing work and outcome
evidence. Checklist presence is not evidence of improvement. Preserve approvals,
money-path constraints, permissions and other safety gates; syntactic additions
can weaken them too.

### Targets

Targets must be execution-harness files:

- agent instruction files at any depth: `AGENTS.md`, `AGENTS.override.md`,
  `CLAUDE.md`, `GEMINI.md` and `copilot-instructions.md`;
- harness directories: `.claude/`, `.codex/`, `.agents/`, `.cursor/rules/`,
  `.github/workflows/`, `.github/instructions/`, `.githooks/` and `docs/sumbi/`;
- harness files: `.cursorrules`, `.pre-commit-config.yaml`, `CODEOWNERS`,
  `.github/CODEOWNERS` and `.github/pull_request_template.md`;
- paths the owner declares as harness tools, such as check scripts or launchers,
  in `.sumbi/config.toml`:

  ```toml
  [harness]
  paths = ["tools/checks/**", "scripts/launch_worker.ps1"]
  ```

  The paths are repository-relative globs with the same syntax as `exclude`.

Everything else, including product source and general documentation, is refused.
Within those paths a bundle supports up to 64 exact UTF-8 targets as a resource
bound, with the suffixes `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.json`, `.toml`,
`.yaml`, `.yml`, `.md`, `.txt`, `.ini`, `.cfg` and `.sh`. There are no file deletions
or executable-mode changes. Keep each round to one to three interventions; a
coupled intervention may require more than three source files. Target text
rejects C0/C1 and DEL controls except tabs, LF and strict CRLF. Text and target
paths reject Unicode bidirectional embedding/override/isolate controls. A line-ending-only change
prints an explicit local review note because unified diffs normalize CRLF.
Traversal, symlinks, hardlinks, metadata, nested repositories, ignored/excluded paths and known secret/session-log areas are
refused. This path filter is not a secret scanner: review target contents too.

Apply re-reads bundle, evidence and target bytes under the existing install
lock, then uses the install transaction, backups and rollback. The supplied
collect evidence is the baseline; improve never rescans agent logs. A reused
intervention ID is refused. Revert restores only unchanged applied files and
preserves owner edits; ledger history remains append-only. Local backups retain
original bytes and target paths under the existing `.sumbi/` privacy ignores.

## Comparison

Pre-register the custom prediction using the existing [registration JSON
schema](MEASUREMENT.md#registration-json-schema) before applying. The `register`
convenience command continues to copy catalog predictions; custom changes use
the documented JSON schema. Optional `improve --registration FILE` checks the
intervention ID, prediction and registration order; apply also refuses a
registration timestamp later than the current UTC time. Then pass
`--interventions .sumbi/interventions.jsonl` to `compare` to account for the
actual apply timestamp. Apply does not claim adoption, cost savings or success.
The existing comparison exposure reader ignores revert audit rows; do not treat
a reverted intervention as persistent exposure or compare across that reversal.

## JSON files

`--json-out OUT` writes the same allow-listed JSON object that `--json` prints
for the selected mode (guide, observations, proposal, revert counts,
registration or check) to `OUT`. It is UTF-8 without a byte-order mark, LF,
published atomically, and avoids shell redirection differences such as a BOM
added by an older PowerShell. Standard output is unchanged, so text mode still
prints the local review diff. The name differs from collect's `--json OUT`
because improve's `--json` is already a format flag. Output may not replace an
input file (evidence, bundle, registration, before/after reports, signals), a
bundle target, a file the revert backup manifest may restore (checked before
reverting), anything under the repository's `.sumbi/` or a local agent-log
directory under the home directory, using collect's log-root check. Apply
writes the file after the transaction; a failed write reports an error but does
not undo an applied change.

```console
sumbi improve --evidence collect.json --json-out out/observations.json
```

## External interventions

Some changes are applied through the host repository's own pull request rather
than an improve bundle. Register them in the same intervention ledger so a
later round can compare them without hand-written notes:

```console
sumbi improve --root . --register-external worker-cap-round \
  --applied-at 2030-01-03T00:00:00Z --change worker-time-cap \
  --predict tokens-per-day:decrease:30 --predict tokens-per-success:decrease:10 \
  --revert-if tool-error-rate:increase:50 --before-days 2 --after-days 2
```

The command appends one `"action": "register"` row to
`.sumbi/interventions.jsonl` under the install lock. It refuses an intervention
ID that already appears in the ledger, and improve apply refuses to reuse a
registered ID. Every value is a bounded label or number: the ID, `--change`
labels (1-16) and metric labels use `[a-z][a-z0-9-]{0,63}`; `--predict` and
`--revert-if` take `METRIC:increase|decrease:PERCENT` (1-16 each). There is no
free-text field. Keep a description or rationale in your own local notes, as a
bundle keeps its hypothesis outside the ledger. The row records the windows
`[applied-before_days, applied)` and `[applied, applied+after_days)`, the
registration time, and `preregistered: false` when registered after the apply
time. Its `utc_time` is the apply time, so `compare --interventions` reads it
as the actual apply time.

```json
{"action": "register", "practice_id": "external-change",
 "intervention_id": "worker-cap-round", "changes": ["worker-time-cap"],
 "predictions": [{"metric": "tokens-per-day", "direction": "decrease",
   "rough_size_percent": 30.0}],
 "revert_if": [{"metric": "tool-error-rate", "direction": "increase",
   "threshold_percent": 50.0}],
 "windows": {"before_days": 2.0, "after_days": 2.0,
   "before": {"since": "2030-01-01T00:00:00Z", "until": "2030-01-03T00:00:00Z"},
   "after": {"since": "2030-01-03T00:00:00Z", "until": "2030-01-05T00:00:00Z"},
   "bounds": "[since,until)"},
 "applied_at": "2030-01-03T00:00:00Z", "registered_at": "2030-01-02T09:00:00Z",
 "preregistered": true, "utc_time": "2030-01-03T00:00:00Z"}
```

Check the registration with two collect reports for the windows before and
after the change:

```console
sumbi improve --root . --check worker-cap-round \
  --before before-collect.json --after after-collect.json --json-out out/check.json
```

The before report must end, and the after report start, on their own side of
the apply time; each window reports whether it matches the registration. Base
cost metrics are normalized per day of each report's window so unequal windows
stay readable: `sessions-per-day`, `tokens-per-day`, the reported token
components (`new-input-`, `cache-write-`, `cache-read-`, `output-` and
`reasoning-output-subset-tokens-per-day`), `compactions-per-day`,
`estimated-active-seconds-per-day`, and `tool-error-rate` (tool errors per
tool result). Each row shows before, after, change percent and observed
direction; a missing component stays `null`. Predictions and revert criteria
naming these metrics are shown side by side, with `crossed` for a revert
criterion whose observed change reaches its threshold in its direction.
`tokens-per-success`, `time-per-success` and `success-rate` are
`not-available` with reason `no-success-denominator`: collect has no success
outcome.

Role metrics come from the collect [usage profile](MEASUREMENT.md#usage-profile)
as reported, already per day or a share, not divided again. For each role
(`orchestrator`, `worker`): `<role>-sessions-per-day`, `<role>-tokens-per-day`,
`<role>-tool-calls-per-day`, `<role>-tool-error-rate`,
`<role>-compactions-per-day`, `<role>-active-seconds-per-day`,
`<role>-median-session-tokens`, `<role>-tool-seconds-per-paired-call`,
`<role>-long-tail-share` (token share of sessions over the long-session
threshold), `<role>-compacted-session-share`, `<role>-top-decile-share`, and
`<role>-<class>-tool-median-seconds` for each tool class; plus
`children-per-parent-max` and `children-per-parent-median`. They read
`/profile/summary` by default. `--project-key KEY` reads
`/profile/projects/KEY` instead, so a check can follow one repository's workers;
`role_metrics_scope` in the output names the scope. A metric missing from a
report (an absent project, a role with no sessions) is `not-reported`. Compare
reports collected with the same `--long-session-minutes`. Other labels are
`not-available` with reason `not-in-collect`. The
verdict is always `observation-not-causal`; work volume, model mix and other
changes in the same windows are not removed, and `crossed` is a prompt for the
owner's review, not an automatic revert. Registrations are read only from
`register` rows; bundle applies keep their success-based comparison flow.

### External signal files

A hook or wrapper may log simple events of its own, such as a warning or cap
count. Pass that file to a check with `--signals FILE`:

```json
{"ts": "2030-01-02T10:00:00Z", "event": "warn"}
{"ts": 1893636000, "event": "cap"}
```

Each nonblank line is a JSON object with `ts` (epoch seconds, or ISO 8601 with
a UTC offset) and an `event` label. All other fields are ignored and never
printed. The check counts labels per before/after window. Labels outside
`[a-z][a-z0-9-]{0,63}` count as `invalid` in their window; lines without a JSON
object or a valid `ts` count as `unreadable_lines`; events outside both windows
count as `outside_windows`. The file is at most 64 MiB. This is a separate
input, not a [sumbi-events v1](EVENTS.md) stream: do not put these lines under
`<home>/.sumbi/events/`, where unknown event types are coverage defects.
