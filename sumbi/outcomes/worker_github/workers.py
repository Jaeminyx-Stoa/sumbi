"""Measure fixed worker sessions against remote GitHub constituent outcomes."""

from collections import Counter
from datetime import timedelta
import math

from sumbi.core.privacy import current_key, pseudonym, pseudonym_key, read_salt
from sumbi.core.records import Coverage
from sumbi.core.stats import estimate, wilson
from sumbi.core.time import Window
from sumbi.events.registry import ADAPTERS, DEFAULT_AGENTS
from sumbi.measure.attribution import Attributor
from sumbi.outcomes.github.deliver import judge
from sumbi.outcomes.github.ledger import Deliverable
from sumbi.outcomes.local_verify.workers import token_measurement
from sumbi.outcomes.worker_github.links import (link_prs, public_links, references,
    repositories, repository_owners, start_repo)
from sumbi.outcomes.worker_github.scope import capture
from sumbi.sessions.builder import collect
from sumbi.sessions.session import TOKEN_KINDS

STATES = ("success", "failed", "immature", "in_progress", "no_pr", "no_change")
METRICS = (*TOKEN_KINDS, "total", "time")


def sessions(home, scan, agents):
    found, coverage = [], {}
    for agent in agents if agents is not None else DEFAULT_AGENTS:
        measured = Coverage()
        rows = collect(ADAPTERS[agent], home, scan, measured, collect_links=True,
            worker_links=True)
        found.extend(rows)
        coverage[agent] = {**measured.as_dict(), "sessions_read": len(rows)}
    return found, coverage


def dispatched(session):
    return session.dispatch_kind in ("subagent", "noninteractive_exec")


def worker_judgment(session, lifetime, links, outcomes, days, authored=False):
    """Reuse constituent judgment, but a repair cannot recover this dispatch."""
    edits = any(lifetime.contains(t) for t in session.edits.values())
    incomplete = bool(session.local_evidence_gaps)
    changed = edits or incomplete or authored or any(
        evidence != "cwd_branch" for evidence, _, _ in links.values())
    if not links or not changed:
        return {"state": "no_pr" if changed else "no_change",
            "reason": "no_linked_pr" if changed else "no_known_edit",
            "attempt_prs": []}
    # Link extraction already excludes actions after closure. Continued PRs
    # need their original creation bound only for the reused ledger judge;
    # dispatch membership, costs and elapsed time still use the worker start.
    created = [p.created_at for key in links if (p := outcomes.pull(key))]
    identity = Deliverable(session.id(), min([session.start_at, *created]),
        tuple(sorted({p.rsplit("#", 1)[0] for p in links})), tuple(sorted(links)), ())
    result = judge(identity, outcomes, days)
    for pr_id in result["attempt_prs"]:
        pr = outcomes.pull(pr_id)
        if not pr or not pr.merged_at:
            continue
        follows, reverts = outcomes.disturbances(pr, days)
        fixes = outcomes.commit_fixes(pr, days)
        reason = ("checks_red" if pr.checks == "red" else "reverted" if reverts
            else "follow_up_fix" if fixes or any(p.merged_at for p in follows) else None)
        if reason:
            result.update(state="failed", reason=reason)
            break
    return result


def worker_row(session, scan, links, outcomes, days, scope, gaps, authored=False):
    lifetime = Window(session.start_at, scan.until)
    tokens, complete, observed = token_measurement(session, lifetime)
    judgment_incomplete = False
    try:
        result = worker_judgment(session, lifetime, links, outcomes, days, authored)
    except (ValueError, OverflowError, RecursionError):
        # Malformed temporal evidence or cyclic repairs affect this unit only.
        gaps["judgment_conflicts"] += 1
        links = {}
        judgment_incomplete = True
        result = {"state": "in_progress", "reason": "outcome_evidence_incomplete",
            "attempt_prs": []}
    latest = max((t for t in session.times if lifetime.contains(t)), default=None)
    prs = [outcomes.pull(p) for p in result["attempt_prs"]]
    return {"id": session.id(), "agent": session.public_agent(),
        "dispatched_at": session.start_at.isoformat(),
        "dispatch_kind": session.dispatch_kind, "start_scope": scope,
        "last_at": latest.isoformat() if latest else None,
        "state": result["state"], "reason": result["reason"], "task_type": None,
        "links": public_links(session.id(), links),
        "weak_link": any(evidence == "cwd_branch" for evidence, _, _ in links.values()),
        "prs": [pseudonym("pr", p) for p in result["attempt_prs"]],
        "missing_prs": sum(p is None for p in prs),
        "checks_basis": [p.checks_basis for p in prs if p and p.merged_at],
        "tokens": tokens, "tokens_complete": complete, "observed_total": observed,
        "elapsed_seconds": (latest - session.start_at).total_seconds() if latest else None,
        "elapsed_measurement": "observed_worker_span",
        "model": sorted(session.models), "effort": sorted(session.efforts),
        "cli_version": sorted(session.versions),
        "metadata_incomplete": dict(session.metadata_incomplete),
        "evidence_incomplete": bool(session.local_evidence_gaps) or judgment_incomplete}


