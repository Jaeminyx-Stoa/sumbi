"""Pre-registered comparisons for the explicitly weaker local outcome source."""

from collections import Counter
from pathlib import Path

from sumbi.catalog import load_judgment_policy
from sumbi.compare import (DECISION_ORDER, EXCLUDED_SHARE, MIX_DISTANCE, fraction,
                           mix_distance, mix_report, verdict)
from sumbi.compare_stats import bootstrap, newcombe, sample_size
from sumbi.deliver import wilson
from sumbi.local_outcomes import METRICS, STATES, deliver_local
from sumbi.model import Window, timestamp
from sumbi.privacy import pseudonym, pseudonym_key, read_salt
from sumbi.registration import read_registration


def compare_local(home: Path, repository: Path, registration_path: Path, *, agents=None,
                  verify=None, scan_until=None, active_minutes=5, salt=None, seed=1729, resamples=5000):
    registration = read_registration(registration_path)
    if registration.outcome_source != "local-verify":
        raise ValueError("Registration and both arms must use local-verify outcome source")
    if load_judgment_policy()["decision_order"] != DECISION_ORDER:
        raise ValueError("Comparison decision order does not match the bundled judgment policy")
    key = salt if salt is not None else read_salt()
    with pseudonym_key(key):
        period = Window(registration.before.since, registration.after.until)
        measured = deliver_local(home, period, repository, agents=agents, verify=verify,
                                 scan_until=scan_until, active_minutes=active_minutes, salt=key)
        windows = {"before": registration.before, "after": registration.after}
        candidates = {arm: [r for r in measured["units"] if w.contains(timestamp(r["dispatched_at"]))]
                      for arm, w in windows.items()}
        arms, exclusions = {}, {}
        flags, mixes = [], {}
        def flag(name, blocking, evidence):
            flags.append({"name": name, "blocking": blocking, "evidence": evidence})
        scope_mismatches = {arm: dict(Counter(r["start_scope"] for r in rows
                            if r["start_scope"] == "same_origin_other_checkout")) for arm, rows in candidates.items()}
        if any(scope_mismatches.values()):
            flag("unit_start_scope_mismatch", True, scope_mismatches)
        for arm, rows in candidates.items():
            arms[arm], excluded = [], []
            for row in rows:
                last = timestamp(row["last_at"])
                reason = (row["state"] if row["state"] in ("no_change", "in_progress") else
                          "exposure_mixed" if arm == "before" and last and last >= registration.applied_at else None)
                if reason:
                    excluded.append({"id": row["id"], "reason": reason})
                else:
                    arms[arm].append(row)
            # Administrative no-change units are counted separately. Recent work
            # and mixed exposure can bias retained outcomes, so cap their share.
            risky = sum(r["reason"] != "no_change" for r in excluded)
            share = fraction(risky, len(rows))
            exclusions[arm] = {"share": fraction(len(excluded), len(rows)), "risky_share": share,
                               "counts": dict(Counter(r["reason"] for r in excluded)), "units": excluded}
            if share["value"] is not None and share["value"] > EXCLUDED_SHARE:
                flag(arm + "_excluded_or_mixed", True, share)
        for kind in ("agent", "model", "effort", "cli_version"):
            unobservable, partial, asymmetric = [], [], []
            if kind != "agent":
                for agent in sorted({r["agent"] for rows in arms.values() for r in rows}):
                    reported = {arm: [bool(r[kind]) for r in rows if r["agent"] == agent] for arm, rows in arms.items()}
                    if not any(any(v) for v in reported.values()):
                        unobservable.append(agent)
                    elif any(any(v) and not all(v) for v in reported.values()):
                        partial.append(agent)
                    if any(reported["before"]) != any(reported["after"]):
                        asymmetric.append(agent)
                for suffix, affected, blocking in (("unobservable", unobservable, False),
                         ("metadata_partial", partial, True), ("metadata_asymmetric", asymmetric, True)):
                    if affected:
                        flag(kind + "_" + suffix, blocking, {"agents": affected})
            counts = {arm: Counter(value for r in rows if r["agent"] not in unobservable
                       for value in ([r["agent"]] if kind == "agent" else r[kind])) for arm, rows in arms.items()}
            summaries = {arm: mix_report(c) for arm, c in counts.items()}
            distance = mix_distance(counts["before"], counts["after"])
            changed = bool(counts["before"] and counts["after"] and
                           summaries["before"]["dominant"] != summaries["after"]["dominant"])
            mixes[kind] = {**summaries, "total_variation_distance": distance, "dominant_changed": changed}
            if changed or distance is not None and distance > MIX_DISTANCE:
                flag(kind + "_mix_shift", True, mixes[kind])
        sizes = [len(r) for r in arms.values()]
        if max(sizes) > 1.5 * min(sizes):
            flag("volume_shift", False, fraction(max(sizes) - min(sizes), min(sizes)))
        for event in registration.confounders:
            inside = [arm for arm, w in windows.items() if w.contains(event["at"])]
            if inside:
                blocking = event["label"].lower().replace("-", "_").split("_", 1)[0] in ("runner", "model", "effort", "cli")
                flag("registered_event", blocking, {"at": event["at"].isoformat(), "label": event["label"], "arms": inside})
        # No-change reclassification can otherwise manufacture a rate gain.
        no_change = {arm: fraction(sum(r["state"] == "no_change" for r in rows), len(rows))
                     for arm, rows in candidates.items()}
        if all(s["value"] is not None for s in no_change.values()) and abs(
                no_change["after"]["value"] - no_change["before"]["value"]) > MIX_DISTANCE:
            flag("no_change_share_shift", True, no_change)
        coverage_reasons = []
        coverage = measured["coverage"]
        if not measured["units"]:
            coverage_reasons.append("no_worker_session_coverage")
        if coverage["excluded_scope"].get("missing_start_metadata"):
            coverage_reasons.append("missing_start_metadata")
        if any(any(c[k] for k in ("broken_lines", "unreadable_files", "invalid_timestamps",
                                  "invalid_token_records", "unknown_record_types")) for c in coverage["adapters"].values()):
            coverage_reasons.append("session_coverage_errors")
        if coverage["commands"].get("unmatched_shape"):
            coverage_reasons.append("unmatched_command_shape")
        if coverage["commands"].get("unknown_exit_code"):
            coverage_reasons.append("unknown_verification_exit")
        if any(coverage["commands"].get(k) for k in ("unknown_execution_start", "unknown_execution_cwd",
                "verification_cwd_unconfirmed", "command_timestamp_missing", "edit_timestamp_missing")):
            coverage_reasons.append("verification_execution_evidence_incomplete")
        if any(not r["tokens_complete"] for rows in arms.values() for r in rows):
            coverage_reasons.append("token_totals_incomplete")
        if any(r["elapsed_seconds"] is None for rows in arms.values() for r in rows):
            coverage_reasons.append("elapsed_time_not_reported")
        # Parent costs have no evidence-backed allocation to individual workers.
        # All cost proposals explicitly concern workers only, not the workspace.
        overhead = measured["dispatch_overhead"]
        if overhead["sessions"]:
            flag("dispatch_overhead_excluded", False, {"sessions": overhead["sessions"],
                 "observed_total": overhead["observed_total"]})
            if any(not r["tokens_complete"] for r in overhead["rows"]):
                flag("dispatch_overhead_tokens_incomplete", False, {"sessions": overhead["sessions"]})
        rates = {arm: wilson(sum(r["state"] == "success" for r in rows), len(rows)) for arm, rows in arms.items()}
        success = newcombe(rates["before"]["numerator"], rates["before"]["denominator"],
                           rates["after"]["numerator"], rates["after"]["denominator"])
        lower, upper = success["interval_95"] or (None, None)
        success["non_inferiority"] = ("inconclusive" if lower is None else "non_inferior" if lower >
            -registration.margin_pp / 100 else "inferior" if upper < -registration.margin_pp / 100 else "inconclusive")
        costs, ratios = bootstrap(arms["before"], arms["after"], METRICS, seed=seed, resamples=resamples)
        proposal, reasons = verdict(coverage_reasons=sorted(set(coverage_reasons)),
            preregistered=registration.preregistered, blocking_flags=sorted({f["name"] for f in flags if f["blocking"]}),
            arms=arms, registered_size=registration.sample_size_per_arm, success=success,
            margin_pp=registration.margin_pp, ratios=ratios)
        return {"schema_version": "local-compare-1.0", "outcome_source": "local-verify",
            "registration": {"outcome_source": registration.outcome_source,
                "intervention_id": pseudonym("intervention", registration.intervention_id),
                "applied_at": registration.applied_at.isoformat(), "registered_at": registration.registered_at.isoformat(),
                "preregistered": registration.preregistered, "predictions": list(registration.predictions),
                "non_inferiority_margin_pp": registration.margin_pp,
                "windows": {arm: {"since": w.since.isoformat(), "until": w.until.isoformat(), "bounds": "[since,until)"}
                            for arm, w in windows.items()}},
            "sample_size": {"registered_per_arm": registration.sample_size_per_arm,
                "needed_per_arm": sample_size(rates["before"]["rate"], registration.margin_pp),
                "actual": {arm: len(rows) for arm, rows in arms.items()}},
            "bootstrap": {"seed": seed, "resamples": resamples, "unit": "worker_session"},
            "arms": {arm: {"n": len(rows), "success_rate": rates[arm], "cost": costs[arm],
                "candidate_states": {s: sum(r["state"] == s for r in candidates[arm]) for s in STATES},
                "units": rows} for arm, rows in arms.items()},
            "success_difference": success, "ratios": ratios, "exclusions": exclusions,
            "mixes": mixes, "flags": flags, "coverage": {**coverage, "incomplete_reasons": sorted(set(coverage_reasons))},
            "dispatch_overhead": overhead, "excluded_worker_spend": measured["excluded_worker_spend"],
            "cost_basis": measured["cost"]["basis"], "scan_until": measured["scan_until"],
            "pseudonyms": measured["pseudonyms"],
            "active_minutes": active_minutes, "limitations": measured["limitations"],
            "decision_order": DECISION_ORDER, "verdict": {"label": "proposal", "proposal": proposal, "reasons": reasons}}


