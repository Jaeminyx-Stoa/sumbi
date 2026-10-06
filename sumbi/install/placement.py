"""Agent-neutral start counts and conservative instruction placement guidance."""

from __future__ import annotations

from sumbi.sessions.builder import collect as collect_sessions

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath

from sumbi.core.records import Coverage
from sumbi.core.time import Window
from sumbi.core.paths import normalize_path
from .errors import InstallError
from .gaps import has_import
from .files import read_bytes, safe_path
from .exclusions import GitIgnore, excluded_by, local_git
from sumbi.events.registry import ADAPTERS
from .files import _without_code
import posixpath


class _IgnoreChecks:
    """Keep ignore roots and query results local, including nested contexts."""

    def __init__(self, root: Path, repositories: list[str]):
        self.root = root
        self.repositories = repositories
        self.contexts, self.results = {}, {}

    def check(self, relative: str) -> bool | None:
        scopes = ["."] + sorted((p for p in self.repositories if
            _key(relative) == _key(p) or _key(relative).startswith(_key(p) + "/")), key=len)
        for index, scope in enumerate(scopes):
            if scope not in self.contexts:
                self.contexts[scope] = GitIgnore(self.root if scope == "."
                    else safe_path(self.root, scope))
            context = self.contexts[scope]
            target = scopes[index + 1] if index + 1 < len(scopes) else relative
            local = target if scope == "." else target[len(scope):].lstrip("/") or "."
            key = scope, local
            if key not in self.results:
                ignored = context.excludes(local) or context.check(local)
                self.results[key] = ignored if context.available else None
            if self.results[key] is not False:
                return self.results[key]
        return False


def load_rules() -> dict:
    return json.loads(Path(__file__).with_name("load_rules.v1.json").read_text(encoding="utf-8"))


def read_starts(home: Path, *, now: datetime | None = None) -> tuple[list, Window, dict]:
    """Read default native adapter starts without exporting log text."""

    until = now or datetime.now(timezone.utc)
    window = Window(until - timedelta(days=14), until)
    sessions, coverage = [], {}
    for agent in ("claude-code", "codex"):
        adapter = ADAPTERS[agent]
        measured = Coverage()
        try:
            sessions.extend(collect_sessions(adapter, home, window, measured))
        except Exception:
            coverage[agent] = {**measured.as_dict(), "status": "unavailable"}
            continue
        coverage[agent] = measured.as_dict()
    return sessions, window, coverage


def _git_identity(directory: Path) -> tuple[Path, Path] | None:
    """Return worktree root/common directory from git, never a folder-name guess."""
    result = local_git(directory, "rev-parse", "--path-format=absolute",
        "--show-toplevel", "--git-common-dir")
    if result is None or result.returncode:
        return None
    values = os.fsdecode(result.stdout).splitlines()
    if len(values) != 2 or any(not Path(value).is_absolute() for value in values):
        return None
    return Path(values[0]).resolve(), Path(values[1]).resolve()


def observed_starts(root: Path, report: dict, sessions, window: Window | None = None) -> dict:
    """Count session starts, with worker attribution and uncertain child coverage."""
    root = root.resolve()
    counts, coverage, roles, agent_roles = Counter(), Counter(), Counter(), Counter()
    provenance = Counter()
    repositories = report["versioning"]["nested_repositories"]["paths"]
    patterns = tuple(e["pattern"] for e in report["exclusions"]["patterns"])
    _count_starts(root, sessions, window, repositories, patterns,
        counts, coverage, roles, agent_roles, provenance)
    paths = {}
    for (agent, path, kind, repository, role, evidence), count in sorted(counts.items()):
        row = paths.setdefault((agent, path, kind, repository),
            {"agent": agent, "path": path, "kind": kind,
                "repository": repository, "count": 0, "roles": {"top-level": 0, "worker": 0},
                "start_evidence": {}})
        if kind == "same-repository-worktree":
            row["scope"] = kind
        row["count"] += count
        row["roles"][role] += count
        row["start_evidence"][evidence] = row["start_evidence"].get(evidence, 0) + count
    return {"status": "observed" if roles else "no-starts-found", "sessions": sum(roles.values()),
        "placement_sessions": sum(counts.values()),
        "start_evidence": dict(sorted(provenance.items())),
        "roles": {role: roles[role] for role in ("top-level", "worker")},
        "agents": [{"agent": agent, "roles": {role: agent_roles[agent, role]
            for role in ("top-level", "worker")},
            "placement_sessions": sum(row["count"] for row in paths.values()
                if row["agent"] == agent)}
            for agent in sorted({agent for agent, _ in agent_roles})],
        "paths": list(paths.values()),
        "coverage": dict(sorted(coverage.items()))}


