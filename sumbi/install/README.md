# Offline install (M1b)

Python 3.11 or later and the standard library are sufficient. Use the shared CLI;
`python -m sumbi.install` remains available for existing callers:

```console
sumbi install
sumbi install --dry-run --json plan.json
sumbi install --apply --select handoff,parallel-worktrees
sumbi install --exclude 'samples/**' --exclude '**/generated'
python -m unittest discover -s tests
```

`--root` selects a repository; the default is the current directory. Dry-run
only reads unless `--json` explicitly requests a new plan file. JSON contains
the full inventory, evidence, proposals, zero-context unified diffs, and hashes.
Its destination must be repository-relative, have an existing parent, and not
already exist or overlap a planned target or repository metadata.

The default plan includes all candidate practices. Use `--select` to start
with one to three practices for a later measured round. IDs that do not address
a current gap produce no change. Unknown IDs fail before application.

## Inventory and gaps

Inventory never executes a project command or loads a repository module. It
asks local git for work-tree boundaries and standard ignore rules, offline.
It reports paths, counts and parsed workflow job labels. Commands, prompts,
permission values, MCP details and instruction text are not exported. Imports
are resolved relative to their source file, only within the repository; cycles
are bounded and external, missing and linked targets are visible without being
read. Imports inside Markdown fenced code blocks (backticks or tildes, including
unterminated blocks) and inline backtick spans are ignored. Required checks and
rulesets remain `unknown (offline)`.

If the inspected root is not inside a git work tree, inventory reports
`root-not-versioned` and lists discovered nested git repositories as relative
paths with a count. Applying at such a root can write files outside any review
or pull request; the warning does not change `--apply` behavior. Discovery follows
the same exclusions and link restrictions as the rest of inventory.

Codex starts inside a nested repository normally stop discovery at its git root.
Claude Code discovers ancestor `CLAUDE.md` files and can load `AGENTS.md` under
version/settings-dependent fallback rules. Install reads recent session starts
through the collect adapter registry and reports `target-not-loaded` with
recommended entry paths when a planned instruction misses most eligible observed
session starts. Top-level and worker counts are shown separately. Workers with
only an inferred first cwd contribute unknown coverage, not recommendation votes.
Ignored, missing and unsafe cwd paths are withheld from placement output.
Starts in another local worktree of the inspected repository are recognized by
the same git common directory, queried with `core.fsmonitor=false` and
`core.untrackedCache=false`. They use scope `same-repository-worktree` and paths
relative to that worktree; its folder name and absolute path are not exported.
They count toward repository placement, rather than `outside_workspace`.
Folder names never establish repository identity.
See [load rules and limitations](../../docs/LOAD_RULES.md). Inherited cost
estimates remain heuristics, not proof of loaded context.

Instruction size uses the named `script-aware-v1` rule after normalizing line
endings: `ceil(other_characters / 4)` plus one token per Hangul, Han, Hiragana
or Katakana character. This is an upper-leaning heuristic, not a tokenizer or
a guaranteed upper bound. JSON and text report the rule name so estimates from
different rules cannot be confused. Skill descriptions use the same rule.
Unique imported files count once
per agent. Root instructions are reported separately from the largest inherited
scope: siblings are never summed as if they loaded together. Nested instructions
include ancestor instructions and their import closures. The largest scope is
chosen by estimated tokens, rather than character count. Claude Markdown rules
with non-empty `paths:` in YAML front matter are conditional: they load when
matching files are read. Their paths, characters and token estimate are reported
separately in `cost.conditional_instructions.claude`, excluded from the
always-loaded instruction budget. Missing or empty `paths:` stays always-loaded.
The front-matter check supports scalar, flow-list and indented block forms; it
is a bounded YAML subset and does not execute tags or path expressions.
Other agents' rule estimates remain conservative. No estimate measures a
particular task's loaded context. Dynamic/global agent configuration
is not read. Skill-description estimates
use UTF-8 text front matter, including common scalar and block descriptions.
Every-prompt counts identify declared `UserPromptSubmit` hooks; sumbi does not
claim that a configured hook actually ran.

Files larger than 1 MiB, invalid UTF-8/configuration, unsupported workflow
syntax and inaccessible directories produce diagnostics. Default exclusions
prune `.git`, `.sumbi`, `.tmp`, `node_modules`, `vendor`, `.venv`, `venv`, `dist`,
`build` and `__pycache__` at any depth. A `fixtures` or `testdata` tree beneath
any `tests` or `test` directory is excluded too. Other nested instruction files
belong to the repository's own scopes, including instructions for test code.
The ordinary `tmp` directory remains eligible source.

