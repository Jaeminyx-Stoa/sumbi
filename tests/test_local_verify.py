"""Authored synthetic machine events, command shapes, units and source gates."""

import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sumbi.adapters.claude_code import bash_exit_code, collect as collect_claude
from sumbi.adapters.codex import collect as collect_codex
from sumbi.cli import main
from sumbi.local_compare import compare_local
from sumbi.local_outcomes import deliver_local
from sumbi.model import Coverage, Window, execution_cwd, timestamp
from sumbi.registration import read_registration
from sumbi.verification import declared_commands, recognize

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
        with tempfile.TemporaryDirectory() as temporary:
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
        self.temporary = tempfile.TemporaryDirectory()
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
        sessions = collect_claude(self.home, self.window, Coverage())
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            self.assertEqual(session.start_at, timestamp("2030-01-01T01:59:00Z"))
            self.assertEqual(session.start_cwd, str(self.repo))
            self.assertEqual(session.start_evidence, "first-observed-cwd")
        self.write_codex("header")
        session = collect_codex(self.home, self.window, Coverage())[0]
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
        from sumbi.local_outcomes import state
        from sumbi.model import CommandExecution, Session
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
        with patch("sumbi.model.RepositoryAttributor.origin", return_value=("example.test/owner/project", "origin")):
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


if __name__ == "__main__":
    unittest.main()
