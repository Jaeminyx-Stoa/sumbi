"""Discover owner repositories from authorship and fetch only evidenced outcomes."""

from collections import Counter
from datetime import timedelta

from sumbi.core.time import Window
from sumbi.outcomes.github.ledger import Deliverable
from sumbi.outcomes.worker_github.links import authorship


class ScopedOutcomes:
    """Route repository-local judgment to lazily captured outcome adapters."""

    def __init__(self, provider, window, repos):
        self.provider, self.window = provider, window
        self.sources = {}
        self.ensure(repos)

    def ensure(self, repos):
        missing = sorted(set(repos) - self.sources.keys())
        if not missing:
            return
        source = self.provider([Deliverable("repository", self.window.since, (r,), (), ())
            for r in missing]) if callable(self.provider) else self.provider
        self.sources.update((r, source) for r in missing)

    def pull(self, identity):
        return self.sources[identity.rsplit("#", 1)[0]].pull(identity)

    def observation(self, repo):
        return self.sources[repo].observation(repo)

    def branch_pulls(self, repo, branch):
        return self.sources[repo].branch_pulls(repo, branch)

    def disturbances(self, pull, days):
        return self.sources[pull.id.rsplit("#", 1)[0]].disturbances(pull, days)

    def commit_fixes(self, pull, days):
        return self.sources[pull.id.rsplit("#", 1)[0]].commit_fixes(pull, days)


def owned_repositories(found, scan, owners, fixed):
    repos = set()
    for session in found:
        if session.id() not in fixed:
            continue
        lifetime = Window(session.start_at, scan.until)
        for kind, value, _, _ in authorship(session, lifetime, Counter()):
            repo = value[0] if kind == "push_result" else (
                value.rsplit("#", 1)[0] if kind == "pr_created" else None)
            if repo and repo.split("/", 1)[0] in owners:
                repos.add(repo)
    return repos


def discovery_window(initial, window, fixed):
    """Late first authorship must be discoverable before any owner outcome exists."""
    latest = max((t for s in initial if s.id() in fixed for t in s.times), default=window.until)
    try:
        latest += timedelta(microseconds=1)
    except OverflowError:
        pass
    return Window(window.since, max(window.until, latest))


def capture(initial, window, repos, owners, outcomes, fixed, collect_sessions):
    """Freeze workers first; extend their lifetime scan as repositories are observed."""
    outcomes = ScopedOutcomes(outcomes, window, repos)
    measured = set(repos)
    if owners:
        measured.update(owned_repositories(initial, discovery_window(initial, window, fixed),
            owners, fixed))
    scan, found = window, initial
    while True:
        measured.update(owned_repositories(found, scan, owners, fixed))
        repos = tuple(sorted(measured))
        outcomes.ensure(repos)
        observations = [outcomes.observation(r) for r in repos]
        extended = Window(window.since, max([scan.until, *[o.until for o in observations if o]]))
        found, adapters = collect_sessions(extended)
        discovered = owned_repositories(found, extended, owners, fixed) - measured
        if not discovered:
            return repos, outcomes, observations, extended, found, adapters
        measured.update(discovered)
        scan = extended
