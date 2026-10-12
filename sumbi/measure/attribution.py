"""Candidate project and repository attribution."""

from dataclasses import dataclass, field
import fnmatch
import ntpath
import os
from pathlib import Path
import re
import stat
import subprocess
from sumbi.core.paths import normalize_origin, normalize_path
from sumbi.core.privacy import pseudonym


@dataclass
class ProjectRule:
    name: str
    origins: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.name):
            raise ValueError("Project rule names must be bounded labels")


class Attributor:
    """Git origins are candidates, never proof of deliverable ownership."""

    def __init__(self, rules: list[ProjectRule]):
        self.rules = rules
        self.cache: dict[str, tuple[str | None, str]] = {}
        self.roots: dict[str, str] = {}
        # Definitive `git rev-parse --show-toplevel` answers per real directory:
        # (git's own toplevel string or None, as git printed it). See _toplevel.
        self.toplevels: dict[str, str | None] = {}
        # Local query health is separate from the stable attribution labels.
        self.origin_queries: dict[str, str] = {}
        # `.git` identity of a directory when its origin was read directly.
        self.origin_fingerprints: dict[str, object] = {}
        # Origin reads that failed transiently (timeout, OS error, unexpected exit):
        # cached for that path as before, but never shared with subdirectories.
        self.origin_failures: set[str] = set()

    def repository_path(self, value: str) -> str:
        """Resolve an existing file/subdirectory to its local repository root."""
        normalized = normalize_path(value)
        if normalized in self.roots:
            return self.roots[normalized]
        root = normalized
        path = Path(value)
        # Do not interpret foreign Windows paths as relative paths on POSIX.
        if not normalized.startswith("//") and (os.name == "nt" or not ntpath.splitdrive(value)[0]):
            while not path.exists() and path != path.parent:
                path = path.parent
            directory = path if path.is_dir() else path.parent
            if directory.is_dir():
                top = self._toplevel(str(directory))
                if top:
                    root = normalize_path(top)
        self.roots[normalized] = root
        return root

    def _toplevel(self, directory: str) -> str | None:
        """Repository root for a directory, as git printed it (not normalized).

        An answer is reused only while the filesystem proves it still holds: no
        `.git` entry between the directory and the root, and one at the root (or,
        for "not a repository", none on the whole chain). A timeout, an OS error
        or any other failure is never cached, so the next lookup asks git again.
        """
        key = self._real(directory)
        if key is not None and key in self.toplevels:
            known, fingerprint = self.toplevels[key]
            if self._unchanged(key, known, fingerprint):
                return known
            del self.toplevels[key]
        top, definitive = None, False
        try:
            result = subprocess.run(["git", "-C", directory, "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
            if result.returncode == 0 and result.stdout.strip():
                top, definitive = result.stdout.strip(), True
            elif result.returncode == 128 and result.stderr.strip() == self.NOT_A_REPOSITORY:
                definitive = True
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            pass
        if definitive and key is not None:
            fingerprint = self._git_entry(top) if top else None
            # An unprovable root (or a race with the query) is simply not cached.
            if not (top and fingerprint in (None, False)):
                self.toplevels[key] = (top, fingerprint)
        return top

    NOT_A_REPOSITORY = "fatal: not a git repository (or any of the parent directories): .git"

    @staticmethod
    def _real(directory: str) -> str | None:
        try:
            return os.path.realpath(directory)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _git_entry(directory: str):
        """Identity of `directory/.git`: None if absent, False if unprovable.

        A directory is identified by its inode and its config file; a `.git` file
        by its content and the gitdir it names, which must exist and be readable.
        """
        path = os.path.join(directory, ".git")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return False
        try:
            mode = os.stat(path).st_mode
            if stat.S_ISDIR(mode):
                git_dir, content = path, None
            elif not stat.S_ISREG(mode):
                # Never open a FIFO, socket or device: reading could block forever.
                return False
            else:
                with open(path, "rb") as handle:
                    content = handle.read(4096)
                text = content.decode("utf-8")
                if not text.startswith("gitdir:"):
                    return False
                git_dir = os.path.normpath(os.path.join(directory, text[7:].strip()))
            if not os.path.isdir(git_dir) or not os.access(git_dir, os.R_OK | os.X_OK):
                return False
            os.stat(os.path.join(git_dir, "HEAD"))
            common = git_dir
            link = os.path.join(git_dir, "commondir")
            if os.path.isfile(link):
                with open(link, "rb") as handle:
                    common = os.path.normpath(os.path.join(
                        git_dir, handle.read(4096).decode("utf-8").strip()))
            try:
                config = os.stat(os.path.join(common, "config"))
                config_id = (config.st_ino, config.st_dev, config.st_size, config.st_mtime_ns)
            except FileNotFoundError:
                config_id = None
            target = os.stat(git_dir)
            return (info.st_ino, info.st_dev, content, target.st_ino, target.st_dev,
                config_id)
        except (OSError, ValueError, UnicodeError):
            return False

    @staticmethod
    def _is_bare(directory: str) -> bool:
        """Git's own marks of a bare repository: HEAD, objects and refs together."""
        return (os.path.isfile(os.path.join(directory, "HEAD"))
            and os.path.isdir(os.path.join(directory, "objects"))
            and os.path.isdir(os.path.join(directory, "refs")))

    @classmethod
    def _unchanged(cls, real: str, top: str | None, fingerprint=None) -> bool:
        """Cheap filesystem proof that git would still give the cached answer.

        Between the directory and the root nothing may look like a repository
        (a `.git` entry of any kind, or bare-repository marks); the root's
        `.git` must be the same one that was cached; for "not a repository" the
        whole chain, up to a Git discovery ceiling, must be free of repositories.
        """
        wanted = normalize_path(top) if top else None
        ceilings = {normalize_path(c) for c in os.environ.get(
            "GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if c}
        current = real
        while True:
            if wanted is not None and normalize_path(current) == wanted:
                entry = cls._git_entry(current)
                return entry is not None and entry is not False and entry == fingerprint
            if cls._git_entry(current) is not None or cls._is_bare(current):
                return False
            parent = os.path.dirname(current)
            if parent == current or normalize_path(parent) in ceilings:
                return wanted is None
            current = parent

    def _known_toplevel(self, directory: str) -> str | None:
        """A cached, still proven repository root; never starts a process."""
        key = self._real(directory)
        known = self.toplevels.get(key) if key is not None else None
        if known and known[0] and self._unchanged(key, *known):
            return known[0]
        return None

    def event_link(self, path: str) -> dict:
        root = self.repository_path(path)
        # Scope against the observed operand as well as its repository root.
        link = self.link(path)
        root_link = self.link(root) if root != normalize_path(path) else link
        if root_link["bucket"] == "unassigned" or root_link["evidence"] in (
            "git_origin_out_of_scope", "git_origin_candidate"):
            return root_link
        if link["bucket"] != "project" and root_link["bucket"] == "project":
            link = root_link
        if root != normalize_path(path):
            link = {**link, "project_key": root_link["project_key"]}
        return link

    def origin(self, cwd: str) -> tuple[str | None, str]:
        normalized = normalize_path(cwd)
        if normalized in self.cache:
            return self.cache[normalized]
        origin, state, query = None, "path_only", "unknown"
        try:
            if not normalized.startswith("//") and (os.name == "nt"
                or not ntpath.splitdrive(cwd)[0]) and Path(cwd).is_dir():
                toplevel = self._known_toplevel(cwd)
                if toplevel is not None and normalize_path(toplevel) != normalized:
                    # A proven work-tree subdirectory reads its repository's config.
                    # The root goes to git as printed, not as normalized for keys.
                    root_key = normalize_path(toplevel)
                    if self.origin_fingerprints.get(root_key) != self._git_entry(toplevel):
                        # The root was recreated or repointed since its origin was read.
                        self.cache.pop(root_key, None)
                        self.origin_queries.pop(root_key, None)
                    shared = self.origin(toplevel)
                    if root_key not in self.origin_failures:
                        self.cache[normalized] = shared
                        self.origin_queries[normalized] = self.origin_queries[root_key]
                        return shared
                    # A transient failure at the root is never shared; ask for this path.
                self.origin_fingerprints[normalized] = self._git_entry(cwd)
                options = dict(
                    capture_output=True, text=True, timeout=5,
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
                )
                repo = subprocess.run(["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
                    **options)
                if repo.returncode == 0 and repo.stdout.strip() == "true":
                    result = subprocess.run(["git", "-C", cwd, "config", "--get",
                        "remote.origin.url"], **options)
                    if result.returncode == 0 and result.stdout.strip():
                        origin = normalize_origin(result.stdout)
                        state = "origin" if origin else "unsupported_origin"
                        query = "checkout" if origin else "unknown"
                    elif result.returncode not in (0, 1):
                        self.origin_failures.add(normalized)
                elif repo.returncode == 128 and repo.stderr.strip() == (
                    "fatal: not a git repository (or any of the parent directories): .git"):
                    query = "outside"
                else:
                    self.origin_failures.add(normalized)
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            state = "origin_unavailable"
            self.origin_failures.add(normalized)
        self.cache[normalized] = origin, state
        self.origin_queries[normalized] = query
        return origin, state

    def link(self, cwd: str) -> dict:
        origin, state = self.origin(cwd)
        path = normalize_path(cwd)
        matches = []
        evidence = "path_pattern"
        if state in ("unsupported_origin", "origin_unavailable") and any(r.origins
            for r in self.rules):
            return {"project_key": pseudonym("project", path), "rule": None,
                "evidence": state, "bucket": "unassigned"}
        if origin:
            matches = [r.name for r in self.rules if any(
                fnmatch.fnmatchcase(origin, normalize_origin(p) or p) for p in r.origins)]
            evidence = "git_origin_candidate"
            if not matches and any(r.origins for r in self.rules):
                return {"project_key": pseudonym("project", origin), "rule": None,
                    "evidence": "git_origin_out_of_scope", "bucket": "other"}
        if not matches:
            matches = [r.name for r in self.rules if any(
                fnmatch.fnmatchcase(path, normalize_path(p)) for p in r.paths)]
            evidence = "path_pattern" if state == "path_only" else "path_pattern_" + state
        key = pseudonym("project", origin or path)
        if len(set(matches)) > 1:
            return {"project_key": key, "rule": None, "evidence": "ambiguous_rules",
                "bucket": "unassigned"}
        if not self.rules:
            return {"project_key": key, "rule": None, "evidence": "git_origin_candidate"
                if origin else "cwd_candidate",
                "bucket": "project"}
        return {"project_key": key, "rule": matches[0] if matches else None,
            "evidence": evidence if matches else "unmatched_path", "bucket": "project"
            if matches else "other"}

    def session_link(self, paths: set[str]) -> dict:
        links = [self.link(path) for path in sorted(paths)]
        if not links:
            return {"bucket": "unassigned", "project_key": None, "rule": None,
                "evidence": "missing_cwd", "links": []}
        signatures = {(x["project_key"], x["rule"], x["bucket"]) for x in links}
        if len(signatures) > 1:
            return {"bucket": "unassigned", "project_key": None, "rule": None,
                "evidence": "multiple_projects", "links": links}
        return {**links[0], "links": links}


class RepositoryAttributor(Attributor):
    """Exact repository scope, consolidating subdirectories and local clones."""

    def __init__(self, repository: Path):
        super().__init__([])
        self.path = normalize_path(str(repository.resolve()))
        self.repository_origin, _ = self.origin(str(repository.resolve()))

    def link(self, cwd: str) -> dict:
        origin, state = self.origin(cwd)
        path = normalize_path(cwd)
        matched = path == self.path or path.startswith(self.path + "/")
        evidence = "path_pattern"
        bucket = "project" if matched else "other"
        if self.repository_origin and origin:
            matched = origin == self.repository_origin
            bucket = "project" if matched else "other"
            evidence = "git_origin_candidate" if matched else "git_origin_out_of_scope"
        elif self.repository_origin and state in ("unsupported_origin", "origin_unavailable"):
            matched, bucket, evidence = False, "unassigned", state
        identity = (self.repository_origin or self.path) if matched else (origin or path)
        return {"project_key": pseudonym("project", identity),
            "rule": "repository" if matched else None, "evidence": evidence, "bucket": bucket}
