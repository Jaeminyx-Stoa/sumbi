"""One pre-registered comparison engine over source-neutral fixed units."""

from collections import Counter
from fractions import Fraction

from sumbi.catalog import load_judgment_policy
from sumbi.core.privacy import pseudonym, pseudonym_key, read_salt
from sumbi.core.time import Window
from sumbi.judge.interventions import exposure_gap, exposure_side, gap_summary, in_exposure_gap
from sumbi.judge.registration import read_registration
from sumbi.judge.stats import bootstrap, estimate, newcombe, sample_size, wilson
from sumbi.outcomes.units import (EXCLUDED_SHARE, MIX_DISTANCE, UNATTRIBUTED_SHARE,
                                  OutcomeSource, flag, fraction)
from sumbi.outcomes.local_verify.workers import verifier_signals, verifier_summary
from sumbi.sessions.session import TOKEN_KINDS

DECISION_ORDER = ["coverage", "comparability", "success_non_inferiority", "time_and_tokens"]
METRICS = (*TOKEN_KINDS, "total", "time")


def mix_distance(before, after):
    b, a = sum(before.values()), sum(after.values())
    if not b or not a:
        return None
    return float(sum(abs(Fraction(before[k], b) - Fraction(after[k], a))
                     for k in sorted(before.keys() | after.keys())) / 2)


def mix_report(counts):
    total = sum(counts.values())
    maximum = max(counts.values(), default=0)
    return {"values": {k: fraction(v, total) for k, v in sorted(counts.items())},
            "dominant": sorted(k for k, v in counts.items() if v == maximum)}


def agent_only_in_one_arm(arms):
    agents = {arm: {r["agent"] for r in rows} for arm, rows in arms.items()}
    return sorted(agents["before"] ^ agents["after"])


def verdict(*, coverage_reasons, preregistered, blocking_flags, arms, registered_size,
            success, margin_pp, ratios):
    """The catalog's gate order; a later rejection cannot bypass earlier gates."""
    if coverage_reasons:
        return "withhold", ["incomplete_coverage", *coverage_reasons]
    reasons = ([] if preregistered else ["not_preregistered"])
    if blocking_flags:
        reasons.extend(["not_comparable", *blocking_flags])
    if reasons:
        return "withhold", reasons
    if any(row["state"] in ("immature", "in_progress") for rows in arms.values() for row in rows):
        return "withhold", ["immature_or_in_progress"]
    if any(len(rows) < registered_size for rows in arms.values()):
        return "withhold", ["insufficient_sample"]
    interval = success["interval_95"]
    if interval is None:
        return "withhold", ["inconclusive_success"]
    lower, upper = interval
    if upper < -margin_pp / 100:
        return "reject", ["success_inferior"]
    if lower <= -margin_pp / 100:
        return "withhold", ["inconclusive_success"]
    tokens, time = (ratios[k]["classification"] for k in ("total", "time"))
    # Success improvement with a cost penalty requires the owner's tradeoff,
    # including when both costs worsen.
    if (lower > 0 and "worse" in (tokens, time)) or {tokens, time} == {"improved", "worse"}:
        return "owner_decides", ["success_cost_tradeoff" if lower > 0 else "time_token_tradeoff"]
    if tokens == time == "worse":
        return "reject", ["tokens_and_time_worse"]
    if tokens == "worse" and time == "uncertain":
        return "reject", ["tokens_worse_without_offset"]
    if time == "worse" and tokens == "uncertain":
        return "reject", ["time_worse_without_offset"]
    if "worse" not in (tokens, time) and "improved" in (tokens, time):
        return "adopt", ["non_inferior_with_cost_improvement"]
    return "withhold", ["no_detectable_change"]


