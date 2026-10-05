"""Deliverable judgments, event links and token cost per success (offline)."""

from collections import Counter
from datetime import timedelta
import fnmatch
import math
import ntpath
from pathlib import Path
import re

from sumbi.ledger import Deliverable, read_ledger
from sumbi.model import Attributor, Coverage, TOKEN_KINDS, Window, normalize_origin, normalize_path
from sumbi.outcomes import Outcomes, REVERT
from sumbi.privacy import current_key, pseudonym, pseudonym_key, read_salt
from sumbi.report import ADAPTERS

STATES = ("success", "failed", "in_progress", "immature")
CHECKS_BASES = ("historical", "current_policy", "all_visible", "unknown")
LINKS = ("deliverable_id", "tool_reference", "project_time_weak", "ambiguous", "unallocated", "unassigned", "other")


def wilson(successes, total):
    if type(total) is not int or type(successes) is not int or not 0 <= successes <= total:
        raise ValueError("Wilson counts must be integers with 0 <= successes <= total")
    if total == 0:
        return {"numerator": 0, "denominator": 0, "rate": None, "wilson_95": None}
    z = 1.959963984540054
    p, z2 = successes / total, z * z
    center = (p + z2 / (2 * total)) / (1 + z2 / total)
    radius = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    return {"numerator": successes, "denominator": total, "rate": p,
            "wilson_95": [max(0.0, center - radius), min(1.0, center + radius)]}


