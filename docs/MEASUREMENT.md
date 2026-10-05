# M1a session measurements

`sumbi collect` reads local Claude Code and Codex JSONL logs. It uses Python 3.11
or later and the standard library at runtime. Installation exposes the `sumbi`
console command; running `python -m sumbi` also works from a checkout.
The build backend also uses only the standard library. An existing pip installer
can install this checkout offline with
`python -m pip install --no-index --no-deps --no-build-isolation .`.
The in-tree backend is retained for offline builds; wheels and source archives
include the text catalog, its credits and all command modules. Runtime and
build dependency lists remain empty.

```sh
sumbi collect --since 2030-01-01T00:00Z --until 2030-01-02T00:00Z \
  --project sample --match-origin 'https://example.test/team/*' \
  --match-path '*sample*' --json out/collect.json
```

Use `--home DIR` to read another local home. `--agents` selects a comma-separated
list of `claude-code` and `codex`; both are enabled by default. Each `--project`
starts a named rule with one or more following `--match-origin` or `--match-path`
patterns. Repeat the project block for additional rules. Names are configured
labels, never built-in project names. Patterns use shell-style glob matching,
including matching across directory separators. The default idle threshold is
five minutes; `--idle-minutes` accepts any positive finite number.

The command prints a text summary and writes versioned JSON to `out/collect.json`
unless `--json OUT` selects another file. With `--json -`, JSON goes to stdout and
the summary goes to stderr. `--local-review local/review.txt` separately opts into
friction lines from in-window message text. That file can contain private text;
keep it outside version control. Collection never modifies the source logs.
Destinations inside source directories and overlapping output destinations are
rejected. Output files are replaced atomically.

## Windows, tokens and repeated records

All event windows are `[since, until)`. Bounds must be UTC ISO timestamps with
`Z` or `+00:00`. The collectors scan whole files, including pre-window events for
baselines and post-window events for completed pairs and message deduplication.
Broken JSON, invalid UTF-8 and non-object lines increase coverage counters and
do not stop subsequent records from being read. There is no modification-time
filter, line cap or model-search cap.

Claude sources are the JSONL files directly inside each project directory and
all JSONL files below its subagent directories, including nested histories.
The session header or file identity stays stable when later records omit IDs.
Subagents have distinct IDs linked to the parent session. Assistant messages are
deduplicated by message ID across the entire session. The snapshot with the
largest reported output wins; a tie selects the latest timestamp. The selected
snapshot's timestamp assigns the entire message to one window. Thus a streamed
message finalized at the excluded end contributes to the next window, even if
an earlier partial snapshot was inside this one. Input, cache write and cache
read are taken from that same snapshot, not summed across content blocks.
Reasoning is not reported separately by this adapter.

Codex sources are recursive `rollout-*.jsonl` files below the sessions directory.
The first session metadata header identifies the rollout. A child stream can
contain its parent's metadata afterward; that inherited header cannot rebind
the stream or change its candidate cwd. In some versions `session_id` describes
the root group rather than the individual thread. Files with the same first
header identity are merged as resumes. Canonical JSON fingerprints discard exact
replayed events across those files. Repeated cumulative totals at different
timestamps contribute zero additional tokens. Fingerprints and raw records are
not included in the report.

Cumulative token snapshots are ordered by timestamp and ordinal where available.
The first complete snapshot uses a zero baseline; a pre-window snapshot supplies
the baseline for the first in-window delta. Any decrease in a reported cumulative
counter marks a reset of the counter vector. The new vector is counted from zero,
and the reset is exposed. This assumes synchronized counters within one thread;
counter corrections cannot be distinguished from resets by these records alone.
Malformed counters and cached deltas exceeding input deltas are exposed as invalid
token records rather than silently clamped. Models and reasoning effort come from
all relevant turn contexts, including the last context before the window.

New input is input minus cached input; cache read is cached input. Codex cache
write remains **not reported**, even when a particular version supplies an extra
counter whose semantics this adapter has not established. Reasoning output is a
subset of output and is never added to the total again. Claude totals sum new
input, cache write, cache read and output. Codex totals sum new input, cache read
and output.

