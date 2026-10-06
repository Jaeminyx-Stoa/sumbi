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
list of `claude-code`, `codex` and opt-in `sumbi-events`; the two native adapters
are enabled by default. The [open event contract](EVENTS.md) describes generic
logs beneath `<home>/.sumbi/events/`, delta tokens, pseudonymous emitter metadata
and conservative local execution evidence. Each `--project`
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
Subagents have distinct IDs linked to the parent session. Older layouts store
sidechain streams as project-level `agent-<id>.jsonl` files. A stream whose first
record has `isSidechain: true` uses the same identity as a `subagents/` stream:
`<parent>:subagent:<agentId>`, falling back to the agent filename ID when
`agentId` is absent. Its `sessionId` identifies the parent, not the child.
The worker role is fixed when the stream is identified; later records cannot
change a session's role. Assistant messages are
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
timestamps use logged ordinals when available, with accepted record order as the
tie breaker and as the fallback when an ordinal is absent.
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

`sumbi install --apply` invokes `sumbi.install.baseline.record_baseline` once before practice
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

## M1d deliverables and cost per success

`sumbi deliver` joins the ledger, GitHub outcomes and the same billable
events used by `collect`. A directory selects offline recorded responses;
`--outcomes github` selects the read-only live REST adapter. Copy
[the empty template](deliverables.template.csv) into a private local ledger.

```sh
sumbi deliver --ledger deliverables.csv --outcomes recorded-outcomes \
  --since 2030-01-01T00:00Z --until 2030-01-10T00:00Z \
  --project sample --match-path '/synthetic/sample*' --json out/deliver.json
```

The collect options `--home`, `--agents`, project matching blocks, `--idle-minutes`,
`--salt-file` and `--json` work here too. `--follow-up-days` selects a positive
finite observation window, default 7 days. There is no `--local-review`: this
command never reads message prose for linking. JSON defaults to
`out/deliver.json`; `--json -` puts the summary on stderr. Writes are atomic and
cannot overwrite the ledger, outcomes, salt file or session-log directories.

### Ledger contract

Use UTF-8 CSV with these exact columns, in order. All fields are single-line,
without surrounding whitespace, and at most 4096 characters. Lists use semicolons
without spaces; quote a CSV field when it contains a comma. Empty lists are blank.
Errors name the row and field, never echo input values or private paths.

| Column | Meaning and validation |
| --- | --- |
| `id` | Unique 1-64 character identifier: initial letter/digit, then letters, digits, underscore, dot or hyphen. Choose a public-safe ID; it appears verbatim in reports. |
| `dispatched_at` | Required UTC ISO timestamp ending in `Z` or `+00:00`. Fixed at dispatch. |
| `acceptance` | Required short text, 1-512 characters, fixed at dispatch. Validated then discarded; never included in reports. |
| `repos` | Required list of `owner/repo` IDs. Case-insensitive, no repeated entries. |
| `prs` | Optional list of `owner/repo#n[:role]`, positive PR numbers. Roles are exactly `constituent` (default), `retry`, or `followup`. Each PR must belong to `repos` and may occur only once across the ledger, regardless of role. Every constituent must succeed. Keep retries and follow-ups in the original deliverable row. |
| `branches` | Optional unique list of bounded literal Git branch names, at most 200 characters each. No expansions or invalid dot segments. Branch names stay in memory. |
| `state_override` | Blank, `abandoned`, or `abandoned@<UTC timestamp>`. No override can declare success. Abandonment may be recorded without a PR. A timestamp cannot precede dispatch. |
| `accepted_by_human` | Blank means human acceptance is not required. `n` means required and pending; `y` means required and accepted. |
| `notes` | Optional local text; validated then discarded. |
| `task_type` | Optional tenth column, after `notes`; blank is not reported. Same 1-64 character label charset as `id`; appears in deliver and compare reports. The original nine-column header remains valid. |

Keep real ledgers and recorded API responses outside version control: acceptance,
notes, branch names, PR titles/bodies and commit messages may contain private text.
The shipped fixtures are authored synthetic data only.

For example, `example/sample#12:retry;example/sample#15:constituent` records a
replacement attempt. A closed-unmerged retry is superseded only when a constituent
in the same repository merges after that retry closes; otherwise it remains a failure. Open retries
remain pending. Merged retries need their own checks and observation windows.
Any listed retry excludes first-pass success. At least one constituent is needed
for success; roles never excuse a failed constituent.

An explicit `:followup` needs no title/body reference and can span ledger repositories.
A follow-up merged strictly
inside a constituent's observation window excludes first-pass success and needs
its own green merge and complete observation window for eventual success. For
unmerged follow-ups, creation time selects the window; open work is pending and
closed-unmerged work fails. Out-of-window explicit follow-ups remain linked for
spend but do not affect that constituent's outcome. Missing listed PRs prevent a
success claim. Automatic text-based detection still applies to every role.

### Live GitHub outcomes

Run from a scratch folder, keeping all destinations outside Git working trees:

```sh
sumbi deliver --ledger deliverables.csv --outcomes github \
  --cache private-cache --record private-recordings \
  --since 2030-01-01T00:00Z --until 2030-01-10T00:00Z --json deliver.json
```

Authentication uses the first nonblank value of `GITHUB_TOKEN`, then `GH_TOKEN`,
then captured stdout from `gh auth token` if `gh` exists. A missing credential
fails with a fixed diagnostic. Tokens are used only in the Authorization header,
never URLs, logs, reports or recordings. Redirects are rejected to avoid forwarding
credentials. GitHub permissions only need to read repository contents, PRs,
checks and commit statuses. The adapter performs GET requests only.

PRs are enumerated by creation time with pagination, including open and closed
work. Commits are enumerated on the default branch and captured PR target branches
from the earliest dispatch through observation time. This avoids search-index lag
and the search API's result cap. Unreachable/deleted branch history and commits on
unrelated branches are outside this evidence scope. Request or pagination failure
stops collection instead of silently claiming complete coverage. Scan patterns
are bounded literal PR references and full SHAs; title, body and commit-message
fields are limited to 65536 characters each. Oversize responses fail explicitly.

Check runs (`filter=all`) and commit statuses are read for the merge SHA, falling
back to the PR head when the merge SHA has no pre-merge results. Only starts and
status updates at or before merge qualify. A check completed after merge cannot
contribute its later conclusion: it is red (still pending) at merge. The latest
attempt by start time and latest status update win; missing evidence is unknown.
`observed_checks_at_merge` is green/red/unknown for these visible results.

