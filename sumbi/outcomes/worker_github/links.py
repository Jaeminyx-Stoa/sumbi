"""Own-session references and repository scope; private operands stay local."""

import re

from sumbi.core.paths import execution_cwd, normalize_origin
from sumbi.core.privacy import pseudonym
from sumbi.outcomes.github.ledger import REPO

EVIDENCE = {"pr_url": 4, "pushed_branch": 3, "created_branch": 2, "cwd_branch": 1}


def repositories(values):
    if not values or any(not isinstance(v, str) or not re.fullmatch(REPO, v) for v in values):
        raise ValueError("Worker GitHub requires explicit owner/name repositories")
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
            refs.update(own)
        elif kind == "refs":
            refs.update(data)
    return refs


def start_repo(session, attributor, repos):
    cwd = execution_cwd(session.start_cwd)
    origin = attributor.origin(cwd)[0] if cwd else None
    return next((r for r in repos if origin and origin.lower()
        == normalize_origin("https://github.com/" + r).lower()), None)


def link_prs(refs, repos, origin_repo, outcomes, dispatched_at):
    """Keep every constituent and choose the strongest evidence per PR."""
    links = {}
    for kind, value in sorted(refs):
        if kind == "pr" and value.rsplit("#", 1)[0] in repos:
            links[value] = ("pr_url", None)
    branch_repos = (origin_repo,) if origin_repo else repos
    for kind, name in sorted(refs):
        evidence = ("pushed_branch" if kind in ("branch_pushed", "branch_push_intent")
            else "created_branch" if kind == "branch_created"
            else "cwd_branch" if kind in ("branch", "branch_observed", "branch_change")
            else None)
        if evidence is None:
            continue
        for repo in branch_repos:
            for identity in outcomes.branch_pulls(repo, name):
                pr = outcomes.pull(identity)
                # Reused branch names cannot import a PR that predates dispatch.
                if pr.created_at < dispatched_at:
                    continue
                if identity not in links or EVIDENCE[evidence] > EVIDENCE[links[identity][0]]:
                    links[identity] = evidence, name
    return links


def public_links(session_id, links):
    return [{"session_id": session_id, "pr_id": pseudonym("pr", identity),
        "repository_id": pseudonym("repository", identity.rsplit("#", 1)[0]),
        "branch_id": pseudonym("branch", name) if name else None,
        "evidence": evidence, "strength": "explicit" if evidence == "pr_url"
        else "weak" if evidence == "cwd_branch" else "strong"}
        for identity, (evidence, name) in sorted(links.items())]
