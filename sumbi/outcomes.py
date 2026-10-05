"""Offline outcome interface over recorded GitHub REST response fixtures."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Protocol

from sumbi.ledger import REPO, utc

SHA = r"[0-9a-fA-F]{40}"
FIX = re.compile(r"\b(?:fix(?:es|ed|ing)?|revert(?:s|ed|ing)?|regression|follow[ -]?up|hotfix)\b", re.I)
REVERT = re.compile(r"\brevert(?:s|ed|ing)?\b", re.I)


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
    def disturbances(self, pull: PullRequest, days: float) -> tuple[list[PullRequest], list[datetime]]: ...


class FixtureOutcomes:
    def __init__(self, directory: Path):
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

    def _load(self, raw):
        if not isinstance(raw, dict) or set(raw) != {"repository", "coverage_start", "observed_at",
                "pulls_complete", "commits_complete", "pulls", "commits"}:
            raise ValueError("Outcomes: expected documented repository fixture fields")
        repo = raw["repository"]
        if not isinstance(repo, str) or not re.fullmatch(REPO, repo) or repo.lower() in self.observations:
            raise ValueError("Outcomes: invalid or duplicate repository ID")
        repo = repo.lower()
        start, end = utc(raw["coverage_start"], "Outcomes coverage_start"), utc(raw["observed_at"], "Outcomes observed_at")
        if start >= end or any(type(raw[k]) is not bool for k in ("pulls_complete", "commits_complete")):
            raise ValueError("Outcomes: invalid observation coverage")
        self.observations[repo] = Observation(start, end, raw["pulls_complete"], raw["commits_complete"])
        if not isinstance(raw["pulls"], list) or not isinstance(raw["commits"], list):
            raise ValueError("Outcomes: pulls and commits must be arrays")
        for entry in raw["pulls"]:
            if not isinstance(entry, dict) or set(entry) != {"response", "checks_at_merge"}:
                raise ValueError("Outcomes: each pull needs response and checks_at_merge")
            pr = entry["response"]
            number = pr["number"]
            if type(number) is not int or number <= 0 or pr["state"] not in ("open", "closed") or type(pr["merged"]) is not bool:
                raise ValueError("Outcomes: invalid PR number, state or merged flag")
            identity = repo + "#" + str(number)
            if identity in self.pulls:
                raise ValueError("Outcomes: duplicate PR")
            created = utc(pr["created_at"], "Outcomes PR created_at")
            closed = utc(pr["closed_at"], "Outcomes PR closed_at") if pr["closed_at"] is not None else None
            merged = utc(pr["merged_at"], "Outcomes PR merged_at") if pr["merged_at"] is not None else None
            head, merge = pr["head"]["sha"], pr["merge_commit_sha"]
            if (not isinstance(head, str) or not re.fullmatch(SHA, head)
                    or (merge is not None and (not isinstance(merge, str) or not re.fullmatch(SHA, merge)))
                    or bool(merged) != pr["merged"] or (merged and (not merge or pr["state"] != "closed"))
                    or (pr["state"] == "closed") != bool(closed)
                    or created >= end or any(t and (t < created or t >= end) for t in (closed, merged))
                    or (merged and closed < merged)):
                raise ValueError("Outcomes: inconsistent PR timestamps or SHA evidence")
            if not isinstance(pr["title"], str) or (pr["body"] is not None and not isinstance(pr["body"], str)):
                raise ValueError("Outcomes: PR title/body must be strings or null body")
            checks = self._checks(entry["checks_at_merge"], head, merged)
            self.pulls[identity] = PullRequest(identity, pr["state"], created, closed, merged,
                                              head.lower(), merge.lower() if merge else None,
                                              checks, pr["title"], pr["body"] or "")
        commits = []
        for commit in raw["commits"]:
            if not re.fullmatch(SHA, commit["sha"]) or not isinstance(commit["commit"]["message"], str):
                raise ValueError("Outcomes: invalid commit evidence")
            when = utc(commit["commit"]["committer"]["date"], "Outcomes commit date")
            if not start <= when < end:
                raise ValueError("Outcomes: commit outside declared coverage")
            commits.append((when, commit["commit"]["message"]))
        self.commits[repo] = commits

    @staticmethod
    def _checks(snapshot, head, merged):
        if snapshot is None:
            return "unknown"
        if not merged or not isinstance(snapshot, dict) or set(snapshot) != {"head_sha", "captured_at", "required", "check_runs", "statuses"}:
            raise ValueError("Outcomes: invalid checks_at_merge snapshot")
        if snapshot["head_sha"] != head or utc(snapshot["captured_at"], "Outcomes checks captured_at") != merged:
            raise ValueError("Outcomes: checks snapshot must identify merge time and PR head")
        required = snapshot["required"]
        if not isinstance(required, list) or any(not isinstance(n, str) or not n for n in required) or len(set(required)) != len(required):
            raise ValueError("Outcomes: required checks must be an explicit unique name list")
        if not isinstance(snapshot["check_runs"], list) or not isinstance(snapshot["statuses"], list):
            raise ValueError("Outcomes: check_runs and statuses must be arrays")
        latest = {}
        for check in snapshot["check_runs"]:
            name = check["name"]
            if (not isinstance(name, str) or not name or check["status"] not in ("queued", "in_progress", "completed")
                    or (check["status"] == "completed") != bool(check["completed_at"])
                    or check["conclusion"] not in (None, "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale", "startup_failure")):
                raise ValueError("Outcomes: invalid check-run state")
            start = utc(check["started_at"], "Outcomes check started_at")
            end = utc(check["completed_at"], "Outcomes check completed_at") if check["completed_at"] else None
            if check["head_sha"] != head or start > merged or (end and (end < start or end > merged)):
                raise ValueError("Outcomes: check evidence is not at merge on the PR head")
            status = "green" if check["status"] == "completed" and end and check["conclusion"] in ("success", "neutral", "skipped") else "red"
            if name in latest and latest[name][0] == start and latest[name][1] != status:
                raise ValueError("Outcomes: conflicting check evidence")
            if name not in latest or start >= latest[name][0]:
                latest[name] = start, status
        latest_statuses = {}
        for status in snapshot["statuses"]:
            when = utc(status["updated_at"], "Outcomes status updated_at")
            if when > merged or status["sha"] != head:
                raise ValueError("Outcomes: status evidence is not at merge on the PR head")
            name = status["context"]
            if not isinstance(name, str) or not name or status["state"] not in ("success", "pending", "failure", "error"):
                raise ValueError("Outcomes: invalid commit status")
            value = "green" if status["state"] == "success" else "red"
            if name in latest_statuses and latest_statuses[name][0] == when and latest_statuses[name][1] != value:
                raise ValueError("Outcomes: conflicting commit statuses")
            if name not in latest_statuses or when >= latest_statuses[name][0]:
                latest_statuses[name] = when, value
        for name, (when, value) in latest_statuses.items():
            if name in latest:
                value = "green" if value == latest[name][1] == "green" else "red"
            latest[name] = when, value
        if any(n not in latest for n in required):
            return "unknown"
        return "green" if all(latest[n][1] == "green" for n in required) else "red"

    def pull(self, identity):
        return self.pulls.get(identity)

    def observation(self, repo):
        return self.observations.get(repo)

    def disturbances(self, pull, days):
        repo, number = pull.id.rsplit("#", 1)
        end = pull.merged_at + timedelta(days=days)
        refs = re.compile(r"(?<![A-Za-z0-9_./-])(?:#" + number + "|"
                          + re.escape(repo) + "#" + number + "|https://github\\.com/"
                          + re.escape(repo) + "/pull/" + number + "|(?:PR|pull request)\\s+" + number
                          + "|" + re.escape(pull.merge_sha) + r")(?![A-Za-z0-9])", re.I)
        follows = [p for p in self.pulls.values() if p.id.rsplit("#", 1)[0] == repo
                   and pull.merged_at < p.created_at < end
                   and FIX.search(p.title + "\n" + p.body) and refs.search(p.title + "\n" + p.body)]
        reverts = [t for t, message in self.commits.get(repo, []) if pull.merged_at < t < end
                   and REVERT.search(message) and re.search(r"(?<![0-9a-f])" + pull.merge_sha + r"(?![0-9a-f])", message, re.I)]
        return sorted(follows, key=lambda p: (p.created_at, p.id)), sorted(reverts)
