# Evidence-driven local improvements

`improve` connects an existing collect report to a concrete, reviewed harness
change. It can address a new source/configuration defect outside the seed
catalog. It does not infer causality, generate code, fetch internet candidates,
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
    "path": "tools/capacity.py",
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

A bundle supports up to 64 exact UTF-8 targets as a resource bound, including
source, configuration and harness documentation (`.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.json`, `.toml`,
`.yaml`, `.yml`, `.md`, `.txt`, `.ini`, `.cfg`, `.sh`). There are no file deletions
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