def compare(home, registration_path, source: OutcomeSource, *, agents=None, salt=None,
            seed=1729, resamples=5000, interventions_path=None, intervention_id=None):
    registration = read_registration(registration_path)
    gap = exposure_gap(registration, interventions_path, intervention_id)
    if registration.outcome_source != source.name:
        raise ValueError(source.registration_error)
    if load_judgment_policy()["decision_order"] != DECISION_ORDER:
        raise ValueError("Comparison decision order does not match the bundled judgment policy")
    key = salt if salt is not None else read_salt()
    with pseudonym_key(key):
        period = Window(registration.before.since, registration.after.until)
        windows = {"before": registration.before, "after": registration.after}
        units, measured = source.measure(home, period, registration, windows, agents=agents, salt=key)
        candidates = {arm: [u for u in units if w.contains(u.dispatched_at)] for arm, w in windows.items()}
        flags = source.source_gates({}, candidates=candidates, report=measured, stage="candidates")
        arms, exclusions = assign_arms(candidates, registration.applied_at, gap, source, flags)
        metadata = arm_metadata(arms)
        mixes = metadata_mixes(metadata, source.mix_kinds, flags)
        tasks = task_mix(arms, mixes, flags) if source.task_breakdowns else {}
        context_flags(arms, registration, windows, flags)
        flags.extend(source.source_gates(arms, candidates=candidates, report=measured, stage="retained"))
        reasons = coverage_reasons(arms, candidates, measured, windows, source)
        flags.extend(source.source_gates(arms, candidates=candidates, report=measured, stage="coverage"))
        rates, success, costs, ratios = statistics(arms, registration.margin_pp, seed, resamples)
        proposal, decisions = verdict(coverage_reasons=reasons, preregistered=registration.preregistered,
            blocking_flags=sorted({f["name"] for f in flags if f["blocking"]}), arms=arms,
            registered_size=registration.sample_size_per_arm, success=success,
            margin_pp=registration.margin_pp, ratios=ratios)
        comparison = {"registration": public_registration(registration, windows),
            "follow_up_days": registration.follow_up_days, "gap_fields": {"exposure_gap": gap} if gap is not None else {},
            "thresholds": {"excluded_or_unlinked_share": EXCLUDED_SHARE, "unattributed_spend_share": UNATTRIBUTED_SHARE,
                           "mix_total_variation": MIX_DISTANCE, "volume_relative_difference": 0.50},
            "sample_size": sample_report(registration, rates, arms), "bootstrap": {"seed": seed, "resamples": resamples},
            "arms": arms, "candidates": candidates, "rates": rates, "costs": costs, "success": success,
            "ratios": ratios, "exclusions": exclusions, "mixes": mixes, "flags": flags,
            "breakdowns": task_breakdowns(arms, tasks) if source.task_breakdowns else [],
            "coverage_reasons": reasons, "decision_order": DECISION_ORDER,
            "verdict": {"label": "proposal", "proposal": proposal, "reasons": decisions}}
        return source.public_fields(measured, comparison)


def exclusion_reason(unit, arm, applied_at, gap):
    if in_exposure_gap(unit.dispatched_at, gap):
        return "exposure_gap"
    if unit.unlinked:
        return "unlinked"
    if unit.excluded_state:
        return unit.excluded_state
    if unit.exposure_times and {exposure_side(t, applied_at, gap) for t in unit.exposure_times} != {arm}:
        return "exposure_mixed"
    return None


def assign_arms(candidates, applied_at, gap, source, flags):
    arms, exclusions = {}, {}
    for arm, rows in candidates.items():
        retained, excluded = [], []
        for unit in rows:
            reason = exclusion_reason(unit, arm, applied_at, gap)
            if reason:
                excluded.append({"id": unit.id, "reason": reason})
            else:
                retained.append(unit)
        arms[arm] = sorted(retained, key=lambda u: u.order_key)
        counts = Counter(r["reason"] for r in excluded)
        share = fraction(len(excluded), len(rows))
        risky = fraction(sum(r["reason"] != "no_change" for r in excluded), len(rows))
        exclusions[arm] = {"share": share, **({"risky_share": risky} if source.risky_exclusions else {}),
                           "counts": dict(sorted(counts.items()) if source.sort_exclusion_counts else counts.items()),
                           source.exclusion_collection: excluded}
        gate_share = risky if source.risky_exclusions else share
        if gate_share["value"] is not None and gate_share["value"] > EXCLUDED_SHARE:
            flags.append(flag(arm + source.exclusion_flag, True, gate_share))
    return arms, exclusions


