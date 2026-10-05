"""Fixed worker-session units and local verification outcome measurements."""

from collections import Counter
from datetime import timedelta
from pathlib import Path

from sumbi.model import Coverage, RepositoryAttributor, TOKEN_KINDS, Window, normalize_path
from sumbi.privacy import current_key, pseudonym_key, read_salt
from sumbi.report import ADAPTERS
from sumbi.verification import declared_commands, recognize

STATES = ("success", "failed", "unverified", "in_progress", "no_change")
METRICS = (*TOKEN_KINDS, "total", "time")


def token_measurement(session, scan):
    """Partial totals remain null, with observed components retained separately."""
    events = [values for at, _, kind, (values, *_) in session.attribution_events
              if kind == "usage" and scan.contains(at)]
    required = ("new_input", "cache_read", "output") if session.agent == "codex" else TOKEN_KINDS[:4]
    tokens = {kind: sum(v[kind] for v in events) if events and all(v.get(kind) is not None for v in events)
              else None for kind in TOKEN_KINDS}
    complete = bool(events) and all(tokens[k] is not None for k in required)
    tokens["total"] = sum(tokens[k] for k in required) if complete else None
    observed = sum(v.get(k) or 0 for v in events for k in TOKEN_KINDS if k != "reasoning_output")
    return tokens, complete, observed


def state(session, scan, declared, active_minutes, repository):
    edits = [t for t in session.edits.values() if t < scan.until]
    executions = sorted((e for e in session.commands.values() if e.at < scan.until), key=lambda e: e.at)
    coverage = Counter()
    coverage.update(session.local_evidence_gaps)
    matches = []
    for execution in executions:
        shape = recognize(execution.command, declared)
        coverage[shape] += 1
        if shape == "matched":
            coverage["unknown_exit_code"] += execution.exit_code is None
            coverage["unknown_execution_start"] += execution.started_at is None
            coverage["unknown_execution_cwd"] += execution.cwd is None
            matches.append(execution)
    times = [t for t in session.times if t < scan.until]
    latest = max(times, default=None)
    if not edits and not session.local_evidence_gaps.get("edit_timestamp_missing"):
        result, reason = "no_change", "no_known_edit"
    elif session.local_evidence_gaps:
        result, reason = "unverified", "incomplete_edit_or_command_timestamps"
    elif latest is not None and scan.until - latest <= timedelta(minutes=active_minutes) and (
            session.completed_at is None or session.completed_at < latest or session.completed_at >= scan.until):
        result, reason = "in_progress", "recent_activity"
    else:
        last_edit = max(edits)
        after = [e for e in matches if e.at > last_edit]
        # A tie or unknown last code cannot preserve an earlier success.
        last = [e for e in after if e.at == after[-1].at] if after else []
        codes = {e.exit_code for e in last}
        if not last:
            result, reason = "unverified", "no_check_after_last_edit"
        elif any(e.started_at is None or e.started_at <= last_edit or e.started_at > e.at for e in last):
            result, reason = "unverified", "check_start_not_after_last_edit"
        elif any(not e.cwd or normalize_path(e.cwd) != normalize_path(str(repository.resolve())) for e in last):
            result, reason = "unverified", "verification_cwd_unconfirmed"
            coverage["verification_cwd_unconfirmed"] += 1
        elif None in codes or len(codes) != 1:
            result, reason = "unverified", "last_check_exit_unknown"
        else:
            result = "success" if next(iter(codes)) == 0 else "failed"
            reason = "last_check_passed" if result == "success" else "last_check_failed"
    return result, reason, coverage, latest


