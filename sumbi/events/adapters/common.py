"""Record translation helpers, independent of accounting and windows."""

import hashlib
import json

from sumbi.core.records import last_records
from sumbi.core.values import timestamp
from sumbi.events.schema import Identity


def digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True,
        separators=(",", ":")).encode()).digest()


def record_identity(record):
    return Identity(digest(record))


def key(value):
    return str(value) if value is not None else None


def latest(home, path):
    """Return the timestamp of the file's last record that has one, or None.

    Native streams append records as they happen, so this bounds the times a file
    holds; the builder still requires a margin and an old modification time.
    """
    for event in last_records(path):
        if (when := timestamp(event.get("timestamp"))) is not None:
            return when
    return None
