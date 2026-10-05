"""Read-only GitHub REST outcomes. Credentials and response prose stay private."""

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

from sumbi.ledger import PR, utc
from sumbi.outcomes import FixtureOutcomes, SHA

API = "https://api.github.com"
MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 65536
CACHE_SECONDS = 300


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
    raise ValueError("GitHub authentication required: set GITHUB_TOKEN or GH_TOKEN, or sign in with gh")


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

    REST cannot prove the historical required-check list. `checks` stays unknown;
    `observed_checks` reports visible results on the head or merge SHA at merge.
    Recordings include normalized fixtures and filtered REST pages for offline tests.
    """

    def __init__(self, ledger, *, cache=None, record=None):
        self.cache = outside_repository(cache if cache is not None else Path.home() / ".cache/sumbi/outcomes")
        self.record = outside_repository(record) if record is not None else None
        if self.record and (self.record == self.cache or self.record.is_relative_to(self.cache)
                            or self.cache.is_relative_to(self.record)):
            raise ValueError("GitHub cache and record directories must not overlap")
        self._token = github_token()
        self._secrets = {self._token, *[os.environ.get(k, "").strip() for k in ("GITHUB_TOKEN", "GH_TOKEN")]} - {""}
        if any(ord(c) < 32 or ord(c) > 126 for c in self._token):
            raise ValueError("GitHub credential has an invalid format")
        self._opener = urllib.request.build_opener(NoRedirect())
        self.pulls, self.observations, self.commits = {}, {}, {}
        self.files_read = 0
        self.requests = self.cache_hits = 0
        self._entries = {}
        self._recordings = {}
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
            return re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[credential]", value)
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        return value

    @staticmethod
    def _filter(path, raw):
        """Allow-list only data used by this adapter, dropping all author fields."""
        endpoint = path.split("?", 1)[0]

        def pull(pr):
            fields = ("number", "state", "created_at", "closed_at", "merged_at", "merge_commit_sha", "title", "body")
            result = {k: pr[k] for k in fields}
            result["head"] = {"sha": pr["head"]["sha"]}
            result["base"] = {"ref": pr["base"]["ref"]}
            return result

        if "/pulls" in endpoint:
            return [pull(p) for p in raw] if isinstance(raw, list) else pull(raw)
        if endpoint.endswith("/check-runs"):
            fields = ("name", "head_sha", "started_at", "completed_at", "status", "conclusion")
            return {"total_count": raw["total_count"], "check_runs": [{k: c[k] for k in fields} for c in raw["check_runs"]]}
        if endpoint.endswith("/statuses"):
            # GitHub commit-status objects omit sha; the request supplies it.
            return [{k: s[k] for k in ("context", "updated_at", "state")} for s in raw]
        if endpoint.endswith("/commits"):
            return [{"sha": c["sha"], "commit": {"message": c["commit"]["message"],
                    "committer": {"date": c["commit"]["committer"]["date"]}}} for c in raw]
        raise ValueError("GitHub response endpoint is unsupported")

    def _get(self, path, *, missing=False):
        key = hashlib.sha256(path.encode("utf-8")).hexdigest()
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
                    filtered = self._redact(self._filter(path, raw))
                    for obj in filtered if isinstance(filtered, list) else [filtered]:
                        if isinstance(obj, dict):
                            values = [obj.get("title"), obj.get("body"), obj.get("commit", {}).get("message")]
                            if any(isinstance(v, str) and len(v) > MAX_TEXT for v in values):
                                raise ValueError("GitHub response text exceeds the scan limit")
                    payload = {"fetched_at": time.time(), "observed_at": captured_at, "response": filtered}
                    _write(destination, payload)
                    break
                except urllib.error.HTTPError as exc:
                    headers, code = exc.headers, exc.code
                    rate_message = exc.read(4096).lower() if code == 403 else b""
                    exc.close()
                    if code == 404 and missing:
                        return None
                    limited = code == 429 or (code == 403 and (
                        headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After")
                        or b"rate limit" in rate_message))
                    if limited and retry < 3:
                        try:
                            delay = max(0, float(headers.get("Retry-After", 0)),
                                        float(headers.get("X-RateLimit-Reset", 0)) - time.time()) + 1
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
                    raise ValueError("GitHub request failed (HTTP " + str(code) + "); check access or retry later") from None
                except (urllib.error.URLError, OSError, UnicodeError, json.JSONDecodeError,
                        KeyError, TypeError, AttributeError):
                    raise ValueError("GitHub response was unavailable or malformed") from None
        self._asof = min(self._asof, datetime.fromtimestamp(payload.get("observed_at", payload["fetched_at"]), timezone.utc))
        if self.record:
            _write(self.record / "responses" / (key + ".json"), payload)
        return payload["response"]

    def _pages(self, path, key=None):
        collected = []
        for page in range(1, 10001):
            raw = self._get(path + ("&" if "?" in path else "?") + "per_page=100&page=" + str(page))
            rows = raw[key] if key else raw
            if not isinstance(rows, list):
                raise ValueError("GitHub page must contain an array")
            collected.extend(rows)
            if len(rows) < 100:
                if key and len(collected) < raw["total_count"]:
                    raise ValueError("GitHub check-run capture is incomplete")
                return collected
        raise ValueError("GitHub pagination limit reached; capture is incomplete")

    def _repository(self, repo, start, wanted):
        prefix = "/repos/" + repo
        # Listing, rather than search, avoids search's indexing lag and 1000-result cap.
        pulls = []
        for page in range(1, 10001):
            rows = self._get(prefix + "/pulls?state=all&sort=created&direction=desc&per_page=100&page=" + str(page))
            pulls.extend(p for p in rows if utc(p["created_at"], "GitHub PR timestamp") >= start)
            if len(rows) < 100 or any(utc(p["created_at"], "GitHub PR timestamp") < start for p in rows):
                break
        else:
            raise ValueError("GitHub PR capture is incomplete")
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
        end = self._asof
        if start >= end:
            raise ValueError("GitHub observation must follow ledger dispatch")
        raw = {"repository": repo, "coverage_start": start.isoformat(), "observed_at": end.isoformat(),
               "pulls_complete": True, "commits_complete": True, "pulls": entries,
               "commits": [c for c in commits.values() if start <= utc(c["commit"]["committer"]["date"], "GitHub commit timestamp") < end]}
        self._load(raw)
        self._recordings[repo] = raw
        self._record_repo(repo)

    def pull(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(PR, identity):
            raise ValueError("GitHub PR identity is invalid")
        pr = super().pull(identity)
        entry = self._entries.get(identity)
        if pr and pr.merged_at and "observed_checks_at_merge" not in entry:
            try:
                value = self._observed_checks(pr)
            except (KeyError, TypeError, AttributeError):
                raise ValueError("GitHub check response was malformed") from None
            entry["observed_checks_at_merge"] = value
            from dataclasses import replace
            pr = replace(pr, observed_checks=value)
            self.pulls[identity] = pr
            self._record_repo(identity.rsplit("#", 1)[0])
        return pr

    def disturbances(self, pull, days):
        follows, reverts = super().disturbances(pull, days)
        return [self.pull(p.id) for p in follows], reverts

    def _observed_checks(self, pr):
        repo = pr.id.rsplit("#", 1)[0]
        for sha in dict.fromkeys([pr.merge_sha, pr.head_sha]):
            if not sha or not re.fullmatch(SHA, sha):
                raise ValueError("GitHub PR SHA is invalid")
            prefix = "/repos/" + repo + "/commits/" + sha
            checks = self._pages(prefix + "/check-runs?filter=all", "check_runs")
            statuses = self._pages(prefix + "/statuses")
            runs = []
            for c in checks:
                if c["head_sha"].lower() != sha.lower() or not c["started_at"]:
                    continue
                if utc(c["started_at"], "GitHub check timestamp") > pr.merged_at:
                    continue
                c = dict(c)
                if not c["completed_at"] or utc(c["completed_at"], "GitHub check timestamp") > pr.merged_at:
                    # A later completion cannot prove its earlier conclusion.
                    c.update(status="in_progress", conclusion=None, completed_at=None)
                runs.append(c)
            states = [{**s, "sha": sha} for s in statuses
                      if utc(s["updated_at"], "GitHub status timestamp") <= pr.merged_at]
            names = sorted({c["name"] for c in runs} | {s["context"] for s in states})
            if names:
                return self._checks({"head_sha": sha, "captured_at": pr.merged_at.isoformat(),
                                     "required": names, "check_runs": runs, "statuses": states}, sha, pr.merged_at)
        return "unknown"

    def _record_repo(self, repo):
        if self.record:
            _write(self.record / (hashlib.sha256(repo.encode()).hexdigest() + ".json"), self._recordings[repo])