**REST does not expose an authoritative historical required-check list.** Every
merged PR reports `checks_at_merge` and `checks_basis` side by side, using this
ladder in order:

1. `historical`: an authoritative required-check snapshot at merge, under the
   unchanged offline contract below. It always wins when present.
2. `current_policy`: the union of classic base-branch protection and active
   ruleset `required_status_checks`, configured now, applied to results visible
   at merge. Classic checks apply when protection is enabled and
   `enforcement_level` is not `off`. Every required name must be green; any red
   or still pending result is red, and a missing required result is unknown.
   Optional results do not affect this verdict.
3. `all_visible`: both policy sources were read and require no checks. All
   visible results at merge must be green, and at least one must exist. No results
   gives unknown with PR reason `no_checks` and deliverable reason `checks_none`.
4. `unknown`: a policy endpoint returned an access-denied 403 or a 404. Checks
   remain unknown with reason `checks_policy_unreadable`. The single exception
   is a rules endpoint 403 whose message exactly equals `Upgrade to GitHub Pro or
   make this repository public to enable this feature.`; it means zero ruleset
   rules. This exception never applies to the classic branch endpoint.

Names match exactly and case-sensitively against check-run `name` or status
`context`; no whitespace trimming, workflow prefix removal, globbing or aliases
are applied. Latest check attempts are selected independently per name and app,
and latest statuses per context. For an unpinned requirement, every matching
check source and status must pass. An `app_id` or `integration_id` pin requires a
check run with that `app.id`; a status or another app cannot satisfy it. Classic
`contexts` mirror `checks`: a matching check object supplies its app pin rather
than introducing another unpinned requirement. Null/omitted IDs and classic
`app_id: -1` mean any app. Duplicate requirements from the two policy sources are
deduplicated by name and app ID. Completed `success`, `neutral` and `skipped`
checks qualify; statuses require `success`. Red takes precedence over a missing
requirement for current-policy judgments. No results are combined across SHAs.
Missing start timestamps cannot establish a check's presence at merge.

The policy may have changed since the merge. Admin bypass under `non_admins`
enforcement is caught only when a required result was red or missing at merge;
this adapter does not audit bypass actors. `all_visible` proves observed green
results without claiming an enforced gate. `observed_checks_at_merge` remains a
diagnostic and may be red when an optional failure leaves `current_policy` green.
Other collection failures still stop capture instead of inferring green checks.

Each deliverable takes the weakest basis of its merged attempt PRs, including
repairs (`historical` > `current_policy` > `all_visible` > `unknown`). Missing PRs
or no merged attempts give `unknown`; unmerged retries supply no merge basis.
JSON `checks_basis_counts` and the text summary count deliverables per basis,
including unknown. Other check reasons distinguish `checks_red` and
`checks_missing_required`. `checks_at_merge` is the sole checks verdict field;
the misleading `required_checks_at_merge` alias has been removed.
M2 requires the same checks basis across both arms. Mixed bases are not comparable,
including different bases among one deliverable's merged attempts.

