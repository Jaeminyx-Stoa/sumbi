"""Shared accounting, coverage, time and candidate project links."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from sumbi.privacy import pseudonym

TOKEN_KINDS = ("new_input", "cache_write", "cache_read", "output", "reasoning_output")
EVIDENCE_TYPES = ("cwd", "tool_path", "previous_event", "unassigned")
COUNT_KINDS = ("compactions", "tool_calls", "tool_results", "tool_errors", "api_errors",
               "user_input_requests", "counter_resets")


def label(value: object, category: str = "label") -> str:
    """Allow bounded machine labels; fingerprint unsupported free-text shapes."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_<][A-Za-z0-9_.:<>-]{0,95}", value):
        return value
    return pseudonym(category, str(value))


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except ValueError:
        return None


def epoch(value: object, *, milliseconds: bool = False) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        try:
            return datetime.fromtimestamp(value / (1000 if milliseconds else 1), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return timestamp(value)


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime

    def __post_init__(self):
        if (self.since.tzinfo is None or self.until.tzinfo is None
                or self.since.utcoffset().total_seconds() != 0
                or self.until.utcoffset().total_seconds() != 0 or self.since >= self.until):
            raise ValueError("Window bounds must be UTC and since must precede until")

    def contains(self, when: datetime | None) -> bool:
        return when is not None and self.since <= when < self.until

    def overlap(self, start: datetime, end: datetime) -> float:
        return max(0.0, (min(end, self.until) - max(start, self.since)).total_seconds())


@dataclass
class Coverage:
    files_scanned: int = 0
    lines_read: int = 0
    broken_lines: int = 0
    duplicate_events: int = 0
    unreadable_files: int = 0
    invalid_timestamps: int = 0
    invalid_token_records: int = 0
    inherited_session_meta: int = 0
    unknown_record_types: Counter = field(default_factory=Counter)

    def unknown(self, kind: object):
        self.unknown_record_types[label(kind, "type")] += 1

    def as_dict(self):
        return {name: (dict(sorted(value.items())) if isinstance(value, Counter) else value)
                for name, value in vars(self).items()}


def records(path: Path, coverage: Coverage):
    """Continue after broken JSON, invalid UTF-8 and non-object records."""
    coverage.files_scanned += 1
    try:
        with path.open("rb") as stream:
            for raw in stream:
                coverage.lines_read += 1
                try:
                    event = json.loads(raw.decode("utf-8"))
                    if not isinstance(event, dict):
                        raise ValueError("Expected object")
                except (ValueError, UnicodeDecodeError):
                    coverage.broken_lines += 1
                    continue
                yield event
    except OSError:
        coverage.unreadable_files += 1


def mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_path(value: str) -> str:
    value = value.replace("\\", "/")
    if ntpath.splitdrive(value)[0] or value.startswith("//"):
        return ntpath.normpath(value).replace("\\", "/").casefold().rstrip("/")
    return os.path.normpath(value).replace("\\", "/").rstrip("/")


def normalize_origin(value: str) -> str | None:
    """Discard transport, credentials and .git; preserve repository path case."""
    value = value.strip()
    if "://" not in value:
        match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", value)
        if not match:
            return None
        host, path = match.groups()
    else:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ("https", "http", "ssh", "git") or not parsed.hostname:
                return None
            host, path = parsed.hostname, parsed.path
            if parsed.port and parsed.port not in (22, 80, 443, 9418):
                host += ":" + str(parsed.port)
        except ValueError:
            return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return host.casefold() + "/" + path if path else None


@dataclass
class ProjectRule:
    name: str
    origins: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.name):
            raise ValueError("Project rule names must be bounded labels")