def arm_metadata(arms):
    # Linked sessions can belong to several retained units. Count each once.
    return {arm: sorted({s["id"]: s for u in rows for s in u.session_metadata}.values(), key=lambda s: s["id"])
            for arm, rows in arms.items()}


def observability(metadata, kind, flags):
    unobservable, partial, asymmetric = [], [], []
    for agent in sorted({s["agent"] for rows in metadata.values() for s in rows}):
        reported = {arm: [bool(s[kind]) for s in rows if s["agent"] == agent] for arm, rows in metadata.items()}
        if not any(any(values) for values in reported.values()):
            unobservable.append(agent)
            continue
        if any(any(values) and not all(values) for values in reported.values()) or any(
                s.get("metadata_incomplete", {}).get(kind) for rows in metadata.values()
                for s in rows if s["agent"] == agent):
            partial.append(agent)
        if all(reported.values()) and any(reported["before"]) != any(reported["after"]):
            asymmetric.append(agent)
    for suffix, affected, blocking in (("unobservable", unobservable, False),
            ("metadata_partial", partial, True), ("metadata_asymmetric", asymmetric, True)):
        if affected:
            flags.append(flag(kind + "_" + suffix, blocking, {"agents": affected}))
    return unobservable


def metadata_mixes(metadata, kinds, flags):
    shifted = agent_only_in_one_arm(metadata)
    if shifted:
        flags.append(flag("agent_mix_shift", True, {"agents": shifted}))
    mixes = {}
    for kind in kinds:
        unobservable = observability(metadata, kind, flags) if kind != "agent" else []
        counts = {arm: Counter(value for s in rows if s["agent"] not in unobservable
                  for value in ([s["agent"]] if kind == "agent" else s[kind])) for arm, rows in metadata.items()}
        summaries = {arm: mix_report(c) for arm, c in counts.items()}
        distance = mix_distance(counts["before"], counts["after"])
        changed = bool(counts["before"] and counts["after"] and
                       summaries["before"]["dominant"] != summaries["after"]["dominant"])
        mixes[kind] = {**summaries, "total_variation_distance": distance, "dominant_changed": changed}
        if changed or distance is not None and distance > MIX_DISTANCE:
            if kind == "agent" and shifted:
                next(f for f in flags if f["name"] == "agent_mix_shift")["evidence"].update(mixes[kind])
            else:
                flags.append(flag(kind + "_mix_shift", True, mixes[kind]))
    return mixes


def task_mix(arms, mixes, flags):
    tasks = {arm: Counter(u.task_type or "<not_reported>" for u in rows) for arm, rows in arms.items()}
    distance = mix_distance(tasks["before"], tasks["after"])
    mixes["task_type"] = {**{arm: mix_report(c) for arm, c in tasks.items()}, "total_variation_distance": distance}
    if distance is not None and distance > MIX_DISTANCE:
        flags.append(flag("task_type_mix_shift", False, mixes["task_type"]))
    return tasks


def context_flags(arms, registration, windows, flags):
    sizes = [len(rows) for rows in arms.values()]
    if max(sizes) > 1.5 * min(sizes):
        flags.append(flag("volume_shift", False, fraction(max(sizes) - min(sizes), min(sizes))))
    for event in registration.confounders:
        inside = [arm for arm, w in windows.items() if w.contains(event["at"])]
        if inside:
            kind = event["label"].lower().replace("-", "_").split("_", 1)[0]
            flags.append(flag("registered_event", kind in ("runner", "model", "effort", "cli"),
                              {"at": event["at"].isoformat(), "label": event["label"], "arms": inside}))


def coverage_reasons(arms, candidates, measured, windows, source):
    reasons = source.coverage_reasons(arms, candidates=candidates, report=measured, windows=windows)
    if any(any(c[k] for k in ("broken_lines", "unreadable_files", "invalid_timestamps", "invalid_token_records",
                              "unknown_record_types")) for c in measured["coverage"]["adapters"].values()):
        reasons.append("session_coverage_errors")
    if any(not u.tokens_complete for rows in arms.values() for u in rows):
        reasons.append("token_totals_incomplete")
    if any(u.elapsed_seconds is None and (not source.finalized_elapsed_only or u.state in ("success", "failed"))
           for rows in arms.values() for u in rows):
        reasons.append("elapsed_time_not_reported")
    return sorted(set(reasons))


