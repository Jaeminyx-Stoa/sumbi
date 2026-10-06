"""Discover owner repositories from authorship and fetch only evidenced outcomes."""

from collections import Counter
from datetime import timedelta

from sumbi.core.time import Window
from sumbi.outcomes.github.ledger import Deliverable
from sumbi.outcomes.github.live import RepositoryUnreadable
from sumbi.outcomes.worker_github.links import authorship


class ScopedOutcomes:
    """Route repository-local judgment to lazily captured outcome adapters."""

    def __init__(self, provider, window, repos):
        self.provider, self.window = provider, window
        self.sources = {}
        self.unreadable = set()
        self.ensure(repos)

    def ensure(self, repos, *, discovered=False):
        missing = sorted(set(repos) - self.sources.keys() - self.unreadable)
        if not missing:
            return
        for repo in missing:
            try:
                source = self.provider([Deliverable("repository", self.window.since,
                    (repo,), (), ())]) if callable(self.provider) else self.provider
            except RepositoryUnreadable:
                if not discovered:
                    raise
                self.unreadable.add(repo)
                continue
            self.sources[repo] = source

    def pull(self, identity):
        if identity.rsplit("#", 1)[0] in self.unreadable:
            return None
        return self.sources[identity.rsplit("#", 1)[0]].pull(identity)

    def observation(self, repo):
        if repo in self.unreadable:
            return None
        return self.sources[repo].observation(repo)

    def branch_pulls(self, repo, branch):
        if repo in self.unreadable:
            return ()
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
        outcomes.ensure(repos, discovered=True)
        observations = [outcomes.observation(r) for r in repos]
        extended = Window(window.since, max([scan.until, *[o.until for o in observations if o]]))
        found, adapters = collect_sessions(extended)
        discovered = owned_repositories(found, extended, owners, fixed) - measured
        if not discovered:
            return repos, outcomes, observations, extended, found, adapters
        measured.update(discovered)
        scan = extended