class Attributor:
    """Git origins are candidates, never proof of deliverable ownership."""

    def __init__(self, rules: list[ProjectRule]):
        self.rules = rules
        self.cache: dict[str, tuple[str | None, str]] = {}
        self.roots: dict[str, str] = {}

    def repository_path(self, value: str) -> str:
        """Resolve an existing file/subdirectory to its local repository root."""
        normalized = normalize_path(value)
        if normalized in self.roots:
            return self.roots[normalized]
        root = normalized
        path = Path(value)
        # Do not interpret foreign Windows paths as relative paths on POSIX.
        if not normalized.startswith("//") and (os.name == "nt" or not ntpath.splitdrive(value)[0]):
            while not path.exists() and path != path.parent:
                path = path.parent
            directory = path if path.is_dir() else path.parent
            if directory.is_dir():
                try:
                    result = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                                            capture_output=True, text=True, timeout=5,
                                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
                    if result.returncode == 0 and result.stdout.strip():
                        root = normalize_path(result.stdout.strip())
                except (OSError, subprocess.TimeoutExpired, UnicodeError):
                    pass
        self.roots[normalized] = root
        return root

    def event_link(self, path: str) -> dict:
        root = self.repository_path(path)
        # Scope against the observed operand as well as its repository root.
        link = self.link(path)
        root_link = self.link(root) if root != normalize_path(path) else link
        if root_link["bucket"] == "unassigned" or root_link["evidence"] in (
                "git_origin_out_of_scope", "git_origin_candidate"):
            return root_link
        if link["bucket"] != "project" and root_link["bucket"] == "project":
            link = root_link
        if root != normalize_path(path):
            link = {**link, "project_key": root_link["project_key"]}
        return link

    def origin(self, cwd: str) -> tuple[str | None, str]:
        normalized = normalize_path(cwd)
        if normalized in self.cache:
            return self.cache[normalized]
        origin, state = None, "path_only"
        try:
            if not normalized.startswith("//") and (os.name == "nt" or not ntpath.splitdrive(cwd)[0]) and Path(cwd).is_dir():
                options = dict(
                    capture_output=True, text=True, timeout=5,
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                )
                repo = subprocess.run(["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"], **options)
                if repo.returncode == 0 and repo.stdout.strip() == "true":
                    result = subprocess.run(["git", "-C", cwd, "config", "--get", "remote.origin.url"], **options)
                    if result.returncode == 0 and result.stdout.strip():
                        origin = normalize_origin(result.stdout)
                        state = "origin" if origin else "unsupported_origin"
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            state = "origin_unavailable"
        self.cache[normalized] = origin, state
        return origin, state

    def link(self, cwd: str) -> dict:
        origin, state = self.origin(cwd)
        path = normalize_path(cwd)
        matches = []
        evidence = "path_pattern"
        if state in ("unsupported_origin", "origin_unavailable") and any(r.origins for r in self.rules):
            return {"project_key": pseudonym("project", path), "rule": None,
                    "evidence": state, "bucket": "unassigned"}
        if origin:
            matches = [r.name for r in self.rules if any(
                fnmatch.fnmatchcase(origin, normalize_origin(p) or p) for p in r.origins)]
            evidence = "git_origin_candidate"
            if not matches and any(r.origins for r in self.rules):
                return {"project_key": pseudonym("project", origin), "rule": None,
                        "evidence": "git_origin_out_of_scope", "bucket": "other"}
        if not matches:
            matches = [r.name for r in self.rules if any(
                fnmatch.fnmatchcase(path, normalize_path(p)) for p in r.paths)]
            evidence = "path_pattern" if state == "path_only" else "path_pattern_" + state
        key = pseudonym("project", origin or path)
        if len(set(matches)) > 1:
            return {"project_key": key, "rule": None, "evidence": "ambiguous_rules", "bucket": "unassigned"}
        if not self.rules:
            return {"project_key": key, "rule": None, "evidence": "git_origin_candidate" if origin else "cwd_candidate",
                    "bucket": "project"}
        return {"project_key": key, "rule": matches[0] if matches else None,
                "evidence": evidence if matches else "unmatched_path", "bucket": "project" if matches else "other"}

    def session_link(self, paths: set[str]) -> dict:
        links = [self.link(path) for path in sorted(paths)]
        if not links:
            return {"bucket": "unassigned", "project_key": None, "rule": None,
                    "evidence": "missing_cwd", "links": []}
        signatures = {(x["project_key"], x["rule"], x["bucket"]) for x in links}
        if len(signatures) > 1:
            return {"bucket": "unassigned", "project_key": None, "rule": None,
                    "evidence": "multiple_projects", "links": links}
        return {**links[0], "links": links}


