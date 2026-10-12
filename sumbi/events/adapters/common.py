"""Record translation helpers, independent of accounting and windows."""

import hashlib
import json
import re

from sumbi.core.values import epoch, timestamp
from sumbi.events.schema import Identity

_VALUE = rb'\s*:\s*("(?:[^"\\]|\\.)*"|[^\s,}\]]+)'
TIME_FIELDS = {"timestamp": (b'"timestamp"', re.compile(rb'"timestamp"' + _VALUE)),
    "started_at": (b'"started_at', re.compile(rb'"started_at(_ms)?"' + _VALUE)),
    "completed_at": (b'"completed_at', re.compile(rb'"completed_at(_ms)?"' + _VALUE))}
FIELD_NAMES = {"timestamp": {"timestamp"}, "started_at": {"started_at", "started_at_ms"},
    "completed_at": {"completed_at", "completed_at_ms"}}
# A name spelled with \u escapes holds no quote, so it is one quote-free run before
# a colon; the possessive run keeps the scan linear on long escaped strings.
_KEY_RUN = re.compile(rb'"([^"]*+)"\s*:')


class Unprovable(ValueError):
    """A time-bearing field whose value a raw scan cannot decode."""


def digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True,
        separators=(",", ":")).encode()).digest()


def record_identity(record):
    return Identity(digest(record))


def key(value):
    return str(value) if value is not None else None


def latest_time(path, fields):
    """Return the latest time any named field supplies anywhere in the file, or None.

    The raw scan matches the field names at every nesting depth, so it can only
    over-report a translator's times; values decode exactly as the translator
    decodes them. Raises Unprovable for an undecodable scalar or a named field
    spelled with \\u escapes, which the literal match cannot see, and OSError when
    the file cannot be read. Callers must then read the file in full.
    """
    best = None
    names = set().union(*(FIELD_NAMES[name] for name in fields))
    with path.open("rb") as stream:
        for raw in stream:
            if b"\\u" in raw and _escaped_name(raw, names):
                raise Unprovable("escaped field name")
            for name in fields:
                literal, pattern = TIME_FIELDS[name]
                if literal not in raw:
                    continue
                for match in pattern.finditer(raw):
                    when = _field_time(name, match)
                    if when is not None and (best is None or when > best):
                        best = when
    return best


def _escaped_name(raw, names):
    """Whether any escaped key on this line decodes to one of `names`."""
    for match in _KEY_RUN.finditer(raw):
        run = match.group(1)
        if b"\\u" not in run:
            continue
        try:
            if json.loads(b'"' + run + b'"') in names:
                return True
        except (ValueError, UnicodeDecodeError):
            continue
    return False


def _field_time(name, match):
    text = match.group(match.lastindex)
    if text[:1] in (b"{", b"["):
        return None
    try:
        value = json.loads(text)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Unprovable(name) from None
    if name == "timestamp":
        return timestamp(value)
    return epoch(value, milliseconds=match.group(1) is not None)
