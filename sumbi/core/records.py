"""JSONL reading and coverage counts."""

from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path
from sumbi.core.values import label


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


def records(path: Path, coverage: Coverage, *, ignore_blank: bool = False):
    """Continue after broken JSON, invalid UTF-8 and non-object records."""
    coverage.files_scanned += 1
    try:
        with path.open("rb") as stream:
            for raw in stream:
                coverage.lines_read += 1
                if ignore_blank and not raw.strip():
                    continue
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