def _ancestors(cwd: str) -> list[str]:
    path = PurePosixPath(cwd)
    return [p.as_posix() for p in (path, *path.parents)]


def _join(parent: str, entry: str) -> str:
    return (PurePosixPath(parent) / entry).as_posix()


def _key(path: str) -> str:
    return path.casefold() if os.name == "nt" else path


def _scope(row: dict, report: dict) -> str:
    return row["repository"] or ("." if report["versioning"]["root_versioned"] else row["path"])


def _bytes(root: Path, path: str, contents: dict[str, bytes]) -> bytes | None:
    return contents[path] if path in contents else read_bytes(root, path)


def _entry(directory: str, rule: dict, root: Path, contents: dict) -> str:
    for name in rule.get("precedence", []):
        candidate = _join(directory, name)
        data = _bytes(root, candidate, contents)
        if data and data.strip():
            return candidate
    return _join(directory, rule["entry"])


def _loaded(target: str, row: dict, rule: dict, report: dict, root: Path,
    contents: dict[str, bytes]) -> bool | None:
    ancestors = _ancestors(row["path"])
    scope = rule["scope"]
    if scope == "unknown":
        return None
    if scope == "git-root-to-cwd":
        if report["versioning"]["root_versioned"] is None and not row["repository"]:
            return None
        boundary = _scope(row, report)
        dirs = ancestors[:ancestors.index(boundary) + 1]
        entries = [_entry(directory, rule, root, contents) for directory in dirs]
        return _key(target) in {_key(p) for p in entries}
    if scope == "ancestors":
        entries = [_join(p, name) for p in ancestors
            for name in rule.get("launch_entries", [rule["entry"]])]
        if _key(target) in {_key(p) for p in entries}:
            return True
        if rule.get("imports") == "launch":
            if any(has_import(report, _key(p), _key(target)) for p in entries):
                return True
        fallback = rule.get("agents_fallback")
        if fallback and PurePosixPath(target).name == fallback["entry"]:
            # Version, settings, excluded files, and ancestors outside the
            # inventory are unknown. Do not claim a fallback was loaded.
            if any(_bytes(root, p, contents) is not None for p in entries):
                return False
            return None
        return False
    if scope == "repository":
        if not row["repository"] and not report["versioning"]["root_versioned"]:
            return None
        boundary = _scope(row, report)
        if "rule_directory" in rule:
            prefix = _join(boundary, rule["rule_directory"]) + "/"
            data = _bytes(root, target, contents)
            frontmatter = data.decode("utf-8-sig").split("---", 2) if data else []
            return _key(target).startswith(_key(prefix)) and target.endswith(
                rule["rule_suffix"]) and bool(
                    len(frontmatter) == 3 and not frontmatter[0].strip()
                    and rule["required_frontmatter"] in [line.strip()
                        for line in frontmatter[1].splitlines()])
        return _key(target) == _key(_join(boundary, rule["entry"]))
    return None