def judge(deliverable: Deliverable, outcomes: Outcomes, days=7):
    """All constituent PRs must succeed. Follow-ups are attempts, not successes."""
    if deliverable.abandoned:
        return {"state": "failed", "first_pass_success": False, "closed_at": deliverable.abandoned_at,
                "reason": "abandoned", "attempt_prs": list(deliverable.prs)}
    if not deliverable.prs:
        return {"state": "in_progress", "first_pass_success": False, "closed_at": None,
                "reason": "no_pr", "attempt_prs": []}
    pulls = [outcomes.pull(p) for p in deliverable.prs]
    known = [p for p in pulls if p is not None]
    if any(p.created_at < deliverable.dispatched_at or (p.merged_at and p.merged_at < deliverable.dispatched_at) for p in known):
        raise ValueError("Ledger PR evidence precedes dispatch")
    constituents = [p for p in known if deliverable.role(p.id) == "constituent"]
    superseded = {p.id for p in known if deliverable.role(p.id) == "retry" and not p.merged_at
                  and p.state == "closed" and any(c.merged_at and c.merged_at > p.closed_at
                      and c.id.rsplit("#", 1)[0] == p.id.rsplit("#", 1)[0] for c in constituents)}
    explicit_follows = [p for p in known if deliverable.role(p.id) == "followup"]
    memo = {}

    def attempt(pr):
        if pr.id not in memo:
            memo[pr.id] = assess(pr)
        return memo[pr.id]

    def assess(pr):
        observation = outcomes.observation(pr.id.rsplit("#", 1)[0])
        if not pr.merged_at:
            return ("failed" if pr.state == "closed" else "in_progress", False, pr.closed_at, "closed_unmerged" if pr.state == "closed" else "open", [])
        follows, reverts = outcomes.disturbances(pr, days)
        fixes = getattr(outcomes, "commit_fixes", lambda p, d: [])(pr, days)
        end = pr.merged_at + timedelta(days=days)
        eligible = [p for p in explicit_follows if p.id != pr.id
                    and pr.merged_at < (p.merged_at or p.created_at) < end]
        follows = list({p.id: p for p in [*follows, *eligible] if p.id not in superseded}.values())
        revert_prs = [p for p in follows if REVERT.search(p.title + "\n" + p.body)]
        reverts = sorted([*reverts, *[p.merged_at for p in revert_prs if p.merged_at]])
        repairs = [p for p in follows if p not in revert_prs]
        descendants = [p.id for p in revert_prs]
        results = []
        for follow in repairs:
            result = attempt(follow)
            results.append(result)
            descendants.extend([follow.id, *result[4]])
        if pr.checks != "green":
            reason = "checks_none" if pr.checks_reason == "no_checks" else pr.checks_reason
            return "in_progress", False, pr.merged_at, reason, descendants
        # A merged repair following every revert can recover eventual success.
        if reverts and not any(p.merged_at and p.merged_at > max(reverts) for p in repairs):
            return "failed", False, pr.merged_at, "reverted", descendants
        if any(p.state == "open" for p in revert_prs):
            return "in_progress", False, None, "revert_pending", descendants
        if results:
            if any(r[0] == "failed" for r in results):
                return "failed", False, max((r[2] for r in results if r[2]), default=pr.merged_at), "follow_up_failed", descendants
            if any(r[0] == "in_progress" for r in results):
                return "in_progress", False, None, "follow_up_pending", descendants
        mature_at = max([pr.merged_at, *fixes]) + timedelta(days=days)
        complete = (observation is not None and observation.pulls_complete and observation.commits_complete
                    and observation.start <= pr.merged_at and observation.until >= mature_at)
        if not complete:
            reason = "window_open" if observation and observation.until < mature_at else "outcome_coverage_incomplete"
            return "immature", False, pr.merged_at, reason, descendants
        if any(r[0] == "immature" for r in results):
            return "immature", False, max(r[2] for r in results if r[2]), "follow_up_window_open", descendants
        return "success", not (follows or reverts or fixes), max([pr.merged_at, *[r[2] for r in results if r[2]]]), "accepted", descendants

    required = [p for p in known if deliverable.role(p.id) != "followup" and p.id not in superseded]
    results = [attempt(p) for p in required]
    if not any(deliverable.role(p) == "constituent" for p in deliverable.prs) and not any(r[0] == "failed" for r in results):
        return {"state": "in_progress", "first_pass_success": False, "closed_at": None,
                "reason": "no_constituent", "attempt_prs": list(deliverable.prs)}
    if len(known) != len(pulls) and not any(r[0] == "failed" for r in results):
        return {"state": "in_progress", "first_pass_success": False, "closed_at": None,
                "reason": "missing_pr", "attempt_prs": sorted(set([*deliverable.prs, *[p for r in results for p in r[4]]]))}
    state = next((s for s in ("failed", "in_progress", "immature") if any(r[0] == s for r in results)), "success")
    if state == "success" and deliverable.human == "n":
        state, reason = "in_progress", "human_acceptance_pending"
    else:
        reason = next((r[3] for r in results if r[0] == state), "accepted")
    closed = max((r[2] for r in results if r[2]), default=None)
    if state == "in_progress":
        closed = None
    return {"state": state, "first_pass_success": state == "success" and all(r[1] for r in results)
            and not any(deliverable.role(p) == "retry" for p in deliverable.prs),
            "closed_at": closed, "reason": reason,
            "attempt_prs": sorted(set([*deliverable.prs, *[p for r in results for p in r[4]]]))}


def empty_tokens():
    return {**dict.fromkeys(TOKEN_KINDS), "total": 0}


def add_tokens(target, values):
    for kind in TOKEN_KINDS:
        if values.get(kind) is not None:
            target[kind] = (target[kind] or 0) + values[kind]
    target["total"] += sum(values.get(k) or 0 for k in TOKEN_KINDS if k != "reasoning_output")


def cost(tokens, successes):
    return {"spend": tokens, "successes": successes,
            "tokens_per_success": {k: (v / successes if successes and v is not None else None) for k, v in tokens.items()},
            "basis": "reported_tokens_excluding_reasoning_subset", "monetary_cost": "not_evaluated"}


def id_matches(value, ledger, *, path=False):
    if not value:
        return set()
    flags = re.I if path and (ntpath.splitdrive(value)[0] or value.startswith("//")) else 0
    return {d.id for d in ledger if re.search(r"(?<![A-Za-z0-9])" + re.escape(d.id) + r"(?![A-Za-z0-9])", value, flags)}


def reference_matches(refs, ledger, judgments):
    return {d.id for d in ledger if any((kind in ("branch", "branch_change", "branch_observed") and value in d.branches)
            or (kind == "pr" and value in judgments[d.id]["attempt_prs"]) for kind, value in refs)}


