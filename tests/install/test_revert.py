"""Synthetic hash-guarded restore, refusal, retry and metadata safety."""

import json
import os
import stat
import unittest
from unittest.mock import patch
from support import require_file_modes

from sumbi.install import apply_plan, build_plan, revert_install
from sumbi.install.errors import InstallError
from sumbi.install import revert as revert_module
from sumbi.install.__main__ import main
from .test_inventory import OfflineTest


class RevertTests(OfflineTest):
    def installed(self):
        root = self.copy_fixture("codex-only")
        plan = build_plan(root, select=["handoff"])
        result = apply_plan(plan)
        backup_id = result["backup"].rsplit("/", 1)[1]
        return root, plan, backup_id

    def ledger(self, root):
        return [json.loads(line) for line in (root / ".sumbi/interventions.jsonl").read_bytes().splitlines()]

    def test_missing_metadata_is_a_clear_non_mutating_error(self):
        root = self.copy_fixture("empty")
        with self.assertRaisesRegex(InstallError, "No local install metadata"):
            revert_install(root, "20300115T000000.000000Z")
        self.assertFalse((root / ".sumbi").exists())

    def test_existing_ledger_permissions_survive_revert(self):
        root, _, backup_id = self.installed()
        ledger = root / ".sumbi/interventions.jsonl"
        if os.name == "nt":
            self.skipTest("POSIX permission bits are not represented on Windows.")
        require_file_modes(self, root, (0o640,))
        ledger.chmod(0o640)
        revert_install(root, backup_id)
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o640)

    @unittest.skipIf(os.name == "nt", "POSIX special permission bits are not represented on Windows.")
    def test_original_permission_bits_and_ledger_mode_survive_apply_revert(self):
        require_file_modes(self, self.copy_fixture("empty"), (0o640, 0o2640, 0o4640, 0o1640, 0o7640))
        for mode in (0o640, 0o2640, 0o4640, 0o1640, 0o7640):
            with self.subTest(mode=oct(mode)):
                root = self.copy_fixture("codex-only")
                target = root / "AGENTS.md"
                original = target.read_bytes()
                target.chmod(mode)
                ledger = root / ".sumbi/interventions.jsonl"
                ledger.parent.mkdir(mode=0o700)
                history = b'{"action":"prior-synthetic-intervention"}\n'
                ledger.write_bytes(history)
                ledger.chmod(mode)
                result = apply_plan(build_plan(root, select=["handoff"]))
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
                self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), mode)
                self.assertEqual(revert_install(root, result["backup_id"])["refused"], 0)
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
                self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), mode)
                self.assertTrue(ledger.read_bytes().startswith(history))
                audit = ledger.read_bytes()
                revert_install(root, result["backup_id"])
                self.assertEqual(ledger.read_bytes(), audit)
                self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), mode)

    def test_happy_revert_restores_absence_bytes_and_append_only_audit(self):
        root, plan, backup_id = self.installed()
        before_ledger = (root / ".sumbi/interventions.jsonl").read_bytes()
        later = b'{"practice_id":"later-synthetic-intervention"}\n'
        with (root / ".sumbi/interventions.jsonl").open("ab") as stream:
            stream.write(later)
        result = revert_install(root, backup_id)
        self.assertEqual(result["refused"], 0)
        for change in plan.changes:
            self.assertEqual((root / change.path).read_bytes() if (root / change.path).exists() else None, change.before)
        self.assertTrue((root / ".sumbi/interventions.jsonl").read_bytes().startswith(before_ledger + later))
        self.assertEqual(self.ledger(root)[-1]["action"], "revert")
        self.assertEqual(self.ledger(root)[-1]["backup_id"], backup_id)
        self.assertTrue((root / ".sumbi/.gitignore").exists())
        self.assertTrue((root / ".sumbi/backups" / backup_id / "manifest.json").exists())

    def test_modified_file_refused_per_file_then_retry_and_idempotence(self):
        root, plan, backup_id = self.installed()
        target = root / "AGENTS.md"
        manual = target.read_bytes().replace(b"Leave a handoff document", b"Owner's changed handoff rule")
        target.write_bytes(manual)
        result = revert_install(root, backup_id)
        self.assertEqual(result["refused"], 1)
        self.assertEqual(target.read_bytes(), manual)
        self.assertFalse((root / "docs/sumbi/handoff.md").exists())
        ledger = (root / ".sumbi/interventions.jsonl").read_bytes()
        self.assertEqual(revert_install(root, backup_id)["refused"], 1)
        self.assertEqual((root / ".sumbi/interventions.jsonl").read_bytes(), ledger)
        target.write_bytes(next(c.after for c in plan.changes if c.path == "AGENTS.md"))
        self.assertEqual(revert_install(root, backup_id)["refused"], 0)
        ledger = (root / ".sumbi/interventions.jsonl").read_bytes()
        target.write_bytes(b"New owner work.\n")
        new_doc = root / "docs/sumbi/handoff.md"
        new_doc.write_bytes(b"New owner document.\n")
        self.assertEqual(revert_install(root, backup_id)["refused"], 0)
        self.assertEqual(target.read_bytes(), b"New owner work.\n")
        self.assertEqual(new_doc.read_bytes(), b"New owner document.\n")
        self.assertEqual((root / ".sumbi/interventions.jsonl").read_bytes(), ledger)

    def test_invalid_old_prepared_or_tampered_manifest_fails_before_writes(self):
        root, plan, backup_id = self.installed()
        manifest_path = root / ".sumbi/backups" / backup_id / "manifest.json"
        original = json.loads(manifest_path.read_text())
        for mutation in ("old", "prepared", "hash", "traversal", "metadata", "duplicate", "missing-applied",
                         "negative-mode", "file-type-mode", "boolean-mode"):
            manifest = json.loads(json.dumps(original))
            if mutation == "old":
                manifest.pop("version")
            elif mutation == "prepared":
                manifest["status"] = "prepared"
            elif mutation == "hash":
                manifest["files"][0]["before_hash"] = "0" * 64
            elif mutation == "traversal":
                manifest["files"][0]["path"] = "../outside.md"
            elif mutation == "metadata":
                manifest["files"][0]["path"] = ".git/config"
            elif mutation == "duplicate":
                manifest["files"].append(manifest["files"][0])
            elif mutation == "missing-applied":
                manifest["files"][0].pop("applied_hash")
            elif mutation.endswith("-mode"):
                manifest["files"][0]["mode"] = {"negative-mode": -1, "file-type-mode": 0o100644,
                                              "boolean-mode": True}[mutation]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.subTest(mutation=mutation), self.assertRaises(InstallError):
                revert_install(root, backup_id)
            for change in plan.changes:
                self.assertEqual((root / change.path).read_bytes(), change.after)
            self.assertFalse((root / ".sumbi/install.lock").exists())
        manifest_path.write_text(json.dumps(original), encoding="utf-8")
        (root / ".sumbi/backups" / backup_id / "files/AGENTS.md").write_bytes(b"Corrupt backup.\n")
        with self.assertRaises(InstallError):
            revert_install(root, backup_id)

    def test_ids_locks_and_unsafe_current_targets(self):
        root, plan, backup_id = self.installed()
        for identifier in ("../escape", "", "id/child", "20300101T000000.000000Z/.."):
            with self.assertRaises(InstallError):
                revert_install(root, identifier)
        lock = root / ".sumbi/install.lock"
        lock.write_bytes(b"Owner lock.\n")
        with self.assertRaises(InstallError):
            revert_install(root, backup_id)
        self.assertEqual(lock.read_bytes(), b"Owner lock.\n")
        lock.unlink()
        os.link(root / "AGENTS.md", root / "linked.md")
        result = revert_install(root, backup_id)
        self.assertEqual(result["refused"], 1)
        self.assertEqual((root / "AGENTS.md").read_bytes(), next(c.after for c in plan.changes if c.path == "AGENTS.md"))

    def test_ledger_write_failure_rolls_back_reverted_files(self):
        root, plan, backup_id = self.installed()
        write = revert_module._write
        def fail_ledger(*args):
            if args[1] == ".sumbi/interventions.jsonl":
                raise OSError("Synthetic disk error")
            return write(*args)
        with patch.object(revert_module, "_write", side_effect=fail_ledger), self.assertRaises(InstallError):
            revert_install(root, backup_id)
        for change in plan.changes:
            self.assertEqual((root / change.path).read_bytes(), change.after)
        self.assertFalse((root / ".sumbi/install.lock").exists())

    def test_cli_revert_exit_status(self):
        root, _, backup_id = self.installed()
        (root / "AGENTS.md").write_bytes(b"Changed manually.\n")
        from contextlib import redirect_stdout, redirect_stderr
        import io
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["--root", str(root), "--revert", backup_id])
        self.assertEqual(code, 1)
        self.assertIn('"refused": 1', output.getvalue())
        self.assertNotIn(str(root), output.getvalue() + errors.getvalue())

    def test_symlink_target_refused_and_linked_manifest_rejected(self):
        root, _, backup_id = self.installed()
        outside = root.parent / "outside.md"
        outside.write_bytes(b"Synthetic outside work.\n")
        target = root / "AGENTS.md"
        applied = target.read_bytes()
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            target.write_bytes(applied)
            self.skipTest("Symlink creation is unavailable on this test host.")
        result = revert_install(root, backup_id)
        self.assertEqual(result["refused"], 1)
        self.assertEqual(outside.read_bytes(), b"Synthetic outside work.\n")
        manifest = root / ".sumbi/backups" / backup_id / "manifest.json"
        backup_data = manifest.read_bytes()
        manifest.unlink()
        outside_manifest = root.parent / "manifest.json"
        outside_manifest.write_bytes(backup_data)
        manifest.symlink_to(outside_manifest)
        with self.assertRaises(InstallError):
            revert_install(root, backup_id)
        self.assertEqual(outside_manifest.read_bytes(), backup_data)

    def test_invalid_ledger_and_missing_target_preserve_other_work(self):
        root, plan, backup_id = self.installed()
        ledger = root / ".sumbi/interventions.jsonl"
        previous = ledger.read_bytes()
        ledger.write_bytes(b"Invalid synthetic ledger\n")
        with self.assertRaises(InstallError):
            revert_install(root, backup_id)
        for change in plan.changes:
            self.assertEqual((root / change.path).read_bytes(), change.after)
        ledger.write_bytes(previous)
        (root / "AGENTS.md").unlink()
        self.assertEqual(revert_install(root, backup_id)["refused"], 1)
        self.assertFalse((root / "AGENTS.md").exists())