def deliver_workers(home, window, repos, outcomes, *, agents=None, salt=None, follow_up_days=7,
    repo_owners=()):
    owners = repository_owners(repo_owners)
    repos = repositories(repos, allow_empty=bool(owners))
    if not math.isfinite(follow_up_days) or follow_up_days <= 0:
        raise ValueError("Follow-up days must be positive and finite")
    try:
        window.until + timedelta(days=follow_up_days)
    except OverflowError:
        raise ValueError("Outcome observation window exceeds timestamp range") from None
    with pseudonym_key(salt if salt is not None else read_salt()):
        # Freeze dispatch membership before a live adapter reads any outcome.
        initial, _ = sessions(home, window, agents)
        fixed = {s.id() for s in initial if s.start_at and window.contains(s.start_at)
            and dispatched(s)}
        repos, outcomes, observations, scan, found, adapters = capture(initial, window,
            repos, owners, outcomes, fixed, lambda scan: sessions(home, scan, agents))
        units, overhead, excluded, gaps = [], [], Counter(), Counter()
        attributor = Attributor([])
        for session in found:
            if not session.in_window(scan):
                continue
            if not session.start_at:
                excluded["missing_start_metadata"] += 1
                continue
            origin_repo = start_repo(session, attributor, repos)
            lifetime = (Window(session.start_at, scan.until)
                if session.start_at < scan.until else None)
            if lifetime is None:
                continue
            refs = references(session, lifetime)
            evidence_repos = set()
            links = link_prs(session, lifetime, refs, repos, origin_repo, outcomes,
                attributor, gaps if session.id() in fixed else Counter(), evidence_repos)
            if not origin_repo and not evidence_repos:
                excluded["repository_unconfirmed"] += 1
                continue
            if not dispatched(session):
                tokens, complete, observed = token_measurement(session, scan)
                overhead.append({"id": session.id(), "agent": session.public_agent(),
                    "tokens": tokens, "tokens_complete": complete, "observed_total": observed})
                continue
            if session.id() in fixed:
                unreadable_only = bool(evidence_repos & outcomes.unreadable) and not (
                    evidence_repos - outcomes.unreadable) and not any(
                    evidence != "cwd_branch" for evidence, _, _ in links.values())
                if unreadable_only:
                    excluded["repository_unreadable"] += 1
                    links = {}
                row = worker_row(session, scan, links, outcomes, follow_up_days,
                    "start_origin" if origin_repo else "own_reference", gaps,
                    authored=bool(evidence_repos))
                if unreadable_only:
                    row.update(state="in_progress", reason="repository_unreadable")
                units.append(row)
        return worker_report(window, scan, repos, units, overhead, excluded, adapters,
            observations, follow_up_days, gaps, outcomes.unreadable)


def worker_report(window, scan, repos, units, overhead, excluded, adapters, observations, days,
    gaps, unreadable=()):
    units.sort(key=lambda row: row["id"])
    overhead.sort(key=lambda row: row["id"])
    retained = [r for r in units if r["state"] != "no_change" and not r["weak_link"]
        and r["reason"] != "repository_unreadable"]
    return {"schema_version": "worker-github-deliver-1.0", "outcome_source": "worker-github",
        "unit": "worker_session", "dispatch_window": {"since": window.since.isoformat(),
            "until": window.until.isoformat(), "bounds": "[since,until)"},
        "scan_until": scan.until.isoformat(), "follow_up_days": days,
        "pseudonyms": {"salted": current_key() is not None,
            "algorithm": "hmac-sha256" if current_key() is not None else "sha256"},
        "states": {s: sum(r["state"] == s for r in units) for s in STATES},
        "state_reasons": state_reasons(units), "units": units,
        "success_rate": wilson(sum(r["state"] == "success" for r in retained), len(retained)),
        "cost": {"basis": "retained_worker_lifetime_only", "per_success":
            {m: {**estimate(retained, m), "interval_method": "not_estimated_descriptive"}
                for m in METRICS}},
        "dispatch_overhead": {"sessions": len(overhead), "rows": overhead,
            "observed_total": sum(r["observed_total"] for r in overhead)},
        "excluded_worker_spend": {"observed_total": sum(r["observed_total"]
            for r in units if r not in retained)},
        "coverage": {"adapters": adapters, "excluded_scope": dict(sorted(excluded.items())),
            "repositories": [{"id": pseudonym("repository", repo),
                **({"reason": "repository_unreadable"} if repo in unreadable else {}),
                "coverage_start": o.start.isoformat() if o else None,
                "observed_at": o.until.isoformat() if o else None,
                "pulls_complete": o.pulls_complete if o else False,
                "commits_complete": o.commits_complete if o else False}
                for repo, o in zip(repos, observations)],
            "missing_prs": sum(r["missing_prs"] for r in units),
            "evidence_gaps": dict(sorted(gaps.items())),
            "link_evidence_counts": dict(sorted(Counter(link["evidence"]
                for r in units for link in r["links"]).items()))},
        "limitations": ["worker_costs_exclude_dispatch_overhead", "shell_edits_invisible",
            "deleted_logs_undetectable", "last_observed_time_is_not_acceptance",
            "continued_pr_requires_authorship", "repairs_fail_original_dispatch"]}


def text_summary(report):
    rate = report["success_rate"]
    lines = ["sumbi worker GitHub", "Outcome source: worker-github", "States: "
        + "; ".join(f"{s} {n}" for s, n in report["states"].items()),
        f"Retained success: {rate['numerator']}/{rate['denominator']}; Wilson 95% "
        + str(rate["wilson_95"]), "Cost scope: retained worker sessions only",
        f"Dispatch overhead sessions: {report['dispatch_overhead']['sessions']}; observed "
        f"tokens {report['dispatch_overhead']['observed_total']}"]
    lines.extend(reason_lines(report["state_reasons"]))
    for metric in ("total", "time"):
        row = report["cost"]["per_success"][metric]
        lines.append(f"Worker {metric} per success: {row['numerator']}/{row['denominator']} "
            f"= {row['value']}; descriptive only")
    return "\n".join(lines)


def state_reasons(rows):
    return {state: dict(sorted(Counter(r["reason"] for r in rows
        if r["state"] == state).items())) for state in STATES}


def reason_lines(reasons, prefix=""):
    return [f"{prefix}{state}: {reason} {count}" for state, counts in reasons.items()
        for reason, count in counts.items()]
