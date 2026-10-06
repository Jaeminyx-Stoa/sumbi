"""Bounded values and UTC timestamps."""

from datetime import datetime, timezone
import math
import re
from sumbi.core.privacy import pseudonym


TOKEN_KINDS = ("new_input", "cache_write", "cache_read", "output", "reasoning_output")


def label(value: object, category: str = "label") -> str:
    """Allow bounded machine labels; fingerprint unsupported free-text shapes."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_<][A-Za-z0-9_.:<>-]{0,95}", value):
        return value
    return pseudonym(category, str(value))


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def epoch(value: object, *, milliseconds: bool = False) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        try:
            return datetime.fromtimestamp(value / (1000 if milliseconds else 1), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return timestamp(value)


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}
