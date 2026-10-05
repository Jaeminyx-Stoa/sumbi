"""Repository-relative glob exclusions shared by reads and planned writes."""

from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path, PurePosixPath
import tomllib

from .errors import InstallError

DEFAULT_EXCLUDES = tuple(f"**/{name}" for name in (
    ".git", ".sumbi", "node_modules", "vendor", ".venv", "venv", "dist",
    "build", "__pycache__",
)) + tuple(f"**/{tests}/**/{data}" for tests in ("test", "tests")
          for data in ("fixtures", "testdata"))


def _validate(patterns: object) -> tuple[str, ...]:
    if not isinstance(patterns, (list, tuple)) or any(
        not isinstance(p, str) or not p or p.startswith("/") or "\\" in p
        or ":" in p or any(ord(c) < 32 for c in p)
        or any(part in {"", ".", ".."} for part in p.split("/"))
        for p in patterns
    ):
        raise InstallError("Exclusions must be a list of repository-relative glob strings.")
    return tuple(dict.fromkeys(patterns))


def load_excludes(root: Path, extra: list[str] | tuple[str, ...] = ()) -> tuple[str, ...]:
    # This single configuration file is read explicitly before pruning .sumbi.
    from .inventory import read_bytes

    raw = read_bytes(root, ".sumbi/config.toml")
    configured = ()
    if raw is not None:
        try:
            configured = _validate(tomllib.loads(raw.decode("utf-8-sig")).get("exclude", []))
        except (ValueError, UnicodeError):
            raise InstallError("Invalid install exclusion configuration.") from None
    return tuple(dict.fromkeys((*DEFAULT_EXCLUDES, *configured, *_validate(extra))))


def matches(relative: str, pattern: str) -> bool:
    """Match path segments; ** spans zero or more, * stays in one segment."""
    parts, glob = PurePosixPath(relative).parts, pattern.split("/")

    @lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if j == len(glob):
            return i == len(parts)
        if glob[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i].casefold(), glob[j].casefold()) and match(i + 1, j + 1)

    return match(0, 0)


def excluded_by(relative: str, patterns: tuple[str, ...]) -> str | None:
    """An excluded directory also excludes every descendant, including imports."""
    path = PurePosixPath(relative)
    ancestors = (path, *path.parents)
    return next((p for p in patterns if any(matches(a.as_posix(), p) for a in ancestors)), None)
