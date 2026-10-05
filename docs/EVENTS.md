# sumbi-events JSONL v1

Any coding agent or local wrapper can provide session measurements and local
verification evidence by emitting this format. The adapter consumes records;
it does not execute commands, install a producer, authenticate an emitter or
infer events from transcripts. Native Claude Code and Codex adapters remain
available separately.

## Location and selection

Store UTF-8 JSONL files beneath `<home>/.sumbi/events/`, including nested files.
Keep this home outside version control. Each nonblank line is one JSON object.
Use one event ID across copied or resumed files; do not emit a second usage
delta for the same consumption.

```sh
sumbi collect --agents sumbi-events --home local-log-home \
  --since 2030-01-01T00:00Z --until 2030-01-02T00:00Z
sumbi deliver --agents sumbi-events --home local-log-home \
  --outcome-source local-verify --repository . --verify scripts/check.sh \
  --since 2030-01-01T00:00Z --until 2030-01-02T00:00Z \
  --scan-until 2030-01-03T00:00Z
```

`--agents` selects adapters. The default remains `claude-code,codex`; opt into
`sumbi-events`, alone or in a comma-separated mixed selection. `--home` selects
the log home, not the measured repository. Collection is read-only, and output
destinations inside any supported session-log directory are rejected.
Mixed adapters must represent disjoint underlying observations. Reporting the
same work through both a native adapter and an open emitter is not deduplicated
across formats; choose one source for that work.

## Envelope