def _recommendation(row: dict, rule: dict, report: dict, root: Path, contents: dict) -> str:
    directory = _scope(row, report) if rule["scope"] in {"git-root-to-cwd",
        "repository"} else row["path"]
    try:
        return _entry(directory, rule, root, contents)
    except InstallError:
        pass
    return _join(directory, rule["entry"])


def annotate_placement(plan, sessions, window: Window | None = None,
    coverage: dict | None = None) -> None:
    """Attach counts and warnings; never relocate or apply a target implicitly."""
    rules = load_rules()
    starts = observed_starts(plan.root, plan.report, sessions, window)
    if window:
        starts["window"] = {"since": window.since.isoformat(), "until": window.until.isoformat()}
    if coverage is not None:
        starts["adapter_coverage"] = coverage
        for agent, measured in coverage.items():
            if measured.get("status") == "unavailable" or any(measured.get(k) for k in
                ("broken_lines", "unreadable_files", "invalid_timestamps",
                    "unknown_record_types")):
                plan.report["warnings"].append({"kind": "start-coverage-incomplete",
                    "agent": agent})
    plan.report["observed_starts"] = starts
    if any(starts["coverage"].get(k) for k in ("worker_start_unknown", "gitignore_unknown_cwd")):
        plan.report["warnings"].append({"kind": "start-coverage-incomplete"})
    plan.report["load_rules_version"] = rules["version"]
    plan.report["load_rules_revision"] = rules["revision"]
    by_agent = {r["agent"]: r for r in rules["agents"]}
    contents = {c.path: c.after for c in plan.changes}
    # Imports introduced by this plan also participate in launch guidance.
    report = dict(plan.report)
    report["claude_imports"] = list(report["claude_imports"])
    for change in plan.changes:
        if change.path.endswith("CLAUDE.md"):
            added = change.after[len(change.before or b""):].decode("utf-8-sig")
            for line in _without_code(added).splitlines():
                if line.startswith("@"):
                    target = posixpath.normpath(
                        posixpath.join(str(PurePosixPath(change.path).parent), line[1:]))
                    if target in contents:
                        report["claude_imports"].append({"source": change.path, "path": target,
                            "status": "resolved"})
    report["claude_imports"] = [{**item, "source": _key(item["source"]),
        **({"path": _key(item["path"])} if "path" in item else {})}
        for item in report["claude_imports"]]
    instruction_targets = [c.path for c in plan.changes if PurePosixPath(c.path).name in
        {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "GEMINI.md",
            "copilot-instructions.md"}
        or "/.cursor/rules/" in "/" + c.path]
    for agent in sorted({r["agent"] for r in starts["paths"]}):
        rule = by_agent.get(agent)
        rows = [r for r in starts["paths"] if r["agent"] == agent]
        total = sum(r["count"] for r in rows)
        if rule is None or rule["support"] == "unverified":
            plan.report["warnings"].append({"kind": "load-rules-unverified", "agent": agent,
                "starts": total})
            continue
        for target in instruction_targets:
            states = []
            for row in rows:
                try:
                    loaded = _loaded(target, row, rule, report, plan.root, contents)
                except (InstallError, UnicodeError):
                    loaded = None
                states.append((loaded, row))
            missed = sum(r["count"] for loaded, r in states if loaded is False)
            unknown = sum(r["count"] for loaded, r in states if loaded is None)
            if missed > total / 2:
                recommendations = Counter()
                for loaded, row in states:
                    if loaded is not False:
                        continue
                    recommendations[_recommendation(row, rule, report, plan.root,
                        contents)] += row["count"]
                plan.report["warnings"].append({"kind": "target-not-loaded", "agent": agent,
                    "target": target, "starts": total, "missed_starts": missed,
                    "unknown_starts": unknown, "support": rule["support"],
                    "recommended_paths": [{"path": p, "starts": n} for p, n in
                        sorted(recommendations.items(),
                            key=lambda item: (-item[1], item[0]))]})
            elif unknown:
                plan.report["warnings"].append({"kind": "target-load-unknown", "agent": agent,
                    "target": target, "starts": total, "unknown_starts": unknown})


