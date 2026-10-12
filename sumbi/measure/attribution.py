"""Candidate project and repository attribution."""

from dataclasses import dataclass, field
import fnmatch
import ntpath
import os
from pathlib import Path
import re
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
            known = self.toplevels[key]
            if self._unchanged(key, known):
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
            self.toplevels[key] = top
        return top

    NOT_A_REPOSITORY = "fatal: not a git repository (or any of the parent directories): .git"

    @staticmethod
    def _real(directory: str) -> str | None:
        try:
            return os.path.realpath(directory)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _unchanged(real: str, top: str | None) -> bool:
        """Cheap filesystem proof that git would still give the cached answer."""
        wanted = normalize_path(top) if top else None
        current = real
        while True:
            if wanted is not None and normalize_path(current) == wanted:
                return os.path.lexists(os.path.join(current, ".git"))
            if os.path.lexists(os.path.join(current, ".git")):
                return False
            parent = os.path.dirname(current)
            if parent == current:
                return wanted is None
            current = parent

    def _known_toplevel(self, directory: str) -> str | None:
        """A cached, still proven repository root; never starts a process."""
        key = self._real(directory)
        top = self.toplevels.get(key) if key is not None else None
        return top if top and self._unchanged(key, top) else None

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
                    shared = self.origin(toplevel)
                    self.cache[normalized] = shared
                    self.origin_queries[normalized] = self.origin_queries[
                        normalize_path(toplevel)]
                    return shared
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
                elif repo.returncode == 128 and repo.stderr.strip() == (
                    "fatal: not a git repository (or any of the parent directories): .git"):
                    query = "outside"
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            state = "origin_unavailable"
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
