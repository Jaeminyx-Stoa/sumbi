"""Local verification evidence adapted to fixed comparison units."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sumbi.core.values import timestamp
from sumbi.outcomes.local_verify.workers import STATES, deliver_local, verifier_signals
from sumbi.outcomes.units import MIX_DISTANCE, Unit, flag, fraction


@dataclass
class LocalVerifySource:
    repository: Path
    verify: list[str] | None = None
    scan_until: datetime | None = None
    active_minutes: float = 5

    name = "local-verify"
    registration_error = "Registration and both arms must use local-verify outcome source"
    mix_kinds = ("agent", "model", "effort", "cli_version")
    deduplicate_metadata = False
    exclusion_flag = "_excluded_or_mixed"
    exclusion_collection = "units"
    risky_exclusions = True
    sort_exclusion_counts = False
    task_breakdowns = False
    finalized_elapsed_only = False

    def measure(self, home, period, registration, windows, *, agents, salt):
        report = deliver_local(home, period, self.repository, agents=agents, verify=self.verify,
                               scan_until=self.scan_until, active_minutes=self.active_minutes, salt=salt,
                               verification_windows=windows)
        units = []
        for row in report["units"]:
            last = timestamp(row["last_at"])
            units.append(Unit(row["id"], timestamp(row["dispatched_at"]), row["state"], row["reason"],
                              (last,) if last else (), (row,), row["tokens"], row["tokens_complete"],
                              row["elapsed_seconds"], row["task_type"], row, row["id"],
                              excluded_state=row["state"] if row["state"] in ("no_change", "in_progress") else None,
                              start_scope=row["start_scope"]))
        return units, report

    @staticmethod
    def signals(report):
        verification = report["verification"]["arms"]
        return ([{"name": "verifier_never_passed", "evidence": verification}]
                if any(verifier_signals(counts) for counts in verification.values()) else [])

    def source_gates(self, arms, *, candidates, report, stage):
        flags = []
        if stage == "candidates":
            flags.extend(flag(s["name"], True, s["evidence"]) for s in self.signals(report))
            mismatches = {arm: dict(Counter(u.start_scope for u in rows
                          if u.start_scope == "same_origin_other_checkout")) for arm, rows in candidates.items()}
            if any(mismatches.values()):
                flags.append(flag("unit_start_scope_mismatch", True, mismatches))
        elif stage == "retained":
            no_change = {arm: fraction(sum(u.state == "no_change" for u in rows), len(rows))
                         for arm, rows in candidates.items()}
            if all(s["value"] is not None for s in no_change.values()) and abs(
                    no_change["after"]["value"] - no_change["before"]["value"]) > MIX_DISTANCE:
                flags.append(flag("no_change_share_shift", True, no_change))
        elif stage == "coverage":
            overhead = report["dispatch_overhead"]
            if overhead["sessions"]:
                flags.append(flag("dispatch_overhead_excluded", False, {"sessions": overhead["sessions"],
                                  "observed_total": overhead["observed_total"]}))
                if any(not r["tokens_complete"] for r in overhead["rows"]):
                    flags.append(flag("dispatch_overhead_tokens_incomplete", False, {"sessions": overhead["sessions"]}))
        return flags

    def coverage_reasons(self, arms, *, candidates, report, windows):
        reasons = []
        coverage = report["coverage"]
        if not report["units"]:
            reasons.append("no_worker_session_coverage")
        if coverage["excluded_scope"].get("missing_start_metadata"):
            reasons.append("missing_start_metadata")
        if coverage["commands"].get("unmatched_shape"):
            reasons.append("unmatched_command_shape")
        if coverage["commands"].get("unknown_exit_code"):
            reasons.append("unknown_verification_exit")
        if any(coverage["commands"].get(k) for k in ("unknown_execution_start", "unknown_execution_cwd",
                "verification_cwd_unconfirmed", "command_timestamp_missing", "edit_timestamp_missing")):
            reasons.append("verification_execution_evidence_incomplete")
        return reasons

    def public_fields(self, report, comparison):
        c = comparison
        registration = {"outcome_source": c["registration"]["outcome_source"],
                        "intervention_id": c["registration"]["intervention_id"],
                        **{k: v for k, v in c["registration"].items() if k not in ("outcome_source", "intervention_id")}}
        return {"schema_version": "local-compare-1.0", "outcome_source": "local-verify",
                "verification": report["verification"], "signals": self.signals(report), **c["gap_fields"],
                "registration": registration,
                "sample_size": {k: c["sample_size"][k] for k in ("registered_per_arm", "needed_per_arm", "actual")},
                "bootstrap": {**c["bootstrap"], "unit": "worker_session"},
                "arms": {arm: {"n": len(rows), "success_rate": c["rates"][arm], "cost": c["costs"][arm],
                              "candidate_states": {s: sum(u.state == s for u in c["candidates"][arm]) for s in STATES},
                              "units": [u.public for u in rows]} for arm, rows in c["arms"].items()},
                "success_difference": c["success"], "ratios": c["ratios"], "exclusions": c["exclusions"],
                "mixes": c["mixes"], "flags": c["flags"],
                "coverage": {**report["coverage"], "incomplete_reasons": c["coverage_reasons"]},
                "dispatch_overhead": report["dispatch_overhead"], "excluded_worker_spend": report["excluded_worker_spend"],
                "cost_basis": report["cost"]["basis"], "scan_until": report["scan_until"],
                "pseudonyms": report["pseudonyms"], "active_minutes": self.active_minutes,
                "limitations": report["limitations"], "decision_order": c["decision_order"], "verdict": c["verdict"]}
