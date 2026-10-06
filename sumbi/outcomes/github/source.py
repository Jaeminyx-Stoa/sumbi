"""GitHub delivery evidence adapted to fixed comparison units."""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sumbi.core.privacy import pseudonym
from sumbi.core.values import timestamp
from sumbi.outcomes.github.deliver import STATES, deliver
from sumbi.outcomes.github.ledger import Deliverable, read_ledger
from sumbi.outcomes.github.recorded import Outcomes
from sumbi.outcomes.units import Unit, UNATTRIBUTED_SHARE, flag, fraction


@dataclass
class GitHubSource:
    ledger_path: Path
    outcomes: Outcomes
    rules: list | None = None
    idle_minutes: float = 5
    _ledger: list[Deliverable] = field(init=False, repr=False)

    name = "github"
    registration_error = "GitHub comparison requires github outcome source in registration"
    mix_kinds = ("model", "effort", "cli_version")
    deduplicate_metadata = True
    exclusion_flag = "_excluded_or_unlinked"
    exclusion_collection = "deliverables"
    risky_exclusions = False
    sort_exclusion_counts = True
    task_breakdowns = True
    finalized_elapsed_only = True

    def measure(self, home, period, registration, windows, *, agents, salt):
        report = deliver(home, period, self.ledger_path, self.outcomes, agents=agents,
            rules=self.rules,
            idle_minutes=self.idle_minutes, salt=salt,
            follow_up_days=registration.follow_up_days,
            comparison_metadata=True, dispatch_windows=tuple(windows.values()))
        ledger = read_ledger(self.ledger_path)
        self._ledger = ledger
        dispatches = {d.id: d.dispatched_at for d in ledger}
        scoped = {r["id"] for r in report["cost"]["projects"]}
        included = {d.id for d in ledger
            if scoped.intersection(pseudonym("repository", r) for r in d.repos)}
        metadata = {s["id"]: s for s in report["session_metadata"]}
        sessions = {r["id"]: set() for r in report["deliverables"]}
        weak = set()
        for link in report["links"]:
            identity = link["deliverable_id"]
            if identity in sessions:
                sessions[identity].add(link["session_id"])
                if link["evidence"] == "project_time_weak":
                    weak.add(identity)
        units = []
        for row in report["deliverables"]:
            identity = row["id"]
            if identity not in included:
                continue
            observed = tuple(metadata[sid] for sid in sorted(sessions[identity]))
            times = tuple(when for s in observed for field in ("first_at", "last_at")
                if (when := timestamp(s[field])) is not None)
            public = {**{k: v for k, v in row.items() if k != "tokens_complete"},
                "id": pseudonym("deliverable", identity)}
            units.append(Unit(public["id"], dispatches[identity], row["state"], row["reason"],
                times,
                observed, row["tokens"], row["tokens_complete"],
                row["elapsed_seconds"],
                row["task_type"], public, identity, unlinked=not times
                or identity in weak,
                checks_basis=tuple(p["checks_basis"] for p in row["pr_outcomes"]
                    if p["merged"])))
        return units, report

    def source_gates(self, arms, *, candidates, report, stage):
        if stage != "retained":
            return []
        flags = []
        bases = self.bases(arms)
        all_bases = set(bases["before"]) | set(bases["after"])
        if len(all_bases) > 1 or "unknown" in all_bases:
            flags.append(flag("checks_basis_mixed_or_unknown", True, bases))
        unattributed = self.unattributed(report)
        if unattributed["value"] is not None and unattributed["value"] > UNATTRIBUTED_SHARE:
            flags.append(flag("unattributed_spend", True, unattributed))
        return flags

    def coverage_reasons(self, arms, *, candidates, report, windows):
        reasons = []
        if not sum(c["sessions_in_lifetime_scan"] for c in report["coverage"]["adapters"].values()):
            reasons.append("no_session_coverage")
        relevant = {u.order_key for rows in candidates.values() for u in rows}
        for d in self._ledger:
            if d.id not in relevant:
                continue
            if any(self.outcomes.pull(p) is None for p in d.prs):
                reasons.append("missing_pr_evidence")
            arm = next(a for a, w in windows.items() if w.contains(d.dispatched_at))
            for repo in d.repos:
                o = self.outcomes.observation(repo)
                if (o is None or not o.pulls_complete or not o.commits_complete
                    or o.start > windows[arm].since or o.until < windows[arm].until):
                    reasons.append("outcome_coverage_incomplete")
        return reasons

    @staticmethod
    def bases(arms):
        return {arm: dict(sorted(Counter(b for u in rows for b in u.checks_basis).items()))
            for arm, rows in arms.items()}

    @staticmethod
    def unattributed(report):
        spend = report["cost"]["lifetime_scan_spend"]
        # The linked bucket already contains weak links; count it only once.
        return fraction(sum(spend[b]["total"] for b in ("unallocated", "unassigned"))
            + report["coverage"]["evidence_tokens"]["project_time_weak"],
            sum(spend[b]["total"] for b in ("linked", "unallocated", "unassigned")))

    def public_fields(self, report, comparison):
        c = comparison
        registration = {"intervention_id": c["registration"]["intervention_id"],
            "outcome_source": c["registration"]["outcome_source"],
            **{k: v for k, v in c["registration"].items()
                if k not in ("intervention_id", "outcome_source", "windows")},
            "follow_up_days": c["follow_up_days"],
            "windows": c["registration"]["windows"]}
        bases = self.bases(c["arms"])
        return {"schema_version": "compare-1.0", "pseudonyms": report["pseudonyms"],
            "registration": registration, **c["gap_fields"],
            "thresholds": c["thresholds"], "sample_size": c["sample_size"],
            "bootstrap": {**c["bootstrap"], "unit": "deliverable", "quantiles": [0.025, 0.975]},
            "arms": {arm: {"n": len(rows), "success_rate": c["rates"][arm],
                "cost": c["costs"][arm],
                "states": {s: sum(u.state == s for u in rows) for s in STATES},
                "checks_basis_counts": bases[arm],
                "deliverables": [u.public for u in rows]}
                for arm, rows in c["arms"].items()},
            "success_difference": c["success"], "ratios": c["ratios"],
            "exclusions": c["exclusions"],
            "mixes": c["mixes"], "flags": c["flags"], "task_type_breakdowns": c["breakdowns"],
            "coverage": {**report["coverage"], "incomplete_reasons": c["coverage_reasons"],
                "unattributed_lifetime_share": self.unattributed(report),
                "other_lifetime_tokens": report["cost"]["lifetime_scan_spend"][
                    "other"]["total"]},
            "links": [{**link,
                "deliverable_id": pseudonym("deliverable", link["deliverable_id"])
                if link["deliverable_id"] else None} for link in report["links"]],
            "session_metadata": report["session_metadata"],
            "decision_order": c["decision_order"], "verdict": c["verdict"]}
