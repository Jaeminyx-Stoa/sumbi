"""Record translation helpers, independent of accounting and windows."""

import hashlib
import json

from sumbi.events.schema import Identity


def digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True,
        separators=(",", ":")).encode()).digest()


def record_identity(record):
    return Identity(digest(record))


def key(value):
    return str(value) if value is not None else None