Missing token kinds are JSON `null`, distinct from a reported zero. Aggregate
fields sum only reported values and include `not_reported_sessions` per kind.
`total` is the sum of reported components, with an explicit basis label; it does
not imply full token coverage or monetary cost. Spend shares use that reported
token total. M1a has no price conversion and makes no savings verdict.

## Counts and time

Tool starts, results and failures are counted separately, once per tool ID and
category. Claude tool-use and tool-result blocks, compaction boundaries and API
errors are recognized. Codex function/custom calls, begin/end events and completed
tool items are recognized; failed commands require a non-zero machine exit code
or the anchored CLI completion envelope. Completed items with a logged start
timestamp can supply the call timestamp. Counts include all observed tool kinds;
wrappers and inner tools with different IDs are distinct operations. User input
requests include synchronous and asynchronous call variants. A compaction is
counted from its boundary record, not again from an associated UI item.

Every time figure is labeled **observed** or **estimated**:

- Wall span is the interval between the first and last recorded session event,
  clipped to the window. Resumed idle periods are part of this calendar span.
- Active time sums adjacent event gaps that are no longer than the idle threshold,
  clipping each qualifying gap to the window. Longer gaps contribute nothing.
  There is no padding before or after the recorded events. Sensitivity at 2, 5
  and 10 minutes is always included, together with any custom threshold.
- Observed tool and request durations require both start and end evidence. Paired
  explicit timestamps on completed records qualify; a duration value alone does
  not. Intervals are clipped to the window, even if the endpoints lie outside it.
  Missing endpoints are counted separately. No complete pairs means zero summed
  observed seconds and zero pairs, not evidence that the operation took no time.

A session enters the window when it has an in-window event or a paired observed
interval overlapping it. Sessions without usage are retained for coverage.
Parallel session and operation durations are summed separately; none of these
totals represent the time a person waited. Tool and request durations can overlap
and must not be added to each other or to active time.

## Attribution and privacy

Schema 1.1 allocates reported tokens per billable event, rather than assigning a
whole session to one project. An event is one selected Claude assistant message
or one nonzero Codex cumulative-token delta. The evidence priority is:

1. The Claude message's own `cwd`, or the Codex working directory in force at
   the closing snapshot. A Codex `turn_context` starts a new context; an explicit
   command `workdir` or `cwd` supersedes it for the remainder of that turn.
2. Repository paths in that turn's recognized tool inputs: read/write/edit file
   operands, apply-patch targets, `git -C` operands and a leading shell `cd`.
   Working-directory evidence wins even when a tool targets another repository.
   Distinct projects at this priority leave the event unassigned.
3. The immediately previous billable event's project, only when the gap is
   strictly less than the idle threshold. An unassigned event breaks this chain.
4. Otherwise, unassigned.

Claude user messages start turns; tool-result messages continue the existing
turn. Codex turn contexts reset command directories and tool paths. Relative
operands resolve against the structured working directory. Existing files and
subdirectories resolve to their local Git root; missing operands can use an
existing ancestor's repository. Paths without a resolvable repository remain
path candidates. Supported Windows drive/UNC paths normalize case and separators;
POSIX paths preserve case. UNC paths remain path candidates without filesystem
or Git probing, so collection does not initiate network-share access.
Shell extraction is deliberately limited: it does not
execute commands, infer later `cd` operations, expand variables or evaluate
expressions. Unknown tool inputs, wrapper code and ordinary message prose are
not mined. Paths and commands are retained in memory only.

A Codex delta spanning a working-directory change belongs entirely to the
project in force at its closing snapshot. Cumulative counters provide no measured
split within that interval; proportional splitting would invent precision. Equal
timestamps use logged ordinals when available, otherwise accepted record order.
The counter baseline and attribution state include pre-window events. Neither
future directories nor future tool calls can change an earlier delta's link.
Claude's selected streaming snapshot supplies both usage and its own evidence.

For an existing working-tree directory, local read-only Git commands find the
origin. No remote request is made. HTTPS and SSH origin forms normalize to the
same host/repository identity, removing credentials, transport, default ports,
trailing slashes and `.git`. Hosts are case-insensitive; repository path case is
preserved. Windows paths normalize separators, dot segments and case; POSIX paths
preserve case. Origins and paths are cached during a run.