def deliver_local(home: Path, window: Window, repository: Path, *, agents=None,
                  verify=None, scan_until=None, active_minutes=5, salt=None):
    """Select units solely from start metadata, before inspecting outcomes."""
    if scan_until is not None and scan_until < window.until:
        raise ValueError("Local outcome scan end must cover the dispatch window")
    declared = declared_commands(repository, verify)
    scan = Window(window.since, scan_until or window.until)
    with pseudonym_key(salt if salt is not None else read_salt()):
        attributor = RepositoryAttributor(repository)
        units, overhead, excluded_scope = [], [], Counter()
        adapters, command_coverage = {}, Counter()
        for agent in agents if agents is not None else ADAPTERS:
            measured = Coverage()
            sessions = ADAPTERS[agent].collect(home, scan, measured)
            adapters[agent] = {**measured.as_dict(), "sessions_read": len(sessions)}
            for session in sessions:
                if not session.start_at or not session.start_cwd:
                    if session.in_window(scan):
                        excluded_scope["missing_start_metadata"] += 1
                    continue
                link = attributor.link(session.start_cwd)
                if link["bucket"] != "project":
                    excluded_scope[link["bucket"]] += 1
                    continue
                if not session.is_worker:
                    if session.in_window(scan):
                        tokens, complete, observed = token_measurement(session, scan)
                        overhead.append({"id": session.id(), "agent": agent, "tokens": tokens,
                                         "tokens_complete": complete, "observed_total": observed})
                    continue
                if not window.contains(session.start_at):
                    continue
                lifetime = Window(session.start_at, scan.until)
                tokens, complete, observed = token_measurement(session, lifetime)
                outcome, reason, coverage, latest = state(session, lifetime, declared, active_minutes, repository)
                command_coverage.update(coverage)
                units.append({"id": session.id(), "agent": agent, "parent_id":
                    session.as_dict(lifetime, attributor, active_minutes)["parent_id"],
                    "dispatched_at": session.start_at.isoformat(), "last_at": latest.isoformat() if latest else None,
                    "state": outcome, "reason": reason, "task_type": None,
                    "tokens": tokens, "tokens_complete": complete, "observed_total": observed,
                    "elapsed_seconds": (latest - session.start_at).total_seconds() if latest else None,
                    "model": sorted(session.models), "effort": sorted(session.efforts),
                    "cli_version": sorted(session.versions), "command_coverage": dict(sorted(coverage.items()))})
        units.sort(key=lambda row: row["id"])
        retained = [r for r in units if r["state"] not in ("in_progress", "no_change")]
        from sumbi.deliver import wilson
        from sumbi.compare_stats import estimate
        return {"schema_version": "local-deliver-1.0", "outcome_source": "local-verify",
                "unit": "worker_session", "dispatch_window": {"since": window.since.isoformat(),
                    "until": window.until.isoformat(), "bounds": "[since,until)"},
                "scan_until": scan.until.isoformat(), "active_minutes": active_minutes,
                "pseudonyms": {"method": "hmac_sha256" if current_key() is not None else "sha256",
                               "salted": current_key() is not None, "local_only": True},
                "states": {s: sum(r["state"] == s for r in units) for s in STATES},
                "units": units, "success_rate": wilson(sum(r["state"] == "success" for r in retained), len(retained)),
                "cost": {"basis": "retained_worker_lifetime_only", "per_success":
                    {m: {**estimate(retained, m), "interval_method": "not_estimated_descriptive"} for m in METRICS}},
                "dispatch_overhead": {"sessions": len(overhead), "rows": overhead,
                    "observed_total": sum(r["observed_total"] for r in overhead)},
                "excluded_worker_spend": {"observed_total": sum(r["observed_total"] for r in units if r not in retained)},
                "coverage": {"adapters": adapters, "commands": dict(sorted(command_coverage.items())),
                             "excluded_scope": dict(sorted(excluded_scope.items()))},
                "limitations": ["agent_triggered_machine_check", "no_human_acceptance_or_revert_window",
                                "shell_edits_invisible", "worker_costs_exclude_dispatch_overhead",
                                "last_observed_time_is_not_acceptance", "deleted_logs_undetectable"]}


def text_summary(report):
    lines = ["sumbi local verification", "Outcome source: local-verify", "States: " +
             "; ".join(f"{s} {n}" for s, n in report["states"].items())]
    rate = report["success_rate"]
    lines.append(f"Retained success: {rate['numerator']}/{rate['denominator']}; Wilson 95% {rate['wilson_95']}")
    lines.append(f"Dispatch overhead sessions: {report['dispatch_overhead']['sessions']}; observed tokens {report['dispatch_overhead']['observed_total']}")
    lines.append("Cost scope: retained worker sessions only")
    for metric in ("total", "time"):
        cost = report["cost"]["per_success"][metric]
        lines.append(f"Worker {metric} per success: {cost['numerator']}/{cost['denominator']} = {cost['value']}; descriptive only")
    lines.append("Command coverage: " + "; ".join(f"{s} {n}" for s, n in report["coverage"]["commands"].items()))
    return "\n".join(lines)
