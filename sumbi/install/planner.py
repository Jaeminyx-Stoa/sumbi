"""Build zero-context additive diffs without exporting existing file contents."""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import hashlib
import json
from pathlib import Path
import re

from sumbi.catalog import VERSION, load_catalog
from .errors import InstallError
from .gaps import find_gaps, has_import, shared_target
from .files import MAX_BYTES, read_bytes
from .inventory import inventory
from .exclusions import GitIgnore, excluded_by

# Match the existing managed-block grammar without reading arbitrary comments.
MARKER = re.compile(r"^<!-- sumbi:(begin|end) ([a-z][a-z0-9-]*) -->$")


def managed_blocks(data: bytes) -> dict[str, str]:
    """Validate balanced, non-nested, unique blocks without editing them."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise InstallError("Managed targets must be UTF-8 text.") from None
    blocks: dict[str, str] = {}
    opened = None
    collected: list[str] = []
    for line in text.splitlines():
        match = MARKER.fullmatch(line)
        if "<!-- sumbi:" in line and not match:
            raise InstallError("Malformed managed block marker.")
        if match:
            action, identifier = match.groups()
            if action == "begin":
                if opened is not None or identifier in blocks:
                    raise InstallError("Nested or duplicate managed block.")
                opened, collected = identifier, []
            elif opened != identifier:
                raise InstallError("Unmatched managed block marker.")
            else:
                blocks[identifier] = "\n".join(collected)
                opened = None
        elif opened is not None:
            collected.append(line)
    if opened is not None:
        raise InstallError("Unterminated managed block.")
    return blocks


def block(identifier: str, text: str, newline: str = "\n") -> bytes:
    return (f"<!-- sumbi:begin {identifier} -->\n{text.rstrip()}\n"
            f"<!-- sumbi:end {identifier} -->\n").replace("\n", newline).encode("utf-8")


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


@dataclass(frozen=True)
class Change:
    path: str
    before: bytes | None = field(repr=False)
    after: bytes = field(repr=False)

    def diff(self) -> str:
        # Display portable text diffs; application still uses the original bytes.
        old = (self.before or b"").decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True)
        new = self.after.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True)
        return "".join(difflib.unified_diff(old, new,
                       fromfile=f"a/{self.path}" if self.before is not None else "/dev/null",
                       tofile=f"b/{self.path}", n=0))


@dataclass(frozen=True)
class Plan:
    root: Path = field(repr=False)
    report: dict
    gaps: list[dict]
    practices: list[dict]
    changes: list[Change]
    notes: list[dict]
    exclude: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"catalog_version": VERSION, "inventory": self.report,
                "gaps": self.gaps, "practices": self.practices,
                "changes": [{"path": c.path, "before_hash": digest(c.before),
                             "after_hash": digest(c.after), "diff": c.diff()} for c in self.changes],
                "notes": self.notes}


class _Targets:
    """Share prospective-target checks and additive bytes across practices."""

    def __init__(self, root: Path, report: dict):
        self.root, self.report = root, report
        self.patterns = tuple(entry["pattern"] for entry in report["exclusions"]["patterns"])
        self.git_contexts = {}
        self.ignore_results = {}
        self.before, self.after, self.notes = {}, {}, []

    def ignored(self, target: str) -> bool:
        if target in self.ignore_results:
            return self.ignore_results[target]
        repositories = self.report["versioning"]["nested_repositories"]["paths"]
        scope = next((p for p in sorted(repositories, key=len, reverse=True)
                      if target.startswith(p + "/")), "")
        if scope not in self.git_contexts:
            self.git_contexts[scope] = GitIgnore(self.root / scope)
        context = self.git_contexts[scope]
        ignored = context.check(target[len(scope) + 1:] if scope else target)
        warning = {"kind": "gitignore-unavailable"}
        if not context.available and warning not in self.report["warnings"]:
            self.report["warnings"].append(warning)
        self.ignore_results[target] = ignored
        return ignored

    def excluded(self, target: str) -> bool:
        return bool(excluded_by(target, self.patterns))

    def append(self, target: str, identifier: str, text: str) -> bool:
        if target not in self.before:
            self.before[target] = read_bytes(self.root, target)
            self.after[target] = self.before[target] or b""
        updated = _append_block(self.after[target], identifier, text, target, self.notes)
        if updated is None:
            return False
        self.after[target] = updated
        return True


def build_plan(repository: Path | str = ".", *, budget: int = 2000,
               select: list[str] | None = None,
               exclude: list[str] | tuple[str, ...] = ()) -> Plan:
    root = Path(repository).resolve()
    report = inventory(root, budget, exclude=exclude)
    targets = _Targets(root, report)
    gaps, catalog = find_gaps(report), load_catalog()
    ids = {p["id"] for p in catalog}
    if select is not None and (not select or not set(select).issubset(ids)):
        raise InstallError("Selection must contain known catalog IDs.")
    candidates = {p for gap in gaps for p in gap["candidates"]}
    if "instruction-map" in candidates and report["instructions"]["claude"]["count"]:
        candidates.add("shared-instructions")
    if select is not None:
        candidates.intersection_update(select)
    proposals, ignored_docs = [], []
    for practice in catalog:
        if practice["id"] in candidates:
            proposal, ignored = _plan_practice(practice, targets)
            if ignored:
                ignored_docs.append(practice["id"])
            if proposal:
                proposals.append(proposal)
    if ignored_docs:
        report["warnings"].append({"kind": "practice-docs-ignored", "practice_ids": ignored_docs})
    if targets.ignored(".sumbi/"):
        report["warnings"].append({"kind": "local-install-metadata",
            "message": "The .sumbi/ interventions ledger and backups are local by design; ignored metadata is supported."})
    changes = [Change(path, targets.before[path], data) for path, data in sorted(targets.after.items())
               if data != (targets.before[path] or b"")]
    return Plan(root, report, gaps, proposals, changes, targets.notes, tuple(exclude))


def _plan_practice(practice: dict, targets: _Targets) -> tuple[dict | None, bool]:
    identifier, report = practice["id"], targets.report
    language = report["practice_language"]
    ignored_docs = {item["path"] for item in practice["files"]
                    if item["path"].startswith("docs/sumbi/") and targets.ignored(item["path"])}
    unavailable_docs = ignored_docs | {item["path"] for item in practice["files"]
                                       if item["path"].startswith("docs/sumbi/") and targets.excluded(item["path"])}
    rendered = []
    for item in practice["files"]:
        relative = item["path"]
        paths = report["instructions"]["claude"]["paths"] if identifier == "shared-instructions" else [relative]
        for target in paths:
            if targets.excluded(target) or target in ignored_docs or targets.ignored(target):
                targets.notes.append({"id": identifier, "status": "excluded-target-preserved"})
                continue
            texts = item["text_without_links"] if unavailable_docs and "text_without_links" in item else item["text"]
            text = texts[language]
            if identifier == "shared-instructions":
                text = _shared_text(target, targets)
                if text is None:
                    continue
            if targets.append(target, identifier, text):
                rendered.append({"path": target, "mode": item["mode"], "text": text})
    if not rendered:
        return None, bool(ignored_docs)
    payload = {**practice, "language": language, "files": rendered}
    content_hash = digest(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return {"id": identifier, "risk": practice["risk"], "language": language,
            "files": sorted({item["path"] for item in rendered}), "content_hash": content_hash,
            "prediction": practice["prediction"], "judgment": practice["judgment"],
            "provenance": practice["sources"]}, bool(ignored_docs)


def _shared_text(target: str, targets: _Targets) -> str | None:
    report = targets.report
    agent_paths = report["instructions"]["agents"]["paths"] + (["AGENTS.md"] if "AGENTS.md" in targets.after else [])
    shared = shared_target(target, agent_paths)
    if shared not in agent_paths or targets.excluded(shared) or targets.ignored(shared):
        raise InstallError("Shared instructions require an existing or selected AGENTS.md.")
    if has_import(report, target, shared):
        return None
    depth = len(Path(target).parts) - len(Path(shared).parts)
    return "@" + "../" * depth + "AGENTS.md"


def _append_block(data: bytes, identifier: str, text: str, target: str, notes: list[dict]) -> bytes | None:
    existing = managed_blocks(data)
    if identifier in existing:
        if existing[identifier] != text.rstrip():
            notes.append({"id": identifier, "path": target, "status": "existing-block-preserved"})
        return None
    if data and not data.endswith(b"\n"):
        raise InstallError("A managed target lacks a final newline; add it manually before planning.")
    newline = "\r\n" if b"\r\n" in data else "\n"
    separator = newline.encode() if data and not data.endswith((newline * 2).encode()) else b""
    updated = data + separator + block(identifier, text, newline)
    if len(updated) > MAX_BYTES:
        raise InstallError("A planned target exceeds the bounded file size.")
    return updated
