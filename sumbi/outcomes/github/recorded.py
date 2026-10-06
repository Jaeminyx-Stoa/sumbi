"""Outcome interface and strict recorded GitHub REST response fixtures."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Protocol

from sumbi.outcomes.github.ledger import REPO, utc

SHA = r"[0-9a-fA-F]{40}"
FIX = re.compile(r"\b(?:fix(?:es|ed|ing)?|revert(?:s|ed|ing)?|regression|follow[ -]?up|hotfix)\b", re.I)
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
            if (not isinstance(entry, dict) or not {"response", "checks_at_merge"} <= set(entry)
                    or set(entry) - {"response", "checks_at_merge", "observed_checks_at_merge", "current_policy_evidence"}):
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
            basis = "historical" if entry["checks_at_merge"] is not None else "unknown"
            reason = "" if checks == "green" else "checks_red" if checks == "red" else "checks_missing_required"
            if basis == "unknown":
                checks, basis, reason = self._policy_checks(entry.get("current_policy_evidence"), merged, (head, merge))
            observed = entry.get("observed_checks_at_merge", "unknown")
            if observed not in ("unknown", "green", "red"):
                raise ValueError("Outcomes: invalid observed check label")
            self.pulls[identity] = PullRequest(identity, pr["state"], created, closed, merged,
                                              head.lower(), merge.lower() if merge else None,
                                              checks, pr["title"], pr["body"] or "", observed, basis, reason)
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

    @classmethod
    def _policy_checks(cls, evidence, merged, shas):
        """Judge current requirements separately from authoritative snapshots.

        Names match exactly. Latest attempts are independent per name and app;
        a status sharing an unpinned name must also pass.
        """
        if evidence is None:
            return "unknown", "unknown", "checks_policy_unreadable" if merged else "not_merged"
        if not merged or not isinstance(evidence, dict) or set(evidence) != {"required", "results"}:
            raise ValueError("Outcomes: invalid policy check evidence")
        required, snapshot = evidence["required"], evidence["results"]
        if required is not None and (not isinstance(required, list) or any(
                not isinstance(r, dict) or set(r) != {"context", "app_id"}
                or not isinstance(r["context"], str) or not r["context"]
                or (r["app_id"] is not None and (type(r["app_id"]) is not int or r["app_id"] <= 0))
                for r in required)):
            raise ValueError("Outcomes: invalid policy requirements")
        values = {}
        if snapshot is not None:
            if not isinstance(snapshot, dict) or snapshot.get("head_sha") not in shas:
                raise ValueError("Outcomes: policy results must identify the head or merge SHA")
            sha = snapshot["head_sha"]
            # Validate the same timing and state contract as historical evidence.
            if not isinstance(snapshot.get("check_runs"), list) or not isinstance(snapshot.get("statuses"), list):
                raise ValueError("Outcomes: policy results need arrays")
            cls._checks({**snapshot, "check_runs": [], "statuses": []}, sha, merged)
            if snapshot["required"]:
                raise ValueError("Outcomes: policy results cannot declare historical requirements")
            groups = {}
            for run in snapshot["check_runs"]:
                app_id = run.get("app", {}).get("id")
                if app_id is not None and (type(app_id) is not int or app_id <= 0):
                    raise ValueError("Outcomes: invalid check app ID")
                groups.setdefault((run["name"], app_id), []).append(run)
            for (name, app_id), runs in groups.items():
                values[(name, app_id, "check")] = cls._checks(
                    {**snapshot, "required": [name], "check_runs": runs, "statuses": []}, sha, merged)
            for name in {s["context"] for s in snapshot["statuses"]}:
                values[(name, None, "status")] = cls._checks(
                    {**snapshot, "required": [name], "check_runs": [],
                     "statuses": [s for s in snapshot["statuses"] if s["context"] == name]}, sha, merged)
        if required is None:
            return "unknown", "unknown", "checks_policy_unreadable"
        basis = "current_policy" if required else "all_visible"
        if not required:
            verdicts = list(values.values())
            if not verdicts:
                return "unknown", basis, "no_checks"
        else:
            verdicts = []
            for requirement in required:
                name, app_id = requirement["context"], requirement["app_id"]
                matches = [value for (n, app, kind), value in values.items() if n == name
                           and (app_id is None or (kind == "check" and app == app_id))]
                verdicts.append("red" if "red" in matches else "green" if matches else "unknown")
        if "red" in verdicts:
            return "red", basis, "checks_red"
        if "unknown" in verdicts:
            return "unknown", basis, "checks_missing_required"
        return "green", basis, ""

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
                      and FIX.search(message) and not REVERT.search(message) and refs.search(message))
