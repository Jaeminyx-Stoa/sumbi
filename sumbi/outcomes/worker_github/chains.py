"""Bounded same-agent dispatch ancestry and conservative non-shipping evidence."""

from collections import Counter

from sumbi.core.time import Window
from sumbi.events.authorship import actions
from sumbi.outcomes.worker_github.links import (EVIDENCE, Link, authorship, command_gap, link_prs,
    references, start_repo_cwd)

MAX_ANCESTORS = 64


def ancestor_chain(session, index, gaps):
    """Return available ancestors and whether the root was observed."""
    chain, seen = [], {(session.agent, session.raw_id)}
    current = session
    while current.parent_raw_id:
        key = current.agent, current.parent_raw_id
        reason = ("ancestor_cycle" if key in seen else "ancestor_depth_exceeded"
            if len(chain) >= MAX_ANCESTORS else "ancestor_log_missing" if key not in index
            else None)
        if reason:
            gaps[reason] += 1
            return chain, False
        current = index[key]
        chain.append(current)
        seen.add(key)
    if current.dispatch_kind in ("subagent", "noninteractive_exec"):
        gaps["ancestor_log_missing"] += 1
        return chain, False
    return chain, True


def chain_authorship(session, lifetime, gaps, *, gap_memo=None, run_gaps=None):
    """Ancestor results may finish after dispatch even if their command began earlier."""
    scan = Window(min(session.start_at or lifetime.since, lifetime.since), lifetime.until)
    return tuple(authorship(session, scan, gaps, completed_window=lifetime,
        gap_memo=gap_memo, run_gaps=run_gaps))


def touched_repositories(session, repos, attributor, *, evidence_repos=()):
    """Use the worker's established scope; command intent cannot widen it."""
    origin = start_repo_cwd(session.start_cwd, attributor, repos)
    return ({origin} if origin else set()) | (set(evidence_repos) & set(repos))


def inherited_links(session, lifetime, ancestors, repos, outcomes, attributor, gaps,
    events, gap_memo):
    """Keep ancestor evidence below own authorship; branch identity controls strength."""
    links = {}
    branches = {v for k, v in references(session, lifetime) if k in ("branch", "branch_observed")}
    for ancestor, authored in zip(ancestors, events):
        eligible = [e for e in authored if e[0] in ("push_result", "pr_created")]
        candidates = link_prs(ancestor, lifetime, set(), repos, None, outcomes, attributor,
            gaps, authored_events=eligible, dispatched_at=session.start_at, gap_memo=gap_memo)
        for identity, candidate in candidates.items():
            pr = outcomes.pull(identity)
            branch = candidate.branch or (pr.head_ref if pr else None)
            evidence = ("ancestor_pushed_branch" if candidate.evidence.startswith("pushed_branch")
                else "ancestor_pr_created")
            link = Link(evidence, branch, candidate.role,
                "strong" if branch and branch in branches else "weak")
            previous = links.get(identity)
            if previous is None or (link.strength == "strong", EVIDENCE[link.evidence]) > (
                previous.strength == "strong", EVIDENCE[previous.evidence]):
                links[identity] = link
    return links


def resolve_chain(session, lifetime, index, repos, outcomes, attributor, gaps, *,
    evidence_repos=(), gap_memo=None):
    """A complete chain without authorship is observed non-shipping, not linking risk."""
    ancestors, complete = ancestor_chain(session, index, gaps)
    local_gaps = Counter()
    gap_memo = set() if gap_memo is None else gap_memo
    events = [chain_authorship(s, lifetime, local_gaps, gap_memo=gap_memo,
        run_gaps=gaps) for s in ancestors]
    complete &= not any(s.local_evidence_gaps for s in (session, *ancestors))
    complete &= not any(local_gaps[k] for k in ("authorship_exit_unknown", "link_conflicts"))
    if not any(events):
        for member in (session, *ancestors):
            for identity, execution in member.commands.items():
                if (lifetime.contains(execution.at) and actions(execution.command)
                    and (execution.exit_code == 0
                        or execution.exit_code is None and execution.error is False)):
                    command_gap(local_gaps, "authorship_result_missing", member, identity,
                        gap_memo, gaps)
                    complete = False
    unshipped = complete and not any(events)
    touched = touched_repositories(session, repos, attributor, evidence_repos=evidence_repos)
    links = inherited_links(session, lifetime, ancestors, tuple(sorted(touched)), outcomes,
        attributor, gaps, events, gap_memo)
    return links, unshipped
