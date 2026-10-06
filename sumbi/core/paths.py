"""Portable local paths and repository origins."""

import ntpath
import os
import re
from urllib.parse import unquote, urlsplit


def normalize_path(value: str) -> str:
    value = value.replace("\\", "/")
    if ntpath.splitdrive(value)[0] or value.startswith("//"):
        return ntpath.normpath(value).replace("\\", "/").casefold().rstrip("/")
    return os.path.normpath(value).replace("\\", "/").rstrip("/")


def execution_cwd(value: object) -> str | None:
    """Normalize plain cwd or a strict local Windows file URI, never a remote URI."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    if value.lower().startswith("file:"):
        try:
            uri = urlsplit(value)
            if (uri.netloc or "?" in value or "#" in value or "\\" in value
                    or re.search(r"[\x00-\x1f\x7f]", value)
                    or not re.match(r"^/[A-Za-z]:/", uri.path)
                    or re.search(r"%(?![0-9A-Fa-f]{2})", uri.path)
                    or re.search(r"%(?:2f|5c)", uri.path, re.I)):
                return None
            value = unquote(uri.path, errors="strict")[1:]
            if re.search(r"[\x00-\x1f\x7f]", value):
                return None
        except (ValueError, UnicodeError):
            return None
    elif "://" in value:
        return None
    return normalize_path(value)


def normalize_origin(value: str) -> str | None:
    """Discard transport, credentials and .git; preserve repository path case."""
    value = value.strip()
    if "://" not in value:
        match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", value)
        if not match:
            return None
        host, path = match.groups()
    else:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ("https", "http", "ssh", "git") or not parsed.hostname:
                return None
            host, path = parsed.hostname, parsed.path
            if parsed.port and parsed.port not in (22, 80, 443, 9418):
                host += ":" + str(parsed.port)
        except ValueError:
            return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return host.casefold() + "/" + path if path else None
