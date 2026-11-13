"""Native-format regressions for resumable collection and publication."""

import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sumbi.live import collect_inventory, read_snapshot_bundle, snapshot, write_snapshot
from sumbi.measure.live_incremental import (
    CollectorBusy,
    IncrementalReader,
    collect_incremental,
    exclusive_lock,
)
from sumbi.measure.live_sources import (
    SourceProbe,
    account_sample,
    cached_claude_accounts,
    claude_accounts,
    codex_limits,
    latest_accounts,
    live_inventory,
    sqlite_session,
)
import test_live as native_fixtures


def _try_collect(cache, source, queue):
    try:
        with IncrementalReader(cache):
            queue.put("unexpected_lock")
    except CollectorBusy:
        queue.put("busy")


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "source.jsonl"
        self.cache = self.root / "cache.json"
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.fixtures = native_fixtures.RequestSummaryTests()
        self.fixtures.now = self.now
        self.engine = "codex"
        self.sid = "one"
        self.save([{"type": "session_meta", "payload": {"id": "one"}}, self.codex(-20, 1, 10)])

    def codex(self, *args):
        return self.fixtures.codex(*args)

    def claude(self, sec, ident, value, output=1):
        return dict(self.fixtures.claude(sec, ident, value, output), sessionId="one")

    def encoded(self, records):
        return "".join(json.dumps(x) + "\n" for x in records)

    def save(self, records):
        self.path.write_text(self.encoded(records), encoding="utf-8")

    def append(self, records):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(self.encoded(records))

    def collect(self, now=None, **limits):
        inventory = {
            "windows": [
                {
                    "lane": "alpha",
                    "engine": self.engine,
                    "session_id": self.sid,
                    "source_paths": [str(self.path)],
                }
            ]
        }
        with IncrementalReader(self.cache, **limits) as reader:
            result = collect_incremental(inventory, reader, now or self.now)
        return result

    def test_append_and_unchanged_measured_at_file_read(self):
        first = self.collect()
        self.assertEqual(first["coverage"]["bytes"], self.path.stat().st_size)
        measured = []
        original_open = open

        class MeteredFile:
            def __init__(wrapper, stream):
                wrapper.stream = stream

            def __enter__(wrapper):
                return wrapper

            def __exit__(wrapper, *args):
                wrapper.stream.close()

            def __getattr__(wrapper, key):
                return getattr(wrapper.stream, key)

            def readline(wrapper, *args):
                value = wrapper.stream.readline(*args)
                measured.append(len(value))
                return value

        def metered_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return MeteredFile(stream) if Path(path) == self.path else stream

        with patch("builtins.open", side_effect=metered_open):
            second = self.collect()
        # fstat is allowed; every unchanged source file is closed before read.
        self.assertEqual(second["coverage"]["bytes"], 0)
        self.assertEqual(sum(measured), 0)
        self.assertEqual(first["windows"], second["windows"])
        addition = self.encoded([self.codex(-1, 2, 20)])
        self.append([self.codex(-1, 2, 20)])
        with patch("builtins.open", side_effect=metered_open):
            third = self.collect()
        self.assertEqual(third["coverage"]["bytes"], len(addition.encode()))
        self.assertEqual(sum(measured), len(addition.encode()))
        self.assertEqual(
            (third["windows"][0]["baseline_tokens"], third["windows"][0]["calls_1h"]), (10, 2)
        )
        self.assertEqual(
            third["windows"],
            collect_inventory(
                {
                    "windows": [
                        {
                            "lane": "alpha",
                            "engine": "codex",
                            "session_id": "one",
                            "source_paths": [str(self.path)],
                        }
                    ]
                },
                self.now,
            )["windows"],
        )

    def test_truncate_replace_rewrite_and_session_switch(self):
        self.collect()
        for replace in (False, True):
            with self.subTest(replace=replace):
                if replace:
                    other = self.root / "other.jsonl"
                    other.write_text(self.encoded([self.codex(-1, 9, 99)]))
                    os.replace(other, self.path)
                else:
                    self.save([self.codex(-1, 2, 22)])
                row = self.collect()["windows"][0]
                self.assertEqual(row["baseline_tokens"], 99 if replace else 22)
        self.append([{"type": "session_meta", "payload": {"id": "two"}}, self.codex(-1, 1, 7)])
        row = self.collect()["windows"][0]
        self.assertEqual(
            (row["session_id"], row["baseline_tokens"], row["calls_1h"]), ("two", 7, 1)
        )
        # Same-size in-place rewrite is detected through mtime as well.
        self.save([self.codex(-1, 1, 77)])
        self.collect()
        before = self.path.stat()
        self.save([self.codex(-1, 1, 88)])
        # Some filesystems coalesce same-tick metadata updates. Advance the
        # source mtime deterministically, as a later collector would observe.
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.assertEqual(self.collect()["windows"][0]["baseline_tokens"], 88)

    def test_partial_is_deferred_without_caching_raw_content(self):
        self.collect()
        encoded = self.encoded([self.codex(-1, 2, 20)])
        with self.path.open("a") as stream:
            stream.write(encoded[:-8])
        partial = self.collect()
        self.assertEqual(partial["windows"][0]["calls_1h"], 1)
        self.assertEqual(partial["coverage"]["partial_lines"], 1)
        self.assertEqual(self.collect()["coverage"]["bytes"], 0)
        with self.path.open("a") as stream:
            stream.write(encoded[-8:])
        self.assertEqual(self.collect()["windows"][0]["calls_1h"], 2)
        self.append([{"type": "response_item", "payload": {"content": "PRIVATE_SENTINEL"}}])
        self.collect()
        self.assertNotIn("PRIVATE_SENTINEL", self.cache.read_text())
        self.assertNotIn("response_item", self.cache.read_text())

    def test_counter_duplicate_reset_and_old_boundary_retained(self):
        self.save([self.codex(-20000, 1, 5), self.codex(-7199, 2, 10), self.codex(-1, 3, 20)])
        self.collect()
        self.append([self.codex(-1, 3, 20), self.codex(-1, 1, 30)])
        data = self.collect()
        self.assertEqual(
            (
                data["windows"][0]["baseline_tokens"],
                data["windows"][0]["calls_1h"],
                data["coverage"]["duplicates"],
            ),
            (5, 2, 1),
        )
        later = self.collect(self.now + timedelta(hours=3))["windows"][0]
        self.assertEqual(
            (later["calls_1h"], later["baseline_tokens"], later["context_tokens"]), (0, 5, 30)
        )
        self.assertEqual(later["status"], "stale")

    def test_claude_delayed_duplicate_future_and_model_change(self):
        self.engine = "claude"
        events = [self.claude(-20000, "old", 5), self.claude(-1, "new", 10)]
        self.save(events)
        self.collect()
        additions = [self.claude(-1, "old", 7, 2), self.claude(5, "future", 15)]
        self.append(additions)
        data = self.collect()
        self.assertEqual(data["windows"][0]["calls_1h"], 1)
        self.assertEqual(data["windows"][0]["baseline_tokens"], 7)
        self.assertEqual(data["coverage"]["future"], 1)
        self.assertEqual(
            self.collect(self.now + timedelta(seconds=10))["windows"][0]["calls_1h"], 2
        )
        a, b = self.claude(-2, "m1", 20), self.claude(-1, "m2", 30)
        a["message"]["model"], b["message"]["model"] = "model-a", "model-b"
        self.append([a, b])
        self.assertEqual(self.collect()["windows"][0]["baseline_tokens"], 30)

    def test_engine_and_inventory_session_changes_invalidate_cache(self):
        self.collect()
        self.engine = "claude"
        self.save([self.claude(-1, "c", 55)])
        self.assertEqual(self.collect()["windows"][0]["baseline_tokens"], 55)
        self.sid = "two"
        self.assertGreater(self.collect()["coverage"]["bytes"], 0)

    def test_broken_cache_recovers_and_lock_excludes_second_process(self):
        self.collect()
        for corrupt in ("{bad", '{"version":1,"entries":{"x":{}}}'):
            self.cache.write_text(corrupt)
            data = self.collect()
            self.assertEqual(data["coverage"]["cache_recovered"], 1)
            self.assertEqual(data["windows"][0]["baseline_tokens"], 10)
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        with IncrementalReader(self.cache):
            child = context.Process(target=_try_collect, args=(self.cache, self.path, queue))
            child.start()
            child.join(5)
            self.assertFalse(child.is_alive())
            self.assertEqual(queue.get(timeout=1), "busy")
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(self.collect()["coverage"]["bytes"], 0)

    def test_large_bootstrap_streams_and_explicit_cap_recovers(self):
        # Exceed the old 16 MB whole-file cap without retaining irrelevant text.
        with self.path.open("a") as stream:
            for _ in range(18):
                stream.write(
                    json.dumps({"type": "response_item", "payload": {"content": "x" * 1_000_000}})
                    + "\n"
                )
            stream.write(self.encoded([self.codex(-1, 2, 44)]))
        data = self.collect()
        self.assertGreater(data["coverage"]["bytes"], 16_000_000)
        self.assertEqual(
            (data["windows"][0]["baseline_tokens"], data["windows"][0]["context_tokens"]), (10, 44)
        )
        self.assertLess(self.cache.stat().st_size, 5000)
        self.assertEqual(self.collect()["coverage"]["bytes"], 0)
        self.cache.unlink()
        cap = self.collect(max_rows=1)
        self.assertEqual(cap["windows"][0]["reason"], "row_limit")
        self.assertIsNone(cap["windows"][0]["baseline_tokens"])
        self.assertEqual(self.collect()["windows"][0]["context_tokens"], 44)

    def test_invalid_line_survives_resume_and_unreadable_is_unknown(self):
        with self.path.open("a") as stream:
            stream.write("{bad}\n")
        limited = self.collect(max_rows=2)
        self.assertIsNone(limited["windows"][0]["calls_1h"])
        self.assertIsNone(self.collect()["windows"][0]["calls_1h"])
        self.path.unlink()
        self.assertEqual(self.collect()["windows"][0]["reason"], "source_unreadable")

    def test_publication_is_one_generation_and_failure_preserves_previous(self):
        data = self.collect()
        out = self.root / "out"
        write_snapshot(data, out)
        bundle = read_snapshot_bundle(out)
        self.assertEqual(json.loads(bundle["snapshot.json"]), data)
        self.assertTrue(bundle["fortress-line.txt"].endswith("\n"))
        self.assertEqual(bundle["fortress-line.txt"].count("\n"), 1)
        self.assertIn("관측시각", bundle["LATEST.md"])
        self.assertIn("coverage:", bundle["LATEST.md"])
        changed = dict(data, observed_at="2026-01-01T13:00:00Z")
        original = os.replace

        def fail_at_switch(source, destination):
            if Path(destination).name == "current":
                raise OSError("injected publication failure")
            return original(source, destination)

        with patch("sumbi.live.os.replace", side_effect=fail_at_switch):
            with self.assertRaises(OSError):
                write_snapshot(changed, out)
        self.assertEqual(read_snapshot_bundle(out), bundle)
        write_snapshot(changed, out)
        self.assertEqual(
            json.loads(read_snapshot_bundle(out)["snapshot.json"])["observed_at"],
            changed["observed_at"],
        )
        unknown = snapshot([{"lane": "unknown", "engine": "unknown"}], now=self.now)
        write_snapshot(unknown, out)
        line = (out / "fortress-line.txt").read_text()
        self.assertIn("1h호출 unknown", line)
        self.assertIn("평균 unknown", line)
        with exclusive_lock(out / ".publish.lock"):
            with self.assertRaises(CollectorBusy):
                write_snapshot(data, out)

    def test_legacy_output_migration_preserves_complete_previous_generation(self):
        data = self.collect()
        source = self.root / "published"
        write_snapshot(data, source)
        previous = read_snapshot_bundle(source)
        legacy = self.root / "legacy"
        legacy.mkdir()
        for name, content in previous.items():
            (legacy / name).write_text(content)
        original = os.replace

        def fail_new_generation(source, destination):
            if Path(destination).name == "current" and not Path(source).parent.name.startswith(
                ".generation-legacy-"
            ):
                raise OSError("injected after legacy migration")
            return original(source, destination)

        with patch("sumbi.live.os.replace", side_effect=fail_new_generation):
            with self.assertRaises(OSError):
                write_snapshot(dict(data, observed_at="2026-01-01T13:00:00Z"), legacy)
        self.assertEqual(read_snapshot_bundle(legacy), previous)
        self.assertTrue(
            all((legacy / name).read_text() == content for name, content in previous.items())
        )


