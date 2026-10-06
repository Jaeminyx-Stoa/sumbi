"""Offline regression fixtures for workspace, git and Markdown boundaries."""

import os
from pathlib import Path
import shutil
import subprocess
from support import IsolatedTemporaryDirectory
from unittest.mock import patch
import unittest

from sumbi.install import apply_plan, build_plan, find_gaps, inventory
from sumbi.install.errors import InstallError
from .test_inventory import OfflineTest


class InventoryBoundaryTests(OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "workspace"
        self.root.mkdir()
        environment = patch.dict(os.environ, {
            "GIT_CEILING_DIRECTORIES": str(self.root.parent),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        })
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def git(self, root, *arguments):
        subprocess.run(["git", "-C", str(root), *arguments], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @unittest.skipUnless(shutil.which("git"), "Local git is unavailable.")
    def test_non_git_workspace_prunes_scratch_and_lists_nested_repositories(self):
        self.write("AGENTS.md", "Root instructions.\n")
        self.write(".tmp/x/orig/AGENTS.md", "Copied rules.\n" * 1000)
        self.write(".tmp/x/app/package.json", '{"scripts":{"test":"check","lint":"check","format":"check"}}')
        for name in ("app-a", "app-b"):
            (self.root / name).mkdir()
            self.git(self.root / name, "init", "--quiet")
        self.write("app-c/source.txt", "Ordinary source.\n")
        report = inventory(self.root, budget=100)
        self.assertEqual(report["instructions"]["agents"], {"count": 1, "paths": ["AGENTS.md"]})
        self.assertNotIn("instruction-budget", {g["id"] for g in find_gaps(report)})
        self.assertEqual(report["enforcement"]["test_sources"], [])
        self.assertEqual(report["enforcement"]["lint"]["count"], 0)
        self.assertEqual(report["enforcement"]["format"]["count"], 0)
        self.assertIn({"kind": "root-not-versioned"}, report["warnings"])
        self.assertEqual(report["versioning"], {
            "root_versioned": False,
            "nested_repositories": {"count": 2, "paths": ["app-a", "app-b"]},
        })
        self.assertEqual(next(e["count"] for e in report["exclusions"]["patterns"]
                              if e["pattern"] == "**/.tmp"), 1)

    def test_tmp_remains_a_source_directory(self):
        self.write("tmp/AGENTS.md", "Source instructions.\n")
        self.assertEqual(inventory(self.root)["instructions"]["agents"]["paths"], ["tmp/AGENTS.md"])

    @unittest.skipUnless(shutil.which("git"), "Local git is unavailable.")
    def test_inventory_and_plan_disable_repository_fsmonitor(self):
        self.git(self.root, "init", "--quiet")
        self.write("tracked.txt", "Synthetic source.\n")
        self.git(self.root, "add", "tracked.txt")
        # Git for Windows also runs shebang hooks through its bundled shell.
        hook = self.root / ".git/fsmonitor-hook"
        hook.write_bytes(b"#!/bin/sh\nprintf marker > fsmonitor-marker\n")
        hook.chmod(0o755)
        self.git(self.root, "config", "core.fsmonitor", hook.as_posix())
        marker = self.root / "fsmonitor-marker"
        self.git(self.root, "ls-files", "--others", "--exclude-standard")
        if not marker.exists():
            self.skipTest("Local git cannot run the synthetic fsmonitor hook.")
        marker.unlink()

        report = inventory(self.root)
        self.assertTrue(report["versioning"]["root_versioned"])
        self.assertNotIn({"kind": "gitignore-unavailable"}, report["warnings"])
        self.assertFalse(marker.exists())
        plan = build_plan(self.root, select=["instruction-map"])
        self.assertTrue(plan.changes)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which("git"), "Local git is unavailable.")
    def test_git_ignored_files_are_pruned_before_reading(self):
        self.git(self.root, "init", "--quiet")
        self.write(".gitignore", "scratch/\n")
        self.write("scratch/AGENTS.md", "Ignored instructions.\n")
        self.write("scratch/package.json", '{"scripts":{"test":"check","lint":"check","format":"check"}}')
        self.write("CLAUDE.md", "@scratch/AGENTS.md\n")
        original_walk, original_read = os.walk, Path.read_bytes

        def walk(*args, **kwargs):
            for directory, dirs, files in original_walk(*args, **kwargs):
                self.assertFalse(Path(directory).is_relative_to(self.root / "scratch"))
                yield directory, dirs, files

        def read(path):
            self.assertFalse(path.is_relative_to(self.root / "scratch"))
            return original_read(path)

        with patch("sumbi.install.inventory.walk.os.walk", walk), patch.object(Path, "read_bytes", read):
            report = inventory(self.root)
        self.assertEqual(report["instructions"]["agents"]["count"], 0)
        self.assertEqual(report["enforcement"]["test_sources"], [])
        self.assertEqual(report["enforcement"]["lint"]["count"], 0)
        self.assertEqual(report["enforcement"]["format"]["count"], 0)
        self.assertEqual(report["exclusions"]["gitignore_count"], 1)
        self.assertEqual(report["claude_imports"], [{"source": "CLAUDE.md", "status": "excluded-not-read"}])
        self.assertEqual(report["warnings"], [])

    @unittest.skipUnless(shutil.which("git"), "Local git is unavailable.")
    def test_standard_excludes_subdirectory_root_and_tracked_files(self):
        self.git(self.root, "init", "--quiet")
        self.write(".git/info/exclude", "info-output/\n")
        self.write(".git/global-ignore", "global-output/\n")
        self.git(self.root, "config", "core.excludesFile", str(self.root / ".git/global-ignore"))
        self.write("source/.gitignore", "local-output/\ntracked/\n")
        for name in ("info-output", "global-output", "local-output", "tracked"):
            self.write(f"source/{name}/AGENTS.md", "Scoped rules.\n")
        self.git(self.root, "add", "--force", "source/tracked/AGENTS.md")
        report = inventory(self.root / "source")
        self.assertTrue(report["versioning"]["root_versioned"])
        self.assertEqual(report["instructions"]["agents"]["paths"], ["tracked/AGENTS.md"])
        self.assertEqual(report["exclusions"]["gitignore_count"], 3)

    def test_missing_git_falls_back_with_a_sanitized_warning(self):
        self.write(".gitignore", "scratch/\n")
        self.write("scratch/AGENTS.md", "Fallback instructions.\n")
        with patch("sumbi.install.exclusions.subprocess.run", side_effect=FileNotFoundError):
            report = inventory(self.root)
        self.assertEqual(report["instructions"]["agents"]["paths"], ["scratch/AGENTS.md"])
        self.assertEqual(report["exclusions"]["gitignore_count"], 0)
        self.assertEqual(report["warnings"], [{"kind": "gitignore-unavailable"}])

    @unittest.skipUnless(shutil.which("git"), "Local git is unavailable.")
    def test_failed_git_ignore_query_falls_back(self):
        self.git(self.root, "init", "--quiet")
        self.write(".gitignore", "scratch/\n")
        self.write("scratch/AGENTS.md", "Fallback rules.\n")
        original_run = subprocess.run

        def run(arguments, **kwargs):
            if "ls-files" in arguments:
                return subprocess.CompletedProcess(arguments, 128, b"", b"Synthetic failure")
            return original_run(arguments, **kwargs)

        with patch("sumbi.install.exclusions.subprocess.run", run):
            report = inventory(self.root)
        self.assertEqual(report["instructions"]["agents"]["paths"], ["scratch/AGENTS.md"])
        self.assertIn({"kind": "gitignore-unavailable"}, report["warnings"])

    @unittest.skipUnless(shutil.which("git"), "Local git is unavailable.")
    def test_ignored_absent_targets_are_not_planned_and_changes_block_apply(self):
        self.git(self.root, "init", "--quiet")
        self.write(".gitignore", "AGENTS.md\ndocs/\n.sumbi/\n")
        plan = build_plan(self.root, select=["instruction-map", "handoff"])
        self.assertEqual(plan.changes, [])
        self.write(".gitignore", ".sumbi/\n")
        plan = build_plan(self.root, select=["instruction-map"])
        self.assertTrue(plan.changes)
        self.write(".gitignore", "AGENTS.md\n.sumbi/\n")
        with self.assertRaises(InstallError):
            apply_plan(plan)
        self.assertFalse((self.root / "AGENTS.md").exists())

    @unittest.skipUnless(shutil.which("git"), "Local git is unavailable.")
    def test_nested_repository_ignores_are_used_under_non_git_root(self):
        self.write("app/.gitignore", "scratch/\n")
        self.git(self.root / "app", "init", "--quiet")
        self.write("app/scratch/AGENTS.md", "Ignored rules.\n")
        self.write("app/CLAUDE.md", "@scratch/AGENTS.md\n")
        report = inventory(self.root)
        self.assertEqual(report["instructions"]["agents"]["count"], 0)
        self.assertEqual(report["exclusions"]["gitignore_count"], 1)
        self.assertEqual(report["claude_imports"][0]["status"], "excluded-not-read")

    def test_imports_ignore_code_regions_and_keep_real_import(self):
        self.write("AGENTS.md", "Shared instructions.\n")
        self.write("CLAUDE.md", """```typescript
@/           -> src/
@components/ -> src/components/
```
Inline `@/util/cn.ts` and ``@hidden.md `literal` ``.
~~~ aliases
@tilde.md
~~~~
@AGENTS.md
```unterminated
@missing.md
""")
        self.assertEqual(inventory(self.root)["claude_imports"], [
            {"source": "CLAUDE.md", "path": "AGENTS.md", "status": "resolved"},
        ])

    def test_multiline_spans_and_fence_lengths(self):
        self.write("AGENTS.md", "Shared rules.\n")
        self.write("CLAUDE.md", "````any-info\n@hidden.md\n```\n@still-hidden.md\n`````\n"
                   "`multiline\n@span.md`\n@AGENTS.md\n~~~open\n@hidden.md\n")
        self.assertEqual(inventory(self.root)["claude_imports"], [
            {"source": "CLAUDE.md", "path": "AGENTS.md", "status": "resolved"},
        ])
