"""Discover paths with exclusions, link safety and nested git-ignore contexts."""

import os
from pathlib import Path

from ..exclusions import GitIgnore, excluded_by
from .context import InventoryContext


def scan(context: InventoryContext) -> dict:
    root, patterns = context.root, context.patterns
    found, warnings = [], context.warnings
    counts = dict.fromkeys(patterns, 0)
    git = GitIgnore(root)
    contexts = context.git_contexts
    contexts[root] = git
    nested, ignored = [], set(git.ignored) if git.available else set()
    ignored_count = 0
    if git.versioned is False:
        warnings.append({"kind": "root-not-versioned"})
    def failed(_error: OSError) -> None:
        warnings.append({"kind": "unreadable-directory"})
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        base = Path(directory)
        context_root = next(p for p in (base, *base.parents) if p in contexts)
        git_context = contexts[context_root]
        marker = base / ".git"
        if base != root and (".git" in dirs
            or ".git" in files) and not marker.is_symlink() and not getattr(marker,
                "is_junction", lambda: False)():
            candidate = GitIgnore(base)
            contexts[base] = candidate
            context_root, git_context = base, candidate
            if candidate.available:
                ignored.update((base.relative_to(root) / p).as_posix()
                    for p in candidate.ignored)
            if candidate.versioned:
                nested.append(base.relative_to(root).as_posix())
        kept = []
        for name in sorted(dirs):
            path = base / name
            pattern = excluded_by(path.relative_to(root).as_posix(), patterns)
            if pattern:
                counts[pattern] += 1
                continue
            if git_context.excludes(path.relative_to(context_root).as_posix()):
                ignored_count += 1
                continue
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                warnings.append({"kind": "skipped-link", "path": path.relative_to(root).as_posix()})
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            pattern = excluded_by(relative, patterns)
            if pattern:
                counts[pattern] += 1
                continue
            if git_context.excludes(path.relative_to(context_root).as_posix()):
                ignored_count += 1
                continue
            if path.is_symlink():
                warnings.append({"kind": "skipped-link", "path": relative})
            else:
                found.append(relative)
    if any(not context.available for context in contexts.values()):
        warnings.append({"kind": "gitignore-unavailable"})
    context.paths = sorted(found)
    context.ignored = ignored
    context.nested_repositories = nested
    return {
        "count": sum(counts.values()) + ignored_count,
        "gitignore_count": ignored_count,
        "patterns": [{"pattern": p, "count": count} for p, count in counts.items()],
    }
