"""Synthetic absence, localization, allowlist ignores and selective reversal."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import re
import subprocess
from unittest.mock import patch

from sumbi.catalog import load_catalog
from sumbi.install import apply_plan, build_plan, inventory, revert_install
from sumbi.install.errors import InstallError
from sumbi.install.__main__ import main
from sumbi.install import revert as revert_module
from .test_inventory import OfflineTest


class InstallImprovementTests(OfflineTest):
    def write(self, root, path, text):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    def audit(self, root):
        return [json.loads(line) for line in (root / ".sumbi/interventions.jsonl").read_bytes().splitlines()]

    def test_declared_absent_overrides_mentions_and_supplies_candidate(self):
        for text in ("## Handoff\n", "Keep assets local.\n", "\u042f" * 200 + "\n"):
            with self.subTest(guidance=text):
                root = self.copy_fixture("empty")
                self.write(root, "AGENTS.md", text)
                self.write(root, ".sumbi/config.toml", '[conventions]\nhandoff = "absent"\n')
                report = inventory(root)
                self.assertEqual(report["conventions"]["handoff"]["status"], "absent")
                self.assertIn({"path": ".sumbi/config.toml", "kind": "declared-absent"},
                              report["conventions"]["handoff"]["evidence"])
                plan = build_plan(root, select=["handoff"])
                self.assertIn("missing-handoff", {gap["id"] for gap in plan.gaps})
                self.assertEqual([practice["id"] for practice in plan.practices], ["handoff"])

    def test_declared_absent_never_hides_a_directive(self):
        root = self.copy_fixture("empty")
        self.write(root, "AGENTS.md", "## Handoff\n")
        self.write(root, "CLAUDE.md", "@rules.md\n")
        self.write(root, "rules.md", "Leave a handoff document.\n")
        self.write(root, ".sumbi/config.toml", '[conventions]\nhandoff = "absent"\n')
        plan = build_plan(root, select=["handoff"])
        self.assertEqual(plan.report["conventions"]["handoff"]["status"], "present")
        self.assertIn({"kind": "convention-declaration-conflict", "convention": "handoff"}, plan.report["warnings"])
        self.assertEqual(plan.changes, [])

    def test_language_auto_selection_counts_only_searched_guidance(self):
        hangul = "\uc9c0\uce68" * 100
        for guidance, language in (("", "en"), ("Keep assets local.\n", "en"),
                (hangul + "\n", "ko"), ("\u042f" * 1000 + "\n" + hangul + "\n", "ko"),
                ("a\uac00\n", "en"),
                ("---\ndescription: " + hangul + "\n---\n```\n" + hangul + "\n```\nKeep assets local.\n", "en"),
                ("Keep assets local. `" + hangul + "`\n", "en")):
            with self.subTest(expected=language, guidance=guidance):
                root = self.copy_fixture("empty")
                if guidance:
                    self.write(root, "AGENTS.md", guidance)
                self.write(root, "README.md", hangul + "\n")
                self.assertEqual(inventory(root)["practice_language"], language)

    def test_imported_guidance_votes_on_language(self):
        root = self.copy_fixture("empty")
        self.write(root, "CLAUDE.md", "@guide.md\n")
        self.write(root, "guide.md", "\uc9c0\uce68" * 100 + "\n")
        self.assertEqual(inventory(root)["practice_language"], "ko")

    def test_auto_korean_applies_korean_entry_and_document(self):
        root = self.copy_fixture("empty")
        self.write(root, "AGENTS.md", "\uc9c0\uce68" * 100 + "\n")
        plan = build_plan(root, select=["test-and-verify"])
        self.assertEqual(plan.practices[0]["language"], "ko")
        apply_plan(plan)
        self.assertIn("\uc644\ub8cc\ub97c \ubcf4\uace0\ud558\uae30 \uc804\uc5d0", (root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("# \ud14c\uc2a4\ud2b8 \uc99d\uac70", (root / "docs/sumbi/testing.md").read_text(encoding="utf-8"))
        self.assertEqual(self.audit(root)[0]["language"], "ko")

    def test_language_override_both_applied_texts_hashes_and_neutral_metadata(self):
        plans = {}
        for language, prefix in (("en", "Before claiming completion"),
                                 ("ko", "\uc644\ub8cc\ub97c \ubcf4\uace0\ud558\uae30 \uc804\uc5d0")):
            root = self.copy_fixture("empty")
            self.write(root, "AGENTS.md", "\uc9c0\uce68" * 100 + "\n" if language == "en" else "Keep assets local.\n")
            self.write(root, ".sumbi/config.toml", f'language = "{language}"\n')
            plan = build_plan(root, select=["test-and-verify"])
            plans[language] = plan
            self.assertEqual(plan.report["practice_language"], language)
            apply_plan(plan)
            self.assertIn(prefix, (root / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertEqual(self.audit(root)[0]["language"], language)
            self.assertEqual(self.audit(root)[0]["content_hash"], plan.practices[0]["content_hash"])
            self.assertEqual(build_plan(root, select=["test-and-verify"]).changes, [])
        self.assertNotEqual(plans["en"].practices[0]["content_hash"], plans["ko"].practices[0]["content_hash"])
        for key in ("prediction", "judgment", "provenance"):
            self.assertEqual(plans["en"].practices[0][key], plans["ko"].practices[0][key])

    def test_invalid_language_and_changed_override_fail_safely(self):
        root = self.copy_fixture("empty")
        for value in ('"fr"', '[]', '1', 'true'):
            self.write(root, ".sumbi/config.toml", "language = " + value)
            with self.assertRaisesRegex(InstallError, "language"):
                inventory(root)
        self.write(root, ".sumbi/config.toml", 'language = "en"\n')
        plan = build_plan(root, select=["handoff"])
        self.write(root, ".sumbi/config.toml", 'language = "ko"\n')
        with self.assertRaises(InstallError):
            apply_plan(plan)
        self.assertFalse((root / "AGENTS.md").exists())

    def test_every_catalog_text_has_both_languages_and_identical_links(self):
        for practice in load_catalog():
            for item in practice["files"]:
                with self.subTest(practice=practice["id"], path=item["path"]):
                    self.assertEqual(set(item["text"]), {"en", "ko"})
                    self.assertEqual(re.findall(r"\]\(([^)]+)\)", item["text"]["en"]),
                                     re.findall(r"\]\(([^)]+)\)", item["text"]["ko"]))
                    if practice["id"] != "shared-instructions":
                        self.assertRegex(item["text"]["ko"], r"[\uac00-\ud7a3]")
                    if item["path"] == "AGENTS.md":
                        for language in ("en", "ko"):
                            self.assertTrue(item["text"][language].startswith(item["text_without_links"][language]))
                            self.assertNotIn("docs/sumbi/", item["text_without_links"][language])

    def test_all_practices_apply_both_languages(self):
        catalog = load_catalog()
        for language in ("en", "ko"):
            with self.subTest(language=language):
                root = self.copy_fixture("claude-only")
                self.write(root, ".sumbi/config.toml", f'language = "{language}"\n')
                plan = build_plan(root)
                self.assertEqual({practice["id"] for practice in plan.practices}, {p["id"] for p in catalog})
                result = apply_plan(plan)
                for practice in catalog:
                    for item in practice["files"]:
                        text = (root / item["path"]).read_text(encoding="utf-8")
                        self.assertIn(item["text"][language], text)
                self.assertEqual({record["language"] for record in self.audit(root)}, {language})
                self.assertEqual(revert_install(root, result["backup_id"])["refused"], 0)

    def test_block_records_are_validated_against_applied_hash(self):
        root = self.copy_fixture("codex-only")
        plan = build_plan(root, select=["handoff"])
        result = apply_plan(plan)
        manifest = root / result["backup"] / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        entry = next(entry for entry in data["files"] if entry["path"] == "AGENTS.md")
        entry["blocks"][0]["bytes"] = entry["blocks"][0]["bytes"].replace("Leave a handoff", "Keep a handoff")
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(InstallError, "manifest"):
            revert_install(root, result["backup_id"])
        self.assertEqual((root / "AGENTS.md").read_bytes(), next(c.after for c in plan.changes if c.path == "AGENTS.md"))

    def test_old_manifests_without_blocks_keep_whole_file_guard(self):
        root = self.copy_fixture("codex-only")
        result = apply_plan(build_plan(root, select=["handoff"]))
        manifest = root / result["backup"] / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for entry in data["files"]:
            entry.pop("blocks", None)
        manifest.write_text(json.dumps(data), encoding="utf-8")
        target = root / "AGENTS.md"
        current = target.read_bytes() + b"Owner's addition.\n"
        target.write_bytes(current)
        self.assertEqual(revert_install(root, result["backup_id"])["refused"], 1)
        self.assertEqual(target.read_bytes(), current)

    def test_allowlist_gitignore_omits_docs_and_applies_link_free_blocks(self):
        for pattern in ("/*", "*"):
            for language in ("en", "ko"):
                with self.subTest(pattern=pattern, language=language):
                    root = self.copy_fixture("empty")
                    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
                    self.write(root, ".gitignore", pattern + "\n!.gitignore\n!AGENTS.md\n")
                    self.write(root, "AGENTS.md", "Keep assets local.\n")
                    self.write(root, ".sumbi/config.toml", f'language = "{language}"\n')
                    plan = build_plan(root, select=["handoff", "parallel-worktrees"])
                    self.assertEqual([change.path for change in plan.changes], ["AGENTS.md"])
                    self.assertNotIn("docs/sumbi/", plan.changes[0].diff())
                    self.assertIn({"kind": "practice-docs-ignored", "practice_ids": ["handoff", "parallel-worktrees"]},
                                  plan.report["warnings"])
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        status = main(["--root", str(root), "--home", str(root / "empty-home"),
                                       "--apply", "--select", "handoff,parallel-worktrees"])
                    self.assertEqual(status, 0, errors.getvalue())
                    self.assertEqual(output.getvalue().count("ledger and backups are local by design"), 1)
                    self.assertFalse((root / "docs").exists())
                    self.assertEqual(len(self.audit(root)), 2)
                    self.assertEqual(build_plan(root, select=["handoff", "parallel-worktrees"]).changes, [])

    def test_revert_preserves_unrelated_bytes_and_removes_only_added_separator(self):
        for newline, blank in ((b"\n", False), (b"\r\n", False), (b"\n", True)):
            with self.subTest(newline=newline, blank=blank):
                root = self.copy_fixture("empty")
                original = b"\xef\xbb\xbfKeep assets local." + newline * (2 if blank else 1)
                target = root / "AGENTS.md"
                target.write_bytes(original)
                plan = build_plan(root, select=["handoff", "parallel-worktrees"])
                result = apply_plan(plan)
                prefix, suffix = b"Owner's new preface." + newline, b"Owner's new footer.\xff" + newline
                target.write_bytes(prefix + target.read_bytes() + suffix)
                reverted = revert_install(root, result["backup_id"])
                self.assertEqual(reverted["refused"], 0)
                self.assertEqual(target.read_bytes(), prefix + original + suffix)
                entry = next(entry for entry in reverted["files"] if entry["path"] == "AGENTS.md")
                self.assertEqual(entry["status"], "blocks-removed")
                self.assertEqual([block["status"] for block in entry["blocks"]], ["removed", "removed"])
                self.assertEqual(self.audit(root)[-1]["files"], reverted["files"])
                audit = (root / ".sumbi/interventions.jsonl").read_bytes()
                revert_install(root, result["backup_id"])
                self.assertEqual((root / ".sumbi/interventions.jsonl").read_bytes(), audit)

    def test_edited_block_refused_while_unchanged_block_is_removed_then_retry(self):
        root = self.copy_fixture("codex-only")
        plan = build_plan(root, select=["handoff", "parallel-worktrees"])
        result = apply_plan(plan)
        target = root / "AGENTS.md"
        applied = target.read_bytes()
        changed = applied.replace(b"Parallel agents use", b"Owner changed the rule; agents use")
        target.write_bytes(changed)
        reverted = revert_install(root, result["backup_id"])
        self.assertEqual(reverted["refused"], 1)
        self.assertNotIn(b"<!-- sumbi:begin handoff -->", target.read_bytes())
        self.assertIn(b"Owner changed the rule", target.read_bytes())
        blocks = next(entry["blocks"] for entry in reverted["files"] if entry["path"] == "AGENTS.md")
        self.assertEqual(blocks, [{"id": "handoff", "status": "removed"},
            {"id": "parallel-worktrees", "status": "refused", "reason": "block-content-changed"}])
        audit = (root / ".sumbi/interventions.jsonl").read_bytes()
        revert_install(root, result["backup_id"])
        self.assertEqual((root / ".sumbi/interventions.jsonl").read_bytes(), audit)
        target.write_bytes(target.read_bytes().replace(b"Owner changed the rule; agents use", b"Parallel agents use"))
        self.assertEqual(revert_install(root, result["backup_id"])["refused"], 0)
        self.assertEqual(target.read_bytes(), next(change.before for change in plan.changes if change.path == "AGENTS.md"))

    def test_modified_created_file_is_never_deleted_or_partially_reverted(self):
        root = self.copy_fixture("empty")
        result = apply_plan(build_plan(root, select=["handoff"]))
        target = root / "AGENTS.md"
        data = target.read_bytes() + b"Owner's addition.\n"
        target.write_bytes(data)
        self.assertEqual(revert_install(root, result["backup_id"])["refused"], 1)
        self.assertEqual(target.read_bytes(), data)

    def test_revert_preserves_bom_added_to_an_existing_empty_file(self):
        root = self.copy_fixture("empty")
        target = root / "AGENTS.md"
        target.write_bytes(b"")
        result = apply_plan(build_plan(root, select=["handoff"]))
        target.write_bytes(b"\xef\xbb\xbf" + target.read_bytes() + b"Owner's addition.\n")
        self.assertEqual(revert_install(root, result["backup_id"])["refused"], 0)
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfOwner's addition.\n")

    def test_revert_removes_added_separator_from_mixed_newlines(self):
        root = self.copy_fixture("empty")
        target = root / "AGENTS.md"
        original = b"Header.\r\nKeep assets local.\n"
        target.write_bytes(original)
        result = apply_plan(build_plan(root, select=["handoff"]))
        footer = b"Owner's unrelated addition.\n"
        target.write_bytes(target.read_bytes() + footer)
        self.assertEqual(revert_install(root, result["backup_id"])["refused"], 0)
        self.assertEqual(target.read_bytes(), original + footer)

    def test_created_file_is_rechecked_immediately_before_deletion(self):
        root = self.copy_fixture("empty")
        result = apply_plan(build_plan(root, select=["handoff"]))
        target = root / "AGENTS.md"
        applied = target.read_bytes()
        check, count = revert_module._checked, 0

        def concurrent_edit(repository, path, expected):
            nonlocal count
            if path == "AGENTS.md":
                count += 1
                if count == 2:
                    target.write_bytes(applied + b"Concurrent owner edit.\n")
            return check(repository, path, expected)

        with patch.object(revert_module, "_checked", side_effect=concurrent_edit):
            reverted = revert_install(root, result["backup_id"])
        self.assertEqual(reverted["refused"], 1)
        self.assertEqual(target.read_bytes(), applied + b"Concurrent owner edit.\n")

    def test_partial_revert_ledger_failure_preserves_unrelated_edit(self):
        root = self.copy_fixture("codex-only")
        result = apply_plan(build_plan(root, select=["handoff"]))
        target = root / "AGENTS.md"
        current = target.read_bytes() + b"Owner's unrelated edit.\n"
        target.write_bytes(current)
        write = revert_module._write

        def fail_ledger(*args):
            if args[1] == ".sumbi/interventions.jsonl":
                raise OSError("Synthetic ledger write failure")
            return write(*args)

        with patch.object(revert_module, "_write", side_effect=fail_ledger), self.assertRaises(InstallError):
            revert_install(root, result["backup_id"])
        self.assertEqual(target.read_bytes(), current)
        self.assertTrue((root / "docs/sumbi/handoff.md").exists())
