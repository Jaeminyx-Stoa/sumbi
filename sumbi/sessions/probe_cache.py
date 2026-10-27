"""Local cache of per-file latest-time scans, keyed by path, inode, size and mtime.

`collect_window` scans every pre-window log file for its latest time before it
decides which files to read. That scan depends only on the file's bytes, so an
unchanged file (same inode, size and nanosecond modification time) reuses it.
Session keys are not cached: they read only a first record and, for Claude Code
subagents, a sidecar file. The cache holds local paths, so it is written privately
and never leaves the machine; a corrupt entry or file is ignored.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import tempfile

VERSION = 2


class ProbeCache:
    def __init__(self, directory: Path, name: str):
        self.path = Path(directory) / f"probes-{name}.json"
        self.entries, self.used, self.hits, self.misses = {}, {}, 0, 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == VERSION and isinstance(
                data.get("entries"), dict):
                self.entries = data["entries"]
        except (OSError, ValueError, RecursionError):
            pass

    def get(self, path: Path, stat) -> dict:
        """A mutable probe entry for this file version (empty when not cached)."""
        raw = self.entries.get(str(path))
        try:
            if (isinstance(raw, dict) and raw.get("size") == stat.st_size
                and raw.get("mtime_ns") == stat.st_mtime_ns and raw.get("ino") == stat.st_ino
                and "latest" in raw):
                latest = raw["latest"]
                entry = {"latest": None if latest is None else datetime.fromisoformat(latest)}
                if entry["latest"] is not None and entry["latest"].tzinfo is None:
                    raise ValueError
                self.hits += 1
                return entry
        except (TypeError, ValueError):
            pass
        self.misses += 1
        return {}

    def put(self, path: Path, stat, entry: dict):
        if "latest" not in entry:
            return
        latest = entry["latest"]
        self.used[str(path)] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "ino": stat.st_ino, "latest": None if latest is None else latest.isoformat()}

    def save(self):
        """Keep only files seen this run; write atomically and privately."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                prefix=".sumbi-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": VERSION, "entries": self.used}, stream)
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