Every record has these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`; booleans and other versions are unsupported |
| `type` | One of the eight event types below |
| `event_id` | Nonempty string identifying one immutable event throughout the log home |
| `session_id` | Nonempty string identifying one session throughout the log home |
| `timestamp` | Timezone-bearing ISO 8601 event time, or null when unknown |

Times normalize to UTC. Measurement windows are `[since, until)`. A session's
dispatch needs a known timestamp. Unknown edit or command completion timestamps
remain incomplete evidence rather than disappearing from the outcome ledger.
File names and file modification times do not establish event identity or time.

Session IDs are global across files and emitter agents in this home. A parent
may use a different agent. Emitters must coordinate IDs or provide a distinct
home; reusing a session ID for unrelated work is a conflict, not a new attempt.
Exact repeated event IDs are idempotent even across files. Conflicting contents
for one event ID invalidate both versions and their associated session evidence.
Conflicting dispatch metadata likewise cannot choose the more favorable cohort.

Unknown versions, event types and malformed records increase bounded coverage
categories. Affected known sessions retain evidence gaps. Comparison proposals
are withheld when source coverage is incomplete. Unknown extra fields are
ignored, never interpreted as prompts, commands, outcomes or public text.

## Event fields

### `session_start`

Required fields are `agent`, `role` and `cwd`, in addition to a known envelope
timestamp. `agent` is a nonempty emitter identifier. `role` is exactly `worker`
or `orchestrator`. `cwd` is an absolute local directory. Optional
`parent_session_id` is a nonempty string or null. A worker can have no parent;
its explicit role still enrolls its unit at dispatch. Orchestrators are reported
as overhead rather than successes. Role, agent, parent and dispatch metadata
are fixed for this session ID.

Optional `model`, `effort` and `cli_version` are strings or null. Omitting them
means unknown. Never invent a default model, effort or version to fill a gap.
Use `context` for later changes. Reopening or resuming the same session does not
create another worker unit.

### `context`

Optional `cwd`, `model`, `effort` and `cli_version` update observed context at the
event time. A supplied `cwd` is an absolute local directory or null. Metadata
strings are measurements, not completion prose. Context supports token
attribution; it does not substitute for a command's explicit execution cwd.
Omitted fields leave their previous context unchanged. An explicit null clears
that field to unknown, including a cwd; a cleared cwd does not fall back to an
earlier usage event. Conflicting cwd values at one timestamp stay unknown.
Conflicting metadata observations retain their IDs with an evidence gap and
withhold comparison proposals.
Model, effort and version IDs retain values observed in the measurement window,
plus the latest context before it. A known and unknown mixture exposes
`metadata_incomplete` on local units and blocks the corresponding comparison
metadata gate. Metadata that is entirely unknown remains unobservable rather
than inventing a label or invalidating an otherwise valid verification exit.

### `token_usage`

The required `tokens` object contains per-event **deltas**, not cumulative
snapshots. A producer with cumulative counters must convert them into deltas
and deduplicate consumption before emitting this format. A reset is not a
negative usage delta; v1 has no cumulative-counter or reset event.

| Key | Meaning |
| --- | --- |
| `new_input` | New input tokens, excluding cache reads and writes |
| `cache_write` | Input tokens written into cache |
| `cache_read` | Input tokens read from cache |
| `output` | Output tokens, including any reasoning subset |
| `reasoning_output` | Reasoning subset of output, when separately measured |

Values are nonnegative integers or null. Missing keys mean unknown, not zero.
Booleans, negative values and nonintegers are invalid. When both are known,
`reasoning_output` cannot exceed `output`. Never add reasoning to output again.
Optional explicit `cwd` scopes this usage event; otherwise its context supplies
the attribution candidate. Conflicting or missing project evidence stays
unassigned rather than fabricating a split.

Collection's `tokens.total` follows the existing **reported-component partial
total** convention: it sums observed nonreasoning components. It is not a claim
of complete cost. Generic session reports additionally expose `complete_total`
and per-kind `not_reported_events`; complete cost is null unless all four
nonreasoning fields are known for every selected usage event. Known zeros remain
zeros. These per-kind counts cover valid usage records with unknown components.
`evidence_incomplete` separately marks discarded or conflicting usage evidence
and keeps complete cost null even when earlier valid deltas were fully known.
Local verification cost totals use the same completeness requirement,
including failed workers and retries. An emitter named after a native agent
does not gain that native adapter's token assumptions.

### `tool_start` and `tool_end`

Both require a nonempty `tool_call_id`. Their timestamps establish tool
intervals. A `tool_end` may contain `error`, an explicit boolean; absent error
status remains unreported. Counts and observed paired/unpaired time are
reported without tool names, arguments or output text. An unmatched endpoint
cannot create a duration. Emit these events for tool counts; a command
completion does not duplicate their counts.

### `command_execution`

This is one completed execution attempt. Its envelope timestamp is the
completion time; `started_at` is the execution start time, or null when unknown.
Required `command` is a string, a list of strings or null. Required `exit_code`
is an integer or null, with booleans rejected. Required `cwd` is the explicit
absolute local execution directory or null. Commands and directories stay
local, and the collector never runs a recorded command.

Only the declared verifier's executed program can establish local check
evidence. Reads, syntax-only checks, unsupported wrappers and modified verifier
arguments do not count as a pass. Start and completion must belong to the
worker lifetime, and the start must be strictly after its latest known edit.
An unknown latest check does not preserve an earlier pass. Missing execution
cwd is not repaired from session context. A launch or prerequisite failure is
a failed attempt; it does not prove that tests ran.

The format records producer-supplied machine fields. It does not authenticate
that producer or independently prove human acceptance, required CI, later
reverts or unlogged edits. These are the explicitly weaker local outcomes.

### `file_edit`

The event timestamp records a known edit attempt. No file path, test name or
content is needed. An unknown timestamp creates an evidence gap. Shell edits
without emitted events remain invisible, just as with native log adapters.

### `session_end`

The timestamp records observed session completion. It is not a success claim.
Later activity can make that completion stale; the active-session heuristic
still applies. Actual acceptance depends on the selected outcome source.

## Synthetic example

The following invented records describe one standalone worker. The producer
must use its actual local cwd; do not copy this synthetic path into real logs.

```jsonl
{"schema_version":1,"type":"session_start","event_id":"e1","session_id":"worker-a","timestamp":"2030-01-01T01:00:00Z","agent":"example-agent","role":"worker","cwd":"/fixture/workspace","parent_session_id":null,"model":null,"effort":null,"cli_version":null}
{"schema_version":1,"type":"file_edit","event_id":"e2","session_id":"worker-a","timestamp":"2030-01-01T01:00:01Z"}
{"schema_version":1,"type":"command_execution","event_id":"e3","session_id":"worker-a","timestamp":"2030-01-01T01:00:03Z","started_at":"2030-01-01T01:00:02Z","command":["bash","scripts/check.sh"],"cwd":"/fixture/workspace","exit_code":0}
{"schema_version":1,"type":"token_usage","event_id":"e4","session_id":"worker-a","timestamp":"2030-01-01T01:00:04Z","tokens":{"new_input":10,"cache_write":0,"cache_read":5,"output":3,"reasoning_output":null}}
{"schema_version":1,"type":"session_end","event_id":"e5","session_id":"worker-a","timestamp":"2030-01-01T01:00:05Z"}
```

## Privacy and support limits

Session, parent and emitter identities use pseudonymous IDs. Generic model,
effort and version values also become IDs, preserving presence and mix changes
without publishing private labels. Use `SUMBI_SALT` or `--salt-file` for keyed
IDs before sharing; unsalted reports remain explicitly marked. Raw identifiers,
paths, commands, extra fields and transcripts are not public report fields.
The open adapter does not extract local friction prose from ignored fields.

Support is L1 session measurement and L2 local worker outcomes for conforming
emitters. No proprietary native integration is implied for other agents.
GitHub deliverable ownership links are not supplied by this v1 contract; do
not treat project or parent evidence as an accepted pull request. Instruction
loading, propagation, remote transport and producer authentication have their
own contracts and remain outside this format.

See [measurement rules](MEASUREMENT.md) and [the support matrix](DESIGN.md#support-matrix).
