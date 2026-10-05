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
| Claude Code | CLAUDE.md ancestors at launch, imports expanded, nested files on demand. AGENTS fallback is conditional. | [Claude memory](https://code.claude.com/docs/en/memory) |
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

Placement assumes each agent's default project discovery and filename settings.
Codex project-root overrides, fallback filenames and byte limits can change the
result. Gemini workspace/trust settings and Copilot/Cursor repository context
are also outside a cwd-only observation. Global files are described by the table
but are never read or modified. Placement is path guidance, not loaded-context
measurement; it does not automatically move files or weaken approval rules.

The install CLI reads the previous fourteen UTC days through every registered
collect adapter. Each distinct agent/session contributes one start, using fixed
start metadata when available, otherwise the first dated cwd observation.
Resumed directory changes do not add launches. The fallback's window timestamp
comes from that cwd observation, not inherited parent metadata. Starts outside
the window, outside the workspace, excluded, unsafe, missing or ambiguous are
counted separately. A fallback cannot prove the actual launch directory. JSON
and text show agent labels, counts and workspace-relative paths only, never
raw session IDs, prompts, commands or absolute paths. Paths are local inventory
evidence and can reveal repository names; review them before sharing a plan.

Starts are grouped into workspace root, nested repository, and other subfolder,
with the nearest discovered repository recorded for each subfolder. Adapter
parse/read failures are visible as coverage diagnostics; inventory still works.
Unknown adapters get `load-rules-unverified`. Uncertain paths receive
`target-load-unknown`. A planned instruction target receives `target-not-loaded`
when more than half that agent's observed starts miss its default launch path.
Ties do not warn. Recommended entry paths are ranked by the missed start counts
they address. A non-git workspace root can therefore need root guidance for one
agent and separate nested-repository guidance for another.

The install API exposes `read_starts`, `observed_starts`, and
`annotate_placement` for callers supplying normalized sessions or an explicit
window. Plan construction remains offline and can be used without log reads.
