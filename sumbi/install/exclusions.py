"""Repository-relative glob exclusions shared by reads and planned writes."""

from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path, PurePosixPath
import os
import subprocess
import tomllib

from .errors import InstallError

DEFAULT_EXCLUDES = tuple(f"**/{name}" for name in (
    ".git", ".sumbi", "node_modules", "vendor", ".venv", "venv", "dist",
    "build", "__pycache__", ".tmp",
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


def local_git(root: Path, *arguments: str, data: bytes | None = None):
    """Run only a bounded local git query with repository programs disabled."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), "-c", "core.fsmonitor=false",
             "-c", "core.untrackedCache=false", *arguments], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"}, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


class GitIgnore:
    """Ask local git for standard ignore rules; never execute project code."""

    def __init__(self, root: Path):
        self.root = root
        self.available = True
        self.versioned = None
        self.ignored: set[str] = set()
        result = self._run("rev-parse", "--is-inside-work-tree")
        if result is None:
            return
        if result.returncode == 0 and result.stdout.strip() == b"true":
            self.versioned = True
            result = self._run("ls-files", "--others", "--ignored",
                               "--exclude-standard", "--directory", "-z")
            if result is not None and result.returncode == 0:
                self.ignored = {os.fsdecode(p).rstrip("/")
                                for p in result.stdout.split(b"\0") if p}
            else:
                self.available = False
        elif (result.returncode == 0 and result.stdout.strip() == b"false") or b"not a git repository" in result.stderr:
            self.versioned = False
        else:
            self.available = False

    def _run(self, *arguments: str, data: bytes | None = None):
        result = local_git(self.root, *arguments, data=data)
        if result is None:
            self.available = False
        return result

    def excludes(self, relative: str) -> bool:
        """Match discovered ignored roots without visiting their contents."""
        if not self.available:
            return False
        path = PurePosixPath(relative)
        return any(p.as_posix() in self.ignored for p in (path, *path.parents))

    def check(self, relative: str) -> bool:
        """Check prospective writes too, including files that do not exist."""
        if not self.available or not self.versioned:
            return False
        result = self._run("check-ignore", "--stdin", "-z",
                           data=os.fsencode(relative) + b"\0")
        if result is None or result.returncode not in (0, 1):
            self.available = False
            return False
        return result.returncode == 0