Within git work trees, inventory also prunes ignored paths using git's standard
rules: `.gitignore`, `.git/info/exclude` and the configured global excludes file.
Inventory disables repository-configured git programs.
Tracked files remain eligible even when an ignore pattern matches them. Nested
repositories use their own ignore rules. Ignored roots are counted separately
as `gitignore_count` and included in the total exclusion count, without counting
descendants. Imports into these paths are `excluded-not-read`. Planned targets,
including absent files, are checked with git too, and changed ignore rules are
rechecked when applying. Sumbi's own `.sumbi/` metadata handling stays unchanged.
If git is unavailable, fails or times out, inventory reports
`gitignore-unavailable` and falls back to glob exclusions for the affected work
tree. Versioning is unknown when git cannot establish the root boundary.

Repeat `--exclude GLOB` to add repository-relative exclusions, or configure:

```toml
# .sumbi/config.toml
exclude = ["samples/**", "**/generated"]
```

The installer reads this optional file with `tomllib` before pruning `.sumbi`.
Invalid configuration fails with a sanitized error. Defaults, configuration
and command-line patterns are additive. `*`, `?` and character classes match
within one path segment; `**` matches zero or more segments. Matching ignores
case for consistent Windows and WSL behavior. A matched directory excludes
its entire subtree. Use forward slashes, without absolute paths or traversal.
The report gives only patterns and counts of pruned roots (files or directories),
not their paths or descendant counts. Imports into excluded trees are recorded
as `excluded-not-read` without the target path; instructions in those trees
cannot import back out because they are never scanned. Exclusions also govern
cost scopes, convention evidence, catalog targets and apply-time plan validation.

Links and Windows junctions are not followed. Workflow parsing supports block
YAML with plain or quoted job IDs and scalar display names, without a YAML
dependency. Flow mappings, anchors and multiline names have explicit unknown
diagnostics; this is not a complete YAML parser.

Each convention reports a `status` of `present`, `absent` or `unknown` and an
`evidence` list of paths with kinds `directive` or `declared`. Only `absent`
produces a convention gap or an installation proposal. Detection is a
conservative text heuristic over agent entry files,
rules, skills, commands, subagents and resolved Claude imports. Ordinary README,
roadmap and design documents do not supply evidence unless actually imported
as instructions. Markdown links alone do not load instructions. Only directives
count; idea listings, examples, fenced snippets, metadata and explicit denials
are ignored. Its evidence identifies files, not enforcement proof.
The versioned `sumbi/catalog/conventions.v1.json` lexicon supplies English and
Korean directive patterns and exclusions for review gates, handoffs, plan
approval, worktrees and friction lines. Languages can be added by extending
its script labels and patterns without changing code. If more than half of
the searched guidance's letters use uncovered scripts, undetected conventions
are `unknown`. `convention-language-unsupported` lists dominant script
categories, never source text. Front matter and fenced code do not vote on
language coverage. A directive match still establishes `present`.

An owner can declare implementation paths, including documents not otherwise
searched, in `.sumbi/config.toml`:

```toml
[conventions]
review_gate = ["docs/review.md"]
handoff = ["docs/handoff.md"]
plan_approval = ["docs/planning.md"]
parallel_worktree = ["docs/worktrees.md"]
friction_line = ["docs/reporting.md"]
```

An existing file or directory supplies `declared` evidence and makes the
convention `present`, including when its language is unknown. Declarations
check existence without reading implementation content, including excluded
paths. A missing path produces `convention-declaration-missing` and supplies
no presence evidence. Unknown IDs, malformed path lists, links, absolute paths
and traversal are rejected with sanitized errors. Paths use forward slashes
and must stay within the inspected repository.
CODEOWNERS alone does not establish a risk review gate. A configured test runner
or a documented test command supplies test evidence; no test command is printed.

## Additive application

Each practice inserts a block bounded by:

```markdown
<!-- sumbi:begin practice-id -->
Catalog text.
<!-- sumbi:end practice-id -->
```

Existing bytes remain an exact prefix, including BOMs and newline style. New
files contain managed blocks too. Existing blocks are never replaced, even if
an owner edits their text. Malformed, nested or duplicate markers fail closed.
An existing target without a final newline must be fixed manually: producing a
correct diff for its last line would otherwise expose existing source text.
These deliberately zero-context diffs show only catalog additions.

