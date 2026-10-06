"""Hand-written truths for synthetic harness repositories."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sumbi.install import find_gaps, inventory

FIXTURES = Path(__file__).parent / "fixtures"
BASE_GAPS = {"missing-test-command", "missing-risk-review", "missing-handoff",
             "missing-friction-line", "missing-worktree-rule", "missing-plan-approval"}
INSTRUCTION_KEYS = ("agents", "claude", "claude_rules", "gemini", "copilot", "cursor_rules")


class OfflineTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        guard = patch.object(Path, "home", return_value=home)
        guard.start()
        self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, {}, clear=False)
        guard.start()
        self.addCleanup(guard.stop)
        os.environ.pop("SUMBI_SALT", None)
        for name in ("socket.socket", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("Network access is forbidden."))
            guard.start()
            self.addCleanup(guard.stop)

    def copy_fixture(self, name):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "repository"
        shutil.copytree(FIXTURES / name, root)
        return root


class InventoryTests(OfflineTest):
    def assert_instructions(self, report, **expected):
        self.assertEqual(report["instructions"], {
            key: {"count": len(expected.get(key, [])), "paths": expected.get(key, [])}
            for key in INSTRUCTION_KEYS})

    def assert_gaps(self, report, expected):
        self.assertEqual({gap["id"] for gap in find_gaps(report)}, expected)
        for gap in find_gaps(report):
            self.assertIn("evidence", gap)
            self.assertTrue(gap["candidates"])

    def test_empty_truth(self):
        report = inventory(FIXTURES / "empty")
        self.assert_instructions(report)
        self.assertEqual(report["claude_imports"], [])
        self.assertEqual(report["configurations"], [])
        self.assertEqual(report["capabilities"], {
            key: {"count": 0, "paths": [], "file_count": 0}
            for key in ("claude_skills", "agent_skills", "commands", "agents")})
        self.assertEqual(report["enforcement"], {
            "workflows": [], "codeowners": {"count": 0, "paths": []},
            "pr_templates": {"count": 0, "paths": []},
            "git_hooks": {"count": 0, "paths": []}, "lint": {"count": 0, "paths": []},
            "format": {"count": 0, "paths": []}, "type_check": {"count": 0, "paths": []},
            "test_sources": [], "required_checks": "unknown (offline)", "rulesets": "unknown (offline)"})
        self.assertEqual(report["cost"]["instructions"], {
            key: {"paths": [], "characters": 0, "estimated_tokens": 0, "root_estimated_tokens": 0}
            for key in ("codex", "claude", "gemini", "copilot", "cursor")})
        expected = [{"kind": "root-not-versioned"}] if report["versioning"]["root_versioned"] is False else []
        self.assertEqual(report["warnings"], expected)
        self.assert_gaps(report, BASE_GAPS | {"missing-agents"})

    def test_claude_only_truth(self):
        report = inventory(FIXTURES / "claude-only")
        self.assert_instructions(report, claude=["CLAUDE.md"])
        self.assertEqual(report["cost"]["instructions"]["claude"], {
            "paths": ["CLAUDE.md"], "characters": 8726, "estimated_tokens": 2182, "root_estimated_tokens": 2182})
        self.assert_gaps(report, BASE_GAPS | {"missing-agents", "instruction-budget"})

    def test_codex_only_truth(self):
        report = inventory(FIXTURES / "codex-only")
        self.assert_instructions(report, agents=["AGENTS.md"])
        self.assertEqual(report["enforcement"]["test_sources"], [{"path": "AGENTS.md", "kind": "documented-command"}])
        self.assertEqual(report["cost"]["instructions"]["codex"]["estimated_tokens"], 23)
        self.assert_gaps(report, BASE_GAPS - {"missing-test-command"})

    def test_both_truth(self):
        report = inventory(FIXTURES / "both")
        self.assert_instructions(report, agents=["AGENTS.md"], claude=["CLAUDE.md"])
        self.assertEqual(report["claude_imports"], [{"source": "CLAUDE.md", "path": "AGENTS.md", "status": "resolved"}])
        self.assertEqual(report["cost"]["instructions"]["claude"], {
            "paths": ["AGENTS.md", "CLAUDE.md"], "characters": 99, "estimated_tokens": 25, "root_estimated_tokens": 25})
        self.assert_gaps(report, BASE_GAPS - {"missing-test-command"})

    def test_configured_truth(self):
        report = inventory(FIXTURES / "configured")
        self.assert_instructions(report, agents=["AGENTS.md", "src/AGENTS.md"], claude=["CLAUDE.md"],
                                 claude_rules=[".claude/rules/checks.md"], gemini=["GEMINI.md"],
                                 copilot=[".github/copilot-instructions.md"], cursor_rules=[".cursor/rules/checks.mdc"])
        self.assertEqual(report["claude_imports"], [
            {"source": "CLAUDE.md", "path": "AGENTS.md", "status": "resolved"},
            {"source": "CLAUDE.md", "path": "docs/extra.md", "status": "resolved"}])
        self.assertEqual(report["capabilities"], {
            "claude_skills": {"count": 1, "paths": [".claude/skills/check/SKILL.md"], "file_count": 2},
            "agent_skills": {"count": 1, "paths": [".agents/skills/check/SKILL.md"], "file_count": 1},
            "commands": {"count": 1, "paths": [".claude/commands/check.md"], "file_count": 1},
            "agents": {"count": 1, "paths": [".claude/agents/reviewer.md"], "file_count": 1}})
        self.assertEqual(report["configurations"], [
            {"path": ".claude/settings.json", "hooks": 2, "permission_entries": 2, "mcp_servers": 0, "approval_policy_present": False, "sandbox_mode_present": False},
            {"path": ".codex/config.toml", "hooks": 0, "permission_entries": 0, "mcp_servers": 1, "approval_policy_present": True, "sandbox_mode_present": True},
            {"path": ".codex/hooks.json", "hooks": 1, "permission_entries": 0, "mcp_servers": 0, "approval_policy_present": False, "sandbox_mode_present": False},
            {"path": ".mcp.json", "hooks": 0, "permission_entries": 0, "mcp_servers": 1, "approval_policy_present": False, "sandbox_mode_present": False}])
        self.assertEqual(report["enforcement"], {
            "workflows": [{"path": ".github/workflows/checks.yml", "jobs": [{"id": "verify", "name": "Verify"}, {"id": "lint", "name": "Lint"}], "job_count": 2, "parser": "block YAML subset"}],
            "codeowners": {"count": 1, "paths": [".github/CODEOWNERS"]},
            "pr_templates": {"count": 1, "paths": [".github/pull_request_template.md"]},
            "git_hooks": {"count": 4, "paths": [".githooks/pre-commit", ".husky/pre-commit", ".pre-commit-config.yaml", "lefthook.yml"]},
            "lint": {"count": 3, "paths": [".eslintrc.json", "package.json", "pyproject.toml"]},
            "format": {"count": 3, "paths": [".prettierrc.json", "package.json", "pyproject.toml"]},
            "type_check": {"count": 3, "paths": ["mypy.ini", "package.json", "pyproject.toml"]},
            "test_sources": [{"path": "AGENTS.md", "kind": "documented-command"}, {"path": "Makefile", "kind": "make-target"},
                             {"path": "noxfile.py", "kind": "nox-command"}, {"path": "package.json", "kind": "package-script"},
                             {"path": "pyproject.toml", "kind": "pytest-config"}, {"path": "tox.ini", "kind": "tox-command"}],
            "required_checks": "unknown (offline)", "rulesets": "unknown (offline)"})
        self.assertEqual(report["cost"]["skill_descriptions"], [
            {"path": ".agents/skills/check/SKILL.md", "characters": 41, "estimated_tokens": 11},
            {"path": ".claude/skills/check/SKILL.md", "characters": 20, "estimated_tokens": 5}])
        self.assertEqual(report["cost"]["every_prompt_hooks"], [
            {"path": ".claude/settings.json", "event": "UserPromptSubmit", "count": 1},
            {"path": ".codex/hooks.json", "event": "UserPromptSubmit", "count": 1}])
        self.assertEqual({key: value["estimated_tokens"] for key, value in report["cost"]["instructions"].items()},
                         {"codex": 120, "claude": 137, "gemini": 8, "copilot": 8, "cursor": 14})
        self.assertEqual({key: value["root_estimated_tokens"] for key, value in report["cost"]["instructions"].items()},
                         {"codex": 109, "claude": 137, "gemini": 8, "copilot": 8, "cursor": 14})
        expected = [{"kind": "root-not-versioned"}] if report["versioning"]["root_versioned"] is False else []
        self.assertEqual(report["warnings"], expected)
        self.assert_gaps(report, set())

    def test_export_has_no_source_contents(self):
        report = inventory(FIXTURES / "configured")
        encoded = json.dumps(report)
        self.assertNotIn("SYNTHETIC_PRIVATE", encoded)
        self.assertNotIn("@example-reviewer", encoded)
        self.assertNotIn(str(FIXTURES.resolve()), encoded)

    def test_missing_import_and_budget_are_evidence_bearing(self):
        root = self.copy_fixture("both")
        (root / "CLAUDE.md").write_text("Agent-specific rules.\n", encoding="utf-8")
        report = inventory(root, budget=1)
        gaps = find_gaps(report)
        self.assertEqual([g for g in gaps if g["id"] == "missing-shared-import"], [
            {"id": "missing-shared-import", "evidence": {"paths": ["CLAUDE.md", "AGENTS.md"]}, "candidates": ["shared-instructions"]}])
        self.assertEqual({g["evidence"]["agent"] for g in gaps if g["id"] == "instruction-budget"}, {"codex", "claude"})

    def test_import_cycles_missing_and_external_are_bounded(self):
        root = self.copy_fixture("both")
        (root / "CLAUDE.md").write_text('@AGENTS.md\n@missing.md\n@../outside.md\n@~/private.md\nname@example.invalid\n', encoding="utf-8")
        (root / "AGENTS.md").write_text("@CLAUDE.md\n", encoding="utf-8")
        imports = inventory(root)["claude_imports"]
        self.assertEqual(imports, [
            {"source": "CLAUDE.md", "path": "AGENTS.md", "status": "resolved"},
            {"source": "AGENTS.md", "path": "CLAUDE.md", "status": "resolved"},
            {"source": "CLAUDE.md", "path": "missing.md", "status": "missing-or-link"},
            {"source": "CLAUDE.md", "status": "external-not-read"},
            {"source": "CLAUDE.md", "status": "external-not-read"}])

    def test_invalid_configs_and_unsupported_workflows_are_visible(self):
        root = self.copy_fixture("configured")
        (root / ".claude/settings.json").write_text("not JSON", encoding="utf-8")
        (root / ".codex/config.toml").write_text("[bad", encoding="utf-8")
        (root / ".github/workflows/checks.yml").write_text("jobs: {verify: {}}\n", encoding="utf-8")
        git_warning = "root-not-versioned" if shutil.which("git") else "gitignore-unavailable"
        self.assertEqual({w["kind"] for w in inventory(root)["warnings"]},
                         {"invalid-config", "workflow-jobs-unknown", git_warning})

    def test_alternate_workflow_indentation_and_step_names(self):
        root = self.copy_fixture("configured")
        (root / ".github/workflows/checks.yml").write_text(
            "jobs:\n    'verify':\n      name: Verify\n      steps:\n        - name: Private step\n          run: echo hidden\n", encoding="utf-8")
        self.assertEqual(inventory(root)["enforcement"]["workflows"][0]["jobs"], [{"id": "verify", "name": "Verify"}])

    def test_measurement_and_git_data_are_not_scanned(self):
        root = self.copy_fixture("empty")
        for folder in (".sumbi", ".git", "node_modules"):
            (root / folder).mkdir()
            (root / folder / "AGENTS.md").write_text("Do not load this.\n", encoding="utf-8")
        self.assert_instructions(inventory(root))

    def test_pyproject_and_tox_configs_are_not_executed(self):
        root = self.copy_fixture("empty")
        (root / "pyproject.toml").write_text('[tool.poetry.scripts]\ntest = "private:entry"\n', encoding="utf-8")
        (root / "tox.ini").write_text("[testenv]\ncommands = python -m unittest\n", encoding="utf-8")
        self.assertEqual(inventory(root)["enforcement"]["test_sources"], [
            {"path": "pyproject.toml", "kind": "project-script"}, {"path": "tox.ini", "kind": "tox-command"}])

    def test_multiline_tox_and_project_script_sources(self):
        root = self.copy_fixture("empty")
        (root / "pyproject.toml").write_text('[project.scripts]\ntest = "private:entry"\n', encoding="utf-8")
        (root / "tox.ini").write_text("[testenv]\ncommands =\n    python -m unittest\n", encoding="utf-8")
        self.assertEqual(inventory(root)["enforcement"]["test_sources"], [
            {"path": "pyproject.toml", "kind": "project-script"}, {"path": "tox.ini", "kind": "tox-command"}])

    def test_nox_build_tasks_do_not_count_as_tests(self):
        root = self.copy_fixture("empty")
        (root / "noxfile.py").write_text('def build(session):\n    session.run("python", "-m", "build")\n', encoding="utf-8")
        self.assertEqual(inventory(root)["enforcement"]["test_sources"], [])
        (root / "noxfile.py").write_text('def check(session):\n    session.run("python", "-m", "unittest")\n', encoding="utf-8")
        self.assertEqual(inventory(root)["enforcement"]["test_sources"], [{"path": "noxfile.py", "kind": "nox-command"}])

    def test_crlf_estimate_matches_lf(self):
        root = self.copy_fixture("both")
        expected = inventory(root)["cost"]["instructions"]
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = root / name
            path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(inventory(root)["cost"]["instructions"], expected)

    def test_transitive_and_scoped_imports_satisfy_sharing(self):
        root = self.copy_fixture("both")
        (root / "CLAUDE.md").write_text("@bridge.md\n", encoding="utf-8")
        (root / "bridge.md").write_text("@AGENTS.md\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src/CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        (root / "src/AGENTS.md").write_text("Scoped rules.\n", encoding="utf-8")
        self.assertNotIn("missing-shared-import", {g["id"] for g in find_gaps(inventory(root))})

    def test_sibling_instruction_scopes_are_not_summed(self):
        root = self.copy_fixture("empty")
        (root / "AGENTS.md").write_text("Root\n", encoding="utf-8")
        for name in ("a", "b"):
            (root / name).mkdir()
            (root / name / "AGENTS.md").write_text("Scoped\n", encoding="utf-8")
        self.assertEqual(inventory(root)["cost"]["instructions"]["codex"], {
            "paths": ["AGENTS.md", "a/AGENTS.md"], "characters": 12,
            "estimated_tokens": 3, "root_estimated_tokens": 2})

    def test_a_roadmap_listing_practices_is_not_a_convention(self):
        root = self.copy_fixture("empty")
        (root / "docs").mkdir()
        (root / "docs/roadmap.md").write_text(
            "Planned catalog: risk review, handoff documents, git worktrees for parallel work, plan and approval gates.\n", encoding="utf-8")
        report = inventory(root)
        self.assert_gaps(report, BASE_GAPS | {"missing-agents"})
        self.assertEqual(report["convention_search_paths"], [])

    def test_readme_and_design_rules_are_not_loaded_by_markdown_links(self):
        root = self.copy_fixture("both")
        (root / "docs").mkdir()
        rule = "Write a plan with acceptance criteria and owner approval before implementation.\n"
        (root / "README.md").write_text(rule, encoding="utf-8")
        (root / "docs/design.md").write_text(rule, encoding="utf-8")
        (root / "AGENTS.md").write_text("See [design](docs/design.md).\n", encoding="utf-8")
        report = inventory(root)
        self.assertEqual(report["conventions"]["plan_approval"], {"status": "absent", "evidence": []})
        self.assertIn("missing-plan-approval", {gap["id"] for gap in find_gaps(report)})

    def test_loaded_rules_skills_and_imports_supply_directive_evidence(self):
        for path in ("AGENTS.md", "GEMINI.md", ".github/copilot-instructions.md",
                     ".cursor/rules/planning.mdc", ".claude/rules/planning.md",
                     ".agents/skills/planning/SKILL.md", ".claude/skills/planning/SKILL.md",
                     ".claude/commands/planning.md", ".claude/agents/planning.md", "docs/imported.md"):
            with self.subTest(path=path):
                root = self.copy_fixture("empty")
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("Write a plan with acceptance criteria and owner approval before implementation.\n", encoding="utf-8")
                if path.startswith("docs/"):
                    (root / "CLAUDE.md").write_text("@docs/imported.md\n", encoding="utf-8")
                report = inventory(root)
                self.assertEqual(report["conventions"]["plan_approval"], {"status": "present", "evidence": [{"path": path, "kind": "directive"}]})
                self.assertNotIn("missing-plan-approval", {g["id"] for g in find_gaps(report)})

    def test_instruction_mentions_examples_metadata_and_denials_are_not_rules(self):
        root = self.copy_fixture("empty")
        (root / "AGENTS.md").write_text(
            "Ideas: write a plan before owner approval.\n"
            "Do not require plan approval.\n"
            "Consider using parallel worktrees.\n"
            "Examples:\n```markdown\nLeave a handoff document.\n"
            "End the report with Harness friction: none\n```\n", encoding="utf-8")
        skill = root / ".agents/skills/planning/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: planning\ndescription: Write a plan before approval.\n---\n"
                         "Planning is a catalog topic.\n", encoding="utf-8")
        report = inventory(root)
        self.assert_gaps(report, BASE_GAPS - {"missing-plan-approval", "missing-worktree-rule"})
        for key in ("plan_approval", "parallel_worktree"):
            paths = ([".agents/skills/planning/SKILL.md"] if key == "plan_approval" else []) + ["AGENTS.md"]
            self.assertEqual(report["conventions"][key], {"status": "unknown", "evidence": [
                {"path": path, "kind": "mentioned"} for path in paths]})
