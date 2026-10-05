# Synthetic GitHub REST recording

`responses.json` is authored synthetic data keyed by endpoint path. Tests patch
`urllib` to serve these responses and forbid socket access. No response came from
a real repository or account.

PR 1 merges on January 2. PR 2 is a fix referencing PR 1, created January 4 and
merged January 5. PR 3 closes without merging. A January 3 commit reverts PR 1's
full merge SHA. Visible checks pass at each merge on both the head and merge SHA.
Historical required-check lists are absent. Classic protection is disabled and
rulesets are empty, so these green results use the `all_visible` basis.
Tests vary the authored policy responses for classic and ruleset requirements,
app pins, permission errors and pagination. Explicit historical snapshots still
take precedence over current policy and visible checks.
