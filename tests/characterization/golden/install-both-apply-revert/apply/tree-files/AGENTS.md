# Shared rules

Run `python -m unittest discover -s tests` before reporting completion.

<!-- sumbi:begin review-gates -->
Risky paths need review before release. Preserve existing approvals and require independent review for security, permissions and money paths. See [review levels](docs/sumbi/review.md).
<!-- sumbi:end review-gates -->

<!-- sumbi:begin plan-and-approval -->
For substantial changes, resolve missing requirements, write a plan with acceptance criteria, and critique it before implementation. Follow the existing approval policy. See [planning](docs/sumbi/planning.md).
<!-- sumbi:end plan-and-approval -->

<!-- sumbi:begin handoff -->
Leave a handoff document when work changes hands: acceptance criteria, evidence, remaining work and the next action. See [handoffs](docs/sumbi/handoff.md).
<!-- sumbi:end handoff -->

<!-- sumbi:begin friction-line -->
End a work report with `Harness friction: none` or one short description of the harness obstacle. Keep sensitive details in local notes. See [friction feedback](docs/sumbi/friction.md).
<!-- sumbi:end friction-line -->

<!-- sumbi:begin parallel-worktrees -->
Parallel agents use separate worktrees and explicit file ownership. Do not edit another agent's working copy. See [parallel work](docs/sumbi/parallel.md).
<!-- sumbi:end parallel-worktrees -->
