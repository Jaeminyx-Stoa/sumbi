"""Synthetic reviewed improvements, privacy boundaries, and recovery."""

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sumbi import SCHEMA_VERSION
from sumbi.cli import main
from sumbi.install import apply as transactions
from sumbi.install.errors import InstallError
from sumbi.install.improve import observations, build_improvement, apply_improvement
from sumbi.install.planner import digest
from sumbi.install.revert import revert_install
from sumbi.judge.interventions import exposure_gap
from sumbi.judge.registration import read_registration


class EvidenceImproveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "repo"
        self.root.mkdir()
        self.target = self.root / "device.py"
        self.target.write_bytes(b"LIMIT = 1\n")
        self.evidence = base / "evidence.json"
        self.bundle = base / "bundle.json"
        self.report = {"schema_version": SCHEMA_VERSION,
            "window": {"since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z",
                "bounds": "[since,until)"},
            "summary": {"sessions": 2, "counts": {"tool_errors": 3, "api_errors": 0,
                "compactions": 1}, "tokens": {"total": 100}},
            "coverage": {"unattributed_share": 0.1},
            "private-free-text": "DO-NOT-EXPORT"}
        self.evidence.write_text(json.dumps(self.report))
        after = "LIMIT = 2\n"
        self.raw = {"version": 1, "intervention_id": "device-fix",
            "evidence_sha256": digest(self.evidence.read_bytes()),
            "evidence_refs": [{"pointer": "/summary/counts/tool_errors", "value": 3}],
            "diagnosis": {"id": "capacity", "hypothesis": "PRIVATE-HYPOTHESIS",
                "rationale": "PRIVATE-RATIONALE"},
            "prediction": {"metric": "tokens_per_success", "direction": "decrease",
                "rough_size_percent": 10},
            "verification": {"id": "unit-check", "status": "passed",
                "evidence_sha256": "a" * 64, "declaration": "PRIVATE-VERIFICATION"},
            "review": {"author_family": "family-a", "reviewer_family": "family-b",
                "status": "approved", "safety_gates_preserved": True},
            "changes": [{"path": "device.py", "before_sha256": digest(self.target.read_bytes()),
                "after_sha256": digest(after.encode()), "after_text": after}]}
        self.save()

    def save(self):
        self.bundle.write_text(json.dumps(self.raw))

    def plan(self):
        return build_improvement(self.root, self.evidence, self.bundle)

    def apply(self):
        plan = self.plan()
        return apply_improvement(plan, plan.bundle_sha256)

    def invoke(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["improve", "--root", str(self.root), *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_observations_are_numeric_and_not_causal(self):
        _, result = observations(self.evidence)
        self.assertEqual(result["causality"], "not-established")
        self.assertEqual(result["outcome_verdict"], "not-available-from-collect")
        self.assertEqual(next(f["value"] for f in result["findings"] if f["id"] == "tool-errors"), 3)
        self.assertNotIn("DO-NOT-EXPORT", json.dumps(result))

    def test_empty_collect_is_honest_no_outcome(self):
        self.report["summary"]["sessions"] = 0
        self.report["summary"]["counts"] = {"tool_errors": 0}
        self.report["summary"]["tokens"]["total"] = 0
        self.evidence.write_text(json.dumps(self.report))
        _, result = observations(self.evidence)
        self.assertEqual(result["findings"][0]["value"], 0)
        self.assertEqual(result["outcome_verdict"], "not-available-from-collect")

    def test_bad_collect_numbers_and_schema_are_rejected(self):
        for value in [True, -1, float("nan"), "PRIVATE"]:
            with self.subTest(value=value):
                self.report["summary"]["sessions"] = value
                self.evidence.write_text(json.dumps(self.report))
                with self.assertRaises(InstallError):
                    observations(self.evidence)
        self.evidence.write_text('{"schema_version":"unknown"}')
        with self.assertRaises(InstallError):
            observations(self.evidence)

    def test_diff_and_summary_separate_private_content(self):
        plan = self.plan()
        self.assertIn("+LIMIT = 2", plan.changes[0].diff())
        summary = json.dumps(plan.summary)
        for private in ["PRIVATE", "device.py", str(self.temp.name), "DO-NOT-EXPORT"]:
            self.assertNotIn(private, summary)
        self.assertEqual(plan.summary["change_count"], 1)
        self.assertFalse((self.root / ".sumbi").exists())

    def test_apply_records_baseline_and_reverts_source_without_erasing_history(self):
        result = self.apply()
        self.assertEqual(self.target.read_bytes(), b"LIMIT = 2\n")
        ledger = self.root / ".sumbi/interventions.jsonl"
        record = json.loads(ledger.read_text())
        self.assertEqual(record["action"], "improve")
        self.assertEqual(record["evidence_sha256"], digest(self.evidence.read_bytes()))
        for private in ["PRIVATE", "device.py", "DO-NOT-EXPORT", str(self.temp.name)]:
            self.assertNotIn(private, ledger.read_text())
        reverted = revert_install(self.root, result["backup_id"])
        self.assertEqual(reverted["refused"], 0)
        self.assertEqual(self.target.read_bytes(), b"LIMIT = 1\n")
        history = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual([r["action"] for r in history], ["improve", "revert"])
        self.assertEqual(revert_install(self.root, result["backup_id"])["refused"], 0)
        self.assertEqual(len(ledger.read_text().splitlines()), 2)

    def test_new_file_has_conservative_revert(self):
        self.raw["changes"][0].update(path="tools/new.py", before_sha256=None)
        self.save()
        result = self.apply()
        new = self.root / "tools/new.py"
        self.assertEqual(new.read_bytes(), b"LIMIT = 2\n")
        reverted = revert_install(self.root, result["backup_id"])
        self.assertEqual(reverted["refused"], 0)
        self.assertFalse(new.exists())

    def test_modified_target_is_preserved_on_revert(self):
        result = self.apply()
        self.target.write_bytes(b"OWNER_EDIT = 3\n")
        self.assertEqual(revert_install(self.root, result["backup_id"])["refused"], 1)
        self.assertEqual(self.target.read_bytes(), b"OWNER_EDIT = 3\n")

    def test_review_acknowledgement_and_attestation_required(self):
        plan = self.plan()
        for value in [None, "b" * 64, "PRIVATE"]:
            with self.assertRaises(InstallError):
                apply_improvement(plan, value)
        for field, value in [("reviewer_family", "family-a"), ("status", "pending"),
            ("safety_gates_preserved", False)]:
            previous = self.raw["review"][field]
            self.raw["review"][field] = value
            self.save()
            with self.assertRaises(InstallError):
                self.plan()
            self.raw["review"][field] = previous

    def test_pending_proposal_shows_diff_but_cannot_apply(self):
        self.raw["review"].update(status="pending", reviewer_family=None,
            safety_gates_preserved=None)
        self.raw["verification"].update(status="pending", evidence_sha256=None)
        self.save()
        plan = self.plan()
        self.assertIn("+LIMIT = 2", plan.changes[0].diff())
        code, out, err = self.invoke("--evidence", self.evidence, "--bundle", self.bundle)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("+LIMIT = 2", out)
        with self.assertRaises(InstallError):
            apply_improvement(plan, plan.bundle_sha256)
        self.assertEqual(self.target.read_bytes(), b"LIMIT = 1\n")

    def test_stale_target_evidence_and_bundle_refused(self):
        plan = self.plan()
        self.target.write_bytes(b"LIMIT = 9\n")
        with self.assertRaises(InstallError):
            apply_improvement(plan, plan.bundle_sha256)
        self.target.write_bytes(b"LIMIT = 1\n")
        self.evidence.write_text(self.evidence.read_text() + " ")
        with self.assertRaises(InstallError):
            apply_improvement(plan, plan.bundle_sha256)
        self.evidence.write_text(json.dumps(self.report))
        self.raw["diagnosis"]["hypothesis"] = "changed private hypothesis"
        self.save()
        with self.assertRaises(InstallError):
            apply_improvement(plan, plan.bundle_sha256)
        self.assertEqual(self.target.read_bytes(), b"LIMIT = 1\n")

    def test_bad_numeric_reference_is_refused(self):
        for ref in [{"pointer": "/summary/counts/tool_errors", "value": 4},
            {"pointer": "/private-free-text", "value": 3},
            {"pointer": "/summary/counts/tool_errors", "value": True}]:
            self.raw["evidence_refs"] = [ref]
            self.save()
            with self.assertRaises(InstallError):
                self.plan()

    def test_paths_metadata_private_areas_and_unsupported_payloads_refused(self):
        for name in ["../device.py", "/tmp/device.py", "C:/device.py", "a/../device.py",
            "a\\device.py", ".git/config.json", ".sumbi/config.toml", "logs/data.txt",
            ".codex/sessions/data.json", ".claude/projects/data.json", ".env", ".env.json",
            "secrets/key.py", "credentials.json", "CON.py", "file.exe"]:
            with self.subTest(name=name):
                self.raw["changes"][0]["path"] = name
                self.save()
                with self.assertRaises(InstallError):
                    self.plan()

    def test_link_and_hardlink_targets_refused(self):
        self.target.unlink()
        external = Path(self.temp.name) / "external.py"
        external.write_bytes(b"LIMIT = 1\n")
        self.target.symlink_to(external)
        with self.assertRaises(InstallError):
            self.plan()
        self.target.unlink()
        self.target.hardlink_to(external)
        with self.assertRaises(InstallError):
            self.plan()

    def test_invalid_empty_duplicate_and_no_change_payloads_refused(self):
        entry = dict(self.raw["changes"][0])
        for changes in [[], [entry] * 2, [entry] * 4,
            [{**entry, "after_text": "", "after_sha256": digest(b"")}],
            [{**entry, "after_text": "LIMIT = 1\n", "after_sha256": digest(b"LIMIT = 1\n")}],
            [{**entry, "after_text": "NUL\x00", "after_sha256": digest(b"NUL\x00")}]]:
            self.raw["changes"] = changes
            self.save()
            with self.assertRaises(InstallError):
                self.plan()
        self.bundle.write_text('{"version":1,"version":1}')
        with self.assertRaises(InstallError):
            self.plan()

    def test_existing_mode_is_preserved_new_modes_not_supplied(self):
        self.target.chmod(0o755)
        result = self.apply()
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        revert_install(self.root, result["backup_id"])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)

    def test_same_intervention_cannot_be_applied_twice(self):
        self.apply()
        entry = self.raw["changes"][0]
        entry.update(before_sha256=digest(self.target.read_bytes()), after_text="LIMIT = 3\n",
            after_sha256=digest(b"LIMIT = 3\n"))
        self.save()
        with self.assertRaises(InstallError):
            self.apply()
        self.assertEqual(self.target.read_bytes(), b"LIMIT = 2\n")

    def test_transaction_rolls_back_source_when_ledger_publication_fails(self):
        write = transactions._write
        def fail_ledger(root, relative, expected, data, mode):
            if relative == ".sumbi/interventions.jsonl":
                raise OSError("synthetic failure")
            return write(root, relative, expected, data, mode)
        with patch.object(transactions, "_write", side_effect=fail_ledger):
            with self.assertRaises(InstallError):
                self.apply()
        self.assertEqual(self.target.read_bytes(), b"LIMIT = 1\n")
        self.assertFalse((self.root / ".sumbi/interventions.jsonl").exists())
        self.assertFalse((self.root / ".sumbi/install.lock").exists())

    def test_transaction_rolls_back_new_file_when_manifest_completion_fails(self):
        self.raw["changes"][0].update(path="tools/new.py", before_sha256=None)
        self.save()
        write = transactions._write
        def fail_manifest(root, relative, expected, data, mode):
            if relative.endswith("/manifest.json"):
                raise OSError("synthetic failure")
            return write(root, relative, expected, data, mode)
        with patch.object(transactions, "_write", side_effect=fail_manifest):
            with self.assertRaises(InstallError):
                self.apply()
        self.assertFalse((self.root / "tools/new.py").exists())
        self.assertFalse((self.root / ".sumbi/interventions.jsonl").exists())

    def test_compare_reads_actual_write_from_improvement_ledger(self):
        self.apply()
        path = Path(self.temp.name) / "registration.json"
        now = datetime.now(timezone.utc)
        raw = {"intervention_id": "device-fix", "registered_at": (now-timedelta(days=1)).isoformat(),
            "applied_at": now.isoformat(), "predictions": [self.raw["prediction"]],
            "non_inferiority_margin_pp": 5, "sample_size_per_arm": 20, "follow_up_days": 1,
            "before": {"since": (now-timedelta(days=2)).isoformat(), "until": now.isoformat()},
            "after": {"since": now.isoformat(), "until": (now+timedelta(days=2)).isoformat()}}
        path.write_text(json.dumps(raw))
        gap = exposure_gap(read_registration(path), self.root / ".sumbi/interventions.jsonl")
        self.assertIsNotNone(gap)
        self.assertGreaterEqual(gap["seconds"], 0)

    def test_cli_observations_dry_run_public_json_apply_and_revert(self):
        code, out, err = self.invoke("--evidence", self.evidence)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["causality"], "not-established")
        code, out, err = self.invoke("--evidence", self.evidence, "--bundle", self.bundle)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("+LIMIT = 2", out)
        code, out, err = self.invoke("--evidence", self.evidence, "--bundle", self.bundle, "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("device.py", out)
        self.assertEqual(json.loads(out)["status"], "proposed")
        code, out, err = self.invoke("--evidence", self.evidence, "--bundle", self.bundle,
            "--apply", "--reviewed-sha256", digest(self.bundle.read_bytes()), "--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["status"], "applied")
        code, out, err = self.invoke("--revert", result["backup_id"], "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["files"], 1)
        self.assertEqual(self.target.read_bytes(), b"LIMIT = 1\n")

    def test_cli_invalid_combinations_fail_without_echoing_private_inputs(self):
        for args in [[], ["--apply"], ["--evidence", self.evidence, "--apply"],
            ["--evidence", self.evidence, "--reviewed-sha256", "PRIVATE"],
            ["--evidence", self.evidence, "--registration", "PRIVATE"],
            ["--revert", "PRIVATE", "--bundle", self.bundle]]:
            with self.subTest(args=args):
                code, out, err = self.invoke(*args)
                self.assertEqual(code, 1)
                self.assertNotIn("PRIVATE", err)
                self.assertEqual(self.target.read_bytes(), b"LIMIT = 1\n")


if __name__ == "__main__":
    unittest.main()
