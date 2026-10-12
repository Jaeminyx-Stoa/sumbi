"""Skipping pre-window files must not change any non-coverage collect output."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from support import IsolatedTemporaryDirectory
import unittest

from sumbi.core.records import Coverage
from sumbi.core.time import Window
from sumbi.core.values import timestamp
from sumbi.events.adapters import claude_code, codex
from sumbi.measure.report import collect
from sumbi.sessions.builder import collect_window

WINDOW = Window(timestamp("2020-01-02T00:00:00Z"), timestamp("2020-01-03T00:00:00Z"))
STALE = timestamp("2020-01-01T12:00:00Z").timestamp()
FRESH = timestamp("2020-01-02T12:00:00Z").timestamp()


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

    def compare(self):
        """Return (skipping report, reference report with every file fresh)."""
        self.stamp()
        skipping = collect(self.home, WINDOW)[0]
        self.stamp(fresh=True)
        reference = collect(self.home, WINDOW)[0]
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
        self.assertEqual(self.adapters(skipping)["codex"]["files_skipped"], 0)
        self.assertEqual(skipping["summary"]["sessions"], 1)

    def test_last_records_reads_long_and_partial_tail_lines(self):
        from sumbi.core.records import last_records, records
        path = self.home / "tail.jsonl"
        long_line = json.dumps({"timestamp": "2020-01-01T00:00:01Z", "pad": "x" * 200000})
        path.write_bytes(b'{"a": 1}\nnot json\n' + long_line.encode() + b'\n{"b": 2')
        expected = list(records(path, Coverage()))
        self.assertEqual(list(last_records(path)), expected[::-1])
        self.assertEqual(codex.latest(self.home, path), timestamp("2020-01-01T00:00:01Z"))

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
