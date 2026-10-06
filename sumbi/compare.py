"""Pre-registered, offline before/after judging; verdicts are proposals."""

from collections import Counter
from fractions import Fraction
from pathlib import Path

from sumbi.catalog import load_judgment_policy
from sumbi.compare_stats import bootstrap, newcombe, sample_size
from sumbi.deliver import STATES, deliver, wilson
from sumbi.ledger import read_ledger
from sumbi.interventions import exposure_gap, exposure_side, gap_summary, in_exposure_gap
from sumbi.model import TOKEN_KINDS, Window, timestamp
from sumbi.privacy import pseudonym, pseudonym_key, read_salt
from sumbi.registration import read_registration

EXCLUDED_SHARE = 0.10
UNATTRIBUTED_SHARE = 0.05
MIX_DISTANCE = 0.20
DECISION_ORDER = ["coverage", "comparability", "success_non_inferiority", "time_and_tokens"]
METRICS = (*TOKEN_KINDS, "total", "time")


def fraction(numerator, denominator):
    """Accounting fractions are observed censuses, without sampling intervals."""
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None,
            "interval_95": None, "interval_method": "not_applicable_census"}


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


def compare(home: Path, ledger_path: Path, outcomes, registration_path: Path, *,
            agents=None, rules=None, idle_minutes=5, salt=None, seed=1729, resamples=5000,
            interventions_path=None, intervention_id=None):
    registration = read_registration(registration_path)
    gap = exposure_gap(registration, interventions_path, intervention_id)
    if registration.outcome_source != "github":
        raise ValueError("GitHub comparison requires github outcome source in registration")
    policy = load_judgment_policy()
    if policy["decision_order"] != DECISION_ORDER:
        raise ValueError("Comparison decision order does not match the bundled judgment policy")
    key = salt if salt is not None else read_salt()
    with pseudonym_key(key):
        period = Window(registration.before.since, registration.after.until)
        measured = deliver(home, period, ledger_path, outcomes, agents=agents, rules=rules,
                           idle_minutes=idle_minutes, salt=key, follow_up_days=registration.follow_up_days,
                           comparison_metadata=True, dispatch_windows=(registration.before, registration.after))
        ledger = read_ledger(ledger_path)
        windows = {"before": registration.before, "after": registration.after}
        dispatches = {d.id: d.dispatched_at for d in ledger}
        # Use deliver's repository scope, including its cost denominator scope.
        scoped = {r["id"] for r in measured["cost"]["projects"]}
        included_ids = {d.id for d in ledger if scoped.intersection(pseudonym("repository", r) for r in d.repos)}
        candidates = {arm: [r for r in measured["deliverables"] if r["id"] in included_ids
                            and w.contains(dispatches[r["id"]])] for arm, w in windows.items()}
        metadata = {s["id"]: s for s in measured["session_metadata"]}
        sessions = {r["id"]: set() for rows in candidates.values() for r in rows}
        weak_ids = set()
        for link in measured["links"]:
            identity = link["deliverable_id"]
            if identity in sessions:
                sessions[identity].add(link["session_id"])
                if link["evidence"] == "project_time_weak":
                    weak_ids.add(identity)
        arms, excluded = {}, {}
        for arm, rows in candidates.items():
            arms[arm], excluded[arm] = [], []
            for row in rows:
                identity = row["id"]
                exposure = set()
                for sid in sessions[identity]:
                    s = metadata[sid]
                    for field in ("first_at", "last_at"):
                        when = timestamp(s[field])
                        if when is not None:
                            exposure.add(exposure_side(when, registration.applied_at, gap))
                reason = ("exposure_gap" if in_exposure_gap(dispatches[identity], gap) else
                          "unlinked" if not exposure or identity in weak_ids else
                          "exposure_mixed" if exposure != {arm} else None)
                if reason:
                    excluded[arm].append({"id": pseudonym("deliverable", identity), "reason": reason})
                else:
                    arms[arm].append(row)
            arms[arm].sort(key=lambda r: r["id"])
        flags = []
        def flag(name, blocking, evidence):
            flags.append({"name": name, "blocking": blocking, "evidence": evidence})
        exclusions = {}
        for arm in arms:
            counts = Counter(r["reason"] for r in excluded[arm])
            share = fraction(len(excluded[arm]), len(candidates[arm]))
            exclusions[arm] = {"share": share, "counts": dict(sorted(counts.items())), "deliverables": excluded[arm]}
            if share["value"] is not None and share["value"] > EXCLUDED_SHARE:
                flag(arm + "_excluded_or_unlinked", True, share)
        mixes = {}
        arm_sessions = {arm: [metadata[sid] for sid in sorted({s for r in rows for s in sessions[r["id"]]})]
                        for arm, rows in arms.items()}
        shifted_agents = agent_only_in_one_arm(arm_sessions)
        if shifted_agents:
            flag("agent_mix_shift", True, {"agents": shifted_agents})
        for kind in ("model", "effort", "cli_version"):
            unobservable, partial, asymmetric = [], [], []
            for agent in sorted({s["agent"] for rows in arm_sessions.values() for s in rows}):
                reported = {arm: [bool(s[kind]) for s in rows if s["agent"] == agent]
                            for arm, rows in arm_sessions.items()}
                if not any(any(values) for values in reported.values()):
                    unobservable.append(agent)
                    continue
                if any(any(values) and not all(values) for values in reported.values()):
                    partial.append(agent)
                if all(reported.values()) and any(reported["before"]) != any(reported["after"]):
                    asymmetric.append(agent)
            for suffix, agents_affected, blocking in (("unobservable", unobservable, False),
                    ("metadata_partial", partial, True), ("metadata_asymmetric", asymmetric, True)):
                if agents_affected:
                    flag(kind + "_" + suffix, blocking, {"agents": agents_affected})
            counts = {}
            for arm, rows in arm_sessions.items():
                counter = Counter()
                for session in rows:
                    # Unobservable agents do not contribute to this dimension.
                    # Multiple values remain multiple session-value observations.
                    if session["agent"] not in unobservable:
                        counter.update(session[kind])
                counts[arm] = counter
            summaries = {arm: mix_report(c) for arm, c in counts.items()}
            distance = mix_distance(counts["before"], counts["after"])
            changed = bool(counts["before"] and counts["after"] and
                           summaries["before"]["dominant"] != summaries["after"]["dominant"])
            mixes[kind] = {**summaries, "total_variation_distance": distance,
                           "dominant_changed": changed}
            if changed or distance is not None and distance > MIX_DISTANCE:
                flag(kind + "_mix_shift", True, mixes[kind])
        tasks = {arm: Counter(r["task_type"] or "<not_reported>" for r in rows) for arm, rows in arms.items()}
        task_distance = mix_distance(tasks["before"], tasks["after"])
        mixes["task_type"] = {**{arm: mix_report(c) for arm, c in tasks.items()},
                              "total_variation_distance": task_distance}
        if task_distance is not None and task_distance > MIX_DISTANCE:
            flag("task_type_mix_shift", False, mixes["task_type"])
        sizes = [len(arms[arm]) for arm in arms]
        volume = fraction(max(sizes) - min(sizes), min(sizes))
        if max(sizes) > 1.5 * min(sizes):
            flag("volume_shift", False, volume)
        for event in registration.confounders:
            in_arms = [arm for arm, w in windows.items() if w.contains(event["at"])]
            if in_arms:
                kind = event["label"].lower().replace("-", "_").split("_", 1)[0]
                flag("registered_event", kind in ("runner", "model", "effort", "cli"),
                     {"at": event["at"].isoformat(), "label": event["label"], "arms": in_arms})
        bases = {arm: Counter(p["checks_basis"] for r in rows for p in r["pr_outcomes"] if p["merged"])
                 for arm, rows in arms.items()}
        all_bases = set(bases["before"]) | set(bases["after"])
        if len(all_bases) > 1 or "unknown" in all_bases:
            flag("checks_basis_mixed_or_unknown", True, {arm: dict(sorted(c.items())) for arm, c in bases.items()})
        spend = measured["cost"]["lifetime_scan_spend"]
        # The linked bucket includes weak links, counted once in the denominator.
        unattributed = fraction(sum(spend[b]["total"] for b in ("unallocated", "unassigned"))
                                + measured["coverage"]["evidence_tokens"]["project_time_weak"],
                                sum(spend[b]["total"] for b in ("linked", "unallocated", "unassigned")))
        if unattributed["value"] is not None and unattributed["value"] > UNATTRIBUTED_SHARE:
            flag("unattributed_spend", True, unattributed)
        coverage_reasons = []
        adapters = measured["coverage"]["adapters"]
        if not sum(c["sessions_in_lifetime_scan"] for c in adapters.values()):
            coverage_reasons.append("no_session_coverage")
        if any(any(c[k] for k in ("broken_lines", "unreadable_files", "invalid_timestamps", "invalid_token_records",
                                  "unknown_record_types")) for c in adapters.values()):
            coverage_reasons.append("session_coverage_errors")
        relevant = {r["id"] for rows in candidates.values() for r in rows}
        for d in ledger:
            if d.id not in relevant:
                continue
            if any(outcomes.pull(p) is None for p in d.prs):
                coverage_reasons.append("missing_pr_evidence")
            arm = next(a for a, w in windows.items() if w.contains(d.dispatched_at))
            for repo in d.repos:
                o = outcomes.observation(repo)
                if (o is None or not o.pulls_complete or not o.commits_complete
                        or o.start > windows[arm].since or o.until < windows[arm].until):
                    coverage_reasons.append("outcome_coverage_incomplete")
        if any(not r["tokens_complete"] for rows in arms.values() for r in rows):
            coverage_reasons.append("token_totals_incomplete")
        if any(r["state"] in ("success", "failed") and r["elapsed_seconds"] is None
               for rows in arms.values() for r in rows):
            coverage_reasons.append("elapsed_time_not_reported")
        coverage_reasons = sorted(set(coverage_reasons))
        rate = {arm: wilson(sum(r["state"] == "success" for r in rows), len(rows)) for arm, rows in arms.items()}
        success = newcombe(rate["before"]["numerator"], rate["before"]["denominator"],
                           rate["after"]["numerator"], rate["after"]["denominator"])
        lower, upper = success["interval_95"] or (None, None)
        success["non_inferiority"] = ("inconclusive" if lower is None else
            "non_inferior" if lower > -registration.margin_pp / 100 else
            "inferior" if upper < -registration.margin_pp / 100 else "inconclusive")
        costs, ratios = bootstrap(arms["before"], arms["after"], METRICS, seed=seed, resamples=resamples)
        proposal, reasons = verdict(coverage_reasons=coverage_reasons, preregistered=registration.preregistered,
            blocking_flags=sorted({f["name"] for f in flags if f["blocking"]}), arms=arms,
            registered_size=registration.sample_size_per_arm, success=success, margin_pp=registration.margin_pp, ratios=ratios)
        breakdowns = []
        for task in sorted(tasks["before"].keys() | tasks["after"].keys()):
            breakdowns.append({"task_type": None if task == "<not_reported>" else task, "descriptive_only": True,
                "arms": {arm: {"success_rate": wilson(sum(r["state"] == "success" for r in rows if (r["task_type"] or "<not_reported>") == task),
                         sum((r["task_type"] or "<not_reported>") == task for r in rows)),
                         "cost": {m: _descriptive(rows, task, m) for m in METRICS}} for arm, rows in arms.items()}})
        return {"schema_version": "compare-1.0", "pseudonyms": measured["pseudonyms"],
            "registration": {"intervention_id": pseudonym("intervention", registration.intervention_id),
                "outcome_source": registration.outcome_source,
                "applied_at": registration.applied_at.isoformat(), "registered_at": registration.registered_at.isoformat(),
                "preregistered": registration.preregistered, "predictions": list(registration.predictions),
                "non_inferiority_margin_pp": registration.margin_pp, "follow_up_days": registration.follow_up_days,
                "windows": {arm: {"since": w.since.isoformat(), "until": w.until.isoformat(), "bounds": "[since,until)"}
                            for arm, w in windows.items()}},
            **({"exposure_gap": gap} if gap is not None else {}),
            "thresholds": {"excluded_or_unlinked_share": EXCLUDED_SHARE, "unattributed_spend_share": UNATTRIBUTED_SHARE,
                           "mix_total_variation": MIX_DISTANCE, "volume_relative_difference": 0.50},
            "sample_size": {"registered_per_arm": registration.sample_size_per_arm,
                "needed_per_arm": sample_size(rate["before"]["rate"], registration.margin_pp),
                "baseline": rate["before"], "actual": {arm: len(rows) for arm, rows in arms.items()},
                "one_sided_alpha": 0.025, "power": 0.8, "true_difference": 0,
                "method": "equal_arm_unpooled_normal_approximation",
                "boundary_warning": rate["before"]["rate"] in (0, 1)},
            "bootstrap": {"seed": seed, "resamples": resamples, "unit": "deliverable", "quantiles": [0.025, 0.975]},
            "arms": {arm: {"n": len(rows), "success_rate": rate[arm], "cost": costs[arm],
                "states": {s: sum(r["state"] == s for r in rows) for s in STATES},
                "checks_basis_counts": dict(sorted(bases[arm].items())),
                "deliverables": [{**{k: v for k, v in r.items() if k != "tokens_complete"},
                                  "id": pseudonym("deliverable", r["id"])} for r in rows]} for arm, rows in arms.items()},
            "success_difference": success, "ratios": ratios, "exclusions": exclusions,
            "mixes": mixes, "flags": flags, "task_type_breakdowns": breakdowns,
            "coverage": {**measured["coverage"], "incomplete_reasons": coverage_reasons,
                         "unattributed_lifetime_share": unattributed,
                         "other_lifetime_tokens": spend["other"]["total"]},
            "links": [{**link, "deliverable_id": pseudonym("deliverable", link["deliverable_id"])
                       if link["deliverable_id"] else None} for link in measured["links"]],
            "session_metadata": measured["session_metadata"],
            "decision_order": DECISION_ORDER, "verdict": {"label": "proposal", "proposal": proposal, "reasons": reasons}}


