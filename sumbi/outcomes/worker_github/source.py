"""Worker GitHub measurement and gates for the shared comparison engine."""

from collections import Counter
from dataclasses import dataclass

from sumbi.core.values import timestamp
from sumbi.outcomes.github.source import GitHubSource
from sumbi.outcomes.units import MIX_DISTANCE, Unit, flag, fraction
from sumbi.outcomes.worker_github.workers import STATES, deliver_workers, state_reasons


@dataclass
class WorkerGitHubSource:
    repos: tuple[str, ...]
    outcomes: object
    repo_owners: tuple[str, ...] = ()

    name = "worker-github"
    registration_error = "Registration and both arms must use worker-github outcome source"
    mix_kinds = ("agent", "model", "effort", "cli_version")
    deduplicate_metadata = False
    exclusion_flag = "_excluded_or_unlinked"
    exclusion_collection = "units"
    risky_exclusions = True
    sort_exclusion_counts = True
    task_breakdowns = False
    finalized_elapsed_only = False

    def measure(self, home, period, registration, windows, *, agents, salt):
        report = deliver_workers(home, period, self.repos, self.outcomes, agents=agents,
            salt=salt, follow_up_days=registration.follow_up_days, repo_owners=self.repo_owners)
        units = []
        for row in report["units"]:
            start, last = timestamp(row["dispatched_at"]), timestamp(row["last_at"])
            units.append(Unit(row["id"], start, row["state"], row["reason"],
                (start, last) if last else (start,), (row,), row["tokens"],
                row["tokens_complete"], row["elapsed_seconds"], None, row, row["id"],
                unlinked=row["weak_link"] and row["state"] != "no_change",
                excluded_state=("repository_unreadable" if row["reason"] == "repository_unreadable"
                    else "no_change" if row["state"] == "no_change" else None),
                checks_basis=tuple(row["checks_basis"]), start_scope=row["start_scope"],
                retained_unlinked=row["state"] == "no_pr"))
        return units, report

    def source_gates(self, arms, *, candidates, report, stage):
        if stage == "coverage":
            overhead = report["dispatch_overhead"]
            return [flag("dispatch_overhead_excluded", False,
                {"sessions": overhead["sessions"], "observed_total": overhead["observed_total"]})
                for _ in (0,) if overhead["sessions"]]
        if stage != "retained":
            return []
        flags = []
        bases = GitHubSource.bases(arms)
        all_bases = set(bases["before"]) | set(bases["after"])
        if len(all_bases) > 1 or "unknown" in all_bases:
            flags.append(flag("checks_basis_mixed_or_unknown", True, bases))
        no_change = {arm: fraction(sum(u.state == "no_change" for u in rows), len(rows))
            for arm, rows in candidates.items()}
        if all(s["value"] is not None for s in no_change.values()) and abs(
            no_change["after"]["value"] - no_change["before"]["value"]) > MIX_DISTANCE:
            flags.append(flag("no_change_share_shift", True, no_change))
        return flags

    def coverage_reasons(self, arms, *, candidates, report, windows):
        reasons = []
        coverage = report["coverage"]
        if not report["units"]:
            reasons.append("no_worker_session_coverage")
        if coverage["excluded_scope"].get("missing_start_metadata"):
            reasons.append("missing_start_metadata")
        if any(u.public["evidence_incomplete"] for rows in candidates.values() for u in rows):
            reasons.append("worker_evidence_incomplete")
        if coverage["missing_prs"]:
            reasons.append("missing_pr_evidence")
        for o in coverage["repositories"]:
            if o.get("reason") == "repository_unreadable":
                continue
            if (not o["pulls_complete"] or not o["commits_complete"]
                or timestamp(o["coverage_start"]) > windows["before"].since
                or timestamp(o["observed_at"]) < windows["after"].until):
                reasons.append("outcome_coverage_incomplete")
        return reasons

    def public_fields(self, report, comparison):
        c = comparison
        bases = GitHubSource.bases(c["arms"])
        return {"schema_version": "worker-github-compare-1.0", "outcome_source": self.name,
            "unit": "worker_session", "pseudonyms": report["pseudonyms"],
            "registration": {**c["registration"], "follow_up_days": c["follow_up_days"]},
            **c["gap_fields"], "thresholds": c["thresholds"], "sample_size": c["sample_size"],
            "bootstrap": {**c["bootstrap"], "unit": "worker_session"},
            "arms": {arm: {"n": len(rows), "success_rate": c["rates"][arm],
                "cost": c["costs"][arm], "states": dict(Counter(u.state for u in rows)),
                "state_reasons": state_reasons([u.public for u in rows]),
                "candidate_states": {s: sum(u.state == s for u in c["candidates"][arm])
                    for s in STATES}, "candidate_state_reasons": state_reasons(
                        [u.public for u in c["candidates"][arm]]),
                "checks_basis_counts": bases[arm],
                "units": [u.public for u in rows]} for arm, rows in c["arms"].items()},
            "success_difference": c["success"], "ratios": c["ratios"],
            "exclusions": c["exclusions"], "mixes": c["mixes"], "flags": c["flags"],
            "task_type_breakdowns": [],
            "coverage": {**report["coverage"], "incomplete_reasons": c["coverage_reasons"]},
            "dispatch_overhead": report["dispatch_overhead"],
            "excluded_worker_spend": {"observed_total": sum(u.public["observed_total"]
                for arm, rows in c["candidates"].items() for u in rows
                if u.id not in {r.id for r in c["arms"][arm]})},
            "cost_basis": report["cost"]["basis"], "scan_until": report["scan_until"],
            "limitations": report["limitations"], "decision_order": c["decision_order"],
            "verdict": c["verdict"]}