def text_summary(report):
    lines = ["sumbi compare local verification", "Outcome source: local-verify", "Verdict proposal: " +
             report["verdict"]["proposal"], "Reasons: " + ", ".join(report["verdict"]["reasons"])]
    lines.append("Cost proposal scope: retained worker sessions only")
    lines.append(f"Excluded dispatch overhead: {report['dispatch_overhead']['sessions']} sessions; observed tokens {report['dispatch_overhead']['observed_total']}")
    size = report["sample_size"]
    lines.append(f"Sample per arm: registered {size['registered_per_arm']}; needed {size['needed_per_arm']}; actual {size['actual']}")
    for arm, row in report["arms"].items():
        rate = row["success_rate"]
        lines.append(f"{arm} success: {rate['numerator']}/{rate['denominator']}; Wilson 95% {rate['wilson_95']}")
        lines.append(f"{arm} candidate states: " + "; ".join(f"{s} {n}" for s, n in row["candidate_states"].items()))
        for metric in ("total", "time"):
            cost = row["cost"][metric]
            lines.append(f"{arm} worker {metric} per success: {cost['numerator']}/{cost['denominator']} = {cost['value']}; bootstrap 95% {cost['interval_95']}")
    success = report["success_difference"]
    lines.append(f"After - before success: {success['difference']}; Newcombe 95% {success['interval_95']}; {success['non_inferiority']}; margin {report['registration']['non_inferiority_margin_pp']} percentage points")
    for metric in ("total", "time"):
        ratio = report["ratios"][metric]
        lines.append(f"Worker {metric} after/before: {ratio['numerator']}/{ratio['denominator']} = {ratio['value']}; bootstrap 95% {ratio['interval_95']}; {ratio['classification']}")
    lines.append(f"Bootstrap: seed {report['bootstrap']['seed']}; resamples {report['bootstrap']['resamples']}")
    for arm, exclusion in report["exclusions"].items():
        share = exclusion["share"]
        lines.append(f"{arm} excluded: {share['numerator']}/{share['denominator']}; counts {exclusion['counts']}")
    for flag in report["flags"]:
        lines.append(f"Flag {flag['name']}: " + ("blocking" if flag["blocking"] else "informational"))
    lines.append("Command coverage: " + "; ".join(f"{s} {n}" for s, n in report["coverage"]["commands"].items()))
    return "\n".join(lines)
