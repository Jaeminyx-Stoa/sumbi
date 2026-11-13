"""Small local cache transactions: nonblocking locks and atomic JSON writes."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class CollectorBusy(RuntimeError):
    """Another collector owns the cache or publication directory."""


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectorBusy("collector_busy") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".cache-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
