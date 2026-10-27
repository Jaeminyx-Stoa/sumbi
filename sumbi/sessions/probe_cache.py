"""Local cache of per-file window probes, keyed by path, size and modification time.

`collect_window` probes every log file for its latest time and session keys before
it decides which files to read. Those probes depend only on the file's bytes, so an
unchanged file (same size and nanosecond modification time) reuses them. The cache
holds local paths and native session IDs, so it is written privately and never
leaves the machine; a corrupt or foreign cache file is ignored.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import tempfile

VERSION = 1


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
        if (isinstance(raw, dict) and raw.get("size") == stat.st_size
            and raw.get("mtime_ns") == stat.st_mtime_ns):
            self.hits += 1
            entry = {}
            if "latest" in raw:
                entry["latest"] = (datetime.fromisoformat(raw["latest"])
                    if raw["latest"] is not None else None)
            if "keys" in raw:
                entry["keys"] = (frozenset(raw["keys"][0]), frozenset(raw["keys"][1]))
            return entry
        self.misses += 1
        return {}

    def put(self, path: Path, stat, entry: dict):
        raw = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        if "latest" in entry:
            raw["latest"] = entry["latest"].isoformat() if entry["latest"] else None
        if "keys" in entry:
            raw["keys"] = [sorted(entry["keys"][0]), sorted(entry["keys"][1])]
        self.used[str(path)] = raw

    def save(self):
        """Keep only files seen this run; write atomically and privately."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
