"""Skipping pre-window files must not change any non-coverage collect output."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from support import IsolatedTemporaryDirectory
import unittest
from unittest.mock import patch

from sumbi.core.records import Coverage
from sumbi.core.time import Window
from sumbi.core.values import timestamp
from sumbi.events.adapters import claude_code, codex
from sumbi.measure.report import collect
from sumbi.events.adapters.common import latest_time
from sumbi.sessions.builder import collect as collect_sessions, collect_window

WINDOW = Window(timestamp("2020-01-02T00:00:00Z"), timestamp("2020-01-03T00:00:00Z"))
STALE = timestamp("2020-01-01T12:00:00Z").timestamp()
FRESH = timestamp("2020-01-02T12:00:00Z").timestamp()


def main_path(adapter, home, window, coverage, **options):
    """The pre-skipping call: every file of every adapter is read."""
    options.pop("cache", None)
    return collect_sessions(adapter, home, window, coverage, **options), 0


def without_coverage(report):
    """Coverage diagnostics describe the files read; everything else must match."""
    result = copy.deepcopy(report)
    del result["coverage"]["adapters"]
    return result


class WindowSkipTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.cwd = self.home / "work"
        self.cwd.mkdir()
        self.files = {}

    def write(self, relative, events, mtime):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        self.files[path] = mtime
        return path

    def stamp(self, *, fresh=False):
        now = datetime.now(timezone.utc).timestamp()
        for path, mtime in self.files.items():
            os.utime(path, (now, now) if fresh else (mtime, mtime))

    def compare(self, window=WINDOW):
        """Return (skipping report, report folded by main's unskipped collection path)."""
        self.stamp()
        skipping = collect(self.home, window)[0]
        with patch("sumbi.measure.report.collect_window", main_path):
            reference = collect(self.home, window)[0]
        self.assertEqual(without_coverage(skipping), without_coverage(reference))
        for agent in ("claude-code", "codex"):
            self.assertEqual(reference["coverage"]["adapters"][agent]["files_skipped"], 0)
            for key in ("sessions_in_window", "sessions_read"):
                self.assertLessEqual(skipping["coverage"]["adapters"][agent][key],
                    reference["coverage"]["adapters"][agent][key])
        return skipping, reference

    def claude(self, when, identity, session="main-a", **extra):
        return {"type": "assistant", "uuid": identity, "sessionId": session, "timestamp": when,
            "cwd": str(self.cwd), "message": {"id": identity, "model": "model-a",
                "content": [], "usage": {"input_tokens": 3, "output_tokens": 2}}, **extra}

    def meta(self, identity, when, parent=None):
        payload = {"id": identity, "cwd": str(self.cwd), "cli_version": "0.1.0"}
        if parent is not None:
            payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        return {"type": "session_meta", "timestamp": when, "payload": payload}

    def token(self, when, inputs, cached, output):
        return {"type": "event_msg", "timestamp": when, "payload": {"type": "token_count",
            "info": {"total_token_usage": {"input_tokens": inputs,
                "cached_input_tokens": cached, "output_tokens": output,
                "reasoning_output_tokens": 0}, "last_token_usage": {"input_tokens": inputs,
                "cached_input_tokens": cached, "output_tokens": output,
                "reasoning_output_tokens": 0}}}}

    def adapters(self, report):
        return report["coverage"]["adapters"]

    def test_unrelated_pre_window_file_is_skipped(self):
        self.write(".claude/projects/g/old.jsonl",
            [self.claude("2020-01-01T10:00:00Z", "o1", "old")], STALE)
        self.write(".claude/projects/g/main-a.jsonl",
            [self.claude("2020-01-02T01:00:00Z", "a1")], FRESH)
        skipping, reference = self.compare()
        self.assertEqual(self.adapters(skipping)["claude-code"]["files_skipped"], 1)
        self.assertEqual(self.adapters(skipping)["claude-code"]["files_scanned"], 1)
        self.assertEqual(self.adapters(reference)["claude-code"]["files_scanned"], 2)
        self.assertEqual(skipping["summary"]["sessions"], 1)

    def test_probe_cache_reuses_unchanged_files_and_rescans_changed_ones(self):
        old = self.write(".claude/projects/g/old.jsonl",
            [self.claude("2020-01-01T10:00:00Z", "o1", "old")], STALE)
        self.write(".claude/projects/g/main-a.jsonl",
            [self.claude("2020-01-02T01:00:00Z", "a1")], FRESH)
        self.write(".codex/sessions/g/rollout-first.jsonl", [self.meta("thread-a",
            "2020-01-01T09:00:00Z"), self.token("2020-01-01T10:00:00Z", 100, 40, 10)], STALE)
        self.stamp()
        cache = self.home / "probe-cache"
        plain = collect(self.home, WINDOW)[0]
        first = collect(self.home, WINDOW, cache_dir=cache)[0]
        self.assertEqual(self.adapters(first)["claude-code"]["probe_cache"],
            {"hits": 0, "misses": 2})
        with patch.object(claude_code, "latest", side_effect=AssertionError("rescanned")):
            second = collect(self.home, WINDOW, cache_dir=cache)[0]
        # Only pre-window files are scanned for a latest time, so only they are cached.
        self.assertEqual(self.adapters(second)["claude-code"]["probe_cache"],
            {"hits": 1, "misses": 1})
        self.assertEqual(self.adapters(second)["codex"]["probe_cache"], {"hits": 1, "misses": 0})
        for report in (first, second):
            for agent in ("claude-code", "codex"):
                del self.adapters(report)[agent]["probe_cache"]
            self.assertEqual(report, plain)
        self.assertEqual(oct((cache / "probes-claude-code.json").stat().st_mode & 0o777),
            "0o600")
        # A changed file is probed again: its new in-window record is read.
        with old.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.claude("2020-01-02T05:00:00Z", "o2", "old")) + "\n")
        os.utime(old, (STALE, STALE))
        third = collect(self.home, WINDOW, cache_dir=cache)[0]
        self.assertEqual(self.adapters(third)["claude-code"]["probe_cache"],
            {"hits": 0, "misses": 2})
        self.assertEqual(third["summary"]["sessions"], 2)
        (cache / "probes-claude-code.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(collect(self.home, WINDOW, cache_dir=cache)[0]["summary"]["sessions"], 2)
        malformed = {"size": old.stat().st_size, "mtime_ns": old.stat().st_mtime_ns,
            "ino": old.stat().st_ino}
        for latest in (7, "not a time", "2020-01-01T10:00:00", ["x"]):
            (cache / "probes-claude-code.json").write_text(json.dumps({"version": 2,
                "entries": {str(old): {**malformed, "latest": latest}}}), encoding="utf-8")
            report = collect(self.home, WINDOW, cache_dir=cache)[0]
            self.assertEqual(report["summary"]["sessions"], 2)
            self.assertEqual(self.adapters(report)["claude-code"]["probe_cache"]["hits"], 0)

    def test_resumed_codex_baseline_in_a_stale_file_is_still_read(self):
        self.write(".codex/sessions/g/rollout-first.jsonl", [self.meta("thread-a",
            "2020-01-01T09:00:00Z"), self.token("2020-01-01T10:00:00Z", 100, 40, 10)], STALE)
        self.write(".codex/sessions/g/rollout-resume.jsonl", [self.meta("thread-a",
            "2020-01-02T01:00:00Z"), self.token("2020-01-02T02:00:00Z", 130, 50, 15)], FRESH)
        skipping, _ = self.compare()
        self.assertEqual(self.adapters(skipping)["codex"]["files_skipped"], 0)
        session = skipping["sessions"][0]
        # Only the delta after the pre-window cumulative baseline counts.
        self.assertEqual((session["tokens"]["new_input"], session["tokens"]["output"]), (20, 5))

    def test_codex_parent_with_inherited_meta_in_stale_file_is_read(self):
        self.write(".codex/sessions/g/rollout-parent.jsonl", [self.meta("parent-x",
            "2020-01-01T09:00:00Z"), self.token("2020-01-01T10:00:00Z", 50, 0, 5)], STALE)
        self.write(".codex/sessions/g/rollout-other.jsonl", [self.meta("other-y",
            "2020-01-01T09:00:00Z"), self.token("2020-01-01T10:00:00Z", 50, 0, 5)], STALE)
        self.write(".codex/sessions/g/rollout-child.jsonl", [
            self.meta("child-z", "2020-01-01T11:00:00Z", parent="parent-x"),
            self.meta("parent-x", "2020-01-01T09:00:00Z"),
            self.token("2020-01-02T01:00:00Z", 80, 20, 8)], FRESH)
        skipping, _ = self.compare()
        coverage = self.adapters(skipping)["codex"]
        self.assertEqual((coverage["files_skipped"], coverage["inherited_session_meta"]), (1, 1))

    def test_claude_parent_of_a_window_child_is_read_from_a_stale_file(self):
        self.write(".claude/projects/g/main-p.jsonl",
            [self.claude("2020-01-01T10:00:00Z", "p1", "main-p")], STALE)
        self.write(".claude/projects/g/main-p/subagents/agent-c1.jsonl", [self.claude(
            "2020-01-01T11:00:00Z", "c0", "main-p", agentId="c1"), self.claude(
            "2020-01-02T01:00:00Z", "c1", "main-p", agentId="c1")], FRESH)
        skipping, _ = self.compare()
        self.assertEqual(self.adapters(skipping)["claude-code"]["files_skipped"], 0)

    def test_window_spanning_session_and_event_times(self):
        self.write(".claude/projects/g/main-a.jsonl", [
            self.claude("2020-01-01T23:58:30Z", "a0"),
            self.claude("2020-01-02T00:01:59.900Z", "a1"),
            self.claude("2020-01-02T23:59:59Z", "a2"),
            self.claude("2020-01-03T00:00:00Z", "a3")], FRESH)
        skipping, _ = self.compare()
        events = skipping["sessions"][0]["time"]["events"]
        self.assertEqual(events, {"measurement": "observed", "resolution": "minute_floor",
            "first_at": "2020-01-02T00:01:00Z", "last_at": "2020-01-02T23:59:00Z"})
        self.assertEqual(skipping["sessions"][0]["time"]["wall_span"]["seconds"], 86400.0)

    def test_creation_time_mtime_with_window_records_is_read(self):
        # Some writers leave the modification time at file creation.
        self.write(".codex/sessions/g/rollout-long.jsonl", [self.meta("thread-a",
            "2020-01-01T09:00:00Z"), self.token("2020-01-01T10:00:00Z", 100, 40, 10),
            self.token("2020-01-02T02:00:00Z", 130, 50, 15)], STALE)
        self.write(".codex/sessions/g/rollout-edge.jsonl", [self.meta("thread-b",
            "2020-01-01T09:00:00Z"), self.token("2020-01-01T23:59:30Z", 10, 0, 1)], STALE)
        skipping, _ = self.compare()
        self.assertEqual(self.adapters(skipping)["codex"]["files_skipped"], 1)
        self.assertEqual(skipping["summary"]["sessions"], 1)

    def completion(self, outer, **payload):
        record = {"type": "event_msg", "payload": payload}
        if outer is not None:
            record["timestamp"] = outer
        return record

    def test_codex_completion_times_inside_the_window_keep_the_file(self):
        start_ms = timestamp("2020-01-01T23:59:00Z").timestamp() * 1000
        end_ms = timestamp("2020-01-02T00:01:30Z").timestamp() * 1000
        item = {"type": "item_completed", "started_at_ms": start_ms, "completed_at_ms": end_ms,
            "item": {"type": "CommandExecution", "id": "call-1", "command": ["true"],
                "cwd": str(self.cwd), "exit_code": 0}}
        task = {"type": "task_complete", "turn_id": "turn-1",
            "started_at": start_ms / 1000, "completed_at": end_ms / 1000}
        cases = {"no outer timestamp": self.completion(None, **item),
            "old outer timestamp": self.completion("2020-01-01T10:00:00Z", **item),
            "request completion": self.completion("2020-01-01T10:00:00Z", **task)}
        for name, record in cases.items():
            with self.subTest(case=name):
                self.files.clear()
                for path in self.home.glob(".codex/sessions/g/*"):
                    path.unlink()
                self.write(".codex/sessions/g/rollout-a.jsonl", [self.meta("thread-a",
                    "2020-01-01T09:00:00Z"), record, self.token("2020-01-01T10:00:01Z", 5, 0, 1)],
                    STALE)
                skipping, reference = self.compare()
                self.assertEqual(self.adapters(skipping)["codex"]["files_skipped"], 0)
                self.assertEqual(reference["summary"]["sessions"], 1)
                durations = reference["summary"]["time"]["durations"]
                self.assertEqual(durations["request" if name == "request completion"
                    else "tool"]["seconds"], 90.0)

    def test_latest_time_scan_over_reports_but_never_misses(self):
        path = self.home / "scan.jsonl"
        path.write_text("\n".join([json.dumps({"timestamp": "2020-01-01T00:00:01Z"}),
            json.dumps({"payload": {"nested": {"completed_at": "2020-01-05T00:00:00Z"}}}),
            json.dumps({"payload": {"started_at_ms": 1577836800000, "text":
                '"timestamp": "2099-01-01T00:00:00Z"'}}), "not json"]) + "\n", encoding="utf-8")
        self.assertEqual(latest_time(path, ("timestamp",)), timestamp("2020-01-01T00:00:01Z"))
        self.assertEqual(latest_time(path, ("timestamp", "started_at", "completed_at")),
            timestamp("2020-01-05T00:00:00Z"))
        path.write_text('{"timestamp": 2020-01-01}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            latest_time(path, ("timestamp",))

    def escaped(self, record, name, spelled):
        """Serialize `record` with the key `name` spelled as `spelled` (raw JSON escapes)."""
        line = json.dumps(record)
        self.assertEqual(line.count(json.dumps(name) + ":"), 1)
        line = line.replace(json.dumps(name) + ":", '"' + spelled + '":')
        self.assertEqual(json.loads(line), record)
        return line

    def write_lines(self, relative, lines, mtime):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        self.files[path] = mtime

    def test_codex_escaped_completion_name_keeps_the_file(self):
        start_ms = timestamp("2020-01-01T23:59:00Z").timestamp() * 1000
        end_ms = timestamp("2020-01-02T00:01:30Z").timestamp() * 1000
        record = self.completion("2020-01-01T10:00:00Z", type="item_completed",
            started_at_ms=start_ms, completed_at_ms=end_ms, item={"type": "CommandExecution",
                "id": "call-1", "command": ["true"], "cwd": str(self.cwd), "exit_code": 0})
        self.write_lines(".codex/sessions/g/rollout-a.jsonl", [
            json.dumps(self.meta("thread-a", "2020-01-01T09:00:00Z")),
            self.escaped(record, "completed_at_ms", "completed_at_m\\u0073"),
            json.dumps(self.token("2020-01-01T10:00:01Z", 5, 0, 1))], STALE)
        skipping, reference = self.compare()
        self.assertEqual(self.adapters(skipping)["codex"]["files_skipped"], 0)
        self.assertEqual(reference["summary"]["sessions"], 1)
        self.assertEqual(reference["summary"]["time"]["durations"]["tool"]["seconds"], 90.0)

    def test_claude_escaped_timestamp_name_keeps_the_file(self):
        self.write_lines(".claude/projects/g/main-a.jsonl", [
            json.dumps(self.claude("2020-01-01T10:00:00Z", "a0")),
            self.escaped(self.claude("2020-01-02T01:00:00Z", "a1"), "timestamp",
                "timestam\\u0070")], STALE)
        skipping, reference = self.compare()
        self.assertEqual(self.adapters(skipping)["claude-code"]["files_skipped"], 0)
        self.assertEqual(reference["summary"]["sessions"], 1)

    def test_escapes_outside_time_names_still_skip(self):
        record = self.claude("2020-01-01T10:00:00Z", "o1", "old")
        record["message"]["content"] = [{"type": "text", "text": "\u001b[0m 안 \\u0074"}]
        record["note"] = 1
        self.write_lines(".claude/projects/g/old.jsonl",
            [self.escaped(record, "note", "not\\u0065")], STALE)
        self.assertIn("\\u001b", (self.home / ".claude/projects/g/old.jsonl").read_text("utf-8"))
        skipping, _ = self.compare()
        self.assertEqual(self.adapters(skipping)["claude-code"]["files_skipped"], 1)

    def test_latest_time_refuses_escaped_time_names(self):
        path = self.home / "scan.jsonl"
        for spelled, fields in (("timestam\\u0070", ("timestamp",)),
            ("\\u0073tarted_at", ("started_at",)), ("completed_at\\u005fms", ("completed_at",))):
            with self.subTest(name=spelled):
                path.write_text('{"payload": {"' + spelled + '": 1}}\n', encoding="utf-8")
                with self.assertRaises(ValueError):
                    latest_time(path, fields)
        path.write_text('{"timestamp": "2020-01-01T00:00:01Z", "time\\u0073": 1, '
            '"x": "\\"timestam\\u0070\\": 2"}\n', encoding="utf-8")
        self.assertEqual(latest_time(path, ("timestamp", "started_at", "completed_at")),
            timestamp("2020-01-01T00:00:01Z"))

    def test_minimum_date_window_collects_like_main(self):
        window = Window(timestamp("0001-01-01T00:00:00Z"), timestamp("0001-01-02T00:00:00Z"))
        self.assertEqual(self.compare(window)[0]["summary"]["sessions"], 0)
        self.write(".claude/projects/g/main-a.jsonl",
            [self.claude("2020-01-02T01:00:00Z", "a1")], FRESH)
        self.assertEqual(self.compare(window)[0]["summary"]["sessions"], 0)

    def test_future_window_never_skips(self):
        path = self.write(".claude/projects/g/main-a.jsonl",
            [self.claude("2099-01-01T00:01:00Z", "a1")], STALE)
        self.stamp()
        window = Window(timestamp("2099-01-01T00:00:00Z"), timestamp("2099-01-02T00:00:00Z"))
        sessions, skipped = collect_window(claude_code, self.home, window, Coverage())
        self.assertEqual((len(sessions), skipped), (1, 0))
        self.assertTrue(path.exists())

    def test_probe_matches_full_read_identities(self):
        paths = [self.write(".claude/projects/g/main-p/subagents/agent-c1.jsonl",
            ["not json", self.claude("2020-01-01T11:00:00Z", "c0", "main-p", agentId="c1")],
            STALE), self.write(".codex/sessions/g/rollout-late.jsonl", [
            self.token("2020-01-01T08:00:00Z", 1, 0, 1), self.meta("late", "2020-01-01T09:00:00Z")],
            STALE)]
        for adapter, path in zip((claude_code, codex), paths):
            with self.subTest(adapter=adapter.__name__):
                records = list(adapter.read(self.home, path, Coverage()))
                keys, _ = adapter.session_keys(self.home, path)
                self.assertEqual(keys, {r.session_id for r in records})


if __name__ == "__main__":
    unittest.main()
