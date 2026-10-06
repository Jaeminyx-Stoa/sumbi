"""Local-git synthetic sibling worktrees, with no session or repository data."""

import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from sumbi.install import inventory
from sumbi.install.placement import observed_starts, annotate_placement
from sumbi.install.planner import build_plan
from .test_inventory import OfflineTest
from .test_placement import session


@unittest.skipUnless(shutil.which("git"), "Local git is unavailable.")
class WorktreeStartTests(OfflineTest):
    def setUp(self):
        super().setUp()
        guard = patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        guard.start()
        self.addCleanup(guard.stop)
        self.root = self.copy_fixture("empty")
        self.git(self.root, "init", "--quiet")
        (self.root / "AGENTS.md").write_text("Synthetic guidance.\n", encoding="utf-8")
        self.git(self.root, "add", ".")
        self.git(self.root, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                 "commit", "--quiet", "-m", "Synthetic base")
        self.sibling = self.root.parent / "sibling"
        self.git(self.root, "worktree", "add", "--quiet", "--detach", str(self.sibling))

    def git(self, root, *arguments):
        return subprocess.run(["git", "-C", str(root), "-c", "core.fsmonitor=false",
                               "-c", "core.untrackedCache=false", *arguments], check=True,
                              capture_output=True, timeout=30)

    def test_same_repository_worktree_root_and_relative_subfolder(self):
        nested = self.sibling / "src"
        nested.mkdir()
        report = observed_starts(self.root, inventory(self.root), [
            session("codex", "root", self.root), session("codex", "sibling", self.sibling),
            session("codex", "nested", nested)])
        self.assertEqual(report["sessions"], 3)
        self.assertEqual(report["placement_sessions"], 3)
        self.assertNotIn("outside_workspace", report["coverage"])
        rows = [row for row in report["paths"] if row.get("scope") == "same-repository-worktree"]
        self.assertEqual({row["path"] for row in rows}, {".", "src"})
        self.assertTrue(all(row["count"] == 1 for row in rows))
        self.assertNotIn(str(self.sibling), json.dumps(report))
        self.assertNotIn("sibling", json.dumps(report))

    def test_unrelated_repository_is_outside_despite_similar_name(self):
        unrelated = self.root.parent / "repository-other"
        unrelated.mkdir()
        self.git(unrelated, "init", "--quiet")
        report = observed_starts(self.root, inventory(self.root), [session("codex", "other", unrelated)])
        self.assertEqual(report["sessions"], 0)
        self.assertEqual(report["coverage"]["outside_workspace"], 1)

    def test_sibling_ignore_rules_and_exclusions_still_withhold_paths(self):
        (self.sibling / ".gitignore").write_text("private-output/\n", encoding="utf-8")
        (self.sibling / "private-output").mkdir()
        (self.sibling / ".tmp").mkdir()
        report = observed_starts(self.root, inventory(self.root), [
            session("codex", "ignored", self.sibling / "private-output"),
            session("codex", "excluded", self.sibling / ".tmp")])
        self.assertEqual(report["sessions"], 0)
        self.assertEqual(report["coverage"]["excluded_cwd"], 2)
        self.assertNotIn("private-output", json.dumps(report))

    def test_worktree_starts_vote_in_repository_placement(self):
        plan = build_plan(self.root, select=["handoff"])
        annotate_placement(plan, [session("codex", "sibling", self.sibling)])
        self.assertEqual(plan.report["observed_starts"]["placement_sessions"], 1)
        self.assertFalse(any(w["kind"] == "target-not-loaded" for w in plan.report["warnings"]))

    def test_git_identity_queries_use_hardened_flags_and_fail_closed(self):
        report = inventory(self.root)
        original = subprocess.run
        calls = []

        def run(command, *args, **kwargs):
            if "--git-common-dir" in command:
                calls.append(command)
                self.assertIn("core.fsmonitor=false", command)
                self.assertIn("core.untrackedCache=false", command)
                if str(self.sibling) in command:
                    return subprocess.CompletedProcess(command, 1, b"", b"Synthetic failure")
            return original(command, *args, **kwargs)

        with patch("subprocess.run", side_effect=run):
            starts = observed_starts(self.root, report, [session("codex", "sibling", self.sibling)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(starts["coverage"]["outside_workspace"], 1)
