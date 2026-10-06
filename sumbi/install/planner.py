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


def build_plan(repository: Path | str = ".", *, budget: int = 2000,
               select: list[str] | None = None,
               exclude: list[str] | tuple[str, ...] = ()) -> Plan:
    root = Path(repository).resolve()
    report = inventory(root, budget, exclude=exclude)
    git_contexts = {}

    def ignored_target(target: str) -> bool:
        repositories = report["versioning"]["nested_repositories"]["paths"]
        scope = next((p for p in sorted(repositories, key=len, reverse=True)
                      if target.startswith(p + "/")), "")
        if scope not in git_contexts:
            git_contexts[scope] = GitIgnore(root / scope)
        context = git_contexts[scope]
        ignored = context.check(target[len(scope) + 1:] if scope else target)
        if not context.available and {"kind": "gitignore-unavailable"} not in report["warnings"]:
            report["warnings"].append({"kind": "gitignore-unavailable"})
        return ignored
    patterns = tuple(entry["pattern"] for entry in report["exclusions"]["patterns"])
    gaps = find_gaps(report)
    catalog = load_catalog()
    ids = {p["id"] for p in catalog}
    if select is not None and (not select or not set(select).issubset(ids)):
        raise InstallError("Selection must contain known catalog IDs.")
    candidates = {p for gap in gaps for p in gap["candidates"]}
    # Sharing becomes necessary when the plan seeds AGENTS.md for Claude.
    if "instruction-map" in candidates and report["instructions"]["claude"]["count"]:
        candidates.add("shared-instructions")
    if select is not None:
        candidates.intersection_update(select)
    before: dict[str, bytes | None] = {}
    after: dict[str, bytes] = {}
    proposals, notes = [], []
    for practice in catalog:
        if practice["id"] not in candidates:
            continue
        identifier = practice["id"]
        changed = []
        for item in practice["files"]:
            relative = item["path"]
            # Nested Claude entry files import the root instructions relatively.
            targets = [relative]
            if identifier == "shared-instructions":
                targets = report["instructions"]["claude"]["paths"]
            for target in targets:
                if excluded_by(target, patterns) or ignored_target(target):
                    notes.append({"id": identifier, "status": "excluded-target-preserved"})
                    continue
                if target not in before:
                    before[target] = read_bytes(root, target)
                    after[target] = before[target] or b""
                data = after[target]
                text = item["text"]
                if identifier == "shared-instructions":
                    agent_paths = report["instructions"]["agents"]["paths"] + (["AGENTS.md"] if "AGENTS.md" in after else [])
                    shared = shared_target(target, agent_paths)
                    if shared not in agent_paths or excluded_by(shared, patterns) or ignored_target(shared):
                        raise InstallError("Shared instructions require an existing or selected AGENTS.md.")
                    depth = len(Path(target).parts) - len(Path(shared).parts)
                    text = "@" + "../" * depth + "AGENTS.md"
                    if has_import(report, target, shared):
                        continue
                existing = managed_blocks(data)
                if identifier in existing:
                    if existing[identifier] != text.rstrip():
                        notes.append({"id": identifier, "path": target, "status": "existing-block-preserved"})
                    continue
                if data and not data.endswith(b"\n"):
                    raise InstallError("A managed target lacks a final newline; add it manually before planning.")
                newline = "\r\n" if b"\r\n" in data else "\n"
                separator = newline.encode() if data and not data.endswith((newline * 2).encode()) else b""
                after[target] = data + separator + block(identifier, text, newline)
                if len(after[target]) > MAX_BYTES:
                    raise InstallError("A planned target exceeds the bounded file size.")
                changed.append(target)
        if changed:
            payload = json.dumps(practice, sort_keys=True, separators=(",", ":")).encode()
            proposals.append({"id": identifier, "risk": practice["risk"],
                              "files": sorted(set(changed)), "content_hash": digest(payload),
                              "prediction": practice["prediction"], "judgment": practice["judgment"],
                              "provenance": practice["sources"]})
    changes = [Change(path, before[path], data) for path, data in sorted(after.items()) if data != (before[path] or b"")]
    return Plan(root, report, gaps, proposals, changes, notes, tuple(exclude))
