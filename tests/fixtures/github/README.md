# Synthetic GitHub REST recording

`responses.json` is authored synthetic data keyed by endpoint path. Tests patch
`urllib` to serve these responses and forbid socket access. No response came from
a real repository or account.

PR 1 merges on January 2. PR 2 is a fix referencing PR 1, created January 4 and
merged January 5. PR 3 closes without merging. A January 3 commit reverts PR 1's
full merge SHA. Visible checks pass at each merge on both the head and merge SHA.
Historical required-check lists are absent, so live required checks are unknown.
Tests also author explicit historical snapshots to establish green, red and
missing required-check evidence without substituting today's configuration.
