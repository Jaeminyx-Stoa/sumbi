"""Safety properties of plans, managed blocks and backed-up application."""

from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import stat
from unittest.mock import patch

from sumbi.catalog import VERSION, load_catalog
from sumbi.install import apply_plan, build_plan
from sumbi.install.errors import InstallError
from sumbi.install.planner import Change, block, managed_blocks
from .test_inventory import OfflineTest

apply_module = importlib.import_module("sumbi.install.apply")


class ApplyTests(OfflineTest):
    def test_explicit_intervention_id_groups_practices_and_rejects_private_text(self):
        root = self.copy_fixture("empty")
        plan = build_plan(root, select=["handoff", "test-and-verify"])
        with self.assertRaisesRegex(InstallError, "bounded"):
            apply_plan(plan, intervention_id="synthetic private text")
        self.assertFalse((root / ".sumbi/interventions.jsonl").exists())
        apply_plan(plan, intervention_id="round-01")
        rows = [json.loads(line) for line in (root / ".sumbi/interventions.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["intervention_id"] == "round-01" for r in rows))
        self.assertEqual(len({r["utc_time"] for r in rows}), 1)

    def test_each_fixture_is_idempotent_and_backed_up(self):
        for name in ("empty", "claude-only", "codex-only", "both", "configured"):
            with self.subTest(fixture=name):
                root = self.copy_fixture(name)
                plan = build_plan(root)
                originals = {c.path: c.before for c in plan.changes}
                result = apply_plan(plan)
                self.assertEqual(result["applied"], [p["id"] for p in plan.practices])
                second = build_plan(root)
                self.assertEqual(second.changes, [])
                self.assertEqual(second.practices, [])
                if not originals:
                    self.assertFalse((root / ".sumbi").exists())
                    continue
                backup = root / result["backup"]
                manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual({r["path"] for r in manifest["files"]}, set(originals) | {".sumbi/interventions.jsonl"})
                for path, before in originals.items():
                    if before is None:
                        self.assertTrue(next(r for r in manifest["files"] if r["path"] == path)["absent"])
                    else:
                        self.assertEqual((backup / "files" / path).read_bytes(), before)
                        self.assertTrue((root / path).read_bytes().startswith(before))
                ledger_path = root / ".sumbi/interventions.jsonl"
                ledger = ledger_path.read_bytes()
                records = [json.loads(line) for line in ledger.splitlines()]
                self.assertEqual(len(records), len(plan.practices))
                self.assertEqual(len({r["practice_id"] for r in records}), len(records))
                for record in records:
                    self.assertEqual(record["catalog_version"], VERSION)
                    self.assertEqual(len(record["content_hash"]), 64)
                    self.assertTrue(record["utc_time"].endswith("Z"))
                    self.assertIn("prediction", record)
                    self.assertIn("judgment_policy", record)
                    self.assertEqual(record["baseline"]["status"], "no sessions found")
                    self.assertTrue((root / record["baseline"]["file"]).is_file())
                with patch.object(apply_module, "baseline", side_effect=AssertionError("Empty plans must not run collect.")):
                    self.assertEqual(apply_plan(second)["applied"], [])
                self.assertEqual(ledger_path.read_bytes(), ledger)
                self.assertEqual(len(list((root / ".sumbi/backups").iterdir())), 1)

    def test_blocks_round_trip_including_crlf(self):
        text = "# A title\n\nA rule."
        for newline in ("\n", "\r\n"):
            data = block("sample", text, newline)
            self.assertEqual(managed_blocks(data), {"sample": text})
            self.assertEqual(block("sample", managed_blocks(data)["sample"], newline), data)

    def test_malformed_duplicate_and_nested_blocks_are_rejected(self):
        bad = [b"<!-- sumbi:begin broken -->\n", b"<!-- sumbi:end broken -->\n",
               b"<!-- sumbi:begin broken -->\n<!-- sumbi:end other -->\n",
               block("sample", "one") + block("sample", "two"),
               b"<!-- sumbi:begin outer -->\n" + block("inner", "text") + b"<!-- sumbi:end outer -->\n",
               b" <!-- sumbi:begin spaced -->\n"]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(InstallError):
                managed_blocks(data)

    def test_managed_targets_preserve_crlf_bom_and_existing_documents(self):
        root = self.copy_fixture("codex-only")
        original = b"\xef\xbb\xbf# Existing instructions\r\nKeep this exact text.\r\n"
        (root / "AGENTS.md").write_bytes(original)
        doc = root / "docs/sumbi/handoff.md"
        doc.parent.mkdir(parents=True)
        doc.write_bytes(b"# Owner's document\r\nAn existing rule.\r\n")
        old_doc = doc.read_bytes()
        result = apply_plan(build_plan(root, select=["handoff"]))
        new = (root / "AGENTS.md").read_bytes()
        self.assertTrue(new.startswith(original))
        self.assertNotIn(b"\n", new[len(original):].replace(b"\r\n", b""))
        self.assertTrue(doc.read_bytes().startswith(old_doc))
        self.assertNotIn(b"\n", doc.read_bytes()[len(old_doc):].replace(b"\r\n", b""))
        self.assertNotIn("\r", build_plan(root, select=["parallel-worktrees"]).changes[0].diff())
        self.assertEqual((root / result["backup"] / "files/AGENTS.md").read_bytes(), original)

    def test_existing_block_edits_are_never_overwritten(self):
        root = self.copy_fixture("empty")
        original = block("instruction-map", "Owner-customized instructions.")
        (root / "AGENTS.md").write_bytes(original)
        plan = build_plan(root, budget=1, select=["instruction-map"])
        self.assertIn({"id": "instruction-map", "path": "AGENTS.md", "status": "existing-block-preserved"}, plan.notes)
        apply_plan(plan)
        self.assertEqual((root / "AGENTS.md").read_bytes(), original)

    def test_stale_plan_preserves_concurrent_work(self):
        root = self.copy_fixture("codex-only")
        plan = build_plan(root)
        updated = b"# Concurrent work\nDo not replace this.\n"
        (root / "AGENTS.md").write_bytes(updated)
        with self.assertRaises(InstallError):
            apply_plan(plan)
        self.assertEqual((root / "AGENTS.md").read_bytes(), updated)
        self.assertFalse((root / ".sumbi/interventions.jsonl").exists())
        self.assertFalse((root / ".sumbi/install.lock").exists())

    def test_handcrafted_plan_cannot_overwrite_unmanaged_text(self):
        root = self.copy_fixture("codex-only")
        plan = build_plan(root)
        before = (root / "AGENTS.md").read_bytes()
        forged = replace(plan, changes=[Change("AGENTS.md", before, b"Overwrite everything.\n")])
        with self.assertRaises(InstallError):
            apply_plan(forged)
        self.assertEqual((root / "AGENTS.md").read_bytes(), before)

    def test_partial_write_failure_restores_prior_files(self):
        root = self.copy_fixture("codex-only")
        plan = build_plan(root)
        original = (root / "AGENTS.md").read_bytes()
        write = apply_module._write
        calls = 0
        def fail_once(*args, **kwargs):
            nonlocal calls
            if not args[1].startswith(".sumbi/"):
                calls += 1
            if calls == 3:
                calls += 1  # Recovery writes are allowed after this one failure.
                raise OSError("Synthetic disk failure")
            return write(*args, **kwargs)
        with patch.object(apply_module, "_write", side_effect=fail_once), self.assertRaises(InstallError):
            apply_plan(plan)
        self.assertEqual((root / "AGENTS.md").read_bytes(), original)
        self.assertFalse((root / ".sumbi/interventions.jsonl").exists())
        self.assertTrue((root / ".sumbi/backups").is_dir())
        self.assertEqual((root / ".sumbi/.gitignore").read_bytes(), apply_module.LOCAL_IGNORES)
        self.assertFalse((root / ".sumbi/install.lock").exists())
        for change in plan.changes:
            if change.before is None:
                self.assertFalse((root / change.path).exists())

    def test_exclusive_new_file_publication_never_overwrites(self):
        root = self.copy_fixture("empty")
        target = root / "file.md"
        original_link = os.link
        def race(source, destination):
            Path(destination).write_bytes(b"Concurrent file.\n")
            return original_link(source, destination)
        with patch.object(apply_module.os, "link", side_effect=race), self.assertRaises(FileExistsError):
            apply_module._write(root, "file.md", None, b"Planned bytes.\n", 0o644)
        self.assertEqual(target.read_bytes(), b"Concurrent file.\n")
        self.assertEqual(list(root.glob(".sumbi-*")), [])

    def test_existing_ledger_is_backed_up_and_appended(self):
        root = self.copy_fixture("empty")
        (root / ".sumbi").mkdir()
        old = b'{"practice_id":"older-synthetic-practice"}\n'
        (root / ".sumbi/interventions.jsonl").write_bytes(old)
        result = apply_plan(build_plan(root, select=["handoff"]))
        self.assertTrue((root / ".sumbi/interventions.jsonl").read_bytes().startswith(old))
        self.assertEqual((root / result["backup"] / "files/.sumbi/interventions.jsonl").read_bytes(), old)

    def test_invalid_ledger_is_preserved_without_application(self):
        root = self.copy_fixture("empty")
        (root / ".sumbi").mkdir()
        old = b"invalid synthetic ledger\n"
        (root / ".sumbi/interventions.jsonl").write_bytes(old)
        with self.assertRaises(InstallError):
            apply_plan(build_plan(root))
        self.assertEqual((root / ".sumbi/interventions.jsonl").read_bytes(), old)
        self.assertFalse((root / "AGENTS.md").exists())

    def test_oversized_and_non_utf8_targets_fail_without_writes(self):
        root = self.copy_fixture("empty")
        for data in (b"x" * 1_048_576 + b"\n", b"\xff\n"):
            (root / "AGENTS.md").write_bytes(data)
            with self.assertRaises(InstallError):
                build_plan(root)
            self.assertEqual((root / "AGENTS.md").read_bytes(), data)
        self.assertFalse((root / ".sumbi").exists())

    def test_locks_and_missing_final_newlines_fail_closed(self):
        root = self.copy_fixture("empty")
        plan = build_plan(root)
        (root / ".sumbi").mkdir()
        (root / ".sumbi/install.lock").write_text("Synthetic lock\n", encoding="utf-8")
        with self.assertRaises(InstallError):
            apply_plan(plan)
        self.assertEqual((root / ".sumbi/install.lock").read_text(), "Synthetic lock\n")
        (root / "AGENTS.md").write_bytes(b"No final newline")
        with self.assertRaises(InstallError):
            build_plan(root)

    def test_selection_and_shared_import_dependencies(self):
        root = self.copy_fixture("claude-only")
        with self.assertRaises(InstallError):
            build_plan(root, select=["unknown"])
        with self.assertRaises(InstallError):
            build_plan(root, select=["shared-instructions"])
        plan = build_plan(root, select=["instruction-map", "shared-instructions"])
        self.assertEqual([p["id"] for p in plan.practices], ["instruction-map", "shared-instructions"])
        apply_plan(plan)
        self.assertEqual(build_plan(root, select=["instruction-map", "shared-instructions"]).changes, [])

    def test_nested_claude_import_uses_root_relative_path(self):
        root = self.copy_fixture("both")
        nested = root / "src/CLAUDE.md"
        nested.parent.mkdir()
        nested.write_text("Scoped instructions.\n", encoding="utf-8")
        apply_plan(build_plan(root, select=["shared-instructions"]))
        self.assertIn("@../AGENTS.md", nested.read_text(encoding="utf-8"))
        self.assertEqual(build_plan(root, select=["shared-instructions"]).changes, [])

    def test_nested_claude_shares_scoped_sibling(self):
        root = self.copy_fixture("both")
        (root / "src").mkdir()
        (root / "src/CLAUDE.md").write_text("Scoped rules.\n", encoding="utf-8")
        (root / "src/AGENTS.md").write_text("Scoped common rules.\n", encoding="utf-8")
        apply_plan(build_plan(root, select=["shared-instructions"]))
        self.assertIn("\n@AGENTS.md\n", (root / "src/CLAUDE.md").read_text(encoding="utf-8"))
        self.assertNotIn("@../AGENTS.md", (root / "src/CLAUDE.md").read_text(encoding="utf-8"))

    def test_hardlinked_files_are_rejected(self):
        root = self.copy_fixture("codex-only")
        shared = root / "shared.md"
        os.link(root / "AGENTS.md", shared)
        before = shared.read_bytes()
        with self.assertRaises(InstallError):
            apply_plan(build_plan(root))
        self.assertEqual(shared.read_bytes(), before)

    def test_symlink_targets_and_linked_metadata_are_rejected(self):
        root = self.copy_fixture("empty")
        outside = root.parent / "outside.md"
        outside.write_text("Synthetic outside data.\n", encoding="utf-8")
        try:
            (root / "AGENTS.md").symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation is unavailable on this test host.")
        self.assertEqual(build_plan.__module__, "sumbi.install.planner")
        with self.assertRaises(InstallError):
            build_plan(root)
        self.assertEqual(outside.read_text(), "Synthetic outside data.\n")
        (root / "AGENTS.md").unlink()
        (root / ".sumbi").symlink_to(root.parent, target_is_directory=True)
        with self.assertRaises(InstallError):
            apply_plan(build_plan(root))

    def test_existing_mode_is_preserved(self):
        if os.name == "nt":
            self.skipTest("POSIX mode bits are not enforced on Windows.")
        root = self.copy_fixture("codex-only")
        (root / "AGENTS.md").chmod(0o640)
        apply_plan(build_plan(root, select=["handoff"]))
        self.assertEqual(stat.S_IMODE((root / "AGENTS.md").stat().st_mode), 0o640)

    def test_catalog_is_text_only_and_predictions_are_conditional(self):
        catalog = load_catalog()
        self.assertEqual(len(catalog), 8)
        self.assertEqual(len({p["id"] for p in catalog}), 8)
        for practice in catalog:
            self.assertTrue(practice["gap_ids"])
            self.assertIn(practice["risk"], {"low", "medium"})
            self.assertTrue(practice["prediction"]["condition"])
            self.assertTrue(practice["judgment"])
            for item in practice["files"]:
                self.assertEqual(item["mode"], "managed-block")
                self.assertTrue(item["path"].endswith(".md"))
