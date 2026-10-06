"""Own-session references and repository scope; private operands stay local."""

import re

from sumbi.core.paths import execution_cwd, normalize_origin
from sumbi.core.privacy import pseudonym
from sumbi.events.authorship import actions, command_scope
from sumbi.outcomes.github.ledger import REPO

EVIDENCE = {"pr_created": 4, "pr_created_output": 4, "pushed_branch": 3,
    "pushed_branch_output": 3, "committed_branch": 2, "cwd_branch": 1}


def repositories(values, *, allow_empty=False):
    values = values or ()
    if not values and not allow_empty or any(
        not isinstance(v, str) or not re.fullmatch(REPO, v) for v in values):
        raise ValueError("Worker GitHub requires explicit owner/name repositories")
    return tuple(sorted({v.lower() for v in values}))


def repository_owners(values):
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", v)
        for v in values):
        raise ValueError("Worker GitHub requires valid repository owners")
    return tuple(sorted({v.lower() for v in values}))


def references(session, scan):
    """Only this worker's own bounded tool and context observations qualify."""
    refs = set()
    for at, _, kind, data in session.deliverable_events:
        if not scan.contains(at):
            continue
        if kind == "context":
            name, _ = data
            if name:
                refs.add(("branch", name))
        elif kind == "own":
            name, own = data
            if name:
                refs.add(("branch", name))
            refs.update((k, v) for k, v in own if k != "branch")
        elif kind == "refs":
            refs.update((k, v) for k, v in data if k != "branch")
    return refs


def start_repo(session, attributor, repos):
    return start_repo_cwd(session.start_cwd, attributor, repos)


def authorship(session, scan, gaps):
    results = {}
    for at, _, kind, data in session.deliverable_events:
        if kind == "execution_refs" and scan.contains(at):
            identity, refs = data
            results.setdefault(identity, []).append((at, refs))
    for identity, execution in session.commands.items():
        action = actions(execution.command)
        if not scan.contains(execution.at):
            continue
        output = {ref for at, refs in results.get(identity, [])
            if execution.started_at and execution.started_at <= at <= execution.at for ref in refs}
        if not action and not any(k == "push_result" for k, _ in output):
            continue
        output_only = execution.exit_code is None and execution.error is False
        if execution.error is True or execution.exit_code != 0 and not output_only:
            gaps["failed_authorship_commands" if execution.exit_code is not None
                else "authorship_exit_unknown"] += 1
            continue
        if (not scan.contains(execution.started_at)
            or execution.at < execution.started_at):
            gaps["link_conflicts"] += 1
            continue
        if (len({v for k, v in output if k == "pr_created"}) > 1
            or len({v for k, v in output if k == "branch_committed"}) > 1):
            gaps["link_conflicts"] += 1
            continue
        for kind, value in output:
            if output_only and kind not in ("push_result", "pr_created"):
                continue
            if (kind == "pr_created" and ("create", None) in action
                or kind == "push_result"
                or kind == "branch_committed" and ("commit", None) in action):
                yield kind, value, execution, output


def link_prs(session, scan, refs, repos, origin_repo, outcomes, attributor, gaps,
    evidence_repos=None):
    """Only completed authorship links; mentions and conflicts remain coverage."""
    links = {}
    for kind, value in refs:
        if kind == "pr" and value.rsplit("#", 1)[0] in repos:
            pr = outcomes.pull(value)
            if pr and pr.created_at < session.start_at:
                gaps["pre_dispatch_pr_mentions"] += 1
    for kind, value, execution, output in authorship(session, scan, gaps):
        if kind == "push_result":
            repository, value = value
            scope = (repository,)
        elif kind == "pr_created":
            scope = (value.rsplit("#", 1)[0],)
        else:
            cwd, command_repos, valid = command_scope(execution.command, execution.cwd)
            if not valid:
                gaps["link_conflicts"] += 1
                continue
            cwd_repo = start_repo_cwd(cwd, attributor, repos)
            if command_repos and cwd_repo and cwd_repo not in command_repos:
                gaps["link_conflicts"] += 1
                continue
            scope = tuple(command_repos) if command_repos else (cwd_repo,) if cwd_repo else ()
        if evidence_repos is not None and kind in ("push_result", "pr_created"):
            evidence_repos.update(r for r in scope if r in repos)
        evidence = {"pr_created": "pr_created", "push_result": "pushed_branch",
            "branch_committed": "committed_branch"}[kind]
        if execution.exit_code is None:
            evidence += "_output"
        identities = ([value] if kind == "pr_created" else
            [p for r in scope if r in repos for p in outcomes.branch_pulls(r, value)])
        if kind != "pr_created" and len({p.rsplit("#", 1)[0] for p in identities}) > 1:
            gaps["link_conflicts"] += 1
            continue
        for identity in identities:
            if identity.rsplit("#", 1)[0] not in repos:
                continue
            if identity.rsplit("#", 1)[0] in getattr(outcomes, "unreadable", ()):
                continue
            pr = outcomes.pull(identity)
            if kind == "pr_created" and scope and identity.rsplit("#", 1)[0] not in scope:
                gaps["link_conflicts"] += 1
                continue
            continued = bool(pr and pr.created_at < session.start_at)
            if ((continued and kind == "pr_created")
                or (pr and pr.closed_at and pr.closed_at < execution.at)
                or (pr and kind == "pr_created" and pr.created_at > execution.at)):
                gaps["link_conflicts"] += 1
                continue
            candidate = evidence, None if kind == "pr_created" else value, (
                "continued" if continued else "constituent")
            if identity not in links or EVIDENCE[evidence] > EVIDENCE[links[identity][0]]:
                links[identity] = candidate
    weak_links(refs, repos, origin_repo, outcomes, session.start_at, links)
    return links


def start_repo_cwd(cwd, attributor, repos):
    cwd = execution_cwd(cwd)
    origin = attributor.origin(cwd)[0] if cwd else None
    return next((r for r in repos if origin and origin.lower()
        == normalize_origin("https://github.com/" + r).lower()), None)


def weak_links(refs, repos, origin_repo, outcomes, dispatched_at, links):
    for kind, name in refs:
        if kind not in ("branch", "branch_observed"):
            continue
        for repo in (origin_repo,) if origin_repo else repos:
            for identity in outcomes.branch_pulls(repo, name):
                pr = outcomes.pull(identity)
                if pr and pr.created_at >= dispatched_at:
                    links.setdefault(identity, ("cwd_branch", name, "constituent"))


def public_links(session_id, links):
    return [{"session_id": session_id, "pr_id": pseudonym("pr", identity),
        "repository_id": pseudonym("repository", identity.rsplit("#", 1)[0]),
        "branch_id": pseudonym("branch", name) if name else None,
        "evidence": evidence, "strength": "weak" if evidence == "cwd_branch" else "strong",
        "role": role}
        for identity, (evidence, name, role) in sorted(links.items())]
