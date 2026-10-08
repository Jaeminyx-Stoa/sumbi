"""Read-only scope checks for local edit operands; paths never enter reports."""

import ntpath
import os
from pathlib import Path
import stat
import subprocess

from sumbi.core.paths import normalize_path
from sumbi.outcomes.worker_github.links import start_repo_cwd


class EditScope:
    """Share origin probes with worker starts and cache ignore results per target."""

    def __init__(self, attributor, repos):
        self.attributor, self.repos = attributor, repos
        self.cache = {}

    def target(self, value):
        if value is None:
            return "edit_target_unknown"
        if value not in self.cache:
            try:
                self.cache[value] = self._target(value)
            except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
                self.cache[value] = "edit_target_unknown"
        return self.cache[value]

    def _target(self, value):
        if (value.startswith("//") or os.name != "nt" and ntpath.splitdrive(value)[0]
            or not Path(value).is_absolute()):
            return "edit_target_unknown"
        target = Path(value).resolve()
        directory = target.parent
        while True:
            try:
                mode = directory.stat().st_mode
                if not stat.S_ISDIR(mode):
                    return "edit_target_unknown"
                break
            except FileNotFoundError:
                if directory == directory.parent:
                    return "edit_target_unknown"
                directory = directory.parent
        cwd = str(directory)
        repo = start_repo_cwd(cwd, self.attributor, self.repos)
        query = self.attributor.origin_queries.get(normalize_path(cwd), "unknown")
        if not repo:
            return ("edit_outside_scope" if query in ("outside", "checkout")
                else "edit_target_unknown")
        result = subprocess.run(["git", "-C", cwd, "check-ignore", "--quiet", "--", str(target)],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
        return {0: "edit_ignored", 1: "in_scope"}.get(result.returncode, "edit_target_unknown")

    def changes(self, session, lifetime, gaps):
        """Count coverage once per edit/category, including multi-target patches."""
        changed, known = False, False
        for identity, at in session.edits.items():
            if not lifetime.contains(at):
                continue
            states = {self.target(target) for target in session.edit_targets.get(identity, (None,))}
            known |= "in_scope" in states
            changed |= bool(states & {"in_scope", "edit_target_unknown"})
            gaps.update(state for state in states if state != "in_scope")
        return changed, known