Endpoint shapes are verified against GitHub's REST documentation for
[Get a branch](https://docs.github.com/en/rest/branches/branches#get-a-branch),
[Get rules for a branch](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch),
and [Check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference).
The protected-branch example omits `protection.enabled`; when absent, the adapter
uses `protected`. The rules endpoint is paginated and already excludes disabled
and evaluate-only rules, including rules inherited from organizations. Check runs
expose `app.id`; classic requirements use `app_id` and rules use `integration_id`.
The plan-unavailable error text was observed by the maintainer; the endpoint's
documented response codes list 200 only, without documenting this 403 message.

`--cache DIR` defaults to `~/.cache/sumbi/outcomes`. Responses expire after five
minutes. A cache hit retains the original observation time, so cached data cannot
age a deliverable into success. Files are replaced atomically with private POSIX
file modes where supported; Windows access follows local directory permissions.
403/429 rate limits use reset and Retry-After headers, with up to three retries.
Waits over one hour fail with a later-retry diagnostic. Ordinary access-denied 403s
do not trigger a rate-limit wait. Network timeouts and response size limits are
explicit. No destination may be inside a Git repository or overlap session logs,
the ledger, salt file or report. Cache and record directories must be separate.

`--record DIR` writes allow-listed REST pages under `responses/` and normalized
repository fixtures at the top level, replayable with `--outcomes DIR` without
credentials or network access. All author/personal fields are dropped rather than
retaining logins; this is stronger than pseudonymizing them. Active credentials
and recognizable GitHub token patterns are redacted from retained text. Only fields
used for outcomes are retained. Titles, bodies and commit messages remain private
local evidence; sanitization is not a guarantee that prose contains no private
information. Never commit real recordings. Committed test recordings are authored
synthetic data. Cache/record flags require `--outcomes github`.

### Recorded outcomes contract

`Outcomes` is the adapter interface (`pull`, `observation`, `disturbances`), separate
from judgment. `FixtureOutcomes` reads every top-level `*.json` in its directory.
Each file covers one unique repository and uses this wrapper around GitHub REST
response objects. Unknown wrapper fields and inconsistent evidence are rejected.
No fixture filename or response prose is emitted.

```json
{
  "repository": "example/sample",
  "coverage_start": "2030-01-01T00:00:00Z",
  "observed_at": "2030-02-01T00:00:00Z",
  "pulls_complete": true,
  "commits_complete": true,
  "pulls": [{"response": {}, "checks_at_merge": null}],
  "commits": []
}
```

The empty `response` above is a shape placeholder. A PR response needs `number`,
`state` (`open`/`closed`), `created_at`, nullable `closed_at`, boolean `merged`,
nullable `merged_at`, `head.sha`, nullable `merge_commit_sha`, `title` and nullable
`body`. SHAs are full 40-character hex IDs. Merged PRs must be closed, and timestamp
order must be consistent. Recorded changes precede the exclusive `observed_at`.
Each commit response needs `sha`, `commit.message`, and `commit.committer.date`,
within `[coverage_start, observed_at)`.
PR entries may also carry `observed_checks_at_merge` (`green`, `red`, or
`unknown`); it is diagnostic and never substitutes for `checks_at_merge`.
Live recordings additionally retain `current_policy_evidence`, an object with
exactly `required` and `results`. `required` is null for unreadable policy or an
array of `{ "context": "test", "app_id": null }` requirements (empty for
`all_visible`). `results` is null when no pre-merge results exist, or the snapshot
shape below with `required: []`, the selected head or merge SHA, and check-run
`app.id` fields. This evidence is evaluated on replay and never promoted to
`historical`. Raw policy cache/record pages retain only the enforcement, context
and app fields used by the adapter, plus rule types to preserve pagination.
Denied-policy responses retain a normalized null; the plan exception retains an
empty rule array. Response error prose is discarded.

For merged PRs, `checks_at_merge` is null when historical evidence is unavailable,
or an object with exactly `head_sha`, `captured_at`, `required`, `check_runs` and
`statuses`. `captured_at` equals `merged_at` and `head_sha` equals the PR head.
`required` is the unique list of historically required check names/contexts. An
explicit empty list means no checks were required; null does not mean that.
Check-run REST objects need `name`, `head_sha`, `started_at`, nullable
`completed_at`, `status`, and nullable `conclusion`. Commit status objects need
`context`, `sha`, `updated_at`, and `state`. All evidence must be for this head
and at or before merge. The latest check attempt by start time and latest commit
status by update time win; conflicting ties fail validation. If both check and
status use the same required name, both must pass. Completed `success`, `neutral`
and `skipped` checks qualify; commit statuses require `success`. Missing required
results are unknown, never green. Present-day checks cannot stand in for checks
at merge. Current policy supplies a separately labeled weaker basis, never an
authoritative historical list.

The completeness flags attest that all PRs and commits in the declared interval
were captured (including pagination). They do not infer completeness from a
partial list. A missing PR, unknown checks verdict or incomplete capture prevents
a success claim. Missing historical snapshots may use the weaker basis ladder
above. Coverage counters expose the missing evidence.

### States, attempts and time

All constituent and active repair PRs must merge and have green required checks.
A later PR in
the same repository is a disturbance when its creation is inside
`(merged_at, merged_at + window)` and its title/body contains fix, revert,
regression, hotfix or follow-up wording plus a bounded `#number`, same-repository
qualified PR ID/URL, `PR n`/`pull request n`, or full merge SHA reference. A commit
in the same interval with revert wording and a bounded PR or full merge SHA
reference is a revert. A fix commit with the same reference patterns excludes
first-pass success and extends maturity through its own observation window.
It does not establish a merged PR repair for a revert. These patterns deliberately do not understand arbitrary
prose, abbreviated SHAs or cross-repository issue references. Detection is a
recorded-evidence signal and can have false positives; it is not semantic review.

- `success`: every required attempt merged green, all observation windows closed
  with complete captures, and required human acceptance recorded.
- `failed`: explicit abandonment, a constituent/repair PR closed unmerged, or a
  revert without a later merged repair.
- `in_progress`: no PR yet, open work/repair, missing PR/check evidence, a red
  merge check, or pending human acceptance.
- `immature`: merged work whose observation window remains open or incomplete,
  including the window of a merged follow-up repair.

A detected repair is another attempt of the original deliverable. Eventual
success requires its own green merge and completed observation window. A revert
PR never qualifies as a repair. First-pass success excludes every detected fix
or revert. Several constituent PRs count as one deliverable. Closed unmerged
constituents stay failed: the ledger must not drop failed work to improve rates.

Outcomes are assessed as of each repository's recorded `observed_at`, which may
follow the spend period. Period successes use the final successful merge time,
not the later maturity date. Elapsed seconds run from dispatch to final merge,
recorded abandonment, or a terminal closed-unmerged PR. A revert retains the
original merge as the elapsed endpoint. Unknown abandonment time and work with
no merge or closure are null, never zero. Waiting components are not available
in M1d; elapsed is observed calendar time and does not claim agent/human splits.

### Links and allocation

For each billable event, the priority is:

1. A bounded deliverable ID in its own working directory or structured branch
   (`gitBranch` in Claude, `branch`/`git.branch` in Codex context).
2. A ledger branch or exact PR URL in recognized tool inputs/outputs. Only known
   shell/PR tools supply these references. Literal `git switch`, `checkout` or
   `branch` operands and structured branch fields qualify. A one-line branch
   output qualifies only when paired with a literal Git branch query. Unknown tools,
   wrapper code, shell expansions/chains and ordinary messages are not mined.
3. A unique repository candidate with a dispatch-to-close time match, explicitly
   labeled `project_time_weak`. This is a candidate, not proof of ownership.

Contexts clear turn references. A structured branch persists across turns in the
same directory; a directory change, branch switch or conflicting branch-query
output invalidates it. An unrelated PR head does not change the working branch.
Pre-window evidence can establish context; future
evidence cannot relink earlier events. Claude's selected streaming snapshot owns
its branch and tool inputs. Codex's closing cumulative snapshot owns the delta.
No proportional split is invented. Ambiguity at a priority stops allocation;
several deliverables in a session split only when events establish unique links.
Parent/subagent relationships alone do not establish deliverable ownership.

Windows directory ID matching is case-insensitive; POSIX paths and branches are
case-sensitive. Repository candidates use local Git origins matching ledger `owner/repo` IDs.
With missing origins, a matching project's origin patterns can identify a ledger
repository. A path-only project rule can use the repository basename as its rule
label (`--project sample` for `example/sample`). A unique explicit deliverable
link can identify a single repository when no origin is available. Known foreign
origins, excluded projects, conflicting repositories or multiple ledger repos
without a unique repository leave spend outside allocated deliverable costs.
Ambiguous repository candidates remain `unassigned`; repository-scoped events
with no unique deliverable are `unallocated`; excluded projects are `other`.

JSON links contain pseudonymous session IDs, ledger IDs, evidence labels, event
counts and token kinds. PR and repository IDs are pseudonymized; head SHAs remain
machine IDs. Acceptance, notes, branches, commands, outputs, paths, origins and
outcome prose are never emitted. All existing collection coverage counters remain.

### Cost and rates

Costs are reported **tokens**, not currency. Total excludes reasoning output,
which is an output subset. Unreported kinds remain null; coverage separately
counts events with missing token kinds, including partially reported aggregates.
No prices are assumed. Project matching scopes both cost denominators and spend;
ledger-wide state and success-rate rows still include every ledger deliverable.

- Operational: scoped project spend in `[since, until)` divided by successful
  deliverables whose final merge is in that period, assessed using later recorded
  maturity evidence. Failed and immature work's period spend remains in the
  numerator. The project aggregate includes its separately displayed unallocated
  spend; this never assigns those tokens to a deliverable. `operational_linked`
  also shows the ratio using linked spend alone. Repository rows show project
  ratios; a deliverable touching several repositories counts once in each
  applicable repository denominator, so these rows are not additive.
- Cohort: all observed linked spend from dispatch through the available lifetime
  scan for deliverables dispatched in `[since, until)`, divided by their eventual
  successes. Includes failed work and retries. The scan begins at the earlier of
  period start and first ledger dispatch and ends at the later of period end and
  last repository observation. Logs may be missing: lifetime means all observed
  events, not a guarantee of complete historical logging.

Period and lifetime scan lines separately display `linked`, `unallocated`,
`unassigned` and `other` spend. Unknown spend is never spread into individual
deliverables or cohort costs. The cohort has no invented share of unlinked spend.
Zero successes produce null costs per success. Each rate includes numerator,
denominator and a Wilson 95% interval; denominators include failed, immature and
ongoing rows, so immature cohorts must not be treated as final verdicts. A zero
denominator yields a null rate and interval. M1d always reports savings verdict
`not_evaluated`; M2 applies the completeness thresholds and comparison rules below.

## M2: pre-registered comparison and verdict proposals

`sumbi compare` compares dispatch cohorts before and after **one** intervention.
It reads the same ledger, adapters and recorded/live GitHub outcomes as `deliver`.
It never applies an intervention or decides for the owner. All tests use authored
synthetic fixtures and block network calls.

```sh
sumbi compare --registration registration.json --ledger deliverables.csv \
  --outcomes recorded-outcomes --home local-log-home --agents codex \
  --seed 1729 --resamples 5000 --json out/compare.json
```

Windows and follow-up days come solely from the registration. Other collection
options, including project matching, the pseudonym salt, and live GitHub
`--cache`/`--record`, work as in `deliver`. JSON defaults to `out/compare.json`.
The text summary goes to stderr for `--json -`, otherwise stdout. Output cannot
overwrite the registration, ledger, outcome fixtures, salt, or session logs.
The normalized outcome recording now uses `current_policy_evidence`; old files
using the former policy-field name must be renamed before replay. Exhausting
403/429 rate-limit retries fails capture, including on policy endpoints, and
never caches that transient failure as an unreadable policy.

### Registration JSON schema

Create a pre-registration from explicit planning flags and bundled catalog
predictions before applying the change:

```sh
sumbi register --json registration.json --intervention-id round-01 \
  --outcome-source local-verify --margin-pp 5 --sample-size-per-arm 12 \
  --follow-up-days 7 --window-days 14 --applied-at 2030-01-15T00:00:00Z \
  --practice handoff --practice test-and-verify
```

`--practice` is repeatable and requires unique catalog practice IDs. The command
copies their predictions, sets `registered_at` to the current UTC time, and makes
equal adjacent `[since, until)` windows around the planned `applied_at`.
`--margin-pp` (also `--non-inferiority-margin-pp`) uses absolute percentage points.
All planning flags are required. The existing registration reader validates the
staged JSON before exclusive publication; an existing file is never overwritten.
Invalid flags publish no registration. Past planned times remain valid input
and produce a visibly late registration when compared. The command cannot
authenticate a timestamp or prevent later manual editing.

The top-level object has exactly the required fields below, plus optional
`confounders` and `outcome_source`. Duplicate keys and unknown fields are rejected. Numbers must be
finite JSON numbers, not booleans. All times are UTC ISO strings ending in `Z`
or `+00:00`. This file is local evidence: sumbi validates its contents, but does
not authenticate its timestamp or prevent an owner from editing it retrospectively.

```json
{
  "outcome_source": "github",
  "intervention_id": "round-01",
  "applied_at": "2030-01-08T00:00:00Z",
  "registered_at": "2029-12-31T00:00:00Z",
  "predictions": [
    {"metric": "tokens_per_success", "direction": "decrease", "rough_size_percent": 15}
  ],
  "non_inferiority_margin_pp": 5,
  "sample_size_per_arm": 1570,
  "follow_up_days": 7,
  "before": {"since": "2030-01-01T00:00:00Z", "until": "2030-01-08T00:00:00Z"},
  "after": {"since": "2030-01-08T00:00:00Z", "until": "2030-01-15T00:00:00Z"},
  "confounders": [{"at": "2030-01-09T00:00:00Z", "label": "runner_change"}]
}
```

- `intervention_id` and prediction `metric` use the ledger ID label charset.
  `predictions` is a nonempty array. Direction is `increase` or `decrease`;
  `rough_size_percent` is nonnegative. An optional string `condition` from a
  catalog prediction is accepted locally and discarded from public output.
- The prediction may be copied from `.sumbi/interventions.jsonl`'s `prediction`;
  that install record's `practice_id` and `utc_time` can supply the intervention
  ID and applied time. It does **not** provide margin, sample size, windows, or
  a full registration. Register these before application; do not treat an
  intervention record written later as independent proof of pre-registration.
- `outcome_source` is `github`, `local-verify` or `worker-github` and applies identically to both
  arms. Legacy registrations without it mean `github`; local comparisons require
  it explicitly. The CLI rejects a source override that differs from registration.
- Margin is strictly between 0 and 100 **absolute percentage points** of success
  rate. Five means 0.05, not five percent of the baseline. Sample size is a
  positive integer per arm; follow-up days is positive and finite.
- Both windows are `[since, until)`, nonempty, equal in length. Before must end
  at or before `applied_at`; after starts at or after it. A gap is allowed.
- A registration after application **or** after after-window start is late and
  produces `withhold: not_preregistered`. Equality is accepted. Late dates are
  valid input so their withheld result remains reviewable.
- Each optional confounder is exactly `{at, label}`, where `label` uses the ID
  charset. Labels are public-safe categories, never event descriptions or host
  names. `runner`, `model`, `effort`, or `cli`, alone or followed by `_...` or
  `-...`, identify blocking events. Other labels are informational.

### Arms, exposure, completeness and comparability

Dispatch assigns the candidate arm. Only dispatches inside the two windows enter
the comparison. The lifetime scan begins at before-window start and extends to
the latest repository observation, including retries and failures beyond either
dispatch window. Outcome evidence must cover each cohort's complete dispatch
window; missing PRs, missing repository observations, incomplete PR/commit
enumeration, or session parsing/token errors withhold at the coverage gate.
Unknown record types also block coverage. Duplicates successfully removed by the
adapters are counted and do not block. A run with no observed sessions withholds.
No local reader can prove that a deleted log was ever present: reported coverage
and the unlinked threshold expose observed gaps, not historical completeness.

A deliverable's linked sessions must all be on its dispatch arm's side of
`applied_at`. The boundary belongs to after. Both first and last observed session
timestamps are checked, including context and nonbillable records; a shared or
resumed session spanning application is conservatively mixed. This does not
assert event-specific harness versions that the logs cannot establish.

Both outcome sources accept optional `--interventions .sumbi/interventions.jsonl`.
The registered intervention ID selects the install record; optional
`--intervention-id` must match that registration ID. Apply with
`sumbi install --apply --intervention-id round-01` to attach a shared ID to all
practices in one installation. For older records without `intervention_id`, the
reader matches `practice_id`. Matching practice records must have exactly one
distinct valid UTC `utc_time`; missing, malformed or ambiguous apply evidence
fails the run rather than guessing. Revert audit rows are not apply evidence.
Use a fresh ID for each installation being compared.

The recorded `utc_time` is the install write transaction's time after its
pre-apply baseline, rather than the planned registration time. The report shows
`exposure_gap`: actual write time, earlier/later/equal direction, half-open bounds
and gap length in seconds. When actual write is later, dispatches in
`[applied_at, actual_write)` are excluded with reason `exposure_gap`. When earlier,
before-arm dispatches in `[actual_write, applied_at)` receive the same exclusion.
The lower bound belongs to the gap; its upper bound does not. Each exclusion is
counted in its originating arm and contributes to the mixed-exposure share gate.
Session endpoints in that interval are also mixed; outside it, exposure uses the
actual boundary. The install record describes a transaction, not per-file receipt
or proof that an agent loaded instructions. Without `--interventions`, the
registered boundary and existing exposure behavior are unchanged.

Mixed deliverables are excluded from both arms and counted by originating arm.
Unlinked deliverables are also excluded. A row with only weak project/time
evidence, or any weak allocation alongside stronger links, counts as unlinked;
weak allocation cannot support exposure or lifetime cost. The combined excluded
and unlinked share **over 10% in either candidate arm** blocks comparability.
Exactly 10% does not. Excluded work never silently becomes a failure or success
in the reported retained arm. At dispatch, put the deliverable ID in the
worktree folder or branch name: the existing evidence rules read those fields
to establish strong links.

Unattributed lifetime spend is `(unallocated + unassigned + weak-linked) /
(linked + unallocated + unassigned + weak-linked)`. Here `linked` means strong
linked tokens; the scan's linked bucket already contains weak-linked tokens,
so those tokens are counted once in the denominator. `unassigned` remains a
conservative inclusion because it may belong to this project. Spend confidently
attributed to `other` projects is excluded from both numerator and denominator
and reported separately as context. Shares **over 5%** block comparability.
The numerator and denominator are reported. Zero relevant observed tokens give
an undefined share, not a claimed zero share.

The checks basis must be identical across the retained arms. Every merged attempt,
including follow-up repairs, contributes its basis: historical snapshots cannot
be compared to current-policy or all-visible checks. A mixture inside a single
deliverable also blocks. Unknown merged checks block. Abandoned work and closed
unmerged PRs do not claim a checks-at-merge basis.

Mixes use unique linked sessions per arm, with one observation per distinct
session metadata value (several values in a session contribute several
observations). They are not weighted by tokens. Models, efforts and CLI versions
use collect's bounded labels/pseudonyms. Session metadata includes the agent.
Observability is assessed per agent and dimension over the retained sessions:

- An agent present in only one arm blocks comparison with `agent_mix_shift`,
  listing the affected agents even when the share change is small. Absence of an
  agent is not metadata asymmetry.

- If no session of an agent in either arm reports a dimension, an informational
  `<kind>_unobservable` flag lists that agent. Its sessions are left out of that
  dimension's mix. Changes in an unobservable dimension can only be caught by
  registered confounder events, which block comparison.
- If some sessions of an agent report the dimension and others in the same arm
  do not, `<kind>_metadata_partial` blocks comparison and lists the agents.
- If an agent is present in both arms but reports the dimension in one arm only,
  `<kind>_metadata_asymmetric` blocks comparison and lists the agents.

The mix-shift rule applies to the remaining reported session values. Tied
dominant values are reported as a set. A change in that dominant set, or total variation distance `0.5 * sum(abs(after_share - before_share))`
**over 0.2**, is blocking for each metadata dimension.

Registered runner/model/effort/CLI events inside either dispatch window block.
Events at an excluded window end do not. Other registered events are
informational. Arm sizes differing by more than 50% relative to the smaller arm,
and task-type mix total variation distance over 0.2, are informational. Task mix
uses deliverable counts, including an explicit missing category distinct from a
literal `not_reported` task label. Task breakdowns are descriptive: rates and
costs are shown, with no per-type verdict or multiple-comparison claims.

### Statistical methods

For eventual success, each retained arm reports successes / all retained
deliverables and the existing Wilson 95% interval. Immature and in-progress
deliverables remain in this descriptive denominator, but block a verdict.
The after-minus-before interval is Newcombe's hybrid score **method 10**, without
continuity correction. If the two Wilson intervals are `[La, Ua]` and `[Lb, Ub]`,
with rates `pa` and `pb`, the difference `d = pa - pb` has limits
`d - sqrt((pa-La)^2 + (Ub-pb)^2)` and
`d + sqrt((Ua-pa)^2 + (pb-Lb)^2)`.
Non-inferiority holds only when the lower limit is strictly above `-margin/100`;
inferiority holds only when the upper limit is strictly below it. Equality is
inconclusive. Empty arms have unreported rates and difference intervals.
See [Newcombe (1998), Statistics in Medicine 17:873-890](https://doi.org/10.1002/(SICI)1097-0258(19980430)17:8%3C873::AID-SIM779%3E3.0.CO;2-I).
Tests reproduce all eight method-10 worked contrasts in its Table II to the
published four decimal places and reuse the existing Wilson worked numbers.

The needed sample per arm is the equal-arm unpooled normal approximation
`ceil(2*p*(1-p)*(z(0.975)+z(0.8))^2 / (margin/100)^2)`, using the observed before
rate `p`, one-sided alpha 0.025, power 0.8, and true difference zero.
`statistics.NormalDist` supplies the quantiles. This is a planning approximation,
not an exact power calculation for the Newcombe interval; see
[the non-inferiority sample-size derivation](https://pmc.ncbi.nlm.nih.gov/articles/PMC2701110/).
The registered, estimated needed, and actual per-arm sizes appear together.
Actual sizes must reach the **registered** size. The diagnostic estimate never
rewrites that pre-registration after seeing outcomes. At baseline 0 or 1, this
approximation degenerates to zero; the report warns, and still enforces the
positive registered size. An empty before arm gives an unreported estimate.

Tokens per success sum each arm's **lifetime linked spend**, including failures,
retries, and repairs, then divide by eventual successes. Kinds and the total are
shown; the total excludes the reasoning subset. Time per success sums all arm
deliverables' dispatch-to-final-merge/abandonment elapsed seconds and divides by
successes. This is summed deliverable waiting time, not summed session activity.
Any retained final deliverable without an elapsed measurement blocks coverage.
Required token fields missing from billable events also block coverage; Codex's
structurally unsupported cache-write field and optional reasoning subset remain
unreported without blocking its reported total. Per-kind aggregates require a
reported value for every deliverable; partial aggregates are never zero-filled.

Bootstrap whole deliverables independently **within each arm**, with replacement
at its actual sample size. Every draw resamples success indicators and all
associated spend/time together, then recomputes cost per success and the
after/before ratio. One set of draws is reused across token kinds and time. The
default is seeded `random.Random(1729)` with 5000 draws; `--seed` and
`--resamples` (integer, at least 100) are recorded. Deliverables are sorted by
ledger ID before resampling, making input row order irrelevant. The 95% interval
uses the 2.5th and 97.5th empirical percentiles, linearly interpolated at
`(resamples-1)*q`. See [Efron and Tibshirani (1986), Statistical Science](https://doi.org/10.1214/ss/1177013815).
Intervals are also reported for each arm's aggregate cost per success.

A zero-success draw or zero before-cost denominator yields an undefined ratio.
Undefined draws are counted. If **any** draw is undefined, that metric's interval
is unreported and classification is uncertain; finite draws are never silently
conditioned on success. All-zero after cost with positive before cost is a real
zero ratio. Unreported cost stays null. A ratio is `improved` only if its point
estimate is <= 0.90 and its upper bound < 1.00; `worse` only if its lower bound
> 1.00; otherwise `uncertain`. Only **total tokens** and time drive the verdict;
per-kind ratios are descriptive.

An undefined point ratio is classified as `uncertain` too, including zero
successes, missing cost, or a zero before-cost denominator. The decision table
treats undefined cost as uncertain: it cannot prove improvement or an offset to
a worse cost in the other dimension.

### Decision order and public output

The catalog's `judgment_policy.decision_order` governs four stages:

1. Coverage: incomplete evidence proposes `withhold`, listing specific reasons.
2. Comparability: late registration or blocking flags propose `withhold`, with
   `not_preregistered` and/or `not_comparable` plus named flags.
3. Success: immature/in-progress work, undersized arms, and inconclusive success
   propose `withhold`; inferior success proposes `reject`.
4. Time and total tokens, once success is non-inferior:
   - Significant success improvement (difference lower bound > 0) with any
     worse cost proposes `owner_decides`, even if both costs worsen.
   - One improved cost and the other worse proposes `owner_decides`.
   - Both costs worse otherwise proposes `reject`.
   - One cost worse and the other uncertain, without significant success
     improvement, proposes `reject: tokens_worse_without_offset` or
     `reject: time_worse_without_offset`.
   - Neither cost worse and at least one improved proposes `adopt`.
   - Otherwise propose `withhold: no_detectable_change`.

Every verdict is explicitly a **proposal**. Earlier gates cannot be bypassed by
a later rejection. The owner remains responsible for approval and independent
review; no result modifies a gate. Predictions remain hypotheses and do not
select additional tests or per-type verdicts after seeing outcomes.

JSON contains numerator, denominator, estimate and interval for each inferred
rate/ratio, and per-success numerator/denominator/interval for cost and time.
The difference embeds both success-rate components. Accounting fractions carry
`interval_95: null` with `not_applicable_census`; bookkeeping counts, seeds,
thresholds and registered parameters have no sampling interval. Task cost
breakdowns explicitly use `not_estimated_descriptive`. Null means unreported or
undefined, never zero. The text summary reports the same components and labels.
Coverage counters, link evidence, pseudonymous session metadata, exclusions and
flag evidence make each allocation inspectable.

Compare additionally pseudonymizes deliverable and intervention IDs. PR,
repository and session IDs follow deliver's pseudonym policy. Task/event/metric
labels must be public-safe categories. Acceptance, notes, branches, prompts,
commands, outcome prose, paths, host names and prediction conditions are absent.
Use a local salt before sharing. See the authored
[round calculations and verdict cases](../tests/fixtures/compare/ROUND.md).

## Worker GitHub: fixed dispatches without a ledger

`worker-github` measures dispatched worker sessions against remote PR outcomes.
It requires no per-deliverable ledger. Scope is an explicit, repeatable list of
remote repositories, independent of the dispatcher's checkout location:

```sh
sumbi deliver --outcome-source worker-github --repo example/sample \
  --outcomes recorded-outcomes --home local-log-home \
  --since 2030-01-01T00:00:00Z --until 2030-01-15T00:00:00Z \
  --json out/worker-deliver.json
sumbi compare --registration worker-registration.json --repo example/sample \
  --outcomes recorded-outcomes --home local-log-home --json out/worker-compare.json
```

The registration explicitly names `"outcome_source": "worker-github"` and uses
the same M2 windows, predictions, margin, sample size, confounders and follow-up
days. A differing CLI override is rejected. Use `--outcomes github` for the
existing read-only live adapter with optional `--cache DIR` and `--record DIR`.
Its authentication, destination protection, checks-basis ladder, pagination,
observation-time cache semantics and private recording rules still apply.
`--ledger`, local verification options and project matching rules are rejected;
`--repo` is the authoritative remote scope. JSON and summary routing follow
the existing delivery/comparison commands.

Units are fixed from their own start metadata before live outcome collection:
Claude subagent streams and Codex subagent headers retain the existing worker
rule; a top-level Codex header additionally qualifies only when both
`source == "exec"` and `originator == "codex_exec"`. The local `dispatch_kind`
labels are `subagent`, `noninteractive_exec`, `interactive` and `unknown`.
Later or inherited headers cannot upgrade an interactive session into a worker.
These additional labels do not change existing collection or local-verification
outputs. Interactive and unrecognized top-level sessions are dispatcher overhead.

A unit enters remote scope when its start cwd's locally queried git origin
matches a measured repository, or its own explicit PR/branch evidence identifies
one. A sibling worktree or clone with the same origin is in scope; no physical
checkout-root equivalence is required for remote PR acceptance. An unavailable
cwd can still be scoped by an explicit measured PR. Unconfirmed repositories
and missing starts are counted separately; missing starts block comparison.
No parent's cwd, references, costs or outcome are inherited by a child.

Links use the existing bounded reference extraction from recognized tool inputs
and outputs, with additional literal worker branch observations. Ordinary
messages and unknown tools cannot link work. Every linked PR retains its
strongest own-session evidence:

| Evidence label | Strength and meaning |
| --- | --- |
| `pr_url` | Strongest: an exact GitHub PR URL in a recognized tool input or output. |
| `pushed_branch` | Strong: a literal push target or anchored push result. Input evidence proves the named attempted target, not a successful push. |
| `created_branch` | Strong: a literal branch creation or explicit `gh pr create --head` operand. |
| `cwd_branch` | Weak: structured cwd/context branch, branch query, or ordinary branch switch evidence only. |

Branches match exact PR `head.ref` values in measured repositories, restricted
to the start-origin repository when known. A branch match must refer to a PR
created at or after dispatch; a reused name cannot import an earlier PR. The
worker's branch can match a PR opened later by the main session without reading
the main session's evidence. All matched PRs are constituents; a failed
constituent cannot be dropped. If any link is only weak, the unit counts as
unlinked for comparison and is excluded with that reason.

Recorded fixtures may include `response.head.ref`; older fixtures without it
still support explicit URL links. The live worker capture retains that field
and uses a separate cache namespace so old cached pages cannot silently omit
branch evidence. Private recordings preserve refs; public reports contain only
pseudonymous branch, PR, repository and session IDs, evidence labels and counts.
They never emit branch text, PR numbers, origins, cwd paths or tool operands.

Worker states use the existing constituent checks and disturbance evidence,
with dispatch-level failure rules:

- `success`: every constituent merged green and its follow-up capture is mature
  and complete, with no detected revert or merged fix in the window.
- `failed`: closed unmerged work, red merge checks, a detected revert, a merged
  follow-up fix or a fix commit inside the follow-up window. A later repair
  cannot recover the original worker dispatch into success.
- `immature`: merged work with an open or incomplete follow-up capture.
- `in_progress`: an open PR, missing PR/check evidence or pending repair.
- `no_pr`: known edits without a linked PR, including conservatively incomplete
  edit evidence. It remains non-success in retained success and cost denominators.
- `no_change`: no recognized edits, excluded but counted even if PR evidence exists.

Comparisons use the shared M2 engine: actual apply-time exposure gaps, endpoint
exposure, per-agent metadata observability, agent/model/effort/version mixes,
sample size, Wilson and Newcombe intervals, whole-worker bootstrap and the
ordered coverage/comparability/success/cost verdict gates. Checks bases must be
uniform across every retained merged attempt, including detected follow-ups.
Missing PRs, incomplete repository capture, parse errors and missing required
token evidence block coverage. The combined weak/unlinked and exposure-excluded
share over 10% in either candidate arm blocks comparability. `no_pr` contributes
to this gate while staying in denominators; overlapping reasons count once.
The reported `risky_share` includes retained `no_pr` units as well as risky
exclusions. Exclusions whose reason is `no_change` do not contribute; exposure
gap exclusions still do. A no-change share shift over 0.2 blocks comparison.

All cost estimates and proposals use retained worker sessions only, including
failed and no-PR workers' full observed lifetime tokens. Time is dispatch to the
worker's last observed activity, labeled `observed_worker_span`, rather than
PR acceptance or human waiting time. Dispatcher overhead and excluded worker
spend appear separately. An adopt proposal concerns worker costs only.
Shell edits are invisible, missing/deleted logs are undetectable, branch reuse
is conservative, and disturbance patterns are bounded evidence rather than
semantic review. Sources cannot be mixed between arms.

## Local verification: fixed worker-session outcomes

Workspaces without a pull request flow can explicitly select `local-verify`.
This is a separate, weaker outcome source; it cannot certify human acceptance
or absence of later reverts. Declare the verification scripts in the repository:

```toml
# .sumbi/config.toml
verify = ["scripts/check.sh"]
```

```sh
sumbi deliver --outcome-source local-verify --repository workspace \
  --home local-log-home --since 2030-01-01T00:00:00Z \
  --until 2030-01-08T00:00:00Z --scan-until 2030-01-09T00:00:00Z \
  --json out/local-deliver.json

sumbi compare --registration local-registration.json --repository workspace \
  --home local-log-home --scan-until 2030-01-16T00:00:00Z \
  --seed 1729 --resamples 5000 --json out/local-compare.json
```

The registration must name `"outcome_source": "local-verify"`. Its existing
windows, margin, sample size, predictions and confounders apply. The follow-up
field remains required by the shared schema but provides **no local acceptance
or revert window**. Do not supply a ledger, GitHub outcome directory, cache or
record option. Repeated `--verify scripts/check.sh` declarations replace the
TOML list for a run. Both arms use this single declaration set; sumbi does not
authenticate retrospective edits to registration or verification configuration.
`--scan-until` must cover the dispatch window and defaults to its end. Outcome
evidence and lifetime spend stop at this explicit observation cutoff.

Adapters retain an agent-neutral `CommandExecution` event locally: agent,
session, completion time, command and exit code, with start time and cwd evidence
for safe interpretation. Codex recognizes Desktop `item_completed` /
`CommandExecution`, item start times and older `exec_command_begin/end` pairs.
Codex verification completion requires a numeric `exit_code` machine field;
anchored text in output can inform legacy friction counters but never supplies a
verification outcome.
Execution cwd comes from the execution's explicit field, a paired start's field,
or unambiguous own-session header/turn context in force at its recorded start.
A later context cannot retroactively confirm cwd. Completion-only records with
a valid start time can use historical context; unknown start times, ambiguous
or undated contexts remain unknown. Unrelated tool workdirs and pre-dispatch
replayed context do not prove the worker's execution cwd.
Claude Code pairs `Bash` tool uses and results by ID. The foreground result
envelope with `interrupted: false`, string stdout/stderr and `is_error: false`
means exit zero even where no numeric exit field is logged. An error's anchored
`Exit code N` envelope or numeric machine field supplies nonzero codes. Missing,
interrupted, deferred/background, unpaired or unsupported results remain unknown.
The tool-use input `run_in_background: true` forces an unknown exit even if its
result omits a task ID or resembles a successful foreground envelope.
Text inside stdout never supplies an exit code. Commands, stdout and edit payloads
are never included in public JSON or text summaries.

`deliver` and `compare` report verifier health separately from worker success.
At least one matched completed execution with a known integer exit code and no
exit zero in the measurement window emits `verifier_never_passed`. JSON includes
completed and passed counts and counts by exit code; the signal appears on the
first lines of the text summary. It describes a never-passing verification gate,
including launch or prerequisite failures, and does not prove that tests ran.
Scoped parent sessions and no-change workers contribute to this health signal.
Unknown exit codes remain coverage gaps and do not count as known completions.
Completions outside the dispatch measurement window, including later lifetime
follow-up passes, cannot suppress it. Comparison counts each registered arm
window separately, excluding any intervening hiatus. Any arm with matched
completed verifications and zero passes emits the signal, even if the other arm
passes. JSON evidence includes completed, passed and exit-code counts per arm;
the text summary identifies the failing arms. The signal is a blocking
comparability flag with reason `verifier_never_passed`, after coverage and before
success non-inferiority. This prevents success-based proposals from interpreting
a never-passing gate as an intervention effect. Commands are never emitted.

Recognition accepts a single executed repository-relative script, including
`./scripts/check.sh`, `bash`/`sh`, `-l`/`--login`, quoted `-c`/`-lc` scripts,
environment assignment prefixes, PowerShell's `&` and quoted shell executable,
and `pwsh`/`powershell -Command` wrappers. It matches the entire normalized script
path rather than a basename. Only a bare script invocation counts: trailing
arguments, including help, filters and dry-run options, are `unmatched_shape`.
There is no permitted-argument configuration in this alpha. Compound environment
assignment or export preludes remain unmatched even when their final statement
mentions the script; they cannot establish positive evidence.
Execution cwd must establish that the declared
script is the target repository's script. A same-named script in a different cwd
does not pass. Empty-authority local `file:` URIs with an absolute Windows drive
path are strictly decoded to the same full-path identity. Remote authorities,
malformed escapes/UTF-8, control characters, queries, fragments and encoded separators are
unsupported; there is no basename or broad URI-prefix fallback.
Reads (`Get-Content`, `cat`, `type`, `rg`, `grep`, `Select-String`,
`sed`, `head`, `tail`) do not count, including quoted mentions. Shell syntax-only
checks (`-n`/`--noexec`) do not execute the verifier. Conditional commands,
pipelines, background execution, redirects, substitutions and unsupported options
with a declaration mention are counted as `unmatched_shape`, withholding a
comparison rather than guessing. Coverage holds only counts and safe labels.

Worker units are fixed from session start metadata before their outcomes are
read. Codex subagent metadata and Claude subagent streams identify dispatched
workers; a parent's existence is not required to create its child unit. Missing
start metadata is counted and blocks comparison. The dispatch timestamp selects
the cohort independently of whether the worker edits anything or runs a check.
Sessions starting outside the dispatch window cannot enter merely because they
verify inside it. Parent orchestrators are separately reported as dispatch
overhead, even if they span the intervention.
Edits, completed executions and activity used for a worker outcome are bounded
to `[dispatch, scan-until)`. A verification start must also fall in that lifetime
and strictly after its last known edit. Inherited pre-dispatch edits and checks
cannot make an unchanged child successful or give it negative elapsed time.
Conflicting paired start and completion timestamps, including differences in
timestamp precision, or an untimed paired start leave execution start and cwd
unconfirmed. Later context or an explicit completion cwd cannot repair that
missing ordering proof.
Contradictory explicit start and completion directories likewise leave cwd
unconfirmed, even when the timestamps agree. Equivalent normalized directories
retain their evidence.
Claude's earliest timestamp and earliest cwd-bearing event are preserved
separately; an earlier metadata event without cwd does not erase that cwd.
Local-only start provenance distinguishes session headers from first-observed
cwd evidence. Excluded-scope counts include only sessions observed in the scan.

Install placement accepts a Claude worker's own first-observed cwd from its
subagent stream, where records can repeat `cwd`, `isSidechain`, `agentId` and
`sessionId`. This remains `first-observed-cwd`, distinct from Codex's
`session-header`; placement JSON and text expose counts of each provenance kind,
globally and per path. Repeated records still count as one session. A child's
missing cwd is never repaired by borrowing its parent's cwd. Parent-inferred or
missing worker provenance remains unknown and supplies no placement vote.

Each unit reports a bounded start-scope label. Starts at the configured root or
its physical subdirectories belong to this workspace; subdirectory workers can
verify the declared script by executing it from the configured root. The outcome
cwd rule remains exact-root. A clone or worktree physically outside that workspace
may be admitted as a candidate by shared git-origin evidence, but remains a fixed
unit with `same_origin_other_checkout` scope and blocks comparison with
`unit_start_scope_mismatch`. This alpha does not claim equivalent verification
roots for other checkouts and never substitutes a shared origin for positive cwd
evidence. A change in worktree placement cannot silently change comparable rates.

Known edit tools are Codex `apply_patch`/`FileChange` and Claude
`Edit`/`Write`/`MultiEdit`/`NotebookEdit`. The five states are:

- `success`: the last matched completed verification started strictly after the
  last known edit, has confirmed target cwd and exited zero.
- `failed`: the same machine evidence exited nonzero. A launch or prerequisite
  failure is a failed command attempt; this does not prove that tests executed.
- `unverified`: known edits lack such an execution. This includes an unknown last
  exit, unknown start/cwd, or a check begun before the edit. An earlier pass does
  not survive a later unknown check. It counts as non-success.
- `in_progress`: edits exist and activity was observed within `--active-minutes`
  (default five) of scan end without a later completion signal. It is excluded
  but counted. Freshness is a heuristic; absent terminal events are not proof of
  continuing or completed work.
- `no_change`: no known edits. It is excluded but counted even if a check passed.

Tied edit/check times cannot establish ordering. Units' elapsed time is dispatch
to last observed activity, **not human acceptance time**. Shell-based edits are
invisible, attempted edit tools count conservatively, and deleted or unlogged
sessions cannot be detected. CLI reports these limitations alongside the source.

Local comparison reuses Wilson success intervals, Newcombe method 10,
pre-registered sample gates, whole-unit deterministic bootstrap ratios and the
same ordered verdict policy. `unverified` remains in all retained denominators
and cost numerators. Recent `in_progress` and mixed-exposure units are excluded;
their combined share over 10% in either candidate arm blocks comparability.
No-change exclusions remain visible and a difference in their shares over 0.2
blocks comparison. A worker spanning application is mixed exposure. Agent mix,
model/effort/version observability, mix shifts, registered confounders, missing
token fields, unknown command/result evidence and parse errors retain conservative
gates. Structurally unsupported token kinds stay null, not zero.

Every local cost estimate and proposal is **retained worker-session scope only**.
Lifetime token costs include all observed worker spend from start to scan cutoff,
including failures and verification retries. Lifetime time costs use each
worker's observed elapsed span. Parent overhead and excluded worker spend are
shown separately with observed partial totals; neither is fabricated into an
individual worker or its bootstrap. A parent spanning an intervention does not
poison worker-only comparisons. An `adopt` proposal cannot support a claim of
whole-workspace savings: orchestration and excluded work require separate study.
