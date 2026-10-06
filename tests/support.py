"""Temporary test roots that cannot discover an enclosing Git worktree."""

import os
from pathlib import Path
import stat
import tempfile
from unittest.mock import patch


def require_file_modes(test, root, modes):
    """Skip POSIX mode assertions if this temporary filesystem lacks them."""
    descriptor, name = tempfile.mkstemp(prefix="mode-probe-", dir=root)
    os.close(descriptor)
    path = Path(name)
    try:
        for mode in modes:
            path.chmod(mode)
            if stat.S_IMODE(path.stat().st_mode) != mode:
                test.skipTest("The temporary filesystem does not preserve POSIX file modes.")
    finally:
        path.unlink()


class IsolatedTemporaryDirectory(tempfile.TemporaryDirectory):
    """Set a Git discovery ceiling for this root until explicit cleanup.

    Use as a context manager, or register cleanup with TestCase.addCleanup.
    Nested instances retain the outer ceilings and restore them in LIFO order.
    The parent is also a ceiling: Git does not apply the current directory's
    ceiling when a probe starts at the temporary root itself. Repositories
    explicitly initialized at or below the temporary root remain visible.
    """

    active_roots = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._previous_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        self._ceiling_active = True
        root = Path(self.name).resolve()
        self.active_roots.append(root)
        roots = [str(root), str(root.parent)]
        if self._previous_ceiling:
            roots.append(self._previous_ceiling)
        os.environ["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(roots)

    def cleanup(self):
        try:
            super().cleanup()
        finally:
            if self._ceiling_active:
                self.active_roots.remove(Path(self.name).resolve())
                if self._previous_ceiling is None:
                    os.environ.pop("GIT_CEILING_DIRECTORIES", None)
                else:
                    os.environ["GIT_CEILING_DIRECTORIES"] = self._previous_ceiling
                self._ceiling_active = False


def isolate_github_destinations(test):
    """Bound the mocked REST adapter's destination universe to test roots.

    The real checker scans .git files directly, without consulting Git ceilings.
    Exercise that checker with synthetic ancestry in adapter tests; do not hide
    any filesystem metadata or weaken checks within the test root. A separate
    isolation test exercises the checker against the enclosing real checkout.
    """
    from sumbi.github_outcomes import outside_repository

    def bounded(path):
        target = path.resolve()
        roots = [root for root in IsolatedTemporaryDirectory.active_roots
                 if target.is_relative_to(root)]
        if not roots:
            raise AssertionError("GitHub test destinations must stay in an active temporary root")
        root = max(roots, key=lambda value: len(value.parts))

        class FixtureDestination:
            parents = tuple(parent for parent in target.parents if parent.is_relative_to(root))

            def resolve(self):
                return self

            def __truediv__(self, relative):
                return target / relative

        outside_repository(FixtureDestination())
        return target

    guard = patch("sumbi.github_outcomes.outside_repository", side_effect=bounded)
    guard.start()
    test.addCleanup(guard.stop)