Origin rules take priority. When any origin rules are configured, a known origin
that matches none is explicitly `other` and cannot use a path fallback. An
unsupported or unreadable origin is `unassigned` in that mode. Without origin
rules, a path-only configuration can scope existing repositories as well as
missing working folders. Paths without a usable origin use path patterns.

Every event allocation records its evidence counts, matching rule label and
pseudonymous project key. Several directories with the same origin and rule
remain one project. Without rules, inferred candidate projects are grouped
automatically. With rules, matching is per event: a mixed session counts toward
a project only for its matching billable events. Known unmatched projects are
`other`. Both `other` and `unassigned` count toward `unattributed_share`;
`unassigned_share` separately measures missing or conflicting attribution.
Rules scope the spend breakdown rather than hiding sessions from coverage.

Each session's `allocations` and the coverage `spend` rows contain token kinds,
billable event counts, `evidence_counts`, `evidence_tokens` and `fallback_share`
(previous-event tokens divided by reported tokens in that row). Coverage also
reports the global evidence mix by tokens. The four evidence categories are
`cwd`, `tool_path`, `previous_event` and `unassigned`. Conflicting paths or rules
retain the category that supplied the evidence, even though their bucket is
unassigned. Spend rows count a session once per project; those counts need not
sum to the global session count. Unknown token kinds remain null. Reasoning is
still an output subset. The legacy session `project` field describes aggregate
cwd candidates only; it is not the spend allocation. Consumers must use
`allocations` or `coverage.spend` to judge project spend.

Session, parent and project IDs are stable SHA-256 pseudonyms by default. Reports
and summaries explicitly label these keys as unsalted. Set `SUMBI_SALT` or use
`--salt-file FILE` on either subcommand to select HMAC-SHA256 pseudonyms instead.
The file takes precedence over the environment variable; its bytes are used
verbatim, including any trailing newline. Empty or unreadable keys fail with a
sanitized error. Keep the key private and consistent across measurements that
need matching IDs. Changing it changes session, parent, project and fingerprinted
label IDs, while counts stay the same. Neither keys nor key-file paths enter
reports. Pseudonymization is not anonymization against someone who already knows
candidate origins or paths, especially when keys are unsalted.
JSON and summaries contain only schema categories, counts, durations, machine
labels, configured rule labels and IDs. They do not include cwd values, origins,
file names, prompts, tool names, arguments, outputs or friction text. Unsupported
free-text label shapes are fingerprinted. Labels and rule names should themselves
be chosen for public use. Unknown record types are counted by bounded type label;
the report does not claim to understand their content.

Coverage includes files scanned, lines read, broken lines, exact duplicates,
unknown types, unreadable files, invalid timestamps, invalid token records,
inherited session metadata, sessions read, sessions in the window and reported
spend shares. These are collection diagnostics, not an outcome or completeness
verdict. The deliverable ledger and comparison command remain out of M1a scope.

## Install baselines

`sumbi install --apply` invokes `sumbi.collect.baseline` once before practice
writes when the plan has changes. It collects `[now - 14 days, now)` in UTC,
scoped to the target repository. A normalized Git origin, when available, matches
other local clones of that repository exactly. Otherwise normalized paths match
the repository root and its descendants, with one project key for subdirectories.
A known different origin never falls back to a matching path; an unreadable or
unsupported origin remains unassigned when the target has a usable origin.
Mixed-repository sessions contribute only their matching billable events.

The counts-only JSON is written exclusively to
`.sumbi/baseline/<UTC timestamp>.json`. Coverage diagnostics describe all logs
scanned; `scope` and adapter `sessions_selected` counts distinguish selected
sessions from the wider window. The summary and session rows contain only the
selected repository events and their containing sessions. Tool counts and time
remain session-level diagnostics for selected sessions, not project-allocated
costs. If none match, the status is `no sessions found`
and an empty report is written. `--home DIR` selects the log home for fixtures
or another local user home. Baseline files never overwrite existing files or
source logs, and the installer ignores them in Git.

## Validation

Run `python -m unittest discover -s tests`. Tests use only authored synthetic
records and temporary homes. The fixture README gives the hand arithmetic.
No test uses a network service. Real-data comparisons and raw records belong
outside the repository; only aggregate numbers may be reported for review.