def _count_starts(root: Path, sessions, window, repositories, patterns,
    counts, coverage, roles, agent_roles, provenance) -> None:
    base = normalize_path(str(root.resolve()))
    seen = set()
    ignored = _IgnoreChecks(root, repositories)
    git_identity = _git_identity(root)
    worktree_checks, identities = {}, {}
    for session in sessions:
        identity = session.agent, session.raw_id
        if identity in seen:
            coverage["duplicate_sessions"] += 1
            continue
        seen.add(identity)
        role = "worker" if session.parent_raw_id or getattr(session, "is_worker",
            False) else "top-level"
        dated = [e for e in session.cwd_events if e[0] is not None]
        when = getattr(session, "start_at", None) or min((t for t, _ in dated), default=None)
        if window is not None and not window.contains(when):
            coverage["outside_window"] += 1
            continue
        found, cwd = _start_cwd(session, dated, coverage)
        if not found:
            continue
        path = normalize_path(cwd)
        same_worktree = False
        start_root, start_ignored = root, ignored
        if path != base and not path.startswith(base + "/"):
            if path not in identities:
                identities[path] = _git_identity(Path(cwd)) if git_identity else None
            candidate = identities[path]
            if (candidate is None or candidate[1] != git_identity[1]
                or candidate[0] == git_identity[0]):
                coverage["outside_workspace"] += 1
                continue
            same_worktree = True
            start_root = candidate[0]
            if start_root not in worktree_checks:
                worktree_checks[start_root] = _IgnoreChecks(start_root, [])
            start_ignored = worktree_checks[start_root]
        try:
            relative = Path(os.path.normpath(cwd)).relative_to(start_root).as_posix()
        except ValueError:
            coverage["unsafe_cwd"] += 1
            continue
        if relative != "." and excluded_by(relative, patterns):
            coverage["excluded_cwd"] += 1
            continue
        try:
            directory = start_root if relative == "." else safe_path(start_root, relative)
            if not directory.is_dir():
                coverage["missing_or_non_directory_cwd"] += 1
                continue
            excluded = start_ignored.check(relative)
        except (InstallError, OSError):
            coverage["unsafe_cwd"] += 1
            continue
        if excluded is not False:
            coverage["excluded_cwd" if excluded else "gitignore_unknown_cwd"] += 1
            continue
        roles[role] += 1
        agent_roles[session.agent, role] += 1
        evidence = getattr(session, "start_evidence", None)
        if role == "worker" and (evidence not in {"session-header", "first-observed-cwd"}
            or not getattr(session, "start_cwd", None)):
            coverage["worker_start_unknown"] += 1
            continue
        evidence = evidence or "first-observed-cwd"
        provenance[evidence] += 1
        scope = None if same_worktree else next((p
            for p in sorted(repositories, key=len, reverse=True)
            if path == normalize_path(str(root / p))
            or path.startswith(normalize_path(str(root / p)) + "/")), None)
        if scope:
            relative = scope + relative[len(scope):]
        kind = ("same-repository-worktree" if same_worktree else "workspace-root"
            if relative == "." else "nested-repository" if scope else "subfolder")
        counts[session.agent, relative, kind, scope, role, evidence] += 1


def _start_cwd(session, dated, coverage):
    cwd = getattr(session, "start_cwd", None)
    if not cwd:
        events = list(session.cwd_events)
        if dated:
            first = min(e[0] for e in dated)
            candidates = {p for t, p in dated if t == first}
        else:
            candidates = {p for _, p in events}
        if len(candidates) != 1:
            coverage["missing_or_ambiguous_cwd"] += 1
            return False, None
        cwd = next(iter(candidates))
        coverage["first_observed_cwd"] += 1
    return True, cwd
