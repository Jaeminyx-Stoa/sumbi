"""Default dry-run, JSON output and the optional collect contract."""

from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
from pathlib import Path
import types
from unittest.mock import patch

from sumbi.cli.install import main
from sumbi.install.baseline_hook import baseline
from sumbi.install.errors import InstallError
from .test_inventory import OfflineTest

baseline_module = importlib.import_module("sumbi.install.baseline_hook")


class CliTests(OfflineTest):
    def run_cli(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = main(args)
        return result, output.getvalue(), errors.getvalue()

    def test_unknown_conventions_show_owner_declaration_help(self):
        root = self.copy_fixture("empty")
        (root / "AGENTS.md").write_text("## Handoff\n", encoding="utf-8")
        result, output, errors = self.run_cli(["--root", str(root), "--select", "handoff"])
        self.assertEqual(result, 0, errors)
        self.assertIn("Unknown conventions: handoff", output)
        self.assertIn(".sumbi/config.toml under [conventions]", output)
        self.assertIn('handoff = ["docs/process.md"]', output)
        self.assertIn('"kind": "mentioned", "path": "AGENTS.md"', output)
        self.assertNotIn("missing-handoff", output)
        self.assertIn("No changes proposed.", output)
        self.assertNotIn("## Handoff", output)

    def test_default_is_read_only_and_diffs_have_no_original_text(self):
        root = self.copy_fixture("codex-only")
        before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        result, output, errors = self.run_cli(["--root", str(root)])
        self.assertEqual(result, 0, errors)
        self.assertIn("--- a/AGENTS.md\n+++ b/AGENTS.md\n", output)
        self.assertIn("baseline: available (runs before apply)", output)
        self.assertNotIn("# Repository rules", output)
        self.assertNotIn("python -m unittest discover", output)
        self.assertNotIn(str(root), output)
        self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_json_plan_has_all_evidence_without_original_contents(self):
        root = self.copy_fixture("configured")
        result, _, errors = self.run_cli(["--root", str(root), "--json", "plan.json"])
        self.assertEqual(result, 0, errors)
        raw = (root / "plan.json").read_text(encoding="utf-8")
        plan = json.loads(raw)
        self.assertEqual(plan["changes"], [])
        self.assertEqual(plan["gaps"], [])
        self.assertEqual(len(plan["inventory"]["enforcement"]["workflows"]), 1)
        self.assertNotIn("SYNTHETIC_PRIVATE", raw)
        self.assertNotIn(str(root), raw)

    def test_json_refuses_overwrite_metadata_and_traversal(self):
        root = self.copy_fixture("empty")
        for path in (".fixture", "AGENTS.md", "agents.md", "../escape.json", ".git/plan.json", ".sumbi/plan.json", ".GIT/plan.json", "NUL", "plan.json.", "a//plan.json"):
            with self.subTest(path=path):
                result, _, errors = self.run_cli(["--root", str(root), "--json", path])
                self.assertEqual(result, 1)
                self.assertNotIn(str(root), errors)
        self.assertFalse((root.parent / "escape.json").exists())

    def test_apply_selection_and_repeat_cli(self):
        root = self.copy_fixture("empty")
        args = ["--root", str(root), "--apply", "--select", "handoff"]
        result, output, errors = self.run_cli(args)
        self.assertEqual(result, 0, errors)
        self.assertIn("Applied: handoff", output)
        self.assertFalse((root / "docs/sumbi/testing.md").exists())
        result, output, errors = self.run_cli(args)
        self.assertEqual(result, 0, errors)
        self.assertIn("Applied: none", output)

    def test_repeatable_exclusions_and_config_hide_roots_and_imports(self):
        root = self.copy_fixture("both")
        for tree in ("scratch", "samples", "tests/fixtures", "src/generated"):
            target = root / tree / "CLAUDE.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("@../../AGENTS.md\n", encoding="utf-8")
        (root / ".sumbi").mkdir()
        (root / ".sumbi/config.toml").write_text('exclude = ["**/generated"]\n', encoding="utf-8")
        (root / "CLAUDE.md").write_text("@AGENTS.md\n@tests/fixtures/CLAUDE.md\n", encoding="utf-8")
        result, output, errors = self.run_cli(["--root", str(root), "--exclude", "scratch",
                                              "--exclude", "samples", "--json", "plan.json"])
        self.assertEqual(result, 0, errors)
        self.assertIn("excluded roots: 5", output)
        self.assertIn("exclude scratch: 1", output)
        self.assertIn("exclude samples: 1", output)
        self.assertIn("exclude **/generated: 1", output)
        self.assertIn("excluded-not-read", output)
        raw = (root / "plan.json").read_text(encoding="utf-8")
        for tree in ("scratch", "samples", "tests/fixtures", "src/generated"):
            self.assertNotIn(tree + "/CLAUDE.md", output + raw)

    def test_pending_baseline(self):
        missing = ModuleNotFoundError("Synthetic unavailable entry point", name="sumbi.install.baseline")
        with patch.object(baseline_module.importlib, "import_module", side_effect=missing):
            self.assertEqual(baseline(Path(".")), {"status": "pending (collect not available)"})

    def test_available_baseline_is_only_run_before_apply(self):
        root = self.copy_fixture("empty")
        calls = []
        def entry(*, repository, home=None):
            self.assertFalse((repository / "AGENTS.md").exists())
            calls.append(repository)
            return {"private_text": "SYNTHETIC_PRIVATE_BASELINE"}
        module = types.ModuleType("sumbi.install.baseline")
        module.baseline = entry
        with patch.dict("sys.modules", {"sumbi.install.baseline": module}):
            result, output, errors = self.run_cli(["--root", str(root), "--dry-run"])
            self.assertEqual(result, 0, errors)
            self.assertIn("baseline: available (runs before apply)", output)
            self.assertEqual(calls, [])
            result, output, errors = self.run_cli(["--root", str(root), "--apply", "--select", "handoff"])
        self.assertEqual(result, 0, errors)
        self.assertEqual(calls, [root])
        self.assertIn("baseline: recorded", output)
        self.assertNotIn("SYNTHETIC_PRIVATE_BASELINE", output)
        self.assertNotIn("SYNTHETIC_PRIVATE_BASELINE", (root / ".sumbi/interventions.jsonl").read_text())

    def test_baseline_failure_prevents_practice_writes(self):
        root = self.copy_fixture("empty")
        module = types.ModuleType("sumbi.install.baseline")
        def entry(*, repository, home=None):
            raise ValueError("SYNTHETIC_PRIVATE_ERROR")
        module.baseline = entry
        with patch.dict("sys.modules", {"sumbi.install.baseline": module}):
            result, output, errors = self.run_cli(["--root", str(root), "--apply"])
        self.assertEqual(result, 1)
        self.assertIn("Collect baseline failed", errors)
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR", output + errors)
        self.assertFalse((root / "AGENTS.md").exists())
        self.assertFalse((root / ".sumbi/interventions.jsonl").exists())

    def test_broken_collect_dependency_is_not_silently_pending(self):
        missing = ModuleNotFoundError("Synthetic missing dependency", name="dependency")
        with patch.object(baseline_module.importlib, "import_module", side_effect=missing), self.assertRaises(InstallError):
            baseline(Path("."))

    def test_invalid_budget_and_selection_fail_with_sanitized_errors(self):
        root = self.copy_fixture("empty")
        for options in (["--budget", "0"], ["--select", "unknown"], ["--select", ""]):
            result, _, errors = self.run_cli(["--root", str(root), *options])
            self.assertEqual(result, 1)
            self.assertNotIn(str(root), errors)
