"""Bounded repository files and checked exclusive publication."""

from pathlib import Path, PurePosixPath
import os
import re
import tempfile
from .errors import InstallError


MAX_BYTES = 1_048_576


def checked_relative(relative: str) -> PurePosixPath:
    """Validate portable path grammar without consulting the filesystem."""
    part = PurePosixPath(relative)
    components = relative.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {prefix + str(n) for prefix in ("COM", "LPT") for n in range(1, 10)}
    if (not relative or part.is_absolute() or "\\" in relative or ":" in relative
            or any(p in {"", ".", ".."} or p.endswith((".", " "))
                   or p.split(".", 1)[0].upper() in reserved
                   or any(ord(c) < 32 or c in '<>"|?*' for c in p)
                   for p in components)):
        raise InstallError("Unsafe repository-relative path.")
    return part


def safe_path(root: Path, relative: str) -> Path:
    """Reject ambiguous paths, links and traversal, including dangling links."""
    part = checked_relative(relative)
    current = root
    for component in part.parts:
        current = current / component
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise InstallError("Repository links are not followed.")
    if not current.resolve().is_relative_to(root.resolve()):
        raise InstallError("Path escapes the repository.")
    return current


def read_bytes(root: Path, relative: str) -> bytes | None:
    path = safe_path(root, relative)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise InstallError("Expected a bounded regular file.")
    try:
        return path.read_bytes()
    except OSError:
        raise InstallError("Cannot read a repository file.") from None


def _without_code(text: str, *, inline: bool = True) -> str:
    """Remove fenced blocks and matching backtick spans before import parsing."""
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        if fence is not None:
            if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*(?:\n)?$", line):
                fence = None
            lines.append("\n")
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)", line)
        if opening:
            fence = (opening[1][0], len(opening[1]))
            lines.append("\n")
        else:
            lines.append(line)
    text = "".join(lines)
    if not inline:
        return text
    runs = list(re.finditer(r"`+", text))
    output, start, index = [], 0, 0
    while index < len(runs):
        opening = runs[index]
        closing = next((j for j in range(index + 1, len(runs))
                        if len(runs[j][0]) == len(opening[0])), None)
        if closing is None:
            index += 1
            continue
        output.append(text[start:opening.start()])
        output.append(" ")
        start = runs[closing].end()
        index = closing + 1
    output.append(text[start:])
    return "".join(output)


def _checked(root: Path, relative: str, expected: bytes | None) -> Path:
    path = safe_path(root, relative)
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise InstallError("Apply requires single-link regular files.")
    if read_bytes(root, relative) != expected:
        raise InstallError("Repository changed since planning; rebuild the plan.")
    return path


def _write(root: Path, relative: str, expected: bytes | None,
           data: bytes, mode: int) -> None:
    """Stage bytes in the destination directory, recheck, then replace."""
    path = _checked(root, relative, expected)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, relative)
    descriptor, temporary = tempfile.mkstemp(prefix=".sumbi-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        _checked(root, relative, expected)
        if expected is None:
            # A same-directory hard link publishes staged bytes exclusively.
            # Unlike replace, it cannot overwrite an unanticipated new file.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