def project_repos(cwd, link, attributor, repos):
    if link["bucket"] != "project":
        return set()
    if cwd:
        origin, _ = attributor.origin(cwd)
        if origin:
            return {r for r in repos if normalize_origin("https://github.com/" + r) == origin.lower()}
    rules = [r for r in attributor.rules if r.name == link["rule"]]
    return {repo for repo in repos if any(
        any(fnmatch.fnmatchcase("github.com/" + repo, (normalize_origin(p) or p).lower()) for p in rule.origins)
        or (not rule.origins and rule.name == repo.split("/", 1)[1]) for rule in rules)}


def linked_events(session, scan, attributor, idle, ledger, judgments):
    hints = sorted(session.deliverable_events, key=lambda e: (e[0], e[1]))
    index, current_branch, current_directory, refs = 0, None, None, set()
    for when, order, values, project, project_evidence, cwd in session.event_allocations(scan, attributor, idle):
        own_branch, own_refs = None, set()
        while index < len(hints) and (hints[index][0], hints[index][1]) <= (when, order):
            t, o, kind, data = hints[index]
            if kind == "context":
                context_branch, directory = data
                directory = normalize_path(directory) if isinstance(directory, str) and directory else None
                if context_branch is not None:
                    current_branch = context_branch
                elif directory != current_directory or directory is None:
                    current_branch = None
                current_directory, refs = directory, set()
            elif kind == "refs":
                if any(k in ("branch_change", "branch_observed") and v != current_branch for k, v in data):
                    current_branch = None
                refs.update(data)
            elif (t, o) == (when, order):
                own_branch, own_refs = data
            index += 1
        branch = own_branch or current_branch
        matches = id_matches(cwd, ledger, path=True) | id_matches(branch, ledger)
        evidence = "deliverable_id"
        if not matches:
            matches = reference_matches(refs | own_refs | ({("branch", branch)} if branch else set()), ledger, judgments)
            evidence = "tool_reference"
        repositories = project_repos(cwd, project, attributor, {r for d in ledger for r in d.repos})
        # Explicit deliverable evidence can identify its repository without a cwd.
        if len(matches) == 1 and not repositories and project["bucket"] != "other":
            d = next(d for d in ledger if d.id in matches)
            known_origin = attributor.origin(cwd)[0] if cwd else None
            if not known_origin:
                repositories = set(d.repos)
        if not matches:
            matches = {d.id for d in ledger if repositories.intersection(d.repos)
                       and d.dispatched_at <= when and (judgments[d.id]["closed_at"] is None or when <= judgments[d.id]["closed_at"])}
            evidence = "project_time_weak"
        # Project ambiguity and deliverable ambiguity cannot be split by guesswork.
        selected = next(iter(matches)) if len(matches) == 1 else None
        if selected:
            d = next(d for d in ledger if d.id == selected)
            if when < d.dispatched_at or (repositories and not repositories.intersection(d.repos)):
                selected = None
        if project["bucket"] == "other":
            bucket, selected, evidence = "other", None, "other"
        elif len(repositories) == 1:
            bucket = "linked" if selected else "unallocated"
            if selected is None:
                evidence = "ambiguous" if matches else "unallocated"
        else:
            bucket, selected = "unassigned", None
            evidence = "ambiguous" if matches or len(repositories) > 1 else "unassigned"
        yield when, values, selected, bucket, evidence, next(iter(repositories)) if len(repositories) == 1 else None


