# Synthetic log fixtures

All records are authored for these tests. No record or text was copied from a session.
The adapters were informed by machine-record field shapes: Claude Code assistant
usage/content blocks and sidechain records; Codex cumulative token counts, turn
contexts, response calls, completed command items and task pairs. Fixture cwd
markers are replaced with temporary paths by the tests. No developer path is stored.

The window is ten minutes starting at midnight UTC on January 1, 2030.

| Fixture | New input | Cache write | Cache read | Output | Reasoning subset | Total |
|---|---:|---:|---:|---:|---:|---:|
| Claude parent | 13 | 25 | 37 | 14 | not reported | 89 |
| Claude child | 1 | 0 | 2 | 3 | not reported | 6 |
| Codex, both files together | 65 | not reported | 65 | 38 | 12 | 168 |

Claude's three included messages contribute 65, 10 and 14 tokens. The streamed
message finalized at the excluded end contributes nothing to this window.
Codex's four deltas contribute 40, 70, 23 and 35 tokens. The third is a reset;
the new counter starts from zero. Reasoning is included in output, not added again.

Both parent sessions have a 600-second clipped wall span and 600 estimated active
seconds at each of 2, 5 and 10 minutes. The child has one timestamp and zero span.
Claude paired tool intervals contribute 120 + 60 + 120 = 300 observed seconds.
Codex paired tools contribute 60 + 60 + 60 = 180 observed seconds and its paired
request contributes 120 seconds. Parallel durations are separate measures.

`test_attribution.py` authors additional fixtures directly in temporary homes:

- A Claude session switches repositories: the first message costs
  10 new + 3 cache write + 5 cache read + 2 output = 20 tokens; the second costs
  10 + 3 + 5 + 7 = 25. Each repository counts one event and one containing session.
- A Codex session switches cwd between counters (10 input, 3 cached, 2 output)
  and (30 input, 8 cached, 7 output). The first delta costs 12, the second 25.
  The second delta belongs entirely to the cwd at its closing snapshot.
- Four Claude messages cost 20 each. A 299-second gap carries the first project;
  a subsequent 300-second gap does not, and another evidence-free event stays
  unassigned. The first project gets 40 tokens, half by fallback; unassigned gets 40.
- Tool paths without a cwd allocate 20 Claude tokens or 12 Codex tokens by
  `tool_path`. With a cwd, another repository's tool path does not override it.
  Two conflicting tool projects allocate the whole event to unassigned.
- A repository-scoped baseline includes a mixed session's matching 19-token
  message, excluding its other 19-token message. Its total is 76, not 57.

Additional fixtures check missing evidence, pre-window fallback, Windows/POSIX
operands, leading shell `cd`, command workdir resets, patch targets, origin-scope
precedence, same-timestamp ordinals, streaming snapshot selection, zero deltas,
per-event filters, salted IDs and absence of private operands in JSON and text.