def _descriptive(rows, task, metric):
    from sumbi.compare_stats import estimate
    result = estimate([r for r in rows if (r["task_type"] or "<not_reported>") == task], metric)
    result["interval_method"] = "not_estimated_descriptive"
    return result


def text_summary(report):
    def value(v):
        return "not reported" if v is None else f"{v:.6g}"
    def interval(v):
        return "not reported" if v is None else f"[{v[0]:.6g}, {v[1]:.6g}]"
    lines = ["sumbi compare", "Verdict proposal: " + report["verdict"]["proposal"],
             "Reasons: " + ", ".join(report["verdict"]["reasons"])]
    if "exposure_gap" in report:
        lines.append(gap_summary(report["exposure_gap"]))
    size = report["sample_size"]
    lines.append(f"Sample per arm: registered {size['registered_per_arm']}; needed {value(size['needed_per_arm'])}; "
                 f"actual before {size['actual']['before']}, after {size['actual']['after']}")
    for arm, row in report["arms"].items():
        rate = row["success_rate"]
        lines.append(f"{arm} success: {rate['numerator']}/{rate['denominator']}; Wilson 95% {interval(rate['wilson_95'])}")
        lines.append(f"{arm} merged-attempt checks bases: " + "; ".join(
            f"{basis} {n}" for basis, n in row["checks_basis_counts"].items()))
        for metric, cost in row["cost"].items():
            lines.append(f"{arm} {metric} per success: {value(cost['numerator'])}/{cost['denominator']} = "
                         f"{value(cost['value'])}; bootstrap 95% {interval(cost['interval_95'])}")
    success = report["success_difference"]
    lines.append(f"After - before success: {value(success['difference'])}; Newcombe 95% {interval(success['interval_95'])}; "
                 f"{success['non_inferiority']}; margin {report['registration']['non_inferiority_margin_pp']:g} percentage points")
    for metric, row in report["ratios"].items():
        lines.append(f"{metric} after/before: {value(row['numerator'])}/{value(row['denominator'])} = {value(row['value'])}; "
                     f"bootstrap 95% {interval(row['interval_95'])}; {row['classification']}")
    lines.append(f"Bootstrap: seed {report['bootstrap']['seed']}; resamples {report['bootstrap']['resamples']}")
    for arm, row in report["exclusions"].items():
        share = row["share"]
        lines.append(f"{arm} excluded or unlinked: {share['numerator']}/{share['denominator']}; "
                     + ", ".join(f"{k} {v}" for k, v in row["counts"].items()))
    for flag in report["flags"]:
        lines.append(f"Flag {flag['name']}: " + ("blocking" if flag["blocking"] else "informational"))
    for kind, mix in report["mixes"].items():
        lines.append(f"{kind} mix total variation: {value(mix['total_variation_distance'])}")
        for arm in ("before", "after"):
            lines.append(f"  {arm}: " + "; ".join(
                f"{label} {row['numerator']}/{row['denominator']} = {value(row['value'])}"
                for label, row in mix[arm]["values"].items()))
    for row in report["task_type_breakdowns"]:
        lines.append("Task type " + (row["task_type"] or "not reported") + " (descriptive only): " + "; ".join(
            f"{arm} {r['success_rate']['numerator']}/{r['success_rate']['denominator']}; "
            f"Wilson 95% {interval(r['success_rate']['wilson_95'])}" for arm, r in row["arms"].items()))
    share = report["coverage"]["unattributed_lifetime_share"]
    lines.append(f"Unattributed lifetime tokens: {share['numerator']}/{share['denominator']}; share {value(share['value'])}")
    lines.append(f"Other-project lifetime tokens (context): {report['coverage']['other_lifetime_tokens']}")
    for agent, coverage in report["coverage"]["adapters"].items():
        lines.append(f"Coverage {agent}: sessions {coverage['sessions_read']}; files {coverage['files_scanned']}; "
                     f"broken lines {coverage['broken_lines']}; unreadable files {coverage['unreadable_files']}")
    return "\n".join(lines)