def deliver(home: Path, window: Window, ledger_path: Path, outcomes: Outcomes, *, agents=None,
            rules=None, idle_minutes=5, salt=None, follow_up_days=7, comparison_metadata=False,
            dispatch_windows=None):
    if not math.isfinite(follow_up_days) or follow_up_days <= 0:
        raise ValueError("Follow-up days must be positive and finite")
    with pseudonym_key(salt if salt is not None else read_salt()):
        ledger = read_ledger(ledger_path)
        if dispatch_windows is not None:
            ledger = [d for d in ledger if any(w.contains(d.dispatched_at) for w in dispatch_windows)]
        try:
            judgments = {d.id: judge(d, outcomes, follow_up_days) for d in ledger}
        except OverflowError:
            raise ValueError("Outcome observation window exceeds timestamp range") from None
        observations = [outcomes.observation(r) for r in {r for d in ledger for r in d.repos}]
        end = max([window.until, *[o.until for o in observations if o]])
        begin = min([window.since, *[d.dispatched_at for d in ledger]])
        scan = Window(begin, end)
        attributor = Attributor(rules or [])
        period = {k: empty_tokens() for k in ("linked", "unallocated", "unassigned", "other")}
        lifetime = {k: empty_tokens() for k in period}
        by_deliverable = {d.id: empty_tokens() for d in ledger}
        tokens_complete = dict.fromkeys(by_deliverable, True)
        session_metadata = {}
        evidence_counts, evidence_tokens = Counter(), Counter()
        missing_tokens = {scope: dict.fromkeys(TOKEN_KINDS, 0) for scope in ("period", "lifetime_scan")}
        coverage, links, project_spend = {}, {}, {}
        for agent in agents if agents is not None else ADAPTERS:
            measured = Coverage()
            found = ADAPTERS[agent].collect(home, scan, measured, collect_links=True)
            coverage[agent] = {**measured.as_dict(), "sessions_read": len(found),
                               "sessions_in_period": sum(s.in_window(window) for s in found),
                               "sessions_in_lifetime_scan": sum(s.in_window(scan) for s in found)}
            for session in found:
                if comparison_metadata:
                    times = [t for t in session.times if t < scan.until]
                    session_metadata[session.id()] = {"id": session.id(), "agent": agent,
                        "first_at": min(times).isoformat() if times else None,
                        "last_at": max(times).isoformat() if times else None,
                        "model": sorted(session.models), "effort": sorted(session.efforts),
                        "cli_version": sorted(session.versions)}
                for when, values, selected, bucket, evidence, repo in linked_events(session, scan, attributor,
                                                idle_minutes, ledger, judgments):
                    add_tokens(lifetime[bucket], values)
                    for kind in TOKEN_KINDS:
                        missing_tokens["lifetime_scan"][kind] += values.get(kind) is None
                    if selected:
                        add_tokens(by_deliverable[selected], values)
                        required = ("new_input", "cache_read", "output") + (("cache_write",) if agent == "claude-code" else ())
                        tokens_complete[selected] &= all(values.get(k) is not None for k in required)
                    if window.contains(when):
                        add_tokens(period[bucket], values)
                        for kind in TOKEN_KINDS:
                            missing_tokens["period"][kind] += values.get(kind) is None
                        if repo:
                            add_tokens(project_spend.setdefault(repo, empty_tokens()), values)
                    evidence_counts[evidence] += 1
                    evidence_tokens[evidence] += sum(values.get(k) or 0 for k in TOKEN_KINDS if k != "reasoning_output")
                    identity = session.id(), selected, bucket, evidence
                    row = links.setdefault(identity, {"session_id": session.id(), "deliverable_id": selected,
                            "bucket": bucket, "evidence": evidence, "events": 0, "tokens": empty_tokens()})
                    row["events"] += 1
                    add_tokens(row["tokens"], values)
        repos = {r for d in ledger for r in d.repos}
        scoped_repos = repos if not attributor.rules else {
            repo for repo in repos if any(
                any(fnmatch.fnmatchcase("github.com/" + repo, (normalize_origin(p) or p).lower()) for p in rule.origins)
                or (not rule.origins and rule.name == repo.split("/", 1)[1]) for rule in attributor.rules)} | set(project_spend)
        cohort = [d for d in ledger if window.contains(d.dispatched_at) and scoped_repos.intersection(d.repos)]
        successes = [d for d in ledger if judgments[d.id]["state"] == "success"]
        operational_successes = [d for d in successes if window.contains(judgments[d.id]["closed_at"])
                                 and scoped_repos.intersection(d.repos)]
        cohort_successes = [d for d in cohort if judgments[d.id]["state"] == "success"]
        operational_tokens, cohort_tokens = empty_tokens(), empty_tokens()
        add_tokens(operational_tokens, period["linked"])
        add_tokens(operational_tokens, period["unallocated"])
        for d in cohort:
            add_tokens(cohort_tokens, by_deliverable[d.id])
        total = sum(t["total"] for t in period.values())
        unattributed = period["unassigned"]["total"] + period["unallocated"]["total"] + period["other"]["total"]
        rows = []
        for d in ledger:
            j = judgments[d.id]
            merges = [outcomes.pull(p).merged_at for p in j["attempt_prs"] if outcomes.pull(p) and outcomes.pull(p).merged_at]
            elapsed_end = j["closed_at"] if j["state"] == "failed" else max(merges, default=None)
            rows.append({"id": d.id, "task_type": d.task_type, "state": j["state"], "reason": j["reason"],
                         "checks_basis": deliverable_basis(j["attempt_prs"], outcomes),
                         "first_pass_success": j["first_pass_success"], "eventual_success": j["state"] == "success",
                         "prs": [pseudonym("pr", p) for p in j["attempt_prs"]],
                         "pr_outcomes": [{"id": pseudonym("pr", p), "merged": bool(outcomes.pull(p).merged_at),
                              "role": d.role(p) if p in d.prs else "followup",
                              "head_sha": outcomes.pull(p).head_sha,
                              "checks_at_merge": outcomes.pull(p).checks, "checks_basis": outcomes.pull(p).checks_basis,
                              "checks_reason": outcomes.pull(p).checks_reason,
                              "observed_checks_at_merge": outcomes.pull(p).observed_checks,
                              "follow_up_commit_count": len(getattr(outcomes, "commit_fixes", lambda pr, days: [])(
                                  outcomes.pull(p), follow_up_days)) if outcomes.pull(p).merged_at else 0,
                              "merged_at": outcomes.pull(p).merged_at.isoformat() if outcomes.pull(p).merged_at else None,
                              "dispatch_to_merge_seconds": ((outcomes.pull(p).merged_at - d.dispatched_at).total_seconds()
                                  if outcomes.pull(p).merged_at else None)} for p in j["attempt_prs"] if outcomes.pull(p)],
                         "elapsed_seconds": (elapsed_end - d.dispatched_at).total_seconds() if elapsed_end else None,
                         "elapsed_measurement": "observed" if elapsed_end else "not_reported",
                         "tokens": by_deliverable[d.id],
                         **({"tokens_complete": tokens_complete[d.id]} if comparison_metadata else {})})
        return {"schema_version": "deliver-1.0", "pseudonyms": {"salted": current_key() is not None,
                   "algorithm": "hmac-sha256" if current_key() is not None else "sha256"},
                "window": {"since": window.since.isoformat(), "until": window.until.isoformat(), "bounds": "[since,until)"},
                "follow_up_days": follow_up_days, "states": {s: sum(j["state"] == s for j in judgments.values()) for s in STATES},
                "checks_basis_counts": {basis: sum(row["checks_basis"] == basis for row in rows) for basis in CHECKS_BASES},
                "success_rate": wilson(len(successes), len(ledger)),
                "first_pass_success_rate": wilson(sum(j["first_pass_success"] for j in judgments.values()), len(ledger)),
                "cost": {"operational": cost(operational_tokens, len(operational_successes)),
                         "operational_linked": cost(period["linked"], len(operational_successes)),
                         "cohort": cost(cohort_tokens, len(cohort_successes)),
                         "cohort_success_rate": wilson(len(cohort_successes), len(cohort)),
                         "period_spend": period, "lifetime_scan_spend": lifetime,
                         "projects": [{"id": pseudonym("repository", r), **cost(project_spend.get(r, empty_tokens()), sum(
                             r in d.repos for d in operational_successes))} for r in sorted(scoped_repos)]},
                "coverage": {"adapters": coverage, "outcome_files_read": getattr(outcomes, "files_read", None),
                             "ledger_rows_read": len(ledger),
                             "observations": [{"repository_id": pseudonym("repository", repo),
                                 "coverage_start": outcomes.observation(repo).start.isoformat(),
                                 "observed_at": outcomes.observation(repo).until.isoformat(),
                                 "pulls_complete": outcomes.observation(repo).pulls_complete,
                                 "commits_complete": outcomes.observation(repo).commits_complete} for repo in sorted(repos) if outcomes.observation(repo)],
                             "missing_prs": sum(outcomes.pull(p) is None for d in ledger for p in d.prs),
                             "missing_repository_observations": sum(o is None for o in observations),
                             "evidence_counts": {e: evidence_counts[e] for e in LINKS},
                             "evidence_tokens": {e: evidence_tokens[e] for e in LINKS},
                             "not_reported_token_events": missing_tokens,
                             "unattributed_period_share": unattributed / total if total else 0.0,
                             "savings_verdict": "not_evaluated", "lifetime_scan_seconds": (end - begin).total_seconds(),
                             "lifetime_scan_end": end.isoformat()},
                "deliverables": rows, "links": sorted(links.values(), key=lambda r: (r["session_id"], r["deliverable_id"] or "", r["evidence"])),
                **({"session_metadata": sorted(session_metadata.values(), key=lambda r: r["id"])} if comparison_metadata else {})}


