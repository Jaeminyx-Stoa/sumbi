"""Due-time decisions, lock skipping, records and exit codes for local jobs."""

from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sumbi.cli import main

UTC = timezone.utc


def job_block(name, command, **fields):
    lines = ["[[job]]", "name = " + json.dumps(name),
        "command = " + json.dumps([str(part) for part in command])]
    for key, value in fields.items():
        lines.append(key + " = " + json.dumps(value))
    return "\n".join(lines) + "\n"


def marker_command(marker):
    code = "import pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text('ran')"
    return [sys.executable, "-c", code]


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.config = self.root / "jobs.toml"
        self.marker = self.root / "ran"

    def write_config(self, *blocks):
        self.config.write_text("\n".join(blocks), encoding="utf-8")

    def invoke(self, now):
        errors = StringIO()
        with redirect_stderr(errors):
            code = main(["schedule", "--config", str(self.config),
                "--state", str(self.state), "--now", str(now)])
        return code, errors.getvalue()

    def records(self):
        path = self.state / "runs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def last_start(self, name, value):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / (name + ".json")).write_text(
            json.dumps({"last_start": value}), encoding="utf-8")

    def test_job_not_due_is_skipped(self):
        now = 1_900_000_000.0
        self.write_config(job_block("every-job", marker_command(self.marker),
            every_sec=3600, cwd=str(self.root)))
        self.last_start("every-job", now - 30)
        code, errors = self.invoke(now)
        self.assertEqual((code, errors), (0, ""))
        self.assertFalse(self.marker.exists())
        self.assertEqual([row["status"] for row in self.records()], ["skipped"])

    def test_every_due_runs_and_is_recorded(self):
        now = 1_900_000_000.0
        self.write_config(job_block("every-job", marker_command(self.marker),
            every_sec=60, cwd=str(self.root)))
        code, errors = self.invoke(now)
        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(self.marker.exists())
        record = self.records()[-1]
        self.assertEqual((record["name"], record["status"], record["rc"]),
            ("every-job", "ok", 0))
        self.assertEqual(record["start"], now)
        self.assertEqual(self.last_start_value("every-job"), now)

    def last_start_value(self, name):
        value = json.loads((self.state / (name + ".json")).read_text(encoding="utf-8"))
        return value["last_start"]

    def test_at_utc_already_ran_today_is_skipped(self):
        target = datetime(2030, 1, 1, 12, 0, tzinfo=UTC).timestamp()
        now = target + 3600
        self.write_config(job_block("daily-job", marker_command(self.marker),
            at_utc="12:00", cwd=str(self.root)))
        self.last_start("daily-job", target + 60)
        code, errors = self.invoke(now)
        self.assertEqual((code, errors), (0, ""))
        self.assertFalse(self.marker.exists())
        self.assertEqual([row["status"] for row in self.records()], ["skipped"])

    def test_at_utc_due_runs_once(self):
        target = datetime(2030, 1, 1, 12, 0, tzinfo=UTC).timestamp()
        now = target + 60
        self.write_config(job_block("daily-job", marker_command(self.marker),
            at_utc="12:00", cwd=str(self.root)))
        code, _ = self.invoke(now)
        self.assertEqual(code, 0)
        self.assertTrue(self.marker.exists())
        self.marker.unlink()
        code, _ = self.invoke(now + 60)
        self.assertEqual(code, 0)
        self.assertFalse(self.marker.exists())
        self.assertEqual([row["status"] for row in self.records()], ["ok", "skipped"])

    def test_held_lock_marks_busy_and_does_not_run(self):
        now = 1_900_000_000.0
        self.write_config(job_block("locked-job", marker_command(self.marker),
            every_sec=60, cwd=str(self.root)))
        self.state.mkdir(parents=True, exist_ok=True)
        lock = self.state / "locked-job.lock"
        lock.touch()
        descriptor = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, errors = self.invoke(now)
        self.assertEqual((code, errors), (0, ""))
        self.assertFalse(self.marker.exists())
        self.assertEqual([row["status"] for row in self.records()], ["busy"])
        self.assertFalse((self.state / "locked-job.json").exists())

    def test_failing_command_exits_one_and_records_failed(self):
        now = 1_900_000_000.0
        self.write_config(job_block("failing-job",
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            every_sec=60, cwd=str(self.root)))
        code, errors = self.invoke(now)
        self.assertEqual((code, errors), (1, ""))
        record = self.records()[-1]
        self.assertEqual((record["status"], record["rc"]), ("failed", 3))

    def test_invalid_config_exits_two_and_runs_nothing(self):
        self.write_config(job_block("both-job", marker_command(self.marker),
            every_sec=60, at_utc="12:00", cwd=str(self.root)))
        code, errors = self.invoke(1_900_000_000.0)
        self.assertEqual(code, 2)
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.state / "runs.jsonl").exists())

    def test_timeout_exits_one_and_records_timeout(self):
        now = 1_900_000_000.0
        self.write_config(job_block("slow-job",
            [sys.executable, "-c", "import time; time.sleep(5)"],
            every_sec=60, timeout_sec=1, cwd=str(self.root)))
        code, errors = self.invoke(now)
        self.assertEqual((code, errors), (1, ""))
        record = self.records()[-1]
        self.assertEqual((record["status"], record["rc"]), ("timeout", None))


if __name__ == "__main__":
    unittest.main()
