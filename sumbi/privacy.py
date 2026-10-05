"""Local pseudonym keys; secrets never enter reports or process arguments."""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import os
from pathlib import Path

_UNSET = object()
_KEY = ContextVar("sumbi_pseudonym_key", default=_UNSET)


def read_salt(salt_file: Path | None = None) -> bytes | None:
    """A file takes precedence over SUMBI_SALT; file bytes are used verbatim."""
    value = os.environ.get("SUMBI_SALT")
    key = salt_file.read_bytes() if salt_file is not None else (
        value.encode("utf-8", errors="surrogatepass") if value is not None else None)
    if key == b"":
        raise ValueError("Pseudonym salt must not be empty")
    return key


def current_key() -> bytes | None:
    key = _KEY.get()
    return read_salt() if key is _UNSET else key


@contextmanager
def pseudonym_key(key: bytes | None):
    if key == b"":
        raise ValueError("Pseudonym salt must not be empty")
    token = _KEY.set(key)
    try:
        yield
    finally:
        _KEY.reset(token)


def pseudonym(category: str, value: str) -> str:
    """Keep legacy SHA-256 IDs unless a local key selects HMAC-SHA256."""
    data = value.encode(errors="surrogatepass")
    key = current_key()
    digest = hmac.new(key, data, hashlib.sha256) if key is not None else hashlib.sha256(data)
    return category + "_" + digest.hexdigest()[:24]