def statistics(arms, margin_pp, seed, resamples):
    rates = {arm: wilson(sum(u.state == "success" for u in rows), len(rows)) for arm, rows in arms.items()}
    success = newcombe(rates["before"]["numerator"], rates["before"]["denominator"],
                       rates["after"]["numerator"], rates["after"]["denominator"])
    lower, upper = success["interval_95"] or (None, None)
    success["non_inferiority"] = ("inconclusive" if lower is None else
        "non_inferior" if lower > -margin_pp / 100 else
        "inferior" if upper < -margin_pp / 100 else "inconclusive")
    costs, ratios = bootstrap(arms["before"], arms["after"], METRICS, seed=seed, resamples=resamples)
    return rates, success, costs, ratios


def task_breakdowns(arms, tasks):
    breakdowns = []
    for task in sorted(tasks["before"].keys() | tasks["after"].keys()):
        selected = {arm: [u for u in rows if (u.task_type or "<not_reported>") == task] for arm, rows in arms.items()}
        breakdowns.append({"task_type": None if task == "<not_reported>" else task, "descriptive_only": True,
            "arms": {arm: {"success_rate": wilson(sum(u.state == "success" for u in rows), len(rows)),
                           "cost": {m: {**estimate(rows, m), "interval_method": "not_estimated_descriptive"}
                                    for m in METRICS}} for arm, rows in selected.items()}})
    return breakdowns


def public_registration(registration, windows):
    return {"intervention_id": pseudonym("intervention", registration.intervention_id),
            "outcome_source": registration.outcome_source,
            "applied_at": registration.applied_at.isoformat(), "registered_at": registration.registered_at.isoformat(),
            "preregistered": registration.preregistered, "predictions": list(registration.predictions),
            "non_inferiority_margin_pp": registration.margin_pp,
            "windows": {arm: {"since": w.since.isoformat(), "until": w.until.isoformat(), "bounds": "[since,until)"}
                        for arm, w in windows.items()}}


def sample_report(registration, rates, arms):
    return {"registered_per_arm": registration.sample_size_per_arm,
            "needed_per_arm": sample_size(rates["before"]["rate"], registration.margin_pp),
            "baseline": rates["before"], "actual": {arm: len(rows) for arm, rows in arms.items()},
            "one_sided_alpha": 0.025, "power": 0.8, "true_difference": 0,
            "method": "equal_arm_unpooled_normal_approximation",
            "boundary_warning": rates["before"]["rate"] in (0, 1)}


def text_summary(report):
    local = report.get("outcome_source") == "local-verify"
    lines = summary_intro(report, local)
    size = report["sample_size"]
    if local:
        lines.append(f"Sample per arm: registered {size['registered_per_arm']}; needed {size['needed_per_arm']}; actual {size['actual']}")
    else:
        lines.append(f"Sample per arm: registered {size['registered_per_arm']}; needed {text_value(size['needed_per_arm'])}; "
                     f"actual before {size['actual']['before']}, after {size['actual']['after']}")
    for arm, row in report["arms"].items():
        lines.extend(summary_arm(arm, row, local))
    success = report["success_difference"]
    margin = text_value(report["registration"]["non_inferiority_margin_pp"], local)
    lines.append(f"After - before success: {text_value(success['difference'], local)}; "
                 f"Newcombe 95% {text_interval(success['interval_95'], local)}; "
                 f"{success['non_inferiority']}; margin {margin} percentage points")
    for metric in ("total", "time") if local else report["ratios"]:
        row = report["ratios"][metric]
        lines.append(f"{'Worker ' if local else ''}{metric} after/before: "
                     f"{text_value(row['numerator'], local)}/{text_value(row['denominator'], local)} = "
                     f"{text_value(row['value'], local)}; bootstrap 95% {text_interval(row['interval_95'], local)}; "
                     f"{row['classification']}")
    lines.append(f"Bootstrap: seed {report['bootstrap']['seed']}; resamples {report['bootstrap']['resamples']}")
    for arm, row in report["exclusions"].items():
        share = row["share"]
        if local:
            lines.append(f"{arm} excluded: {share['numerator']}/{share['denominator']}; counts {row['counts']}")
        else:
            lines.append(f"{arm} excluded or unlinked: {share['numerator']}/{share['denominator']}; "
                         + ", ".join(f"{k} {v}" for k, v in row["counts"].items()))
    for item in report["flags"]:
        lines.append(f"Flag {item['name']}: " + ("blocking" if item["blocking"] else "informational"))
    lines.extend(summary_footer(report, local))
    return "\n".join(lines)