The plan is rebuilt against the bundled catalog before application. Stale
snapshots, linked or multiply linked targets, competing installs and oversized
targets are rejected. New files are published exclusively via same-directory
hard links; existing files are staged, rechecked, and atomically replaced with
their original bytes plus new blocks. Filesystems without hard-link support
fail safely. Existing file modes are preserved where the OS supports them;
new practice files use mode 0644, local metadata and backup files use 0600,
and the local metadata directory uses 0700. Windows permissions still depend
on inherited ACLs.

Before target writes, `.sumbi/backups/<UTC timestamp>/files/` holds original
bytes and `manifest.json` records original modes, hashes and absent new files.
Version 1 manifests also record applied target hashes and change status from
`prepared` to `applied` only after successful writes.
The manifest also pre-registers the planned intervention predictions. Successful
application appends one record per practice to `.sumbi/interventions.jsonl`;
the existing ledger is backed up too. `content_hash` is SHA-256 of the canonical
catalog practice object, including its template text and prediction. Records
carry the catalog version, source IDs, touched paths, judgment policy and UTC
time. A second run with the same selection adds no files, backups or records.

Write failures trigger rollback without clobbering concurrent changes. Backup
files remain for recovery and are not deleted automatically. A process killed
during application can leave `install.lock` and a prepared manifest: compare
the manifest's hashes with local files, restore the listed originals or remove
listed newly created files, and remove the lock only after resolving the
transaction. If another writer changed a target, recovery needs owner attention.
On first apply, `.sumbi/.gitignore` excludes `backups/`, `baseline/` and
`install.lock`; `interventions.jsonl` stays committable. Existing ignore-file
bytes are preserved, with the local exclusions appended if needed. Protection
is written before collection and backup creation and stays in place if later
application fails. Do not commit backups: they contain original repository
bytes and are local recovery data. This command never stages or commits files.

Instruction-budget guidance does not trim existing content. The testing
practice leaves the exact command to the owner. Those gaps remain visible until
the owner completes setup, even though the same blocks are not proposed again.
Review guidance is text only; it does not establish required checks or a ruleset.

## Reverting an installation

```console
sumbi install --root workspace --revert BACKUP_ID
```

`BACKUP_ID` is the timestamp directory name printed by apply, beneath
`.sumbi/backups/`. Revert requires a completed version 1 manifest with applied
hashes. Old or prepared manifests require manual recovery and fail safely.
Backed-up original bytes must match their recorded hashes before any mutation.
Links, hard-linked targets, traversal, duplicate paths, unsafe portable names,
and repository metadata targets are rejected. Manifests are local recovery
evidence, not authenticated against deliberate editing by their owner.

For each target, revert restores original bytes and mode, or deletes a file
that was originally absent, only when its current hash equals the applied hash.
Modified, missing or unsafe targets are refused individually; other eligible
files can still be restored. Refusals return a nonzero CLI status. Original
POSIX permission modes include set-ID and sticky bits where supported; manifest
modes exclude file-type bits. The ledger retains its current permission mode.
Revert never restores the old intervention ledger: it appends an `action: revert` record with
backup ID, UTC time and per-file statuses, retaining earlier and later records.
If the ledger append fails, reverted files are rolled back without clobbering
concurrent edits. The shared exclusive install lock also guards revert.

Successful paths are remembered by the audit records. Repeated revert skips
them even if new owner work or a later installation has changed those paths.
Unchanged retries add no duplicate record; changed per-file outcomes append one.
Refused paths can be retried after the owner restores exactly the applied bytes.
Already-original files without a successful audit record are refused rather
than guessed to have been reverted. Empty parent directories, backups, baseline
artifacts and `.sumbi/.gitignore` remain deliberately: backup ignore protection
must survive while recovery data exists. This command never stages or commits.

## Baseline integration

The integration point is `sumbi.collect.baseline(*, repository: pathlib.Path,
home: pathlib.Path | None = None, salt: bytes | None = None)`.
Dry-run probes for this callable but does not invoke it. Apply invokes it once,
before target writes. The collect implementation must stay read-only and offline
and own its aggregate baseline artifact. Apply passes `--home DIR` and the local
pseudonym key selected by `SUMBI_SALT` or `--salt-file FILE`. The collector reads
the previous 14 UTC days, keeping sessions attributed to the target repository
by normalized origin or a path fallback. It writes counts-only JSON to
`.sumbi/baseline/<UTC timestamp>.json` and returns `recorded` or `no sessions found`.
The installer copies only a recognized status, validated relative artifact ID
and session count to the ledger, never arbitrary collector text. Empty reports
are valid baselines. See [measurement scope](../../docs/MEASUREMENT.md).

If the entry point is missing, output and interventions retain the compatibility
status `pending (collect not available)`. An import or execution failure in an
available collector aborts application. Empty plans never run collection.
