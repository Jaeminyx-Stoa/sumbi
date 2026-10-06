"""Read-only GitHub REST outcomes. Credentials and response prose stay private."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from sumbi.outcomes.github.ledger import PR, utc
from sumbi.outcomes.github.recorded import FixtureOutcomes, SHA
from dataclasses import replace

API = "https://api.github.com"
MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 65536
CACHE_SECONDS = 300
PLAN_UNAVAILABLE = "Upgrade to GitHub Pro or make this repository public to enable this feature."


class RepositoryUnreadable(ValueError):
    """A non-rate-limit access failure; callers may exclude discovered scope."""


def outside_repository(path: Path):
    """Resolve symlinks and reject any destination in a Git working tree."""
    target = path.resolve()
    if any((p / ".git").exists() for p in (target, *target.parents)):
        raise ValueError("GitHub destinations must be outside Git repositories")
    return target


def github_token():
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    executable = shutil.which("gh")
    if executable:
        try:
            result = subprocess.run([executable, "auth", "token"], capture_output=True,
                text=True, timeout=15, check=False)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
    raise ValueError(
        "GitHub authentication required: set GITHUB_TOKEN or GH_TOKEN, or sign in with gh")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards Authorization, even to a different host.
        raise ValueError("GitHub redirects are not followed; credentials were not forwarded")


def _write(path, data):
    path = outside_repository(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
            dir=path.parent, delete=False, prefix=".sumbi-") as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o600)
            json.dump(data, stream, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class GitHubOutcomes(FixtureOutcomes):
    """Fetch complete repository intervals and reuse the fixture judgment contract.

    REST cannot prove the historical required-check list. Current policy or all
    visible results supply explicitly weaker bases when no snapshot is available.
    Recordings include normalized fixtures and filtered REST pages for offline tests.
    """

    def __init__(self, ledger, *, cache=None, record=None, head_refs=False):
        self.head_refs = head_refs
        self.cache = outside_repository(cache if cache is not None
            else Path.home() / ".cache/sumbi/outcomes")
        self.record = outside_repository(record) if record is not None else None
        if self.record and (self.record == self.cache or self.record.is_relative_to(self.cache)
            or self.cache.is_relative_to(self.record)):
            raise ValueError("GitHub cache and record directories must not overlap")
        self._token = github_token()
        self._secrets = {self._token,
            *[os.environ.get(k, "").strip() for k in ("GITHUB_TOKEN", "GH_TOKEN")]} - {""}
        if any(ord(c) < 32 or ord(c) > 126 for c in self._token):
            raise ValueError("GitHub credential has an invalid format")
        self._opener = urllib.request.build_opener(NoRedirect())
        self.pulls, self.observations, self.commits = {}, {}, {}
        self.evidence_gaps, self._gap_keys = Counter(), set()
        self._evidence_identity = None
        self.files_read = 0
        self.requests = self.cache_hits = 0
        self._entries = {}
        self._base_refs = {}
        self._policies = {}
        self._recordings = {}
        self._capture_incomplete = set()
        repos = sorted({r for d in ledger for r in d.repos})
        for repo in repos:
            start = min(d.dispatched_at for d in ledger if repo in d.repos)
            # Whole-second interval bounds give repeat runs stable cache keys.
            self._asof = datetime.now(timezone.utc).replace(microsecond=0)
            wanted = {p for d in ledger for p in d.prs if p.rsplit("#", 1)[0] == repo}
            try:
                self._repository(repo, start, wanted)
            except (KeyError, TypeError, AttributeError, OverflowError):
                raise ValueError("GitHub response was malformed") from None

    def _redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[credential]")
            return re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
                "[credential]", value)
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        return value

    @staticmethod
    def _filter(path, raw, *, head_refs=False):
        """Allow-list only data used by this adapter, dropping all author fields."""
        endpoint = path.split("?", 1)[0]

        def pull(pr):
            fields = ("number", "state", "created_at", "closed_at", "merged_at",
                "merge_commit_sha", "title", "body")
            result = {k: pr[k] for k in fields}
            result["head"] = {"sha": pr["head"]["sha"]}
            if head_refs:
                result["head"]["ref"] = pr["head"]["ref"]
            result["base"] = {"ref": pr["base"]["ref"]}
            return result

        if "/pulls" in endpoint:
            return [pull(p) for p in raw] if isinstance(raw, list) else pull(raw)
        if endpoint.endswith("/check-runs"):
            fields = ("name", "head_sha", "started_at", "completed_at", "status", "conclusion")
            if type(raw["total_count"]) is not int or raw["total_count"] < 0:
                raise ValueError("GitHub check-run count was malformed")
            return {"total_count": raw["total_count"], "check_runs": [
                {**{k: c[k] for k in fields}, **({"id": c["id"]} if "id" in c else {}),
                    "app": {"id": c.get("app", {}).get("id")}}
                for c in raw["check_runs"]]}
        if "/rules/branches/" in endpoint:
            return GitHubOutcomes._filter_rules(raw)
        if "/branches/" in endpoint:
            protection = raw.get("protection")
            protection = {} if protection is None else protection
            checks = protection.get("required_status_checks")
            checks = {} if checks is None else checks
            enabled = protection.get("enabled", raw["protected"])
            enforcement = checks.get("enforcement_level") if checks else "off"
            if type(raw["protected"]) is not bool or type(
                enabled) is not bool or enforcement not in (None, "off", "non_admins", "everyone"):
                raise ValueError("GitHub branch policy was malformed")
            return {"protected": raw["protected"], "protection": {
                "enabled": enabled,
                "required_status_checks": {
                    "enforcement_level": enforcement,
                    "complete": not enabled or "required_status_checks" in protection and (
                        not checks or {"enforcement_level", "contexts", "checks"} <= set(checks)),
                    "contexts": checks.get("contexts", []),
                    "checks": [{"context": c["context"], "app_id": c.get("app_id")}
                        for c in checks.get("checks", [])]}}}
        if endpoint.endswith("/statuses"):
            # GitHub commit-status objects omit sha; the request supplies it.
            return [{**{k: s[k] for k in ("context", "updated_at", "state")},
                **({"id": s["id"]} if "id" in s else {})} for s in raw]
        if endpoint.endswith("/commits"):
            return [{"sha": c["sha"], "commit": {"message": c["commit"]["message"],
                "committer": {"date": c["commit"]["committer"]["date"]}}} for c in raw]
        raise ValueError("GitHub response endpoint is unsupported")

    @staticmethod
    def _filter_rules(raw):
        filtered = []
        for rule in raw:
            item = {"type": rule["type"]}
            if rule["type"] == "required_status_checks":
                parameters = rule.get("parameters")
                checks = (parameters.get("required_status_checks")
                    if parameters is not None else None)
                item["parameters"] = {"required_status_checks": None if checks is None else [
                    {"context": c["context"], "integration_id": c.get("integration_id")}
                    for c in checks]}
            filtered.append(item)
        return filtered

    def _get(self, path, *, missing=False, policy=False):
        key, destination, payload = self._cached_response(path)
        if payload is None:
            for retry in range(4):
                captured_at = self._asof.timestamp()
                request = urllib.request.Request(API + path, headers={
                    "Authorization": "Bearer " + self._token,
                    "Accept": "application/vnd.github+json", "User-Agent": "sumbi",
                    "X-GitHub-Api-Version": "2022-11-28"}, method="GET")
                try:
                    self.requests += 1
                    with self._opener.open(request, timeout=30) as response:
                        data = response.read(MAX_BYTES + 1)
                        if len(data) > MAX_BYTES:
                            raise ValueError("GitHub response exceeds the size limit")
                        raw = json.loads(data)
                    filtered = self._redact(self._filter(path, raw, head_refs=self.head_refs))
                    for obj in filtered if isinstance(filtered, list) else [filtered]:
                        if isinstance(obj, dict):
                            values = [obj.get("title"), obj.get("body"),
                                obj.get("commit", {}).get("message")]
                            if any(isinstance(v, str) and len(v) > MAX_TEXT for v in values):
                                raise ValueError("GitHub response text exceeds the scan limit")
                    payload = {"fetched_at": time.time(), "observed_at": captured_at,
                        "response": filtered}
                    _write(destination, payload)
                    break
                except urllib.error.HTTPError as exc:
                    headers, code = exc.headers, exc.code
                    error_body = exc.read(4096) if code == 403 else b""
                    rate_message = error_body.lower()
                    exc.close()
                    if code == 404 and missing:
                        return None
                    limited = code == 429 or (code == 403 and (
                        headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After")
                        or b"rate limit" in rate_message))
                    if limited and retry < 3:
                        try:
                            delay = max(0, float(headers.get("Retry-After", 0)),
                                float(headers.get("X-RateLimit-Reset",
                                    0)) - time.time()) + 1
                        except (ValueError, TypeError):
                            delay = 60 * (retry + 1)
                        if not 0 <= delay <= 3600:
                            raise ValueError("GitHub rate limit requires a later retry") from None
                        # Short sleeps also allow callers to interrupt long waits.
                        while delay > 0:
                            step = min(30, delay)
                            time.sleep(step)
                            delay -= step
                        continue
                    if policy and code in (403, 404) and not limited:
                        # Match the exact plan message only on the rules endpoint.
                        try:
                            plan_unavailable = (code == 403 and "/rules/branches/" in path
                                and json.loads(error_body).get(
                                    "message") == PLAN_UNAVAILABLE)
                        except (ValueError, AttributeError, UnicodeError):
                            plan_unavailable = False
                        payload = {"fetched_at": time.time(), "observed_at": captured_at,
                            "response": [] if plan_unavailable else None}
                        _write(destination, payload)
                        break
                    failure = (RepositoryUnreadable
                        if code in (403, 404) and not limited else ValueError)
                    raise failure("GitHub request failed (HTTP " + str(code)
                        + "); check access or retry later") from None
                except (urllib.error.URLError, OSError, UnicodeError, json.JSONDecodeError,
                    KeyError, TypeError, AttributeError):
                    raise ValueError("GitHub response was unavailable or malformed") from None
        return self._record_response(key, payload)

    def _pages(self, path, key=None, *, evidence=False):
        collected = []
        for page in range(1, 10001):
            raw = self._get(path + ("&" if "?" in path else "?") + "per_page=100&page="
                + str(page), policy=evidence)
            if raw is None and evidence:
                self._capture_gap("checks_unreadable", path)
                return []
            rows = raw[key] if key else raw
            if not isinstance(rows, list):
                raise ValueError("GitHub page must contain an array")
            collected.extend(rows)
            if len(rows) < 100:
                if key and len(collected) < raw["total_count"]:
                    self._capture_gap("checks_capture_incomplete", path)
                return collected
        self._capture_gap("pagination_incomplete", path)
        return collected

    def _capture_gap(self, reason, path):
        self._gap(reason, path)
        self._capture_incomplete.add(path)

    def _repository(self, repo, start, wanted):
        prefix = "/repos/" + repo
        # Listing, rather than search, avoids search's indexing lag and 1000-result cap.
        pulls = []
        for page in range(1, 10001):
            rows = self._get(prefix
                + "/pulls?state=all&sort=created&direction=desc&per_page=100&page=" + str(page))
            pulls.extend(p for p in rows if utc(p["created_at"], "GitHub PR timestamp") >= start)
            if len(rows) < 100 or any(utc(p["created_at"], "GitHub PR timestamp") < start
                for p in rows):
                break
        else:
            self._capture_gap("pulls_capture_incomplete", prefix + "/pulls")
        by_id = {repo + "#" + str(p["number"]): p for p in pulls}
        for identity in sorted(wanted - by_id.keys()):
            pr = self._get(prefix + "/pulls/" + identity.rsplit("#", 1)[1], missing=True)
            if pr is not None:
                by_id[identity] = pr
        commits = {}
        # Include the default branch and every PR target branch in this capture.
        branches = {p["base"]["ref"] for p in by_id.values()}
        for ref in [None, *sorted(branches)]:
            query = {"since": start.isoformat(), "until": self._asof.isoformat()}
            if ref is not None:
                query["sha"] = ref
            for c in self._pages(prefix + "/commits?" + urllib.parse.urlencode(query)):
                commits[c["sha"]] = c
        entries = []
        for identity, pr in sorted(by_id.items()):
            if utc(pr["created_at"], "GitHub PR timestamp") >= self._asof:
                continue
            response = {k: v for k, v in pr.items() if k != "base"}
            # Changes after the earliest cached observation cannot be assessed as of it.
            if response["closed_at"] and utc(response["closed_at"], "GitHub closure") >= self._asof:
                response.update(state="open", closed_at=None, merged_at=None, merge_commit_sha=None)
            response["merged"] = response["merged_at"] is not None
            entries.append({"response": response, "checks_at_merge": None})
            self._entries[identity] = entries[-1]
            self._base_refs[identity] = pr["base"]["ref"]
        end = self._asof
        if start >= end:
            raise ValueError("GitHub observation must follow ledger dispatch")
        raw = {"repository": repo, "coverage_start": start.isoformat(),
            "observed_at": end.isoformat(),
            "pulls_complete": prefix + "/pulls" not in self._capture_incomplete,
            "commits_complete": not any(p.startswith(prefix + "/commits?")
                for p in self._capture_incomplete), "pulls": entries,
            "commits": [c for c in commits.values()
                if start <= utc(c["commit"]["committer"]["date"], "GitHub commit timestamp") < end]}
        self._load(raw, defer_policy=True)
        self._recordings[repo] = raw
        self._record_repo(repo)

    def pull(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(PR, identity):
            raise ValueError("GitHub PR identity is invalid")
        pr = super().pull(identity)
        entry = self._entries.get(identity)
        if pr and pr.merged_at and "observed_checks_at_merge" not in entry:
            self._evidence_identity = identity
            try:
                results = self._merge_results(pr)
                value = self._policy_checks({"required": [], "results": results}, pr.merged_at,
                    (pr.head_sha, pr.merge_sha), self._gap)[0]
                if entry["checks_at_merge"] is None:
                    evidence = {"required": self._policy(identity), "results": results}
                    entry["current_policy_evidence"] = evidence
                    checks, basis, reason = self._policy_checks(evidence, pr.merged_at,
                        (pr.head_sha, pr.merge_sha), self._gap)
                else:
                    checks, basis, reason = pr.checks, pr.checks_basis, pr.checks_reason
            except (KeyError, TypeError, AttributeError):
                raise ValueError("GitHub check response was malformed") from None
            entry["observed_checks_at_merge"] = value
            pr = replace(pr, observed_checks=value, checks=checks, checks_basis=basis,
                checks_reason=reason)
            self.pulls[identity] = pr
            self._record_repo(identity.rsplit("#", 1)[0])
        return pr

    def disturbances(self, pull, days):
        follows, reverts = super().disturbances(pull, days)
        return [self.pull(p.id) for p in follows], reverts

    def _policy(self, identity):
        repo = identity.rsplit("#", 1)[0]
        base = self._base_refs[identity]
        key = repo, base
        if key in self._policies:
            return self._policies[key]
        prefix = "/repos/" + repo
        branch = urllib.parse.quote(base, safe="")
        classic = self._get(prefix + "/branches/" + branch, policy=True)
        rules = []
        for page in range(1, 10001):
            rows = self._get(prefix + "/rules/branches/" + branch + "?per_page=100&page="
                + str(page), policy=True)
            if rows is None:
                rules = None
                break
            rules.extend(rows)
            if len(rows) < 100:
                break
        else:
            self._gap("policy_incomplete", key)
            rules = None
        if classic is None or rules is None:
            self._policies[key] = None
            return None
        required = set()

        def add(context, app_id=None):
            if (not isinstance(context, str) or not context or (app_id is not None
                and (type(app_id) is not int or app_id <= 0))):
                raise ValueError("GitHub required check was malformed")
            required.add((context, app_id))

        protection = classic["protection"]
        checks = protection["required_status_checks"]
        if not checks.get("complete", True) or checks["enforcement_level"] is None:
            self._policies[key] = None
            return None
        if protection["enabled"] and checks["enforcement_level"] != "off":
            # contexts mirror checks; do not turn a pinned check into an unpinned one.
            for c in checks["checks"]:
                # Classic -1 means any app; ruleset IDs have no such sentinel.
                add(c["context"], None if c["app_id"] == -1 else c["app_id"])
            for context in checks["contexts"]:
                if not any(c["context"] == context for c in checks["checks"]):
                    add(context)
        for rule in rules:
            if rule["type"] == "required_status_checks":
                items = rule["parameters"]["required_status_checks"]
                if items is None:
                    self._policies[key] = None
                    return None
                for c in items:
                    add(c["context"], c["integration_id"])
        value = [{"context": n, "app_id": app} for n,
            app in sorted(required, key=lambda r: (r[0], r[1] or 0))]
        self._policies[key] = value
        return value

    def _merge_results(self, pr):
        repo = pr.id.rsplit("#", 1)[0]
        results, paths = [], []
        for sha in dict.fromkeys([pr.merge_sha, pr.head_sha]):
            if not sha or not re.fullmatch(SHA, sha):
                raise ValueError("GitHub PR SHA is invalid")
            prefix = "/repos/" + repo + "/commits/" + sha
            paths.extend([prefix + "/check-runs?filter=all", prefix + "/statuses"])
            checks = self._pages(paths[-2], "check_runs", evidence=True)
            statuses = self._pages(paths[-1], evidence=True)
            results.append({"head_sha": sha, "captured_at": pr.merged_at.isoformat(),
                "required": [], "check_runs": checks,
                "statuses": [{**s, "sha": sha} for s in statuses]})
        if any(p in self._capture_incomplete for p in paths):
            return None
        return results

    def _record_repo(self, repo):
        if self.record:
            _write(self.record / (hashlib.sha256(repo.encode()).hexdigest() + ".json"),
                self._recordings[repo])

    def _record_response(self, key, payload):
        self._asof = min(self._asof,
            datetime.fromtimestamp(payload.get("observed_at", payload["fetched_at"]), timezone.utc))
        if self.record:
            _write(self.record / "responses" / (key + ".json"), payload)
        return payload["response"]

    def _cached_response(self, path):
        cache_key = ("worker-head-refs-v1:" if self.head_refs else "") + path
        key = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        destination = self.cache / (key + ".json")
        payload = None
        try:
            if destination.stat().st_size <= MAX_BYTES:
                cached = json.loads(destination.read_text(encoding="utf-8"))
                age = time.time() - cached["fetched_at"]
                if 0 <= age < CACHE_SECONDS:
                    payload = cached
                    self.cache_hits += 1
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return key, destination, payload
