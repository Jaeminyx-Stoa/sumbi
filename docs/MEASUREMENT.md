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
`checks_missing_required`. `required_checks_at_merge` is retained as a compatibility
alias for the checks verdict; consumers must read its accompanying basis.
Rounds compared later must use the same basis; M2 will enforce this.

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
Live recordings additionally retain `checks_policy_at_merge`, an object with
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
`not_evaluated`; M2 will establish completeness thresholds and comparison rules.