class NativeSourcesTests(unittest.TestCase):
    # Reuse the real request-format builders, not fake snapshot/summary values.
    setUp = IncrementalTests.setUp
    codex = IncrementalTests.codex
    claude = IncrementalTests.claude
    encoded = IncrementalTests.encoded
    save = IncrementalTests.save

    def test_source_probe_and_tmux_proc_cfg_binding(self):
        rollout = self.root / "rollout-one.jsonl"
        self.path.rename(rollout)
        proc = self.root / "proc"
        (proc / "10/task/10").mkdir(parents=True)
        (proc / "10/task/10/children").write_text("11")
        (proc / "11/fd").mkdir(parents=True)
        (proc / "11/fd/9").symlink_to(rollout)
        cfg = self.root / "config"
        source = cfg / "projects/group/one.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text(self.encoded([self.claude(-1, "c", 33)]))
        calls = []

        def run(args):
            calls.append(args)
            return f"1\talpha\told-sid\t{cfg}\tcodex\t10\n2\tbeta\tone\t{cfg}\tunknown\t20\n3\tgamma\tabsent\t{cfg}\tclaude\t30\n"

        inventory = live_inventory(
            target="example", proc_root=proc, sqlite_path=self.root / "unused.db", run=run
        )
        probe = SourceProbe()
        prepared = probe.inventory(inventory)
        self.assertEqual(prepared["windows"][0]["source_paths"], [str(proc / "11/fd/9")])
        self.assertEqual(prepared["windows"][0]["source_targets"], [str(rollout)])
        self.assertEqual(prepared["windows"][1]["engine"], "claude")
        self.assertEqual(prepared["windows"][2]["source_paths"], [])
        self.assertEqual(len(calls), 1)
        self.assertIn("#{@engine}", calls[0][-1])
        with IncrementalReader(self.cache) as reader:
            data = collect_incremental(prepared, reader, self.now)
        self.assertEqual([r["context_tokens"] for r in data["windows"]], [10, 33, None])
        again = SourceProbe(probe.entries)
        again.inventory(inventory)
        self.assertEqual(again.bytes, 0)

    def test_sqlite_exact_session_reads_uncheckpointed_wal(self):
        path = self.root / "native.db"
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.executescript(
            "CREATE TABLE session(id TEXT PRIMARY KEY); CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);"
        )
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        ms = int((self.now - timedelta(seconds=2)).timestamp() * 1000)
        data = {
            "role": "assistant",
            "modelID": "model-a",
            "time": {"created": ms, "completed": ms + 1},
            "tokens": {"input": 7, "output": 2, "cache": {"read": 3, "write": 4}},
        }
        db.execute("INSERT INTO session VALUES (?)", ("exact",))
        db.execute("INSERT INTO message VALUES (?,?,?,?)", ("m1", "exact", ms, json.dumps(data)))
        db.commit()
        self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
        window = sqlite_session(path, "exact")
        self.assertNotIn("_error", window)
        row = snapshot([window], now=self.now)["windows"][0]
        self.assertEqual(
            (row["session_id"], row["context_tokens"], row["calls_1h"]), ("exact", 14, 1)
        )
        calls = []

        def native_stub(args):
            calls.append(args)
            return "1\talpha\texact\t\topencode\t10\n"

        native = live_inventory(
            target="example",
            proc_root=self.root / "proc",
            sqlite_path=path,
            run=native_stub,
            tmux_socket=self.root / "example.sock",
        )
        self.assertEqual(calls[0][:3], ["tmux", "-S", str(self.root / "example.sock")])
        self.assertEqual(
            snapshot(native["windows"], now=self.now)["windows"][0], dict(row, lane="alpha")
        )
        self.assertEqual(sqlite_session(path, "other")["_error"], "sqlite_session_unmatched")
        self.assertEqual(sqlite_session(path, "")["_error"], "sqlite_session_missing")
        self.assertFalse((self.root / "missing.db").exists())
        self.assertEqual(
            sqlite_session(self.root / "missing.db", "exact")["_error"], "sqlite_unreadable"
        )
        self.assertFalse((self.root / "missing.db").exists())
        # Failure injection is at the connection boundary, not the adapter.
        with patch(
            "sumbi.measure.live_sources.sqlite3.connect",
            side_effect=sqlite3.OperationalError("locked"),
        ):
            self.assertEqual(sqlite_session(path, "exact")["_error"], "sqlite_unreadable")

    def test_account_windows_privacy_and_engine_identity(self):
        usage = {
            "at": self.now.timestamp(),
            "accounts": {
                "A": {
                    "who": "PRIVATE_ACCOUNT",
                    "cfg": "PRIVATE_CONFIG",
                    "five_hour": 25,
                    "five_hour_reset": (self.now + timedelta(hours=2)).isoformat(),
                    "seven_day": 70,
                    "seven_day_reset": (self.now + timedelta(days=2)).isoformat(),
                }
            },
        }
        accounts = claude_accounts(usage, self.now)
        self.assertEqual([x["window_minutes"] for x in accounts], [300, 10080])
        event = self.codex(-1, 1, 7)
        event["payload"]["rate_limits"] = {
            "primary": {
                "used_percent": 20,
                "window_minutes": 300,
                "resets_at": int((self.now + timedelta(hours=1)).timestamp()),
            },
            "credits": {"balance": "PRIVATE_CREDITS"},
        }
        self.save([event])
        inventory = {
            "windows": [
                {
                    "lane": "alpha",
                    "engine": "codex",
                    "account": "A",
                    "source_paths": [str(self.path)],
                }
            ],
            "accounts": accounts,
        }
        with IncrementalReader(self.cache) as reader:
            result = collect_incremental(inventory, reader, self.now)
        self.assertEqual(len(latest_accounts(result["accounts"])), 3)
        self.assertEqual({a["engine"] for a in result["accounts"]}, {"codex", "claude"})
        self.assertNotIn("PRIVATE_", json.dumps(result) + self.cache.read_text())
        missing = account_sample("A", "claude", {"used_percent": 4}, self.now)
        self.assertEqual(missing["status"], "unknown")
        self.assertIsNone(missing["window_minutes"])
        self.assertIsNone(missing["resets_at"])
        self.assertEqual(codex_limits(event)[0]["used_percent"], 20)

    def test_quota_cache_zero_bytes_freshness_corruption_and_privacy(self):
        source, cache = self.root / "usage.json", self.root / "quota-cache.json"
        source.write_text(
            json.dumps(
                {
                    "at": self.now.timestamp(),
                    "accounts": {
                        "A": {
                            "who": "PRIVATE_SENTINEL",
                            "five_hour": 25,
                            "five_hour_reset": (self.now + timedelta(hours=2)).isoformat(),
                        }
                    },
                }
            )
        )
        first, count, status, _ = cached_claude_accounts(source, cache, self.now)
        self.assertEqual(count, source.stat().st_size)
        self.assertEqual(first[0]["status"], "ok")
        self.assertEqual(first[1]["status"], "unknown")
        later, count, status, _ = cached_claude_accounts(
            source, cache, self.now + timedelta(seconds=4000)
        )
        self.assertEqual(count, 0)
        self.assertEqual(later[0]["status"], "stale")
        self.assertNotIn("PRIVATE_SENTINEL", cache.read_text())
        cache.write_text("{broken")
        recovered, count, _, corruption = cached_claude_accounts(source, cache, self.now)
        self.assertEqual(corruption, 1)
        self.assertEqual(recovered, first)
        self.assertGreater(count, 0)
        source.write_text("{broken")
        self.assertEqual(cached_claude_accounts(source, cache, self.now)[2], "unknown")
