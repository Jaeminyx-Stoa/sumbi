"""Git discovery must stop inside the worktree without changing its metadata."""

import os
from pathlib import Path
import subprocess
import unittest

from support import IsolatedTemporaryDirectory
from sumbi.outcomes.github.live import outside_repository


class IsolationTests(unittest.TestCase):
    def test_real_github_guard_still_sees_enclosing_checkout_metadata(self):
        parent = Path(__file__).resolve().parents[2] / "local/isolation-test"
        parent.mkdir(parents=True, exist_ok=True)
        with IsolatedTemporaryDirectory(dir=parent) as temporary:
            with self.assertRaisesRegex(ValueError, "outside Git repositories"):
                outside_repository(Path(temporary) / "cache")

    def test_unversioned_root_inside_worktree_cannot_find_parent_git(self):
        parent = Path(__file__).resolve().parents[2] / "local/isolation-test"
        parent.mkdir(parents=True, exist_ok=True)
        with IsolatedTemporaryDirectory(dir=parent) as temporary:
            root = Path(temporary)
            child = root / "nested"
            child.mkdir()
            self.assertIn(str(root.resolve()), os.environ["GIT_CEILING_DIRECTORIES"].split(os.pathsep))
            for cwd in (root, child):
                result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)

    def test_local_git_repository_is_visible_below_ceiling(self):
        with IsolatedTemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repository"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
            child = repo / "nested"
            child.mkdir()
            result = subprocess.run(["git", "-C", str(child), "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(Path(result.stdout.strip()).resolve(), repo.resolve())

    def test_nested_roots_restore_the_environment(self):
        before = os.environ.get("GIT_CEILING_DIRECTORIES")
        with IsolatedTemporaryDirectory() as outer:
            outer_ceiling = os.environ["GIT_CEILING_DIRECTORIES"]
            with IsolatedTemporaryDirectory() as inner:
                self.assertIn(str(Path(inner).resolve()), os.environ["GIT_CEILING_DIRECTORIES"].split(os.pathsep))
                self.assertIn(str(Path(outer).resolve()), os.environ["GIT_CEILING_DIRECTORIES"].split(os.pathsep))
            self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], outer_ceiling)
        self.assertEqual(os.environ.get("GIT_CEILING_DIRECTORIES"), before)