def text_value(value, local=False):
    if local:
        return str(value)
    return "not reported" if value is None else f"{value:.6g}"


def text_interval(value, local=False):
    if local:
        return str(value)
    return "not reported" if value is None else f"[{value[0]:.6g}, {value[1]:.6g}]"


def summary_intro(report, local):
    lines = ["sumbi compare local verification" if local else "sumbi compare"]
    if local:
        if report.get("signals"):
            lines.append("; ".join(arm + " " + verifier_summary(counts)
                         for arm, counts in report["verification"]["arms"].items() if verifier_signals(counts)))
        lines.append("Outcome source: local-verify")
    lines.extend(["Verdict proposal: " + report["verdict"]["proposal"],
                  "Reasons: " + ", ".join(report["verdict"]["reasons"])])
    if "exposure_gap" in report:
        lines.append(gap_summary(report["exposure_gap"]))
    if local:
        lines.append("Cost proposal scope: retained worker sessions only")
        lines.append(f"Excluded dispatch overhead: {report['dispatch_overhead']['sessions']} sessions; observed tokens {report['dispatch_overhead']['observed_total']}")
    return lines


def summary_arm(arm, row, local):
    rate = row["success_rate"]
    lines = [f"{arm} success: {rate['numerator']}/{rate['denominator']}; Wilson 95% {text_interval(rate['wilson_95'], local)}"]
    if local:
        lines.append(f"{arm} candidate states: " + "; ".join(f"{s} {n}" for s, n in row["candidate_states"].items()))
    else:
        lines.append(f"{arm} merged-attempt checks bases: " + "; ".join(
                     f"{basis} {n}" for basis, n in row["checks_basis_counts"].items()))
    for metric in ("total", "time") if local else row["cost"]:
        cost = row["cost"][metric]
        lines.append(f"{arm} {'worker ' if local else ''}{metric} per success: "
                     f"{text_value(cost['numerator'], local)}/{cost['denominator']} = "
                     f"{text_value(cost['value'], local)}; bootstrap 95% {text_interval(cost['interval_95'], local)}")
    return lines


def summary_footer(report, local):
    if local:
        return ["Command coverage: " + "; ".join(f"{s} {n}" for s, n in report["coverage"]["commands"].items())]
    lines = []
    for kind, mix in report["mixes"].items():
        lines.append(f"{kind} mix total variation: {text_value(mix['total_variation_distance'])}")
        for arm in ("before", "after"):
            lines.append(f"  {arm}: " + "; ".join(
                f"{label} {row['numerator']}/{row['denominator']} = {text_value(row['value'])}"
                for label, row in mix[arm]["values"].items()))
    for row in report["task_type_breakdowns"]:
        lines.append("Task type " + (row["task_type"] or "not reported") + " (descriptive only): " + "; ".join(
            f"{arm} {r['success_rate']['numerator']}/{r['success_rate']['denominator']}; "
            f"Wilson 95% {text_interval(r['success_rate']['wilson_95'])}" for arm, r in row["arms"].items()))
    share = report["coverage"]["unattributed_lifetime_share"]
    lines.append(f"Unattributed lifetime tokens: {share['numerator']}/{share['denominator']}; share {text_value(share['value'])}")
    lines.append(f"Other-project lifetime tokens (context): {report['coverage']['other_lifetime_tokens']}")
    for agent, coverage in report["coverage"]["adapters"].items():
        lines.append(f"Coverage {agent}: sessions {coverage['sessions_read']}; files {coverage['files_scanned']}; "
                     f"broken lines {coverage['broken_lines']}; unreadable files {coverage['unreadable_files']}")
    return lines
