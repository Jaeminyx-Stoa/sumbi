"""Synthetic external intervention registration, checks, signals and JSON files."""

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from sumbi import SCHEMA_VERSION
from sumbi.cli import main
from sumbi.install import external
from sumbi.install.errors import InstallError
from sumbi.install.external import check_external, read_external, register_external
from sumbi.install.planner import digest
from sumbi.judge.interventions import exposure_gap
from support import IsolatedTemporaryDirectory


def invoke(*args):
    output, errors = StringIO(), StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = main(["improve", *map(str, args)])
    return code, output.getvalue(), errors.getvalue()


def report(since, until, sessions, tokens, *, tool_results=10, tool_errors=1):
    return {"schema_version": SCHEMA_VERSION,
        "window": {"since": since, "until": until, "bounds": "[since,until)"},
        "summary": {"sessions": sessions,
            "counts": {"tool_errors": tool_errors, "tool_results": tool_results,
                "api_errors": 0, "compactions": 2},
            "tokens": {"total": tokens, "new_input": tokens // 2, "cache_write": 0,
                "cache_read": tokens // 4, "output": tokens // 4, "reasoning_output": None},
            "time": {"active_seconds": {"value": 3600.0},
                "wall_span_seconds": {"value": 7200.0}}},
        "coverage": {"unattributed_share": 0.0}, "private-free-text": "DO-NOT-EXPORT"}


REGISTER = ("--applied-at", "2030-01-03T00:00:00Z", "--change", "worker-time-cap",
    "--predict", "tokens-per-day:decrease:30", "--predict", "tokens-per-success:decrease:10",
    "--predict", "worker-long-tail-share:decrease:70",
    "--revert-if", "tool-error-rate:increase:50", "--before-days", "2", "--after-days", "2")


class ExternalInterventionTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory(prefix="sumbi-external-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.before = self.base / "before.json"
        self.after = self.base / "after.json"
        self.before.write_text(json.dumps(report("2030-01-01T00:00:00Z",
            "2030-01-03T00:00:00Z", 10, 2000)), encoding="utf-8")
        # Unequal after length: per-day normalization keeps the comparison readable.
        self.after.write_text(json.dumps(report("2030-01-03T00:00:00Z",
            "2030-01-04T00:00:00Z", 4, 700, tool_results=10, tool_errors=2)),
            encoding="utf-8")

    def register(self, *extra):
        return invoke("--root", self.root, "--register-external", "cap-round", *REGISTER, *extra)

    def check(self, *extra):
        return invoke("--root", self.root, "--check", "cap-round", "--before", self.before,
            "--after", self.after, *extra)

    def test_registration_round_trip_and_exposure_reader(self):
        code, output, errors = self.register("--json")
        self.assertEqual((code, errors), (0, ""))
        record = json.loads(output)
        self.assertEqual(read_external(self.root, "cap-round"), record)
        self.assertEqual(record["action"], "register")
        self.assertEqual(record["windows"]["before"],
            {"since": "2030-01-01T00:00:00Z", "until": "2030-01-03T00:00:00Z"})
        self.assertEqual(record["windows"]["after"]["until"], "2030-01-05T00:00:00Z")
        self.assertTrue(record["preregistered"])  # Synthetic 2030 apply time.
        ledger = (self.root / ".sumbi/interventions.jsonl").read_bytes()
        self.assertEqual(len(ledger.splitlines()), 1)
        # Existing compare exposure accounting reads the registered apply time.
        registration = type("R", (), {"intervention_id": "cap-round",
            "applied_at": datetime(2030, 1, 3, tzinfo=timezone.utc)})()
        gap = exposure_gap(registration, self.root / ".sumbi/interventions.jsonl")
        self.assertEqual(gap["seconds"], 0)
        code, _, errors = self.register()
        self.assertEqual(code, 1)
        self.assertIn("already exists", errors)
        self.assertEqual((self.root / ".sumbi/interventions.jsonl").read_bytes(), ledger)

    def test_late_registration_is_not_preregistered(self):
        record = register_external(self.root, intervention_id="late-round",
            applied_at=datetime(2030, 1, 3, tzinfo=timezone.utc), changes=["a-change"],
            predictions=[{"metric": "tokens-per-day", "direction": "decrease",
                "rough_size_percent": 5.0}],
            revert_if=[{"metric": "sessions-per-day", "direction": "decrease",
                "threshold_percent": 50.0}], before_days=1.0, after_days=1.0,
            now=datetime(2030, 1, 4, tzinfo=timezone.utc))
        self.assertFalse(record["preregistered"])
        self.assertEqual(record["registered_at"], "2030-01-04T00:00:00Z")
        self.assertEqual(read_external(self.root, "late-round"), record)

    def test_check_output_without_success_denominator(self):
        self.assertEqual(self.register()[0], 0)
        code, output, errors = self.check("--json")
        self.assertEqual((code, errors), (0, ""))
        summary = json.loads(output)
        self.assertEqual(summary["verdict"], "observation-not-causal")
        self.assertEqual(summary["success"]["status"], "not-available")
        metrics = {row["id"]: row for row in summary["metrics"]}
        self.assertEqual(metrics["sessions-per-day"]["before"], 5)
        self.assertEqual(metrics["sessions-per-day"]["after"], 4)
        self.assertEqual(metrics["tokens-per-day"]["change_percent"], -30)
        self.assertEqual(metrics["tool-error-rate"]["after"], 0.2)
        self.assertIsNone(metrics["reasoning-output-subset-tokens-per-day"]["before"])
        predictions = {row["metric"]: row for row in summary["predictions"]}
        self.assertEqual(predictions["tokens-per-day"]["observed_direction"], "decrease")
        self.assertEqual(predictions["tokens-per-success"]["reason"], "no-success-denominator")
        # A known profile metric absent from these minimal reports is not reported.
        self.assertEqual(predictions["worker-long-tail-share"]["reason"], "not-reported")
        self.assertTrue(summary["revert_if"][0]["crossed"])
        self.assertFalse(summary["windows"]["after"]["matches_registration"])
        self.assertTrue(summary["windows"]["before"]["matches_registration"])
        self.assertNotIn("DO-NOT-EXPORT", output)
        self.assertNotIn("signals", summary)

    def test_role_metrics_read_profile_summary_or_one_project(self):
        def profiled(path, share, project_share, median_seconds):
            data = json.loads(path.read_text(encoding="utf-8"))
            role = {"long_tail": {"token_share": share}, "tokens_per_day": {"total": 10},
                "tool_latency": {"shell": {"median_seconds": median_seconds}}}
            data["profile"] = {"summary": {"roles": {"worker": role},
                "fan_out": {"children_max": 4}},
                "projects": {"project_abc": {"roles": {"worker": {
                    "long_tail": {"token_share": project_share}}}}}}
            path.write_text(json.dumps(data), encoding="utf-8")
        profiled(self.before, 0.5, 0.8, 0.1)
        profiled(self.after, 0.25, 0.2, 2.0)
        self.assertEqual(self.register()[0], 0)
        code, output, errors = self.check("--json")
        self.assertEqual((code, errors), (0, ""))
        summary = json.loads(output)
        self.assertEqual(summary["role_metrics_scope"], "all")
        predictions = {row["metric"]: row for row in summary["predictions"]}
        tail = predictions["worker-long-tail-share"]
        self.assertEqual((tail["status"], tail["before"], tail["after"]), ("observed", 0.5, 0.25))
        self.assertEqual(tail["change_percent"], -50)
        metrics = {row["id"]: row for row in summary["metrics"]}
        self.assertEqual(metrics["worker-tokens-per-day"]["after"], 10)  # Already per day.
        self.assertEqual(metrics["worker-shell-tool-median-seconds"]["after"], 2.0)
        self.assertEqual(metrics["children-per-parent-max"]["before"], 4)
        self.assertIsNone(metrics["orchestrator-long-tail-share"]["before"])
        code, output, _ = self.check("--json", "--project-key", "project_abc")
        summary = json.loads(output)
        self.assertEqual(summary["role_metrics_scope"], "project_abc")
        tail = {row["metric"]: row for row in summary["predictions"]}["worker-long-tail-share"]
        self.assertEqual((tail["before"], tail["after"]), (0.8, 0.2))
        code, _, errors = self.check("--project-key", "../Project")
        self.assertEqual(code, 1)
        self.assertIn("Project must be", errors)

    def test_signal_counts_and_invalid_labels(self):
        self.assertEqual(self.register()[0], 0)
        signals = self.base / "events.jsonl"
        rows = [{"ts": "2030-01-02T00:00:00Z", "event": "warn", "text": "PRIVATE-NOTE"},
            {"ts": 1893628800, "event": "cap"},  # 2030-01-03T00:00:00Z, after window
            {"ts": "2030-01-03T01:00:00+00:00", "event": "cap"},
            {"ts": "2030-01-03T02:00:00Z", "event": "Bad Label"},
            {"ts": "2030-01-02T02:00:00Z", "event": "x" * 65},
            {"ts": "2030-02-01T00:00:00Z", "event": "cap"},
            {"ts": "2030-01-02T03:00:00", "event": "naive-time"},
            {"event": "missing-time"}, ["not", "an", "object"]]
        signals.write_text("".join(json.dumps(r) + "\n" for r in rows) + "{broken\n\n",
            encoding="utf-8")
        code, output, errors = self.check("--signals", signals, "--json")
        self.assertEqual((code, errors), (0, ""))
        counted = json.loads(output)["signals"]
        self.assertEqual(counted["before"], {"labels": {"warn": 1}, "invalid": 1})
        self.assertEqual(counted["after"], {"labels": {"cap": 2}, "invalid": 1})
        self.assertEqual((counted["unreadable_lines"], counted["outside_windows"]), (4, 1))
        for private in ("PRIVATE-NOTE", "Bad Label", "missing-time", "naive-time"):
            self.assertNotIn(private, output)

    def test_json_out_has_no_bom_and_matches_json_stdout(self):
        destination = self.base / "out" / "observations.json"
        code, output, errors = invoke("--evidence", self.before, "--json", "--json-out",
            destination)
        self.assertEqual((code, errors), (0, ""))
        data = destination.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(data.decode("utf-8"), output)
        self.assertEqual(self.register()[0], 0)
        checked = self.base / "check.json"
        code, output, _ = self.check("--json-out", checked)
        self.assertEqual(code, 0)
        self.assertIn("Observation, not causation", output)
        self.assertEqual(json.loads(checked.read_bytes().decode("utf-8"))["intervention_id"],
            "cap-round")
        code, output, _ = invoke("--guide", "--json", "--json-out", self.base / "guide.json")
        self.assertEqual(code, 0)
        self.assertEqual((self.base / "guide.json").read_text(encoding="utf-8"), output)

    def test_json_out_refuses_inputs_state_and_log_directories(self):
        for destination in (self.before, self.root / ".sumbi" / "out.json"):
            code, _, errors = invoke("--root", self.root, "--evidence", self.before,
                "--json-out", destination)
            self.assertEqual(code, 1)
            self.assertIn("must not overwrite", errors)
        with patch.object(Path, "home", return_value=self.base / "home"):
            code, _, errors = invoke("--evidence", self.before, "--json-out",
                self.base / "home/.claude/projects/out.json")
        self.assertEqual(code, 1)
        self.assertIn("must not overwrite", errors)
        self.assertEqual(json.loads(self.before.read_text(encoding="utf-8"))["summary"]
            ["sessions"], 10)

    def test_invalid_registration_inputs_are_refused_without_writing(self):
        cases = [("--change", "Free text here"), ("--predict", "tokens-per-day:down:30"),
            ("--predict", "tokens-per-day:decrease:-1"), ("--revert-if", "metric:increase:nan")]
        for option, value in cases:
            args = list(REGISTER)
            args[args.index(option) + 1] = value
            code, _, errors = invoke("--root", self.root, "--register-external", "cap-round",
                *args)
            self.assertEqual(code, 1, value)
            self.assertNotIn(value, errors)
        code, _, errors = invoke("--root", self.root, "--register-external", "Bad ID", *REGISTER)
        self.assertEqual(code, 1)
        code, _, errors = invoke("--root", self.root, "--register-external", "cap-round",
            "--change", "a-change")
        self.assertEqual(code, 1)
        self.assertIn("requires", errors)
        code, _, errors = invoke("--root", self.root, "--register-external", "cap-round",
            *REGISTER, "--evidence", self.before)
        self.assertEqual(code, 1)
        self.assertFalse((self.root / ".sumbi/interventions.jsonl").exists())
        code, _, errors = invoke("--evidence", self.before, "--signals", self.before)
        self.assertEqual(code, 1)
        self.assertIn("need --register-external or --check", errors)

    def applied_bundle(self):
        """Apply one reviewed synthetic harness change; return its backup ID."""
        (self.root / ".sumbi").mkdir(exist_ok=True)
        (self.root / ".sumbi/config.toml").write_text('[harness]\npaths = ["device.py"]\n',
            encoding="utf-8")
        target = self.root / "device.py"
        target.write_bytes(b"LIMIT = 1\n")
        after = "LIMIT = 2\n"
        bundle = self.base / "bundle.json"
        bundle.write_text(json.dumps({"version": 1, "intervention_id": "device-fix",
            "evidence_sha256": digest(self.before.read_bytes()),
            "evidence_refs": [{"pointer": "/summary/sessions", "value": 10}],
            "diagnosis": {"id": "capacity", "hypothesis": "H", "rationale": "R"},
            "prediction": {"metric": "tokens_per_success", "direction": "decrease",
                "rough_size_percent": 10},
            "verification": {"id": "unit-check", "status": "passed",
                "evidence_sha256": "a" * 64, "declaration": "V"},
            "review": {"author_family": "family-a", "reviewer_family": "family-b",
                "status": "approved", "safety_gates_preserved": True},
            "changes": [{"path": "device.py", "before_sha256": digest(b"LIMIT = 1\n"),
                "after_sha256": digest(after.encode()), "after_text": after}]}),
            encoding="utf-8")
        sha = digest(bundle.read_bytes())
        code, _, errors = invoke("--root", self.root, "--evidence", self.before, "--bundle",
            bundle, "--apply", "--reviewed-sha256", sha, "--json-out", target)
        self.assertEqual(code, 1)  # Output over a bundle target is refused before apply.
        self.assertIn("must not overwrite", errors)
        self.assertEqual(target.read_bytes(), b"LIMIT = 1\n")
        code, output, errors = invoke("--root", self.root, "--evidence", self.before,
            "--bundle", bundle, "--apply", "--reviewed-sha256", sha, "--json")
        self.assertEqual((code, errors), (0, ""))
        return json.loads(output)["backup_id"]

    def test_revert_json_out_refuses_restore_targets_before_reverting(self):
        backup = self.applied_bundle()
        target = self.root / "device.py"
        code, _, errors = invoke("--root", self.root, "--revert", backup, "--json-out", target)
        self.assertEqual(code, 1)
        self.assertIn("must not overwrite", errors)
        self.assertEqual(target.read_bytes(), b"LIMIT = 2\n")  # Neither reverted nor replaced.
        # An owner edit that revert would refuse must not be replaced by JSON either.
        target.write_bytes(b"LIMIT = 3\n")
        code, _, errors = invoke("--root", self.root, "--revert", backup, "--json-out",
            self.root / "DEVICE.PY" if os.name == "nt" else target)
        self.assertEqual(code, 1)
        self.assertEqual(target.read_bytes(), b"LIMIT = 3\n")
        target.write_bytes(b"LIMIT = 2\n")
        code, output, errors = invoke("--root", self.root, "--revert", backup, "--json",
            "--json-out", self.base / "revert.json")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(target.read_bytes(), b"LIMIT = 1\n")
        self.assertEqual((self.base / "revert.json").read_text(encoding="utf-8"), output)

    def test_registration_rejects_extra_expectation_fields_before_writing(self):
        for field in ("predictions", "revert_if"):
            arguments = {"predictions": [{"metric": "tokens-per-day", "direction": "decrease",
                "rough_size_percent": 5}], "revert_if": [{"metric": "sessions-per-day",
                "direction": "decrease", "threshold_percent": 50}]}
            arguments[field][0]["note"] = "PRIVATE-FREE-TEXT"
            with self.assertRaises(InstallError) as caught:
                register_external(self.root, intervention_id="extra-round",
                    applied_at=datetime(2030, 1, 3, tzinfo=timezone.utc), changes=["a-change"],
                    before_days=1.0, after_days=1.0, **arguments)
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertFalse((self.root / ".sumbi/interventions.jsonl").exists())

    def test_check_measures_and_hashes_one_snapshot(self):
        self.assertEqual(self.register()[0], 0)
        original = self.before.read_bytes()
        real = external._load

        def load_then_replace(path, limit):
            result = real(path, limit)
            if Path(path) == self.before:
                self.before.write_text(json.dumps(report("2030-01-01T00:00:00Z",
                    "2030-01-03T00:00:00Z", 999, 99999)), encoding="utf-8")
            return result

        with patch.object(external, "_load", side_effect=load_then_replace):
            summary = check_external(self.root, "cap-round", self.before, self.after)
        self.assertEqual(summary["evidence"]["before_sha256"], digest(original))
        metrics = {row["id"]: row for row in summary["metrics"]}
        self.assertEqual(metrics["sessions-per-day"]["before"], 5)

    def test_out_of_range_numeric_signal_time_is_unreadable(self):
        self.assertEqual(self.register()[0], 0)
        signals = self.base / "events.jsonl"
        signals.write_text('{"ts": 1' + "0" * 400 + ', "event": "cap"}\n'
            + '{"ts": 1e400, "event": "cap"}\n'
            + '{"ts": "2030-01-02T00:00:00Z", "event": "warn"}\n', encoding="utf-8")
        code, output, errors = self.check("--signals", signals, "--json")
        self.assertEqual((code, errors), (0, ""))
        counted = json.loads(output)["signals"]
        self.assertEqual(counted["unreadable_lines"], 2)
        self.assertEqual(counted["before"]["labels"], {"warn": 1})

    def test_check_refuses_unknown_crossing_and_tampered_rows(self):
        code, _, errors = self.check()
        self.assertEqual(code, 1)
        self.assertIn("exactly one registration", errors)
        self.assertEqual(self.register()[0], 0)
        # Evidence windows that cross the apply time mix exposure.
        self.after.write_text(json.dumps(report("2030-01-02T12:00:00Z",
            "2030-01-04T00:00:00Z", 4, 700)), encoding="utf-8")
        code, _, errors = self.check()
        self.assertEqual(code, 1)
        self.assertIn("own side", errors)
        self.after.write_text("{}", encoding="utf-8")
        self.assertEqual(self.check()[0], 1)
        ledger = self.root / ".sumbi/interventions.jsonl"
        row = json.loads(ledger.read_text(encoding="utf-8"))
        row["changes"] = ["Free text"]
        ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(InstallError):
            read_external(self.root, "cap-round")
        with self.assertRaises(InstallError):
            check_external(self.root, "cap-round", self.before, self.before)


if __name__ == "__main__":
    unittest.main()
