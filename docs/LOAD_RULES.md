# Instruction discovery and observed placement

The installer vendors `sumbi/install/load_rules.v1.json` as a versioned table
of agent entry files, scopes, support labels, and configuration conditions.
`verified` means checked against primary documentation on the table's date;
it does not certify receipt by a running agent. `secondary` is reserved for
secondary evidence. Generic AGENTS consumers remain `unverified` because they
do not share one discovery algorithm. Adding a collect adapter requires no
installer-specific log parser: the registry supplies normalized sessions.

| Agent | Default entry and discovery | Primary documentation |
|---|---|---|
| Codex | Global guidance and one entry per directory from project root through cwd; without a root, cwd only. Overrides precede AGENTS.md. | [Codex discovery](https://developers.openai.com/codex/guides/agents-md) |
| Claude Code | CLAUDE.md ancestors at launch, imports expanded, nested files on demand. Rules with non-empty paths front matter activate on matching file reads; other rules are always loaded. AGENTS fallback is conditional. | [Claude memory](https://code.claude.com/docs/en/memory) |
| Gemini CLI | Global GEMINI.md, configured workspaces and parents, then trusted context on demand. | [Gemini context](https://geminicli.com/docs/cli/gemini-md/) |
| Copilot | Repository-wide .github/copilot-instructions.md; additional instruction features vary by product. | [Copilot instructions](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/add-custom-instructions/add-repository-instructions) |
| Cursor | .cursor/rules/*.mdc; alwaysApply must be true for every chat. Other types are conditional. | [Cursor rules](https://cursor.com/docs/rules) |
| Generic AGENTS consumers | Entry AGENTS.md; discovery must be checked for the specific agent. | [AGENTS convention](https://agents.md/) |

Claude's documented default fallback requires v2.1.277 or later and no
CLAUDE.md, .claude/CLAUDE.md, or CLAUDE.local.md in cwd or its ancestors.
Some sessions before v2.1.281 cannot use it. Settings, exclusions and subagent
instruction policies can change receipt. An explicit CLAUDE.md import remains
compatible. The installer reports AGENTS fallback as unknown because session
settings and ancestors outside the inspected root are unavailable.

Revision 2 of the version 1 load-rule table records Claude rule activation:
`.claude/rules/*.md` with non-empty `paths:` in YAML front matter is conditional.
Missing or empty `paths:` is always loaded. The inventory supports scalar,
flow-list and block forms without a YAML dependency. Conditional rules appear
in `cost.conditional_instructions.claude`, separately from always-loaded root
and largest-scope estimates, and cannot alone trigger `instruction-budget`.
These estimates use `script-aware-v1`: `ceil(other_characters / 4)` plus one
token per Hangul, Han, Hiragana or Katakana character. This is an upper-leaning
heuristic, not a tokenizer or a guaranteed upper bound; the output names the
rule. Unique files count once within each estimate and sibling scopes are not
summed. Cost does not prove actual instruction receipt.

Placement assumes each agent's default project discovery and filename settings.
Codex project-root overrides, fallback filenames and byte limits can change the
result. Gemini workspace/trust settings and Copilot/Cursor repository context
are also outside a cwd-only observation. Global files are described by the table
but are never read or modified. Placement is path guidance, not loaded-context
measurement; it does not automatically move files or weaken approval rules.

The install CLI reads the previous fourteen UTC days through every registered
collect adapter. Each distinct agent/session contributes one session start,
including dispatched workers. This measures sessions, not operating-system
process launches. Fixed start metadata is preferred when an adapter provides it;
older adapters use the first dated cwd observation for top-level sessions.
Resumed directory changes do not add session starts. The fallback's window timestamp
comes from that cwd observation, not inherited parent metadata. Starts outside
the window, outside the workspace, excluded by globs or Git ignore rules,
unsafe, missing, non-directory or ambiguous are counted separately. Ignore
rules are checked locally in the root and nested repositories; ignored roots
are never exported. If Git ignore checks are unavailable, the affected cwd is
withheld and coverage is unknown. A fallback cannot prove the original start cwd. JSON
and text show agent labels, counts and workspace-relative paths only, never
raw session IDs, prompts, commands or absolute paths. Paths are local inventory
evidence and can reveal repository names; review them before sharing a plan.

Starts are grouped into workspace root, nested repository, and other subfolder,
with the nearest discovered repository recorded for each subfolder. Total and
per-agent counts visibly separate top-level and worker sessions, as do path
rows. `sessions` includes safe, scoped workers whose start evidence is unknown;
`placement_sessions` and path rows include only sessions eligible for guidance.
Worker placement requires a fixed `start_cwd` and either `session-header` or
`first-observed-cwd` provenance from its own stream. A Codex worker's own header
establishes its session start even when it shares its parent's directory.
Claude subagent records can repeat their own `cwd`, `isSidechain`, `agentId` and
`sessionId`; their earliest cwd observation supplies placement evidence without
claiming a session header. Global and per-path `start_evidence` counts keep these
provenances distinct in JSON and text. Neither adapter borrows a parent's cwd.
Parent-inferred or missing worker provenance contributes `worker_start_unknown`
and supplies no paths or recommendation votes; missing or ambiguous cwd has its
own coverage count. Standalone installs using older adapters conservatively
report unknown worker coverage. Adapter
parse/read failures are visible as coverage diagnostics; inventory still works.
Unknown adapters get `load-rules-unverified`. Uncertain paths receive
`target-load-unknown`. A planned instruction target receives `target-not-loaded`
when more than half that agent's eligible observed session starts miss its
default instruction discovery path. Worker fan-out can affect this session-based
majority when each worker has its own start evidence; visible role counts expose that
weighting. Unknown worker starts never decide the majority.
Ties do not warn. Recommended entry paths are ranked by the missed start counts
they address. A non-git workspace root can therefore need root guidance for one
agent and separate nested-repository guidance for another.

A start outside the inspected workspace can still belong to the same
repository: local git must report a different worktree root and the same
resolved git common directory. Identity queries disable `core.fsmonitor` and
`core.untrackedCache`, just like ignore queries; no folder-name inference or
network query is used. These starts have `kind` and `scope`
`same-repository-worktree`, with only the relative path inside that worktree.
They contribute to repository placement counts and never to
`outside_workspace`. Ignore rules are checked in the starting worktree;
exclusions, directory/link safety and worker provenance still apply. Failed git
identity queries cannot establish membership. Worktree names and absolute paths
stay out of reports. Placement maps these starts to the inspected repository's
relative instruction layout; it does not verify other checkouts' file contents
or copy an installation into them. Checkout differences and ancestors outside
each worktree can change actual receipt.

Convention coverage is independent of launch discovery: loaded guidance
candidates and owner declarations produce `present`, `absent` or `unknown`.
The vendored English/Korean lexicon is extendable per language. Predominantly
uncovered scripts produce `convention-language-unsupported` with script labels,
and undetected conventions produce no gaps. Existing paths declared under
`[conventions]` in `.sumbi/config.toml` supply `declared` evidence; missing paths
warn without supplying presence. See the [install guide](../sumbi/install/README.md)
for declaration keys and safeguards.

The install API exposes `read_starts`, `observed_starts`, and
`annotate_placement` for callers supplying normalized sessions or an explicit
window. Plan construction remains offline and can be used without log reads.
