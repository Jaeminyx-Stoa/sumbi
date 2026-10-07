"""Outcome interface and strict recorded GitHub REST response fixtures."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Protocol

from sumbi.outcomes.github.ledger import REPO, utc
from sumbi.events.references import branch
from sumbi.outcomes.github.evidence import at_merge, verdict
from sumbi.outcomes.github.policy import policy_checks

SHA = r"[0-9a-fA-F]{40}"
FIX = re.compile(
    r"\b(?:fix(?:es|ed|ing)?|revert(?:s|ed|ing)?|regression|follow[ -]?up|hotfix)\b", re.I)
REVERT = re.compile(r"\brevert(?:s|ed|ing)?\b", re.I)


def references(pull):
    repo, number = pull.id.rsplit("#", 1)
    return re.compile(r"(?<![A-Za-z0-9_./-])(?:#" + number + "|"
        + re.escape(repo) + "#" + number + "|https://github\\.com/"
        + re.escape(repo) + "/pull/" + number + "|(?:PR|pull request)\\s+" + number
        + "|" + re.escape(pull.merge_sha) + r")(?![A-Za-z0-9_])", re.I)


@dataclass(frozen=True)
class PullRequest:
    id: str
    state: str
    created_at: datetime
    closed_at: datetime | None
    merged_at: datetime | None
    head_sha: str
    merge_sha: str | None
    checks: str
    title: str
    body: str
    observed_checks: str = "unknown"
    checks_basis: str = "unknown"
    checks_reason: str = "checks_policy_unreadable"
    head_ref: str | None = None
    checks_capture_complete: bool = True


@dataclass(frozen=True)
class Observation:
    start: datetime
    until: datetime
    pulls_complete: bool
    commits_complete: bool


class Outcomes(Protocol):
    """Adapters supply outside evidence; judgment does not access the network."""

    def pull(self, identity: str) -> PullRequest | None: ...
    def observation(self, repo: str) -> Observation | None: ...
    def branch_pulls(self, repo: str, head_ref: str) -> list[str]: ...
    def disturbances(self, pull: PullRequest, days: float) -> tuple[list[PullRequest],
        list[datetime]]: ...


class FixtureOutcomes:
    def __init__(self, directory: Path):
        self.evidence_gaps, self._gap_keys = Counter(), set()
        self._evidence_identity = None
        self.pulls = {}
        self.observations = {}
        self.commits = {}
        self.files_read = 0
        try:
            files = sorted(directory.glob("*.json"))
            if not files:
                raise ValueError("Outcomes: expected repository JSON fixtures")
            for path in files:
                with path.open(encoding="utf-8") as stream:
                    raw = json.load(stream)
                self._load(raw)
                self.files_read += 1
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError, AttributeError):
            raise ValueError("Outcomes: malformed or unreadable repository fixture") from None

    def _load(self, raw, *, defer_policy=False):
        if not isinstance(raw, dict) or set(raw) != {"repository", "coverage_start", "observed_at",
            "pulls_complete", "commits_complete", "pulls", "commits"}:
            raise ValueError("Outcomes: expected documented repository fixture fields")
        repo = raw["repository"]
        if not isinstance(repo, str) or not re.fullmatch(REPO,
            repo) or repo.lower() in self.observations:
            raise ValueError("Outcomes: invalid or duplicate repository ID")
        repo = repo.lower()
        start, end = utc(raw["coverage_start"],
            "Outcomes coverage_start"), utc(raw["observed_at"], "Outcomes observed_at")
        if start >= end or any(type(raw[k]) is not bool
            for k in ("pulls_complete", "commits_complete")):
            raise ValueError("Outcomes: invalid observation coverage")
        self.observations[repo] = Observation(start, end, raw["pulls_complete"],
            raw["commits_complete"])
        if not isinstance(raw["pulls"], list) or not isinstance(raw["commits"], list):
            raise ValueError("Outcomes: pulls and commits must be arrays")
        seen = set()
        for entry in raw["pulls"]:
            self._load_pull(entry, repo, end, seen, defer_policy)
        self._load_commits(repo, raw["commits"], start, end)

    def _load_pull(self, entry, repo, end, seen, defer_policy):
        if (not isinstance(entry, dict) or not {"response", "checks_at_merge"} <= set(entry)
            or set(entry) - {"response", "checks_at_merge", "observed_checks_at_merge",
                "current_policy_evidence"}):
            raise ValueError("Outcomes: each pull needs response and checks_at_merge")
        pr = entry["response"]
        number = pr["number"]
        if type(number) is not int or number <= 0 or pr["state"] not in ("open",
            "closed") or type(pr["merged"]) is not bool:
            raise ValueError("Outcomes: invalid PR number, state or merged flag")
        identity = repo + "#" + str(number)
        if identity in seen:
            raise ValueError("Outcomes: duplicate PR")
        seen.add(identity)
        created = utc(pr["created_at"], "Outcomes PR created_at")
        closed = utc(pr["closed_at"],
            "Outcomes PR closed_at") if pr["closed_at"] is not None else None
        merged = utc(pr["merged_at"],
            "Outcomes PR merged_at") if pr["merged_at"] is not None else None
        head, merge = pr["head"]["sha"], pr["merge_commit_sha"]
        head_ref = pr["head"].get("ref")
        if head_ref is not None and not isinstance(head_ref, str):
            raise ValueError("Outcomes: invalid PR head ref")
        if head_ref is not None and branch(head_ref) is None:
            self._evidence_identity = identity
            self._gap("head_ref_unsupported", head_ref)
            head_ref = None
        if (not isinstance(head, str) or not re.fullmatch(SHA, head)
            or merge is not None and (not isinstance(merge, str) or not re.fullmatch(SHA, merge))):
            raise ValueError("Outcomes: invalid PR SHA")
        temporal_gap = (bool(merged) != pr["merged"]
            or merged and (not merge or pr["state"] != "closed")
            or (pr["state"] == "closed") != bool(closed)
            or created >= end or any(t and (t < created or t >= end) for t in (closed, merged))
            or merged and (not closed or closed < merged))
        if not isinstance(pr["title"], str) or (pr["body"] is not None
            and not isinstance(pr["body"], str)):
            raise ValueError("Outcomes: PR title/body must be strings or null body")
        self._evidence_identity = identity
        checks = self._checks(entry["checks_at_merge"], head, merged, self._gap)
        basis = "historical" if entry["checks_at_merge"] is not None else "unknown"
        reason = ("" if checks == "green" else "checks_red" if checks == "red"
            else "checks_missing_required")
        if basis == "unknown":
            checks, basis, reason = self._policy_checks(
                entry.get("current_policy_evidence"), merged, (head, merge),
                (lambda reason, item: None) if defer_policy else self._gap)
        elif "current_policy_evidence" in entry:
            self._policy_checks(entry["current_policy_evidence"], merged, (head, merge), self._gap)
        observed = entry.get("observed_checks_at_merge", "unknown")
        if observed not in ("unknown", "green", "red"):
            raise ValueError("Outcomes: invalid observed check label")
        if temporal_gap:
            self._gap("pr_evidence_inconsistent", pr)
            o = self.observations[repo]
            self.observations[repo] = Observation(o.start, o.until, False, o.commits_complete)
            return
        self.pulls[identity] = PullRequest(identity, pr["state"], created, closed, merged,
            head.lower(), merge.lower() if merge else None,
            checks, pr["title"], pr["body"] or "", observed,
            basis, reason, head_ref, entry["checks_at_merge"] is not None
            or (entry.get("current_policy_evidence") or {}).get("results") is not None)

    def branch_pulls(self, repo, head_ref):
        """Enumerate exact heads without exposing branch text in public reports."""
        return sorted(p.id for p in self.pulls.values()
            if p.id.rsplit("#", 1)[0] == repo and p.head_ref == head_ref)

    def _load_commits(self, repo, rows, start, end):
        commits, seen = [], set()
        self._evidence_identity = repo
        for commit in rows:
            if not re.fullmatch(SHA,
                commit["sha"]) or not isinstance(commit["commit"]["message"], str):
                raise ValueError("Outcomes: invalid commit evidence")
            if commit["sha"].lower() in seen:
                raise ValueError("Outcomes: duplicate commit ID")
            seen.add(commit["sha"].lower())
            when = utc(commit["commit"]["committer"]["date"], "Outcomes commit date")
            if not start <= when < end:
                self._gap("commit_outside_coverage", commit)
                continue
            commits.append((when, commit["commit"]["message"]))
        self.commits[repo] = commits

    def _gap(self, reason, item):
        key = self._evidence_identity, reason, json.dumps(item, sort_keys=True)
        if key not in self._gap_keys:
            self._gap_keys.add(key)
            self.evidence_gaps[reason] += 1

    @staticmethod
    def _checks(snapshot, head, merged, gap=lambda reason, item: None):
        if snapshot is None:
            return "unknown"
        clean, valid = at_merge(snapshot, head, merged, gap)
        if snapshot["required"] is None:
            gap("policy_incomplete", snapshot)
        return verdict(clean) if valid else "unknown"

    @staticmethod
    def _policy_checks(evidence, merged, shas, gap=lambda reason, item: None):
        return policy_checks(evidence, merged, shas, gap)

    def pull(self, identity):
        return self.pulls.get(identity)

    def observation(self, repo):
        return self.observations.get(repo)

    def disturbances(self, pull, days):
        repo = pull.id.rsplit("#", 1)[0]
        end = pull.merged_at + timedelta(days=days)
        refs = references(pull)
        follows = [p for p in self.pulls.values() if p.id.rsplit("#", 1)[0] == repo
            and pull.merged_at < p.created_at < end
            and FIX.search(p.title + "\n" + p.body) and refs.search(p.title + "\n" + p.body)]
        reverts = [t for t, message in self.commits.get(repo, []) if pull.merged_at < t < end
            and REVERT.search(message) and refs.search(message)]
        return sorted(follows, key=lambda p: (p.created_at, p.id)), sorted(reverts)

    def commit_fixes(self, pull, days):
        """Optional adapter extension: bounded follow-up signals without a PR."""
        repo = pull.id.rsplit("#", 1)[0]
        end = pull.merged_at + timedelta(days=days)
        refs = references(pull)
        return sorted(t for t, message in self.commits.get(repo, []) if pull.merged_at < t < end
            and FIX.search(message) and not REVERT.search(message)
            and refs.search(message))
