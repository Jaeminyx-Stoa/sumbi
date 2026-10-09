"""Bundled work approach for evidence-driven harness improvements."""

from copy import deepcopy

GUIDE = {
    "version": 1,
    "title": "Improve the decisions that generate repeated failure",
    "approach": (
        "Use this approach when doing sumbi improve work: start with the user's real outcome, "
        "find the decision structure behind failures, and change the mechanism at its measured "
        "boundary. A broad principle can yield different actions in different contexts. "
        "It does not mean a pilot or an extra review for every task; a small static edit may "
        "need only a direct diff. Existing risk and approval boundaries determine the work."
    ),
    "principles": [
        {
            "id": "real-outcome",
            "question": (
                "What accepted user deliverable should advance, and what is only preparation?"
            ),
            "changed_behavior": (
                "Use the existing goal or deliverable record to show accepted output and remaining "
                "distance. Setup steps, tool calls and rule counts do not establish that outcome."
            ),
        },
        {
            "id": "failure-generating-decisions",
            "question": (
                "Which decision or coverage assumption made this failure possible, "
                "and where does that explanation stop being supported?"
            ),
            "changed_behavior": (
                "Trace the symptom to its generating mechanism and explicit coverage boundary. "
                "Change that decision rule, while keeping unsupported causes as hypotheses."
            ),
        },
        {
            "id": "reuse-and-remove",
            "question": (
                "Where does an existing mechanism or valid proof already do this work, "
                "and which unnecessary step can be removed before adding a rule?"
            ),
            "changed_behavior": (
                "Reuse the operating path and evidence that cover the decision. Remove redundant "
                "preparation or duplicated devices; explain the uncovered boundary of any addition."
            ),
        },
        {
            "id": "measured-constraints",
            "question": (
                "What measured capacity or concrete risk justifies this constraint, "
                "and under what conditions should it change?"
            ),
            "changed_behavior": (
                "Calibrate the action to observed pressure and legitimate risk. Replace arbitrary "
                "fixed defaults with evidence and visible limits on what the measurement can prove."
            ),
        },
        {
            "id": "transfer-across-contexts",
            "question": (
                "Does the same judgment rule help in materially different contexts, "
                "or only in variations of this fixture?"
            ),
            "changed_behavior": (
                "Explain how the rule changes the action for a resource-heavy job, a small static "
                "fix and a money or security path. Reuse valid evidence; inspect a different "
                "context when it could disprove the mechanism, rather than adding a test matrix."
            ),
        },
        {
            "id": "proportionate-safety",
            "question": (
                "Which real hazard needs a gate, and which legitimate user action could "
                "this control block unnecessarily?"
            ),
            "changed_behavior": (
                "Preserve approvals and independent money/security review. Check both an unsafe "
                "action that must remain blocked and a legitimate action that should proceed; "
                "apply existing risk boundaries instead of extending every gate to every edit. "
                "Owner review is available only where repository policy permits it; never "
                "downgrade a required cross-family, money, security or permission review."
            ),
        },
        {
            "id": "coherent-reversible-scope",
            "question": "What one to three coherent interventions can be reviewed and reversed?",
            "changed_behavior": (
                "Keep coupled files together so the mechanism stays consistent. Intervention "
                "scope is different from the CLI's 64-target resource bound. Keep exact reviewed "
                "bytes, backups and a useful rollback path."
            ),
        },
        {
            "id": "outcome-evidence",
            "question": (
                "What fixed outcomes would distinguish an applied patch from a validated "
                "improvement, after accounting for changed working conditions?"
            ),
            "changed_behavior": (
                "Use the same declared success outcomes, elapsed time, new input, cache writes, "
                "cache reads and output tokens. Treat reasoning output as a subset and missing "
                "values as unknown. Keep cohort and model/effort/runner confounders visible; "
                "withhold an improvement claim when the comparison cannot support it."
            ),
        },
    ],
    "worked_example": {
        "setting": "Synthetic example: a data job stops at a hardcoded 20 GB memory threshold.",
        "local_fix": (
            "Raising the stop to 40 GB addresses this instance without explaining the rule."
        ),
        "broader_pattern": (
            "An unmeasured constraint became a universal decision, and preparation activity "
            "masqueraded as progress toward the real deliverable."
        ),
        "principle": (
            "Pilot-first, evidence-calibrated execution where resource uncertainty matters."
        ),
        "changed_behavior": (
            "Use the existing goal to name the accepted output and reuse the existing capacity "
            "probe. For the uncertain resource-heavy job, run a representative bounded pilot "
            "within applicable existing approvals, then choose execution from observed capacity "
            "and coverage. Show real deliverable progress instead of completed preparation "
            "counts. Preserve the requested full scope and horizon. A pilot is not the "
            "deliverable; disclose any reduced output, and obtain owner approval for a change "
            "to the requested scope."
        ),
        "different_contexts": [
            {
                "context": "resource-heavy-job",
                "action": (
                    "Measure the relevant resource pressure with the existing probe; use a "
                    "representative pilot only when it resolves a material execution uncertainty."
                ),
            },
            {
                "context": "small-static-fix",
                "action": (
                    "Inspect the direct diff and reuse applicable proof. A capacity pilot or "
                    "additional review ceremony would not resolve a relevant uncertainty."
                ),
            },
            {
                "context": "money-or-security-path",
                "action": (
                    "Keep required approval and independent review. Check the unsafe operation "
                    "remains blocked and the legitimate approved operation still proceeds. "
                    "When the action depends on live state, verify it at the time of action. "
                    "Recorded hashes or cached proof cannot replace current money/security "
                    "evidence."
                ),
            },
        ],
        "evidence_boundary": (
            "One successful pilot covers its observed workload and environment. It does not "
            "certify every workload or prove lower cost per success; compare fixed outcomes "
            "and time/token components with confounders before making that claim."
        ),
    },
    "review_focus": (
        "Judge the changed mechanism, transfer to materially different contexts, reuse or removal "
        "of existing work, and outcome evidence. Presence of a checklist or these words is not "
        "evidence that a proposal improves the harness."
    ),
}


def improvement_guide() -> dict:
    """Return public, authored guidance without consulting a repository or logs."""
    return deepcopy(GUIDE)


def guide_text(guide: dict) -> str:
    """Render the installed guidance, including operational questions and one example."""
    lines = [guide["title"], guide["approach"]]
    for principle in guide["principles"]:
        lines.extend(["", principle["id"], "Ask: " + principle["question"],
            "Change: " + principle["changed_behavior"]])
    example = guide["worked_example"]
    lines.extend(["", example["setting"], "Local fix: " + example["local_fix"],
        "Pattern: " + example["broader_pattern"], "Principle: " + example["principle"],
        "Change: " + example["changed_behavior"]])
    lines.extend(row["context"] + ": " + row["action"] for row in example["different_contexts"])
    lines.extend(["Coverage: " + example["evidence_boundary"], "Review: " + guide["review_focus"]])
    return "\n".join(lines)
