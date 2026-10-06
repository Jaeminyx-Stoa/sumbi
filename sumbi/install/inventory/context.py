"""Shared scan inputs, cached readers and explicit section data."""

from dataclasses import dataclass, field
import configparser
import json
from pathlib import Path
import tomllib

from ..errors import InstallError
from ..files import read_bytes
from ..exclusions import GitIgnore


def entry(paths: list[str]) -> dict:
    return {"count": len(paths), "paths": sorted(paths)}


@dataclass
class InventoryContext:
    root: Path
    patterns: tuple[str, ...]
    budget: int
    paths: list[str] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)
    git_contexts: dict[Path, GitIgnore] = field(default_factory=dict)
    ignored: set[str] = field(default_factory=set)
    nested_repositories: list[str] = field(default_factory=list)
    instructions: dict[str, list[str]] = field(default_factory=dict)
    conditional_rules: list[str] = field(default_factory=list)
    imports: list[dict] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    skill_descriptions: list[dict] = field(default_factory=list)
    prompt_hooks: list[dict] = field(default_factory=list)

    def content(self, path: str) -> str:
        if path not in self.texts:
            try:
                raw = read_bytes(self.root, path)
                self.texts[path] = (raw or b"").decode("utf-8-sig").replace("\r\n",
                    "\n").replace("\r", "\n")
            except (InstallError, UnicodeError, OSError):
                self.warnings.append({"kind": "unreadable-or-oversized", "path": path})
                self.texts[path] = ""
        return self.texts[path]

    def structured(self, path: str, toml: bool = False) -> dict:
        try:
            value = tomllib.loads(self.content(path)) if toml else json.loads(self.content(path))
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
        self.warnings.append({"kind": "invalid-config", "path": path})
        return {}

    def ini(self, path: str) -> configparser.RawConfigParser:
        parser = configparser.RawConfigParser()
        try:
            parser.read_string(self.content(path))
        except configparser.Error:
            self.warnings.append({"kind": "invalid-config", "path": path})
        return parser
