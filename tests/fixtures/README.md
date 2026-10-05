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
