"""Authored synthetic machine events, command shapes, units and source gates."""

import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
from support import IsolatedTemporaryDirectory
import unittest
from unittest.mock import patch

from sumbi.events.adapters.claude_code import bash_exit_code, collect as collect_claude
from sumbi.events.adapters.codex import collect as collect_codex
from sumbi.cli import main
from sumbi.judge.compare_local import compare_local
from sumbi.outcomes.local_verify.workers import deliver_local
from sumbi.core.records import Coverage
from sumbi.sessions.session import Session
from sumbi.core.time import Window
from sumbi.core.paths import execution_cwd
from sumbi.core.values import timestamp
from sumbi.judge.registration import read_registration
from sumbi.outcomes.local_verify.recognizer import declared_commands, recognize

SCRIPT = "scripts/check.sh"


class CommandTests(unittest.TestCase):
    def test_executed_program_shapes(self):
        commands = ["./scripts/check.sh", "bash scripts/check.sh",
                    "sh --login ./scripts/check.sh", "bash -l scripts/check.sh",
                    "bash -lc './scripts/check.sh'", "sh -l -c 'VAR=value bash scripts/check.sh'",
                    "VAR=value OTHER='two words' bash scripts/check.sh",
                    "& 'C:\\Program Files\\Shell\\bash.exe' --login 'scripts/check.sh'",
                    "& 'C:\\Program Files\\Shell\\bash.exe' -lc 'ENV=x bash scripts/check.sh'",
                    "pwsh -NoProfile -Command \"bash scripts/check.sh\"",
                    ["powershell", "-Command", "& 'C:\\Tools\\bash.exe' 'scripts/check.sh'"],
                    ["bash", "-lc", "VAR=x ./scripts/check.sh"]]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(recognize(command, (SCRIPT,)), "matched")

    def test_reads_never_execute_even_embedded_mentions(self):
        for read in ("Get-Content", "cat", "type", "rg", "grep", "Select-String", "sed", "head", "tail"):
            for command in (f"{read} {SCRIPT}", f"{read} 'docs mention bash {SCRIPT}'",
                            ["pwsh", "-Command", f"{read} {SCRIPT}"],
                            ["bash", "-lc", f"{read} 'one|two' {SCRIPT}"]):
                with self.subTest(command=command):
                    self.assertEqual(recognize(command, (SCRIPT,)), "read")

    def test_uncertain_and_syntax_only_commands_cannot_pass(self):
        commands = ["bash -n scripts/check.sh", "bash --noexec scripts/check.sh",
                    ["pwsh", "-Command", "bash -n 'scripts/check.sh'"],
                    "sh -lc 'bash --noexec scripts/check.sh'", "if true; then bash scripts/check.sh; fi",
                    "echo scripts/check.sh", "python -c 'print(\"scripts/check.sh\")'",
                    "bash scripts/check.sh && echo done", "cat scripts/check.sh; bash scripts/check.sh",
                    "bash $(echo scripts/check.sh)", "bash -lc 'scripts/check.sh' extra",
                    "bash scripts/check.sh > output", "bash -lc 'scripts/check.sh\nfalse'", None]
        commands.extend(["bash scripts/check.sh &", "bash scripts/check.sh >> output", ["bash", SCRIPT, "&"]])
        for command in commands:
            with self.subTest(command=command):
                self.assertNotEqual(recognize(command, (SCRIPT,)), "matched")
        self.assertEqual(recognize("bash elsewhere/check.sh", (SCRIPT,)), "other")
        self.assertEqual(recognize("bash scripts/check.sh.backup", (SCRIPT,)), "unmatched_shape")

    def test_arguments_cannot_weaken_a_declared_verifier(self):
        for flag in ("--help", "--quick", "--dry-run", "--filter", "synthetic-test", "-h", ""):
            for command in (["bash", SCRIPT, flag], [SCRIPT, flag],
                            ["pwsh", "-Command", f"bash {SCRIPT} '{flag}'"]):
                with self.subTest(command=command):
                    self.assertEqual(recognize(command, (SCRIPT,)), "unmatched_shape")
        self.assertEqual(recognize(["bash", SCRIPT], (SCRIPT,)), "matched")

    def test_strict_local_windows_file_uri_identity(self):
        self.assertEqual(execution_cwd("file:///C:/fixture/workspace"), execution_cwd("C:/fixture/workspace"))
        self.assertEqual(execution_cwd("file:///c:/FIXTURE/WORKSPACE"), execution_cwd("C:/fixture/workspace"))
        self.assertEqual(execution_cwd("file:///C:/fixture/space%20workspace"), execution_cwd("C:/fixture/space workspace"))
        invalid = ["file://remote/C:/fixture/workspace", "file://localhost/C:/fixture/workspace",
                   "file:///C:/fixture/workspace?", "file:///C:/fixture/workspace#",
                   "file:///C:/fixture/workspace?other=1", "file:///C:/fixture/workspace#other",
                   "file:///C:/fixture/workspace%00", "file:///C:/fixture/workspace%ZZ",
                   "file:///C:/fixture/workspace%FF", "file:///C:/fixture%2Fworkspace",
                   "file:///C:/fixture/work\nspace", "file:///C:/fixture/work\tspace",
                   "file:///C:/fixture/workspace%09", "file:///C:/fixture/workspace%0A",
                   "file:///C:/fixture%5Cworkspace", "file:///fixture/workspace",
                   "file:C:/fixture/workspace", "https://fixture.invalid/C:/fixture/workspace",
                   "file:///C:/fixture\\workspace", "file:///C:/fixture/workspace\x00"]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(execution_cwd(value))

    def test_config_and_cli_declarations(self):
        with IsolatedTemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".sumbi").mkdir()
            (root / ".sumbi/config.toml").write_text('verify = ["./scripts/check.sh"]', encoding="utf-8")
            self.assertEqual(declared_commands(root), (SCRIPT,))
            self.assertEqual(declared_commands(root, ["scripts/other.sh"]), ("scripts/other.sh",))
            for values in ([], ["../secret.sh"], ["/check.sh"], ["C:\\check.sh"]):
                with self.assertRaisesRegex(ValueError, "repository-relative"):
                    declared_commands(root, values)

    def test_claude_machine_envelopes_and_unknowns(self):
        successful = {"toolUseResult": {"stdout": "Exit code 7\nsynthetic output", "stderr": "", "interrupted": False}}
        self.assertEqual(bash_exit_code(successful, {"is_error": False}), 0)
        self.assertEqual(bash_exit_code({}, {"is_error": True, "content": "Exit code 3\nsynthetic output"}), 3)
        self.assertEqual(bash_exit_code({"toolUseResult": {"exitCode": -9}}, {}), -9)
        for event, block in [({}, {"is_error": False}), ({}, {"is_error": True, "content": "arbitrary failure"}),
                             ({"toolUseResult": {**successful["toolUseResult"], "backgroundTaskId": "task"}}, {}),
                             ({"toolUseResult": {"exitCode": True}}, {}),
                             ({"toolUseResult": {"exitCode": 0, "interrupted": True}}, {})]:
            self.assertIsNone(bash_exit_code(event, block))


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = IsolatedTemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.repo = (Path(self.temporary.name) / "repo").resolve()
        self.repo.mkdir()
        self.codex = self.home / ".codex/sessions"
        self.codex.mkdir(parents=True)
        self.window = Window(timestamp("2030-01-01T00:00:00Z"), timestamp("2030-01-03T00:00:00Z"))

    def write_codex(self, identity, start="2030-01-01T01:00:00Z", *, edits=True,
                    code=0, verify=True, worker=True, legacy=False, latest=None, unknown_tokens=False):
        at = timestamp(start)
        def stamp(seconds):
            return (at + timedelta(seconds=seconds)).isoformat()
        meta = {"id": identity, "cwd": str(self.repo), "cli_version": "1.0"}
        if worker:
            meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
        records = [{"type": "session_meta", "timestamp": start, "payload": meta},
                   {"type": "turn_context", "timestamp": stamp(1), "payload": {"model": "model-a", "effort": "high", "cwd": str(self.repo)}}]
        if edits:
            records.append({"type": "response_item", "timestamp": stamp(2), "payload": {
                "type": "custom_tool_call", "name": "apply_patch", "call_id": "edit-1", "input": "synthetic patch"}})
        if verify:
            if legacy:
                for kind, payload in (("exec_command_begin", {"command": ["bash", SCRIPT]}),
                                      ("exec_command_end", {"exit_code": code})):
                    records.append({"type": "event_msg", "timestamp": stamp(3 if kind.endswith("begin") else 4),
                                    "payload": {"type": kind, "call_id": "check-1", **payload}})
            else:
                records.append({"type": "event_msg", "timestamp": stamp(4), "payload": {"type": "item_completed",
                    "started_at_ms": int((at + timedelta(seconds=3)).timestamp() * 1000),
                    "item": {"type": "CommandExecution", "id": "check-1", "command": ["bash", SCRIPT], "exit_code": code}}})
        tokens = {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 10}
        if unknown_tokens:
            tokens.pop("output_tokens")
        records.append({"type": "event_msg", "timestamp": stamp(5), "payload": {"type": "token_count",
                       "info": {"total_token_usage": tokens}}})
        records.append({"type": "event_msg", "timestamp": latest or stamp(6), "payload": {"type": "agent_message", "message": "synthetic final"}})
        self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        return records

    def save(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def write_claude(self, identity="child", start="2030-01-01T02:00:00Z", code=0, *, background=False):
        at = timestamp(start)
        records = []
        for seconds, role, block in [(0, "assistant", {"type": "tool_use", "name": "Edit", "id": "edit", "input": {"file_path": "synthetic.py"}}),
                                     (1, "assistant", {"type": "tool_use", "name": "Bash", "id": "run", "input": {"command": "bash " + SCRIPT, "run_in_background": background}}),
                                     (2, "user", {"type": "tool_result", "tool_use_id": "run", "is_error": code != 0,
                                                  "content": "Exit code 2\nsynthetic failure" if code else "synthetic output"})]:
            event = {"type": role, "sessionId": "parent", "agentId": identity, "timestamp": (at + timedelta(seconds=seconds)).isoformat(),
                     "cwd": str(self.repo), "version": "1.0", "uuid": identity + str(seconds), "message": {"content": [block]}}
            if role == "assistant":
                event["message"].update(model="model-b", id=identity + str(seconds), usage={"input_tokens": 10,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 5, "output_tokens": 2})
            else:
                event["toolUseResult"] = {"stdout": "synthetic", "stderr": "", "interrupted": False}
            records.append(event)
        self.save(self.home / ".claude/projects/fixture/parent/subagents" / ("agent-" + identity + ".jsonl"), records)
        return records

    def report(self, **kwargs):
        return deliver_local(self.home, self.window, self.repo, verify=[SCRIPT], **kwargs)

    def test_five_states_parent_and_mixed_agents(self):
        self.write_codex("passed")
        self.write_codex("failed", code=2, legacy=True)
        self.write_codex("unverified", verify=False)
        self.write_codex("active", latest="2030-01-02T23:59:00Z")
        self.write_codex("unchanged", edits=False)
        self.write_codex("parent", edits=False, worker=False)
        self.write_claude()
        report = self.report()
        self.assertEqual(report["states"], {"success": 2, "failed": 1, "unverified": 1, "in_progress": 1, "no_change": 1})
        self.assertEqual(report["success_rate"]["numerator"], 2)
        self.assertEqual(report["success_rate"]["denominator"], 4)
        self.assertEqual(report["dispatch_overhead"]["sessions"], 1)
        self.assertEqual(report["dispatch_overhead"]["observed_total"], 110)
        self.assertEqual(report["cost"]["per_success"]["total"]["numerator"], 364)
        self.assertEqual({r["agent"] for r in report["units"]}, {"codex", "claude-code"})
        self.assertTrue(all(r["parent_id"] for r in report["units"]))
        encoded = json.dumps(report)
        for secret in (SCRIPT, str(self.repo), "synthetic patch", "synthetic output", "parent", "passed"):
            # Field names contain parent_id and last_check_passed; check values only.
            if secret not in ("parent", "passed"):
                self.assertNotIn(secret, encoded)

    def test_last_edit_and_last_unknown_execution_are_conservative(self):
        records = self.write_codex("later-edit")
        records.append({"type": "response_item", "timestamp": "2030-01-01T01:00:07Z", "payload": {
            "type": "function_call", "name": "functions.apply_patch", "call_id": "edit-2", "arguments": "{}"}})
        self.save(self.codex / "rollout-later-edit.jsonl", records)
        unknown = self.write_codex("unknown")
        unknown.append({"type": "event_msg", "timestamp": "2030-01-01T01:00:07Z", "payload": {"type": "item_completed",
            "item": {"type": "CommandExecution", "id": "check-2", "command": ["bash", SCRIPT]}}})
        self.save(self.codex / "rollout-unknown.jsonl", unknown)
        report = self.report()
        self.assertEqual(report["states"]["unverified"], 2)
        self.assertEqual(report["coverage"]["commands"]["unknown_exit_code"], 1)

    def test_dispatch_selection_does_not_follow_outcomes_or_parent(self):
        self.write_codex("old-worker", start="2029-12-31T23:59:59Z", latest="2030-01-01T02:00:00Z")
        self.write_codex("other-worker")
        report = self.report()
        self.assertEqual(len(report["units"]), 1)
        self.assertEqual(report["states"]["success"], 1)

    def test_missing_tokens_do_not_become_zero(self):
        self.write_codex("missing", unknown_tokens=True)
        report = self.report()
        self.assertIsNone(report["units"][0]["tokens"]["total"])
        self.assertFalse(report["units"][0]["tokens_complete"])

    def test_input_background_launch_without_result_task_id_is_unknown(self):
        self.write_claude(background=True)
        report = self.report()
        self.assertEqual(report["states"]["unverified"], 1)
        self.assertEqual(report["coverage"]["commands"]["unknown_exit_code"], 1)
        self.assertEqual(report["units"][0]["reason"], "last_check_exit_unknown")

    def test_codex_verification_does_not_infer_numeric_exit_from_output(self):
        for identity, legacy, code in (("item-envelope", False, None), ("legacy-envelope", True, None),
                                       ("boolean-code", False, True)):
            records = self.write_codex(identity, legacy=legacy)
            execution = next(r for r in records if r.get("payload", {}).get("type") in ("item_completed", "exec_command_end"))
            payload = execution["payload"] if legacy else execution["payload"]["item"]
            payload["exit_code"] = code
            payload["output"] = "Process exited with code 0\n"
            self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        report = self.report()
        self.assertEqual(report["states"]["unverified"], 3)
        self.assertEqual(report["coverage"]["commands"]["unknown_exit_code"], 3)

    def test_earlier_claude_metadata_preserves_earliest_cwd_and_start_provenance(self):
        for identity, first in (("metadata-first", True), ("metadata-late", False)):
            records = self.write_claude(identity=identity)
            metadata = {"type": "queue-operation", "sessionId": "parent", "agentId": identity,
                        "timestamp": "2030-01-01T01:59:00Z", "uuid": identity + "metadata"}
            records.insert(0, metadata) if first else records.append(metadata)
            self.save(self.home / ".claude/projects/fixture/parent/subagents" / ("agent-" + identity + ".jsonl"), records)
        sessions = collect_claude(self.home, self.window, Coverage(), session_factory=Session)
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            self.assertEqual(session.start_at, timestamp("2030-01-01T01:59:00Z"))
            self.assertEqual(session.start_cwd, str(self.repo))
            self.assertEqual(session.start_evidence, "first-observed-cwd")
        self.write_codex("header")
        session = collect_codex(self.home, self.window, Coverage(), session_factory=Session)[0]
        self.assertEqual(session.start_evidence, "session-header")
        self.assertEqual(self.report()["states"]["success"], 3)
        self.assertNotIn("start_evidence", json.dumps(self.report()))

    def test_excluded_scope_counts_only_sessions_observed_in_scan(self):
        outside = str((Path(self.temporary.name) / "other").resolve())
        for identity, start in (("old-outside", "2029-12-01T01:00:00Z"), ("current-outside", "2030-01-01T01:00:00Z")):
            records = self.write_codex(identity, start=start, edits=False, verify=False)
            for event in records:
                if event["type"] in ("session_meta", "turn_context"):
                    event["payload"]["cwd"] = outside
            self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        self.assertEqual(self.report()["coverage"]["excluded_scope"], {"other": 1})

    def test_uri_cwd_confirms_full_target_and_keeps_other_paths_unverified(self):
        target = "C:/fixture/workspace"
        repository = Path(self.temporary.name) / "standin"
        # Exercise the state gate without making a host-specific fixture tree.
        from sumbi.outcomes.local_verify.workers import state
        from sumbi.sessions.session import CommandExecution, Session
        at = timestamp("2030-01-01T01:00:00Z")
        session = Session("codex", "synthetic")
        session.times.update((at, at + timedelta(seconds=4)))
        session.edit("edit", at + timedelta(seconds=1))
        for cwd, expected in (("file:///C:/fixture/workspace", "success"),
                              ("file:///C:/fixture/other", "unverified"),
                              ("file://remote/C:/fixture/workspace", "unverified")):
            session.commands["run"] = CommandExecution("codex", "synthetic", at + timedelta(seconds=4),
                ["bash", SCRIPT], 0, at + timedelta(seconds=2), cwd)
            with patch.object(Path, "resolve", return_value=Path(target)):
                self.assertEqual(state(session, self.window, (SCRIPT,), 5, repository)[0], expected)

    def test_missing_edit_or_execution_timestamp_does_not_preserve_success(self):
        for identity, kind in (("unknown-edit-time", "response_item"), ("unknown-check-time", "event_msg")):
            records = self.write_codex(identity)
            if kind == "response_item":
                payload = {"type": "custom_tool_call", "call_id": "edit-2", "name": "apply_patch", "input": "synthetic"}
            else:
                payload = {"type": "item_completed", "item": {"type": "CommandExecution", "id": "check-2", "command": ["bash", SCRIPT], "exit_code": 0}}
            records.append({"type": kind, "timestamp": "invalid-time", "payload": payload})
            self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        report = self.report()
        self.assertEqual(report["states"]["unverified"], 2)
        self.assertEqual(report["coverage"]["commands"]["edit_timestamp_missing"], 1)
        self.assertEqual(report["coverage"]["commands"]["command_timestamp_missing"], 1)

    def test_command_started_before_edit_unknown_start_and_wrong_cwd(self):
        for identity, change in (("overlap", "time"), ("wrong-cwd", "cwd"), ("unknown-start", "unknown")):
            records = self.write_codex(identity)
            execution = next(r for r in records if r.get("payload", {}).get("type") == "item_completed")
            if change == "time":
                execution["payload"]["started_at_ms"] -= 2000
            elif change == "cwd":
                execution["payload"]["item"]["cwd"] = str(self.repo / "elsewhere")
            else:
                execution["payload"].pop("started_at_ms")
            self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        report = self.report()
        self.assertEqual(report["states"]["unverified"], 3)
        self.assertEqual(report["coverage"]["commands"]["unknown_execution_start"], 1)
        self.assertEqual(report["coverage"]["commands"]["verification_cwd_unconfirmed"], 1)

    def test_inherited_history_cannot_supply_worker_edits_checks_or_elapsed(self):
        own = timestamp("2030-01-02T01:00:00Z")
        old = own - timedelta(days=1)
        inherited = [
            {"type": "session_meta", "timestamp": old.isoformat(), "payload": {"id": "parent", "cwd": str(self.repo)}},
            {"type": "response_item", "timestamp": (old + timedelta(seconds=1)).isoformat(), "payload": {
                "type": "custom_tool_call", "name": "apply_patch", "call_id": "parent-edit", "input": "synthetic"}},
            {"type": "event_msg", "timestamp": (old + timedelta(seconds=3)).isoformat(), "payload": {
                "type": "item_completed", "started_at_ms": int((old + timedelta(seconds=2)).timestamp() * 1000),
                "item": {"type": "CommandExecution", "id": "parent-check", "command": ["bash", SCRIPT],
                         "cwd": str(self.repo), "exit_code": 0}}},
        ]
        for identity, edits, verify in (("unchanged-child", False, False), ("own-check-only", False, True),
                                        ("own-edit-only", True, False), ("own-work", True, True)):
            records = self.write_codex(identity, start=own.isoformat(), edits=edits, verify=verify)
            records[1:1] = inherited
            self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        report = self.report()
        self.assertEqual(len(report["units"]), 4)
        self.assertEqual(report["states"], {"success": 1, "failed": 0, "unverified": 1, "in_progress": 0, "no_change": 2})
        self.assertEqual(report["success_rate"]["denominator"], 2)
        self.assertEqual(report["coverage"]["commands"]["matched"], 2)
        self.assertTrue(all(row["elapsed_seconds"] >= 0 for row in report["units"]))
        self.assertTrue(all(timestamp(row["last_at"]) >= own for row in report["units"]))

    def test_activity_and_checks_obey_both_lifetime_bounds(self):
        from sumbi.outcomes.local_verify.workers import state
        from sumbi.sessions.session import Session
        start = timestamp("2030-01-02T01:00:00Z")
        lifetime = Window(start, start + timedelta(minutes=1))
        session = Session("codex", "synthetic")
        session.times.add(start - timedelta(seconds=1))
        session.edit("old-edit", start - timedelta(seconds=3))
        session.execution("old-check", start - timedelta(seconds=1), ["bash", SCRIPT], 0,
                          started_at=start - timedelta(seconds=2), cwd=str(self.repo))
        result = state(session, lifetime, (SCRIPT,), 5, self.repo)
        self.assertEqual(result[0], "no_change")
        self.assertIsNone(result[3])
        self.assertNotIn("matched", result[2])
        # Upper-bound events also cannot replace the genuine worker check.
        session.edit("own-edit", start + timedelta(seconds=1))
        session.execution("own-check", start + timedelta(seconds=3), ["bash", SCRIPT], 0,
                          started_at=start + timedelta(seconds=2), cwd=str(self.repo))
        session.times.add(start + timedelta(seconds=4))
        session.edit("future-edit", lifetime.until)
        session.execution("future-check", lifetime.until, ["bash", SCRIPT], 2,
                          started_at=lifetime.until, cwd=str(self.repo))
        session.times.add(lifetime.until)
        self.assertEqual(state(session, lifetime, (SCRIPT,), 0, self.repo)[0], "success")

    def test_check_crossing_dispatch_does_not_preserve_an_earlier_success(self):
        records = self.write_codex("crossing")
        start = timestamp(records[0]["timestamp"])
        records.append({"type": "event_msg", "timestamp": (start + timedelta(seconds=7)).isoformat(), "payload": {
            "type": "item_completed", "started_at_ms": int((start - timedelta(seconds=1)).timestamp() * 1000),
            "item": {"type": "CommandExecution", "id": "crossing-check", "command": ["bash", SCRIPT],
                     "cwd": str(self.repo), "exit_code": 0}}})
        self.save(self.codex / "rollout-crossing.jsonl", records)
        report = self.report()
        self.assertEqual(report["states"]["unverified"], 1)
        self.assertEqual(report["units"][0]["reason"], "check_start_not_after_last_edit")

    def test_completion_only_cwd_comes_from_execution_start_not_completion(self):
        other = str(self.repo / "other")
        for identity, before, after, explicit, expected in (
                ("wrong-at-start", other, str(self.repo), None, "unverified"),
                ("root-at-start", str(self.repo), other, None, "success"),
                ("explicit-root", other, other, str(self.repo), "success"),
                ("explicit-other", str(self.repo), str(self.repo), other, "unverified")):
            records = self.write_codex(identity)
            start = timestamp(records[0]["timestamp"])
            records.extend({"type": "turn_context", "timestamp": (start + timedelta(seconds=seconds)).isoformat(),
                            "payload": {"cwd": cwd}} for seconds, cwd in ((2.5, before), (3.5, after)))
            completion = next(r for r in records if r.get("payload", {}).get("type") == "item_completed")
            if explicit is not None:
                completion["payload"]["item"]["cwd"] = explicit
            self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
            session = next(s for s in collect_codex(self.home, self.window, Coverage(), session_factory=Session) if s.raw_id == identity)
            self.assertEqual(session.commands["check-1"].cwd, explicit if explicit is not None else before)
        self.assertEqual(self.report()["states"]["success"], 2)
        self.assertEqual(self.report()["states"]["unverified"], 2)

    def test_paired_start_and_legacy_begin_keep_execution_cwd(self):
        for identity, legacy, explicit in (("paired", False, True), ("paired-inferred", False, False),
                                            ("legacy", True, False)):
            records = self.write_codex(identity, legacy=legacy)
            start = timestamp(records[0]["timestamp"])
            if not legacy:
                item = {"type": "CommandExecution", "id": "check-1", "command": ["bash", SCRIPT]}
                if explicit:
                    item["cwd"] = str(self.repo)
                    records.append({"type": "turn_context", "timestamp": (start + timedelta(seconds=2.5)).isoformat(),
                                    "payload": {"cwd": str(self.repo / "other")}})
                index = next(i for i, r in enumerate(records) if r.get("payload", {}).get("type") == "item_completed")
                records.insert(index, {"type": "event_msg", "timestamp": (start + timedelta(seconds=3)).isoformat(),
                                       "payload": {"type": "item_started", "item": item}})
            records.append({"type": "turn_context", "timestamp": (start + timedelta(seconds=3.5)).isoformat(),
                            "payload": {"cwd": str(self.repo / "other")}})
            self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        self.assertEqual(self.report()["states"]["success"], 3)

    def test_conflicting_or_untimed_paired_start_cannot_upgrade_verification(self):
        cases = (
            ("rounded-other", 3.0005, "other", False, "unverified"),
            ("pre-edit-root", 1, "root", True, "unverified"),
            ("pre-edit-inferred", 1, None, False, "unverified"),
            ("untimed-other", None, "other", False, "unverified"),
            ("matched-other", 3, "other", False, "unverified"),
            ("matched-conflicting-cwd", 3, "other", True, "unverified"),
            ("matched-root", 3, "root", False, "success"),
            ("matched-normalized-root", 3, "normalized-root", True, "success"),
        )
        for identity, seconds, cwd, completed_cwd, expected in cases:
            with self.subTest(identity=identity):
                records = self.write_codex(identity)
                start = timestamp(records[0]["timestamp"])
                item = {"type": "CommandExecution", "id": "check-1", "command": ["bash", SCRIPT]}
                if cwd is not None:
                    item["cwd"] = str(self.repo if cwd == "root" else self.repo / "child" / ".."
                                      if cwd == "normalized-root" else self.repo / "other")
                index = next(i for i, r in enumerate(records) if r.get("payload", {}).get("type") == "item_completed")
                paired = {"type": "event_msg", "payload": {"type": "item_started", "item": item}}
                if seconds is not None:
                    paired["timestamp"] = (start + timedelta(seconds=seconds)).isoformat()
                records.insert(index, paired)
                if completed_cwd:
                    records[index + 1]["payload"]["item"]["cwd"] = str(self.repo)
                self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
                session = next(s for s in collect_codex(self.home, self.window, Coverage(), session_factory=Session) if s.raw_id == identity)
                if seconds != 3:
                    self.assertIsNone(session.commands["check-1"].started_at)
                unit = next(r for r in self.report()["units"] if r["id"] == session.id())
                self.assertEqual(unit["state"], expected)
        report = self.report()
        self.assertEqual(report["states"]["success"], 2)
        self.assertEqual(report["states"]["unverified"], 6)

    def test_inferred_cwd_rejects_ambiguous_untimed_or_pre_dispatch_context(self):
        for identity, kind in (("ambiguous-context", "ambiguous"), ("untimed-context", "untimed"),
                               ("pre-dispatch-context", "old"), ("no-execution-start", "missing")):
            records = self.write_codex(identity)
            start = timestamp(records[0]["timestamp"])
            if kind == "ambiguous":
                records.extend({"type": "turn_context", "timestamp": (start + timedelta(seconds=2.5)).isoformat(),
                                "payload": {"cwd": cwd}} for cwd in (str(self.repo), str(self.repo / "other")))
            elif kind == "untimed":
                records.append({"type": "turn_context", "timestamp": "invalid", "payload": {"cwd": str(self.repo)}})
            elif kind == "old":
                records.append({"type": "turn_context", "timestamp": (start - timedelta(seconds=1)).isoformat(),
                                "payload": {"cwd": str(self.repo / "other")}})
            else:
                next(r for r in records if r.get("payload", {}).get("type") == "item_completed")["payload"].pop("started_at_ms")
            self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        report = self.report()
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["states"]["unverified"], 3)
        self.assertEqual(report["coverage"]["commands"]["unknown_execution_cwd"], 3)

    def registration(self, **changes):
        data = json.loads((Path(__file__).parent / "fixtures/compare/registration.json").read_text())
        data.update(outcome_source="local-verify", sample_size_per_arm=1,
                    before={"since": "2030-01-01T00:00:00Z", "until": "2030-01-02T00:00:00Z"},
                    after={"since": "2030-01-02T00:00:00Z", "until": "2030-01-03T00:00:00Z"},
                    applied_at="2030-01-02T00:00:00Z")
        data.update(changes)
        path = Path(self.temporary.name) / "registration.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_compare_units_non_success_and_worker_cost_scope(self):
        self.write_codex("before")
        self.write_codex("after", start="2030-01-02T01:00:00Z", verify=False)
        self.write_codex("parent", start="2029-12-31T01:00:00Z", worker=False, latest="2030-01-02T02:00:00Z")
        report = compare_local(self.home, self.repo, self.registration(), verify=[SCRIPT], resamples=100)
        self.assertEqual(report["arms"]["after"]["success_rate"]["denominator"], 1)
        self.assertEqual(report["arms"]["after"]["success_rate"]["numerator"], 0)
        self.assertEqual(report["arms"]["after"]["candidate_states"]["unverified"], 1)
        self.assertEqual(report["cost_basis"], "retained_worker_lifetime_only")
        self.assertFalse(next(f for f in report["flags"] if f["name"] == "dispatch_overhead_excluded")["blocking"])

    def test_origin_only_other_checkout_is_fixed_unit_but_blocks_comparison(self):
        self.write_codex("before")
        records = self.write_codex("after", start="2030-01-02T01:00:00Z")
        records[0]["payload"]["cwd"] = str((Path(self.temporary.name) / "other-checkout").resolve())
        self.save(self.codex / "rollout-after.jsonl", records)
        with patch("sumbi.measure.attribution.RepositoryAttributor.origin", return_value=("example.test/owner/project", "origin")):
            report = compare_local(self.home, self.repo, self.registration(), verify=[SCRIPT], resamples=100)
        self.assertEqual(report["arms"]["after"]["n"], 1)
        self.assertEqual(report["arms"]["after"]["units"][0]["state"], "success")
        self.assertEqual(report["arms"]["after"]["units"][0]["start_scope"], "same_origin_other_checkout")
        self.assertEqual(report["verdict"]["proposal"], "withhold")
        self.assertIn("unit_start_scope_mismatch", report["verdict"]["reasons"])

    def test_subdirectory_start_can_verify_root_without_scope_mismatch(self):
        self.write_codex("before")
        subdirectory = self.repo / "package"
        subdirectory.mkdir()
        records = self.write_codex("after", start="2030-01-02T01:00:00Z")
        records[0]["payload"]["cwd"] = str(subdirectory)
        self.save(self.codex / "rollout-after.jsonl", records)
        report = compare_local(self.home, self.repo, self.registration(), verify=[SCRIPT], resamples=100)
        self.assertEqual(report["arms"]["after"]["units"][0]["state"], "success")
        self.assertEqual(report["arms"]["after"]["units"][0]["start_scope"], "repository_subdirectory")
        self.assertNotIn("unit_start_scope_mismatch", [f["name"] for f in report["flags"]])

    def test_compare_source_coverage_and_metadata_gates(self):
        self.write_codex("before")
        self.write_codex("after", start="2030-01-02T01:00:00Z", code=None)
        report = compare_local(self.home, self.repo, self.registration(), verify=[SCRIPT], resamples=100)
        self.assertIn("unknown_verification_exit", report["verdict"]["reasons"])
        self.assertEqual(report["verdict"]["proposal"], "withhold")
        with self.assertRaisesRegex(ValueError, "both arms"):
            compare_local(self.home, self.repo, self.registration(outcome_source="github"), verify=[SCRIPT], resamples=100)
        self.write_claude(start="2030-01-02T02:00:00Z")
        report = compare_local(self.home, self.repo, self.registration(), verify=[SCRIPT], resamples=100)
        self.assertIn("agent_mix_shift", [f["name"] for f in report["flags"] if f["blocking"]])

    def test_worker_cost_proposal_can_adopt_with_separate_spanning_parent(self):
        for arm, start, spend in (("before", "2030-01-01T01:00:00Z", 100),
                                  ("after", "2030-01-02T01:00:00Z", 50)):
            for index in range(12):
                identity = arm + str(index)
                records = self.write_codex(identity, start=start)
                token_record = next(r for r in records if r.get("payload", {}).get("type") == "token_count")
                token_record["payload"]["info"]["total_token_usage"] = {
                    "input_tokens": spend, "cached_input_tokens": 0, "output_tokens": 0}
                self.save(self.codex / ("rollout-" + identity + ".jsonl"), records)
        self.write_codex("parent", start="2030-01-01T00:01:00Z", worker=False,
                         latest="2030-01-02T03:00:00Z")
        report = compare_local(self.home, self.repo, self.registration(sample_size_per_arm=12), verify=[SCRIPT], resamples=100)
        self.assertEqual(report["verdict"]["proposal"], "adopt")
        self.assertEqual(report["ratios"]["total"]["value"], 0.5)
        self.assertEqual(report["dispatch_overhead"]["sessions"], 1)
        self.assertEqual(report["arms"]["after"]["n"], 12)

    def test_local_cli_deliver_compare_and_output_protection(self):
        self.write_codex("before")
        self.write_codex("after", start="2030-01-02T01:00:00Z")
        arguments = ["--home", str(self.home), "--repository", str(self.repo), "--verify", SCRIPT, "--json", "-"]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            code = main(["deliver", "--outcome-source", "local-verify", "--since", self.window.since.isoformat(),
                         "--until", self.window.until.isoformat(), *arguments])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stream.getvalue())["states"]["success"], 2)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            code = main(["compare", "--registration", str(self.registration()), "--resamples", "100", *arguments])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stream.getvalue())["outcome_source"], "local-verify")
        config = self.repo / ".sumbi/config.toml"
        config.parent.mkdir()
        config.write_text('verify = ["scripts/check.sh"]', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["deliver", "--outcome-source", "local-verify", "--since", self.window.since.isoformat(),
                  "--until", self.window.until.isoformat(), *arguments[:-2], "--json", str(config)])
        self.assertIn("verify", config.read_text())

    def test_verifier_all_fail_mixed_all_pass_and_no_completed_results(self):
        from sumbi.outcomes.local_verify.workers import text_summary as deliver_summary
        from sumbi.judge.compare_local import text_summary as compare_summary
        for before, after, expected in ((2, 3, True), (2, 0, False), (0, 0, False), (None, None, False)):
            with self.subTest(codes=(before, after)):
                self.write_codex("before", code=before)
                self.write_codex("after", start="2030-01-02T01:00:00Z", code=after)
                delivered = self.report()
                compared = compare_local(self.home, self.repo, self.registration(), verify=[SCRIPT], resamples=100)
                counts = {str(c): (before, after).count(c) for c in (before, after) if c is not None}
                for report, summary in ((delivered, deliver_summary), (compared, compare_summary)):
                    self.assertEqual(report["verification"]["exit_code_counts"], counts)
                    signal_expected = expected if report is delivered else any(c is not None and c != 0 for c in (before, after))
                    self.assertEqual(bool(report["signals"]), signal_expected)
                    if expected:
                        self.assertIn("verifier_never_passed", summary(report).splitlines()[1])
                        self.assertIn("exit 2: 1", summary(report).splitlines()[1])
                        self.assertIn("exit 3: 1", summary(report).splitlines()[1])
                    self.assertNotIn(SCRIPT, json.dumps(report) + summary(report))
                if expected:
                    self.assertEqual(compared["verdict"]["proposal"], "withhold")
                    self.assertIn("verifier_never_passed", compared["verdict"]["reasons"])
                    self.assertNotIn("inconclusive_success", compared["verdict"]["reasons"])
                    self.assertTrue(next(f for f in compared["flags"] if f["name"] == "verifier_never_passed")["blocking"])

    def test_compare_verifier_health_per_arm(self):
        from sumbi.judge.compare_local import text_summary
        for before, after in ((2, 0), (0, 3), (2, 3), (0, 0)):
            with self.subTest(codes=(before, after)):
                self.write_codex("before", code=before)
                self.write_codex("after", start="2030-01-02T01:00:00Z", code=after)
                report = compare_local(self.home, self.repo, self.registration(), verify=[SCRIPT], resamples=100)
                evidence = {arm: {"completed": 1, "passed": int(code == 0), "exit_code_counts": {str(code): 1}}
                            for arm, code in (("before", before), ("after", after))}
                self.assertEqual(report["verification"]["arms"], evidence)
                flags = [f for f in report["flags"] if f["name"] == "verifier_never_passed"]
                if before or after:
                    self.assertEqual(report["signals"], [{"name": "verifier_never_passed", "evidence": evidence}])
                    self.assertEqual(flags, [{"name": "verifier_never_passed", "blocking": True, "evidence": evidence}])
                    self.assertEqual(report["verdict"]["proposal"], "withhold")
                    self.assertIn("verifier_never_passed", report["verdict"]["reasons"])
                    self.assertNotIn("inconclusive_success", report["verdict"]["reasons"])
                    for arm, code in (("before", before), ("after", after)):
                        if code:
                            self.assertIn(arm + " verifier_never_passed", text_summary(report).splitlines()[1])
                            self.assertIn(f"exit {code}: 1", text_summary(report).splitlines()[1])
                else:
                    self.assertEqual(report["signals"], [])
                    self.assertEqual(flags, [])
                    self.assertNotIn("verifier_never_passed", report["verdict"]["reasons"])

    def test_verifier_window_excludes_followup_pass_and_includes_no_change_workers(self):
        self.write_codex("before", code=2, edits=False)
        self.write_codex("after", start="2030-01-02T01:00:00Z", code=2)
        self.write_codex("followup", start="2030-01-03T01:00:00Z", code=0)
        report = self.report(scan_until=timestamp("2030-01-04T00:00:00Z"))
        self.assertEqual(report["verification"], {"completed": 2, "passed": 0, "exit_code_counts": {"2": 2}})
        self.assertEqual(report["signals"][0]["name"], "verifier_never_passed")

    def interventions(self, actual):
        path = Path(self.temporary.name) / "interventions.jsonl"
        path.write_text(json.dumps({"intervention_id": "round-01", "practice_id": "handoff", "utc_time": actual}) + "\n", encoding="utf-8")
        return path

    def test_exposure_gap_later_earlier_and_absent(self):
        for actual, dispatch, excluded_arm in (("2030-01-02T00:10:00Z", "2030-01-02T00:05:00Z", "after"),
                                               ("2030-01-01T23:50:00Z", "2030-01-01T23:55:00Z", "before")):
            with self.subTest(actual=actual):
                self.write_codex("before")
                self.write_codex("after", start="2030-01-02T01:00:00Z")
                self.write_codex("gap", start=dispatch)
                registration = self.registration()
                unchanged = compare_local(self.home, self.repo, registration, verify=[SCRIPT], resamples=100)
                self.assertNotIn("exposure_gap", unchanged)
                self.assertEqual(unchanged["arms"][excluded_arm]["n"], 2)
                report = compare_local(self.home, self.repo, registration, verify=[SCRIPT], resamples=100,
                                       interventions_path=self.interventions(actual))
                self.assertEqual(report["exposure_gap"]["seconds"], 600)
                self.assertEqual(report["exclusions"][excluded_arm]["counts"], {"exposure_gap": 1})
                self.assertEqual(report["arms"][excluded_arm]["n"], 1)
                self.assertIn(excluded_arm + "_excluded_or_mixed", report["verdict"]["reasons"])

    def test_exposure_gap_half_open_boundaries_and_cli(self):
        self.write_codex("before")
        self.write_codex("lower", start="2030-01-02T00:00:00Z")
        self.write_codex("upper", start="2030-01-02T00:10:00Z")
        registration = self.registration()
        interventions = self.interventions("2030-01-02T00:10:00Z")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = main(["compare", "--registration", str(registration), "--repository", str(self.repo),
                           "--home", str(self.home), "--verify", SCRIPT, "--interventions", str(interventions),
                           "--intervention-id", "round-01", "--resamples", "100", "--json", "-"])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["arms"]["after"]["n"], 1)
        self.assertEqual(report["exclusions"]["after"]["counts"], {"exposure_gap": 1})
        original = interventions.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["compare", "--registration", str(registration), "--interventions", str(interventions),
                  "--home", str(self.home), "--repository", str(self.repo), "--verify", SCRIPT, "--json", str(interventions)])
        self.assertEqual(interventions.read_bytes(), original)

    def test_agent_only_in_one_arm_is_its_own_blocking_flag(self):
        for index in range(10):
            self.write_codex("before" + str(index))
            self.write_codex("after" + str(index), start="2030-01-02T01:00:00Z")
        self.write_claude(start="2030-01-02T02:00:00Z")
        report = compare_local(self.home, self.repo, self.registration(), verify=[SCRIPT], resamples=100)
        flag = next(f for f in report["flags"] if f["name"] == "agent_mix_shift")
        self.assertTrue(flag["blocking"])
        self.assertEqual(flag["evidence"]["agents"], ["claude-code"])
        self.assertEqual(sum(f["name"] == "agent_mix_shift" for f in report["flags"]), 1)
        self.assertFalse(any(f["name"].endswith("metadata_asymmetric") for f in report["flags"]))


if __name__ == "__main__":
    unittest.main()