class RepositoryAttributor(Attributor):
    """Exact repository scope, consolidating subdirectories and local clones."""

    def __init__(self, repository: Path):
        super().__init__([])
        self.path = normalize_path(str(repository.resolve()))
        self.repository_origin, _ = self.origin(str(repository.resolve()))

    def link(self, cwd: str) -> dict:
        origin, state = self.origin(cwd)
        path = normalize_path(cwd)
        matched = path == self.path or path.startswith(self.path + "/")
        evidence = "path_pattern"
        bucket = "project" if matched else "other"
        if self.repository_origin and origin:
            matched = origin == self.repository_origin
            bucket = "project" if matched else "other"
            evidence = "git_origin_candidate" if matched else "git_origin_out_of_scope"
        elif self.repository_origin and state in ("unsupported_origin", "origin_unavailable"):
            matched, bucket, evidence = False, "unassigned", state
        identity = (self.repository_origin or self.path) if matched else (origin or path)
        return {"project_key": pseudonym("project", identity),
                "rule": "repository" if matched else None, "evidence": evidence, "bucket": bucket}


@dataclass
class CommandExecution:
    """Agent-neutral machine execution evidence. Commands stay local only."""

    agent: str
    session: str
    at: datetime
    command: str | list[str] | None
    exit_code: int | None
    started_at: datetime | None = None
    cwd: str | None = None


