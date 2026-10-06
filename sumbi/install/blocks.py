"""Exact managed-block bytes and separators for conservative partial reversal."""

import re

from .errors import InstallError
from .planner import managed_blocks


IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*")


def _spans(data: bytes, identifier: str) -> tuple[int, int] | None:
    offset = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    data = data[offset:]
    token = identifier.encode("ascii")
    starts = list(re.finditer(rb"(?m)^<!-- sumbi:begin " + token + rb" -->\r?\n", data))
    ends = list(re.finditer(rb"(?m)^<!-- sumbi:end " + token + rb" -->\r?\n", data))
    if len(starts) != 1 or len(ends) != 1 or starts[0].end() > ends[0].start():
        return None
    return offset + starts[0].start(), offset + ends[0].end()


def added_blocks(before: bytes | None, after: bytes) -> list[dict]:
    """Capture only this apply's suffix; never claim pre-existing blocks."""
    original = before or b""
    if not after.startswith(original):
        raise InstallError("Managed additions must preserve original bytes.")
    suffix = after[len(original):]
    blocks, cursor = [], 0
    for identifier in managed_blocks(suffix):
        start, end = _spans(suffix, identifier)
        separator = suffix[cursor:start]
        if separator not in (b"", b"\n", b"\r\n"):
            raise InstallError("Unsupported managed-block separator.")
        blocks.append({"id": identifier, "bytes": suffix[start:end].decode("utf-8"),
                       "separator": separator.decode("ascii")})
        cursor = end
    if cursor != len(suffix):
        raise InstallError("Unsupported bytes after managed additions.")
    return blocks


def validate_blocks(entries) -> list[dict]:
    """Old manifests omit blocks and retain whole-file hash protection."""
    if not isinstance(entries, list):
        raise ValueError
    seen = set()
    for entry in entries:
        identifier = entry["id"]
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in seen:
            raise ValueError
        seen.add(identifier)
        if entry["separator"] not in ("", "\n", "\r\n") or not isinstance(entry["bytes"], str):
            raise ValueError
        data = entry["bytes"].encode("utf-8")
        try:
            if set(managed_blocks(data)) != {identifier} or _spans(data, identifier) != (0, len(data)):
                raise ValueError
        except InstallError:
            raise ValueError from None
    return entries


def remove_blocks(current: bytes, entries: list[dict], finished: set[str]) -> tuple[bytes, list[dict]]:
    results, data = [], current
    for entry in entries:
        identifier = entry["id"]
        if identifier in finished:
            results.append({"id": identifier, "status": "already-reverted"})
            continue
        span = _spans(data, identifier)
        if span is None or data[span[0]:span[1]] != entry["bytes"].encode("utf-8"):
            results.append({"id": identifier, "status": "refused", "reason": "block-content-changed"})
            continue
        start, end = span
        separator = entry["separator"].encode("ascii")
        # Remove at most one blank separator line, never a preceding text line's newline.
        separator_start = start - len(separator)
        if separator and data[:start].endswith(separator) and (
                separator_start == 0 or data[separator_start - 1:separator_start] == b"\n"):
            start = separator_start
        data = data[:start] + data[end:]
        results.append({"id": identifier, "status": "removed"})
    return data, results
