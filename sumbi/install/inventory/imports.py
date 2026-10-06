"""Resolve bounded, repository-local Claude import closures in discovery order."""

from pathlib import PurePosixPath
import re

from ..files import _without_code
from ..exclusions import excluded_by
from .context import InventoryContext


# Imports occupy token boundaries; email addresses do not match.
CLAUDE_IMPORT = re.compile(r"(?<![\w@])@(?:\"([^\"\n]+)\"|([^\s`<>]+))")


def scan(context: InventoryContext) -> list[dict]:
    paths, patterns, ignored = context.paths, context.patterns, context.ignored
    instructions, content = context.instructions, context.content
    imports, visited = [], set()
    def follow(source: str) -> None:
        if source in visited:
            return
        visited.add(source)
        # Imports must occupy a token boundary; email addresses are not imports.
        for match in CLAUDE_IMPORT.finditer(_without_code(content(source))):
            target = (match.group(1) or match.group(2)).rstrip(",;)")
            # Only local relative references are inspected; no home expansion.
            if target.startswith(("/", "~")) or "\\" in target or ":" in target:
                imports.append({"source": source, "status": "external-not-read"})
                continue
            candidate = PurePosixPath(source).parent / target
            parts: list[str] = []
            escaped = False
            for component in candidate.parts:
                if component == "..":
                    if not parts:
                        escaped = True
                        break
                    parts.pop()
                elif component != ".":
                    parts.append(component)
            relative = "/".join(parts)
            if escaped or not relative:
                imports.append({"source": source, "status": "external-not-read"})
                continue
            if excluded_by(relative, patterns) or any(p.as_posix() in ignored
                for p in (PurePosixPath(relative), *PurePosixPath(relative).parents)):
                imports.append({"source": source, "status": "excluded-not-read"})
                continue
            status = "resolved" if relative in paths else "missing-or-link"
            imports.append({"source": source, "path": relative, "status": status})
            if status == "resolved":
                follow(relative)
    for path in instructions["claude"]:
        follow(path)

    context.imports = imports
    return imports