@dataclass
class Session:
    agent: str
    raw_id: str
    parent_raw_id: str | None = None
    times: set[datetime] = field(default_factory=set)
    cwd_events: list[tuple[datetime | None, str]] = field(default_factory=list)
    models: set[str] = field(default_factory=set)
    efforts: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    tokens: dict[str, int | None] = field(default_factory=lambda: dict.fromkeys(TOKEN_KINDS))
    counts: Counter = field(default_factory=Counter)
    seen: set[bytes] = field(default_factory=set)
    starts: dict[tuple[str, str], datetime] = field(default_factory=dict)
    ends: dict[tuple[str, str], datetime] = field(default_factory=dict)
    intervals: dict[tuple[str, str], tuple[datetime, datetime]] = field(default_factory=dict)
    counted: set[tuple[str, str]] = field(default_factory=set)
    review: set[str] = field(default_factory=set)
    attribution_events: list[tuple] = field(default_factory=list)
    # Bounded branch/PR evidence, enabled only by the deliver command.
    deliverable_events: list[tuple] = field(default_factory=list)
    # These fields are deliberately absent from as_dict/public collect output.
    start_at: datetime | None = None
    start_cwd: str | None = None
    is_worker: bool = False
    commands: dict[str, CommandExecution] = field(default_factory=dict)
    edits: dict[str, datetime] = field(default_factory=dict)
    completed_at: datetime | None = None
    local_evidence_gaps: Counter = field(default_factory=Counter)

    def execution(self, identity, when, command, code, *, started_at=None, cwd=None):
        if when is not None:
            self.commands[str(identity)] = CommandExecution(
                self.agent, self.raw_id, when, command, code, started_at, cwd)
        else:
            self.local_evidence_gaps["command_timestamp_missing"] += 1

    def edit(self, identity, when):
        if when is not None:
            self.edits[str(identity)] = when
        else:
            self.local_evidence_gaps["edit_timestamp_missing"] += 1

    def id(self):
        return pseudonym("session", self.agent + ":" + self.raw_id)

    def accept(self, event: dict, coverage: Coverage) -> bool:
        digest = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).digest()
        if digest in self.seen:
            coverage.duplicate_events += 1
            return False
        self.seen.add(digest)
        when = timestamp(event.get("timestamp"))
        if when:
            self.times.add(when)
        elif "timestamp" in event:
            coverage.invalid_timestamps += 1
        return True

    def cwd(self, value: object, when: datetime | None):
        if isinstance(value, str) and value:
            self.cwd_events.append((when, value))

    def add_tokens(self, values: dict[str, int | None]):
        for key, value in values.items():
            if value is not None:
                self.tokens[key] = (self.tokens[key] or 0) + value

    def context(self, when, order, cwd, *, reset=True):
        if when is not None:
            self.attribution_events.append((when, order, "context", (cwd, reset)))

    def tool_paths(self, when, order, cwd, paths):
        if when is not None and (cwd or paths):
            self.attribution_events.append((when, order, "tool", (cwd, paths)))

    def usage(self, when, order, values, *, cwd=None, paths=()):
        if when is not None:
            self.attribution_events.append((when, order, "usage", (values, cwd, paths)))

    def event_allocations(self, window, attributor, idle_minutes):
        from sumbi.evidence import resolve_path

        context_cwd = None
        command_cwd = None
        turn_paths = set()
        previous = None
        previous_when = None
        for when, order, kind, data in sorted(self.attribution_events, key=lambda e: (e[0], e[1])):
            if when >= window.until:
                break
            if kind == "context":
                raw_cwd, reset = data
                context_cwd = resolve_path(raw_cwd)
                if reset:
                    command_cwd, turn_paths = None, set()
                continue
            if kind == "tool":
                cwd, paths = data
                if cwd:
                    command_cwd = resolve_path(cwd, context_cwd)
                turn_paths.update(p for raw in paths if (p := resolve_path(raw, command_cwd or context_cwd)))
                continue
            values, own_cwd, own_paths = data
            # A Claude line's cwd belongs to that message; Codex context is in force
            # at the closing cumulative snapshot, even across a directory change.
            cwd = resolve_path(own_cwd, context_cwd) if own_cwd else None
            if self.agent == "codex":
                cwd = command_cwd or context_cwd
            if cwd:
                link = attributor.event_link(cwd)
                evidence = "cwd"
            else:
                paths = turn_paths | {p for raw in own_paths if (p := resolve_path(raw, context_cwd))}
                if paths:
                    links = [attributor.event_link(p) for p in sorted(paths)]
                    signatures = {(x["bucket"], x["project_key"], x["rule"]) for x in links}
                    link = links[0] if len(signatures) == 1 else {
                        "bucket": "unassigned", "project_key": None, "rule": None, "evidence": "conflicting_tool_paths"}
                    evidence = "tool_path"
                elif previous is not None and previous["bucket"] != "unassigned" and previous_when is not None \
                        and (when - previous_when).total_seconds() < idle_minutes * 60:
                    link, evidence = previous, "previous_event"
                else:
                    link = {"bucket": "unassigned", "project_key": None, "rule": None, "evidence": "missing_evidence"}
                    evidence = "unassigned"
            previous, previous_when = link, when
            if not window.contains(when):
                continue
            yield when, order, values, link, evidence, cwd

    def allocations(self, window, attributor, idle_minutes):
        rows = {}
        for when, order, values, link, evidence, cwd in self.event_allocations(window, attributor, idle_minutes):
            identity = link["bucket"], link["project_key"], link["rule"]
            row = rows.setdefault(identity, {**link, "events": 0, "tokens": dict.fromkeys(TOKEN_KINDS),
                                            "evidence_counts": dict.fromkeys(EVIDENCE_TYPES, 0),
                                            "evidence_tokens": dict.fromkeys(EVIDENCE_TYPES, 0)})
            row["events"] += 1
            row["evidence_counts"][evidence] += 1
            total = sum(values.get(k) or 0 for k in TOKEN_KINDS if k != "reasoning_output")
            row["evidence_tokens"][evidence] += total
            for key, value in values.items():
                if value is not None:
                    row["tokens"][key] = (row["tokens"][key] or 0) + value
        for row in rows.values():
            row["tokens"]["total"] = sum(row["tokens"][k] or 0 for k in TOKEN_KINDS if k != "reasoning_output")
            total = row["tokens"]["total"]
            row["fallback_share"] = row["evidence_tokens"]["previous_event"] / total if total else 0.0
        return sorted(rows.values(), key=lambda r: (r["bucket"], r["project_key"] or "", r["rule"] or ""))

    def count(self, kind: str, key: object, when: datetime | None, window: Window):
        identity = (kind, str(key))
        if window.contains(when) and identity not in self.counted:
            self.counts[kind] += 1
            self.counted.add(identity)

    def interval(self, kind: str, key: object, start: datetime | None, end: datetime | None):
        identity = (kind, str(key))
        if start is not None:
            self.starts[identity] = min(start, self.starts.get(identity, start))
        if end is not None:
            self.ends[identity] = max(end, self.ends.get(identity, end))
        if identity in self.starts and identity in self.ends:
            begin, finish = self.starts[identity], self.ends[identity]
            if finish >= begin:
                self.intervals[identity] = begin, finish

    def local_text(self, value: object, when: datetime | None, window: Window, enabled: bool):
        if not enabled or not window.contains(when):
            return
        if isinstance(value, str):
            for line in value.splitlines():
                if line.lstrip().startswith("Harness friction:"):
                    self.review.add(line.strip())
        elif isinstance(value, list):
            for item in value:
                self.local_text(item, when, window, enabled)
        elif isinstance(value, dict):
            for key in ("text", "content"):
                self.local_text(value.get(key), when, window, enabled)

    def in_window(self, window: Window):
        return any(window.contains(t) for t in self.times) or any(
            window.overlap(a, b) > 0 for a, b in self.intervals.values())

    def project_paths(self, window: Window) -> set[str]:
        paths = {value for when, value in self.cwd_events if window.contains(when)}
        prior = [(when, value) for when, value in self.cwd_events if when and when < window.since]
        # A changed cwd on the first in-window event supersedes the previous context.
        first = min((t for t in self.times if window.contains(t)), default=window.until)
        if prior and not any(when == first for when, _ in self.cwd_events):
            last = max(t for t, _ in prior)
            paths.update(value for t, value in prior if t == last)
        if not paths:
            paths.update(value for when, value in self.cwd_events if when is None)
        return paths

    def as_dict(self, window: Window, attributor: Attributor, idle_minutes: float):
        times = sorted(self.times)
        span = window.overlap(times[0], times[-1]) if times else 0.0
        thresholds = sorted({2.0, 5.0, 10.0, idle_minutes})
        active = {format(n, "g"): round(sum(window.overlap(a, b) for a, b in zip(times, times[1:])
                                          if (b - a).total_seconds() <= n * 60), 6) for n in thresholds}
        durations = {}
        for kind in ("tool", "request"):
            intervals = [(a, b) for (k, _), (a, b) in self.intervals.items() if k == kind]
            durations[kind] = {"measurement": "observed", "seconds": round(sum(
                window.overlap(a, b) for a, b in intervals), 6),
                "paired_intervals": sum(window.overlap(a, b) > 0 or (a == b and window.contains(a)) for a, b in intervals),
                "unpaired_starts": sum(key[0] == kind and key not in self.intervals and window.contains(t)
                                       for key, t in self.starts.items()),
                "unpaired_ends": sum(key[0] == kind and key not in self.intervals and window.contains(t)
                                     for key, t in self.ends.items())}
        total = sum(self.tokens[k] or 0 for k in TOKEN_KINDS if k != "reasoning_output")
        allocations = self.allocations(window, attributor, idle_minutes)
        return {"id": self.id(), "parent_id": pseudonym("session", self.agent + ":" + self.parent_raw_id)
                if self.parent_raw_id else None, "agent": self.agent,
                "project": attributor.session_link(self.project_paths(window)),
                "allocations": allocations,
                "spend_allocation": "events",
                "models": sorted(self.models), "efforts": sorted(self.efforts),
                "cli_versions": sorted(self.versions),
                "tokens": {**self.tokens, "total": total, "measurement": "observed",
                           "total_basis": "reported_components_excluding_reasoning_subset"},
                "counts": {k: self.counts[k] for k in COUNT_KINDS},
                "time": {"wall_span": {"measurement": "observed", "seconds": round(span, 6)},
                         "active": {"measurement": "estimated", "idle_minutes": idle_minutes,
                                    "seconds": active[format(idle_minutes, "g")],
                                    "sensitivity_seconds": active}, "durations": durations}}
