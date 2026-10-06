"""Synthetic boundaries for inventory, import closures, scopes and application."""

import json
from pathlib import Path
from unittest.mock import patch

from sumbi.install import apply_plan, build_plan, inventory
from sumbi.install.errors import InstallError
from sumbi.install.exclusions import DEFAULT_EXCLUDES, matches
from .test_inventory import OfflineTest


class ExclusionTests(OfflineTest):
    def write(self, root, path, text="Excluded instructions.\n"):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_defaults_prune_fixture_dependency_and_generated_trees(self):
        root = self.copy_fixture("both")
        trees = ["tests/fixtures", "tests/install/fixtures", "test/testdata",
                 "packages/unit/tests/data/testdata", "node_modules", "vendor",
                 ".venv", "venv", "dist", "build", ".git", ".sumbi", "__pycache__"]
        for tree in trees:
            self.write(root, tree + "/AGENTS.md", "Leave a handoff document.\n" + "x" * 10000)
            self.write(root, tree + "/CLAUDE.md", "@../../AGENTS.md\n")
            self.write(root, tree + "/.claude/rules/check.md")
        self.write(root, "tests/helpers/AGENTS.md", "Owned test rules.\n")
        self.write(root, "src/AGENTS.md", "Owned code rules.\n")
        report = inventory(root)
        self.assertEqual(report["instructions"]["agents"]["paths"],
                         ["AGENTS.md", "src/AGENTS.md", "tests/helpers/AGENTS.md"])
        self.assertEqual(report["instructions"]["claude"]["paths"], ["CLAUDE.md"])
        self.assertEqual(report["conventions"]["handoff"], {"status": "absent", "evidence": []})
        self.assertEqual(report["cost"]["instructions"]["claude"]["estimated_tokens"], 25)
        self.assertEqual(report["exclusions"]["count"], len(trees))
        self.assertEqual(sum(e["count"] for e in report["exclusions"]["patterns"]), len(trees))
        self.assertNotIn("paths", report["exclusions"])
        self.assertEqual({e["pattern"] for e in report["exclusions"]["patterns"]}, set(DEFAULT_EXCLUDES))
        encoded = json.dumps(report)
        for tree in trees:
            self.assertNotIn(tree + "/AGENTS.md", encoded)
        self.assertFalse(any(c.path.startswith(tuple(tree + "/" for tree in trees))
                             for c in build_plan(root).changes))

    def test_fixture_names_outside_test_directories_are_owned(self):
        root = self.copy_fixture("empty")
        self.write(root, "src/fixtures/AGENTS.md")
        self.write(root, "contest/testdata/CLAUDE.md")
        report = inventory(root)
        self.assertEqual(report["instructions"]["agents"]["paths"], ["src/fixtures/AGENTS.md"])
        self.assertEqual(report["instructions"]["claude"]["paths"], ["contest/testdata/CLAUDE.md"])
        self.assertEqual(report["exclusions"]["count"], 0)

    def test_globs_are_relative_segment_based_and_descendant_pruning(self):
        for path, pattern, expected in (
            ("src/generated", "**/generated", True),
            ("generated", "**/generated", True),
            ("src/generated/file.md", "src/*", False),
            ("src/generated/file.md", "src/**", True),
            ("tests/fixtures", "**/tests/**/fixtures", True),
            ("pkg/tests/data/fixtures", "**/tests/**/fixtures", True),
            ("pkg/contest/fixtures", "**/test/**/fixtures", False),
            ("SRC/Generated", "src/generated", True),
        ):
            with self.subTest(path=path, pattern=pattern):
                self.assertEqual(matches(path, pattern), expected)
        root = self.copy_fixture("both")
        self.write(root, "private/deep/AGENTS.md")
        self.assertEqual(inventory(root, exclude=["private"])["instructions"]["agents"]["paths"], ["AGENTS.md"])

    def test_config_and_extra_globs_are_additive(self):
        root = self.copy_fixture("both")
        self.write(root, ".sumbi/config.toml", 'exclude = ["scratch/**", "**/generated"]\n')
        for tree in ("scratch", "src/generated", "samples", "tests/fixtures"):
            self.write(root, tree + "/AGENTS.md")
        report = inventory(root, exclude=["samples", "samples"])
        self.assertEqual(report["instructions"]["agents"]["paths"], ["AGENTS.md"])
        self.assertEqual(report["exclusions"]["count"], 5)
        entries = report["exclusions"]["patterns"]
        self.assertEqual([e for e in entries if e["pattern"] == "samples"], [{"pattern": "samples", "count": 1}])

    def test_excluded_imports_never_read_or_expose_the_target(self):
        root = self.copy_fixture("both")
        self.write(root, "CLAUDE.md", "@AGENTS.md\n@tests/fixtures/CLAUDE.md\n@bridge.md\n")
        self.write(root, "bridge.md", "@src/../vendor/secret.md\n")
        self.write(root, "tests/fixtures/CLAUDE.md", "@../../AGENTS.md\n")
        self.write(root, "vendor/secret.md", "Leave a handoff document.\n")
        original_read = Path.read_bytes

        def guarded(path):
            if path.is_relative_to(root / "tests/fixtures") or path.is_relative_to(root / "vendor"):
                raise AssertionError("An excluded import was read.")
            return original_read(path)

        with patch.object(Path, "read_bytes", guarded):
            report = inventory(root)
        self.assertEqual(report["claude_imports"], [
            {"source": "CLAUDE.md", "path": "AGENTS.md", "status": "resolved"},
            {"source": "CLAUDE.md", "status": "excluded-not-read"},
            {"source": "CLAUDE.md", "path": "bridge.md", "status": "resolved"},
            {"source": "bridge.md", "status": "excluded-not-read"},
        ])
        self.assertEqual(report["cost"]["instructions"]["claude"]["paths"], ["AGENTS.md", "CLAUDE.md", "bridge.md"])
        self.assertEqual(report["conventions"]["handoff"], {"status": "absent", "evidence": []})
        self.assertNotIn("tests/fixtures/CLAUDE.md", json.dumps(report))
        self.assertNotIn("vendor/secret.md", json.dumps(report))

    def test_invalid_config_and_globs_fail_without_private_errors(self):
        root = self.copy_fixture("both")
        for text in ('exclude = "SYNTHETIC_PRIVATE"', 'exclude = [1]',
                     'exclude = ["../SYNTHETIC_PRIVATE"]', '[SYNTHETIC_PRIVATE',
                     'exclude = ["/SYNTHETIC_PRIVATE"]'):
            self.write(root, ".sumbi/config.toml", text)
            with self.assertRaises(InstallError) as caught:
                inventory(root)
            self.assertNotIn("SYNTHETIC_PRIVATE", str(caught.exception))
        self.write(root, ".sumbi/config.toml", "exclude = []\n")
        for patterns in ([""], ["../private"], ["a\\b"], ["a//b"], ["a:b"], "private"):
            with self.assertRaises(InstallError):
                inventory(root, exclude=patterns)

    def test_apply_keeps_custom_exclusions_and_is_idempotent(self):
        root = self.copy_fixture("both")
        self.write(root, "scratch/CLAUDE.md", "Scoped private instructions.\n")
        original = (root / "scratch/CLAUDE.md").read_bytes()
        plan = build_plan(root, exclude=["scratch"], select=["handoff"])
        apply_plan(plan)
        self.assertEqual((root / "scratch/CLAUDE.md").read_bytes(), original)
        self.assertEqual(build_plan(root, exclude=["scratch"], select=["handoff"]).changes, [])

    def test_excluded_catalog_targets_are_preserved(self):
        root = self.copy_fixture("both")
        original = (root / "AGENTS.md").read_bytes()
        plan = build_plan(root, exclude=["AGENTS.md", "docs/**"], select=["handoff"])
        self.assertEqual(plan.changes, [])
        self.assertEqual(apply_plan(plan)["applied"], [])
        self.assertEqual((root / "AGENTS.md").read_bytes(), original)
        self.assertFalse((root / "docs").exists())

    def test_exclusions_changed_after_planning_block_apply(self):
        root = self.copy_fixture("both")
        plan = build_plan(root, select=["handoff"])
        self.write(root, ".sumbi/config.toml", 'exclude = ["AGENTS.md"]\n')
        original = (root / "AGENTS.md").read_bytes()
        with self.assertRaises(InstallError):
            apply_plan(plan)
        self.assertEqual((root / "AGENTS.md").read_bytes(), original)
        self.assertFalse((root / ".sumbi/interventions.jsonl").exists())
