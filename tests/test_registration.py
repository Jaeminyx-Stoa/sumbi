"""Synthetic, offline pre-registration publication and apply evidence."""

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sumbi.catalog import load_catalog
from sumbi.cli import main, parser
from sumbi.interventions import exposure_gap, in_exposure_gap
from sumbi.registration import read_registration


class RegistrationCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "registration.json"
        for target in ("socket.socket", "socket.getaddrinfo", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("Tests cannot use the network"))
            guard.start()
            self.addCleanup(guard.stop)

    def arguments(self, **changes):
        options = {"json": str(self.path), "intervention-id": "round-01", "outcome-source": "local-verify",
                   "margin-pp": "5", "sample-size-per-arm": "12", "follow-up-days": "7",
                   "window-days": "14", "applied-at": "2030-01-15T00:00:00Z", "practice": "handoff"}
        options.update(changes)
        return ["register", *[part for k, v in options.items() for part in ("--" + k, v)]]

    def run_cli(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return main(args)
            except SystemExit as exc:
                return exc.code

    def test_happy_path_now_equal_windows_and_catalog_predictions(self):
        begin = datetime.now(timezone.utc)
        self.assertEqual(self.run_cli(self.arguments() + ["--practice", "test-and-verify"]), 0)
        end = datetime.now(timezone.utc)
        registration = read_registration(self.path)
        self.assertTrue(begin <= registration.registered_at <= end)
        self.assertTrue(registration.preregistered)
        self.assertEqual(registration.outcome_source, "local-verify")
        self.assertEqual(registration.margin_pp, 5)
        self.assertEqual(registration.sample_size_per_arm, 12)
        self.assertEqual(registration.follow_up_days, 7)
        self.assertEqual(registration.before.until, registration.applied_at)
        self.assertEqual(registration.after.since, registration.applied_at)
        self.assertEqual(registration.before.until - registration.before.since, timedelta(days=14))
        self.assertEqual(registration.after.until - registration.after.since, timedelta(days=14))
        predictions = {p["id"]: p["prediction"] for p in load_catalog()}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["predictions"], [predictions["handoff"], predictions["test-and-verify"]])
        self.assertTrue(all("condition" not in p for p in registration.predictions))
        self.assertEqual(list(self.root.glob(".sumbi-register-*")), [])

    def test_github_source_and_late_registration_remain_valid(self):
        self.assertEqual(self.run_cli(self.arguments(**{"outcome-source": "github", "applied-at": "2020-01-15T00:00:00Z"})), 0)
        registration = read_registration(self.path)
        self.assertEqual(registration.outcome_source, "github")
        self.assertFalse(registration.preregistered)

    def test_never_overwrites_existing_file_or_leaves_staging_files(self):
        self.path.write_bytes(b"Synthetic existing evidence.\n")
        self.assertEqual(self.run_cli(self.arguments()), 1)
        self.assertEqual(self.path.read_bytes(), b"Synthetic existing evidence.\n")
        self.assertEqual(list(self.root.glob(".sumbi-register-*")), [])

    def test_invalid_flags_do_not_publish_a_registration(self):
        cases = {"margin-pp": ("0", "100", "nan", "inf"), "sample-size-per-arm": ("0", "-1", "1.5"),
                 "follow-up-days": ("0", "nan"), "window-days": ("-1", "inf", "1e100", "1e-20"),
                 "applied-at": ("invalid", "2030-01-15T09:00:00+09:00"),
                 "outcome-source": ("unknown",), "intervention-id": ("private text",), "practice": ("unknown-practice",)}
        for option, values in cases.items():
            for value in values:
                with self.subTest(option=option, value=value):
                    self.assertNotEqual(self.run_cli(self.arguments(**{option: value})), 0)
                    self.assertFalse(self.path.exists())
                    self.assertEqual(list(self.root.glob(".sumbi-register-*")), [])
        self.assertNotEqual(self.run_cli(self.arguments() + ["--practice", "handoff"]), 0)
        self.assertNotEqual(self.run_cli(["register", "--json", str(self.path)]), 0)

    def test_apply_evidence_legacy_grouped_equal_and_invalid_records(self):
        self.assertEqual(self.run_cli(self.arguments()), 0)
        registration = read_registration(self.path)
        interventions = self.root / "interventions.jsonl"
        rows = [{"practice_id": "round-01", "utc_time": "2030-01-15T00:00:00Z"}]
        interventions.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        gap = exposure_gap(registration, interventions)
        self.assertEqual(gap["seconds"], 0)
        self.assertFalse(in_exposure_gap(registration.applied_at, gap))
        rows = [{"intervention_id": "round-01", "practice_id": practice, "utc_time": "2030-01-15T00:01:00Z"}
                for practice in ("handoff", "test-and-verify")]
        rows.append({"action": "revert", "utc_time": "2030-01-15T00:02:00Z"})
        interventions.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.assertEqual(exposure_gap(registration, interventions)["seconds"], 60)
        self.assertIsNone(exposure_gap(registration, None))
        with self.assertRaisesRegex(ValueError, "requires"):
            exposure_gap(registration, None, "round-01")
        with self.assertRaisesRegex(ValueError, "match"):
            exposure_gap(registration, interventions, "other")
        cases = [[], [{"practice_id": "other", "utc_time": "2030-01-15T00:00:00Z"}],
                 [{"practice_id": "round-01", "utc_time": "invalid"}],
                 [{"practice_id": "round-01", "utc_time": "2030-01-15T09:00:00+09:00"}],
                 [rows[0], {**rows[1], "utc_time": "2030-01-15T00:02:00Z"}]]
        for records in cases:
            interventions.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "one valid UTC"):
                exposure_gap(registration, interventions)

    def test_help_exposes_registration_and_exposure_options(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            parser().parse_args(["register", "--help"])
        for flag in ("--practice", "--window-days", "--margin-pp", "--applied-at"):
            self.assertIn(flag, output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            parser().parse_args(["compare", "--help"])
        self.assertIn("--interventions", output.getvalue())
        self.assertIn("--intervention-id", output.getvalue())