def deliverable_basis(identities, outcomes):
    pulls = [outcomes.pull(p) for p in identities]
    bases = [p.checks_basis for p in pulls if p and p.merged_at]
    if any(p is None for p in pulls) or not bases:
        return "unknown"
    return max(bases, key=CHECKS_BASES.index)


def text_summary(report):
    lines = ["sumbi deliver", "States: " + "; ".join(f"{s} {n}" for s, n in report["states"].items())]
    lines.append("Checks bases (deliverables): " + "; ".join(f"{b} {n}" for b, n in report["checks_basis_counts"].items()))
    for label in ("success_rate", "first_pass_success_rate"):
        rate = report[label]
        interval = rate["wilson_95"]
        bounds = f"[{interval[0]:.6f}, {interval[1]:.6f}]" if interval else "not reported"
        lines.append(f"{label}: {rate['numerator']}/{rate['denominator']}; Wilson 95% {bounds}")
    for view in ("operational", "operational_linked", "cohort"):
        row = report["cost"][view]
        value = row["tokens_per_success"]["total"]
        lines.append(f"{view}: {row['spend']['total']} reported tokens / {row['successes']} successes; "
                     + (f"{value:g} tokens per success" if value is not None else "cost per success undefined"))
        lines.append("  " + "; ".join(f"{k} " + (f"{row['tokens_per_success'][k]:g}" if row['tokens_per_success'][k] is not None else "not reported") for k in TOKEN_KINDS))
    for bucket, tokens in report["cost"]["period_spend"].items():
        lines.append(f"Period {bucket}: {tokens['total']} reported tokens")
    for bucket, tokens in report["cost"]["lifetime_scan_spend"].items():
        lines.append(f"Lifetime scan {bucket}: {tokens['total']} reported tokens")
    for row in report["deliverables"]:
        elapsed = str(row["elapsed_seconds"]) if row["elapsed_seconds"] is not None else "not reported"
        lines.append(f"Deliverable {row['id']}: {row['state']}; {row['reason']}; task type {row['task_type'] or 'not reported'}; checks basis {row['checks_basis']}; elapsed seconds {elapsed}; tokens {row['tokens']['total']}")
    lines.append("Link evidence events: " + "; ".join(f"{k} {v}" for k, v in report["coverage"]["evidence_counts"].items()))
    for agent, coverage in report["coverage"]["adapters"].items():
        lines.append(f"Coverage {agent}: files {coverage['files_scanned']}; sessions read {coverage['sessions_read']}; "
                     f"broken lines {coverage['broken_lines']}; duplicates {coverage['duplicate_events']}; "
                     f"unreadable files {coverage['unreadable_files']}; invalid tokens {coverage['invalid_token_records']}")
    lines.append(f"Coverage outcomes: ledger rows {report['coverage']['ledger_rows_read']}; "
                 f"files read {report['coverage']['outcome_files_read']}; missing PRs {report['coverage']['missing_prs']}")
    lines.append(f"Unattributed period token share: {report['coverage']['unattributed_period_share']:.6f}")
    lines.append("Pseudonym keys: " + ("salted (HMAC-SHA256)" if report["pseudonyms"]["salted"] else "unsalted (SHA-256)"))
    lines.append("Savings verdict: not evaluated; costs are reported tokens, not currency.")
    return "\n".join(lines)
