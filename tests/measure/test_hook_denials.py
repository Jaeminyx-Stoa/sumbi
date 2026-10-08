"""Synthetic native results exercise the hook denial measurement contract."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

from support import IsolatedTemporaryDirectory
from sumbi.core.time import Window
from sumbi.core.values import timestamp
from sumbi.events.hook_denials import normalize_reason, recognize
from sumbi.measure.attribution import ProjectRule
from sumbi.measure.report import collect, text_summary


WINDOW = Window(timestamp("2030-01-01T00:00:00Z"), timestamp("2030-01-02T00:00:00Z"))
BLOCKED = "Command blocked by PreToolUse hook: "


def at(second):
    return f"2030-01-01T00:{second // 60:02d}:{second % 60:02d}Z"


def response(second, kind, identity, **fields):
    return {"type": "response_item", "timestamp": at(second),
            "payload": {"type": kind, "call_id": identity, **fields}}


def codex_call(second, identity, output, name="exec_command", custom=False, **fields):
    call = "custom_tool_call" if custom else "function_call"
    return [response(second, call, identity, name=name, arguments="{}"),
            response(second + 1, call + "_output", identity, output=output, **fields)]


def claude_call(second, identity, output, name="Bash", error=False):
    return [{"type": "assistant", "timestamp": at(second), "sessionId": "claude",
             "message": {"content": [{"type": "tool_use", "id": identity,
                                       "name": name, "input": {}}]}},
            {"type": "user", "timestamp": at(second + 1), "sessionId": "claude",
             "message": {"content": [{"type": "tool_result", "tool_use_id": identity,
                                       "is_error": error, "content": output}]}}]


class HookDenialTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory(prefix="sumbi-hooks-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(self.checkout)], check=True, capture_output=True)

    def save(self, name, records, agent="codex"):
        if agent == "codex":
            path = self.home / ".codex/sessions" / ("rollout-" + name + ".jsonl")
            records = [{"type": "session_meta", "timestamp": at(0),
                        "payload": {"id": name, "cwd": str(self.checkout)}}, *records]
        else:
            path = self.home / ".claude/projects/group" / (name + ".jsonl")
            records = [{**record, "sessionId": name, "cwd": str(self.checkout)}
                       for record in records]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    def report(self, **kwargs):
        return collect(self.home, kwargs.pop("window", WINDOW), salt=kwargs.pop("salt", b"synthetic"),
                       **kwargs)[0]

    def test_all_native_shapes_mixed_home_and_existing_counts(self):
        records = codex_call(1, "plain", BLOCKED + "command form required; accepted: synthetic")
        records += codex_call(3, "recovery", "completed")
        records += codex_call(5, "blocks", [{"type": "input_text", "text": BLOCKED +
                                           "path scope excludes synthetic"}], custom=True)
        records += codex_call(7, "nested", "Script error:\nError: " + BLOCKED +
                              "file size limit exceeded", name="functions.exec")
        records += codex_call(9, "json", json.dumps({"status": "rejected",
                              "reason": BLOCKED + "tool not allowed"}), exit_code=1)
        self.save("codex", records)
        self.save("claude", claude_call(1, "denial", "PreToolUse:Bash hook error: "
                  "slot wait timed out", error=True) + claude_call(3, "recovery", "completed") +
                  claude_call(5, "timeout", "PreToolUse hook did not respond before its timeout "
                              "while waiting", error=True), agent="claude-code")
        report = self.report(rules=[ProjectRule("sample", paths=[str(self.checkout)])])
        group = report["hook_denials"]["summary"]
        self.assertEqual((group["sessions"], group["sessions_with_denials"], group["denials"],
                          group["timeouts"], group["tool_calls"]), (2, 2, 5, 1, 8))
        self.assertEqual(group["denials_per_100_tool_calls"], 62.5)
        self.assertEqual(group["timeouts_per_100_tool_calls"], 12.5)
        self.assertEqual(group["denials_by_class"], {"command_form": 1, "path_scope": 1,
            "size_limit": 1, "tool_not_allowed": 1, "wait_timeout": 1, "other": 0})
        self.assertEqual(group["first_call_denials"], {"sessions": 2, "denied_sessions": 2,
                                                      "share": 1.0})
        self.assertEqual((group["recovered"], group["unrecovered"]), (2, 3))
        # Recognition does not add hook-specific failures to existing tool_errors.
        self.assertEqual(report["summary"]["counts"]["tool_errors"], 3)
        self.assertEqual(report["summary"]["counts"]["tool_results"], 8)
        self.assertEqual(report["schema_version"], "1.3")
        self.assertEqual(set(report["hook_denials"]["by_agent"]), {"claude-code", "codex"})
        self.assertEqual(len(report["hook_denials"]["by_project_and_agent"]), 2)
        for row in report["hook_denials"]["by_project_and_agent"]:
            self.assertEqual(row["rule"], "sample")
            self.assertTrue(row["project_key"].startswith("project_"))
            self.assertEqual(row["denials"], report["hook_denials"]["by_agent"][row["agent"]]["denials"])
        public = json.dumps(report) + text_summary(report)
        for private in (BLOCKED, "accepted: synthetic", "PreToolUse:Bash", "slot wait timed out",
                        str(self.checkout)):
            self.assertNotIn(private, public)
        self.assertIn("Hook denials all", text_summary(report))
        self.assertIn("Recovery: recovered", text_summary(report))

    def test_false_positives_and_unpaired_results(self):
        quoted = BLOCKED + "synthetic reason"
        outputs = ["source code:\n" + quoted, "configuration:\n" + quoted,
                   "file listing:\n" + quoted, "Subagent summary quoting: " + quoted,
                   "  " + quoted, "Script error: summary quoting " + quoted,
                   json.dumps({"status": "rejected", "reason": "quoted: " + quoted}),
                   json.dumps({"status": "accepted", "reason": quoted}),
                   [{"type": "input_text", "text": "source code:"},
                    {"type": "input_text", "text": quoted}],
                   [{"type": "image", "text": ""}, {"type": "input_text", "text": quoted}]]
        records = []
        for index, output in enumerate(outputs):
            records += codex_call(index * 2 + 1, str(index), output, name="exec")
        records += [response(30, "function_call_output", "missing", output=quoted),
                    response(31, "function_call", "own", name="exec", arguments="{}"),
                    response(32, "function_call_output", "different", output=quoted)]
        records += codex_call(33, "wrong-wrapper", "Script error: " + quoted, name="exec_command")
        self.save("codex", records)
        records = claude_call(1, "quoted", "Subagent summary:\nPreToolUse:Bash hook error: synthetic",
                              name="Agent")
        records += claude_call(3, "wrong-tool", "PreToolUse:Read hook error: synthetic", name="Bash")
        records += claude_call(5, "blocks", [{"type": "text", "text": "source code:"},
                              {"type": "text", "text": "PreToolUse:Bash hook error: synthetic"}])
        self.save("claude", records, agent="claude-code")
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual((group["denials"], group["timeouts"]), (0, 0))
        self.assertIsNone(group["first_call_denials"]["share"])

    def test_recovery_third_call_boundary_and_same_tool(self):
        records = codex_call(1, "denied", BLOCKED + "synthetic constraint")
        records += codex_call(3, "other-one", "completed", name="read")
        records += codex_call(5, "other-two", "completed", name="read")
        records += codex_call(7, "third", "completed")
        records += codex_call(9, "late-denied", BLOCKED + "synthetic constraint")
        for index in range(3):
            records += codex_call(11 + index * 2, "unrelated-" + str(index), "completed", name="read")
        records += codex_call(17, "fourth", "completed")
        self.save("codex", records)
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual((group["recovered"], group["unrecovered"]), (1, 1))
        self.assertEqual(group["top_reason_ids"][0]["count"], 2)

    def test_failed_and_pending_calls_consume_recovery_window(self):
        records = codex_call(1, "denied", "Script error: " + BLOCKED + "synthetic", name="exec")
        records += codex_call(3, "failed", "Script error: synthetic failure", name="exec")
        records += [response(5, "function_call", "pending", name="exec", arguments="{}")]
        records += codex_call(7, "rejected", '{"status":"rejected","reason":"synthetic"}', name="exec")
        records += codex_call(9, "late", "completed", name="exec")
        self.save("codex", records)
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual((group["recovered"], group["unrecovered"]), (0, 1))

    def test_first_call_denials_use_successful_results_not_call_starts(self):
        self.save("first", codex_call(1, "ordinary-error", "failed", exit_code=1) +
                  codex_call(3, "denied", BLOCKED + "synthetic"))
        self.save("later", codex_call(1, "success", "completed") +
                  codex_call(3, "denied", BLOCKED + "synthetic"))
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual(group["first_call_denials"], {"sessions": 1, "denied_sessions": 2, "share": .5})

    def test_windows_preserve_prior_success_and_bound_recovery(self):
        self.save("codex", codex_call(1, "prior", "completed") +
                  codex_call(3, "denied", BLOCKED + "synthetic") + codex_call(5, "retry", "completed"))
        window = Window(timestamp(at(3)), timestamp(at(6)))
        group = self.report(window=window)["hook_denials"]["summary"]
        self.assertEqual((group["denials"], group["recovered"], group["unrecovered"]), (1, 0, 1))
        self.assertEqual(group["first_call_denials"]["sessions"], 0)
        self.assertEqual(group["tool_calls"], 2)
        self.assertEqual(self.report(window=Window(timestamp(at(4)), timestamp(at(5))))
                         ["hook_denials"]["summary"]["denials"], 1)
        self.assertEqual(self.report(window=Window(timestamp(at(5)), timestamp(at(7))))
                         ["hook_denials"]["summary"]["denials"], 0)

    def test_normalization_salts_and_cross_agent_reason_matching(self):
        first = 'file size limit 42 exceeded for "/synthetic/file-a"; accepted: synthetic-a'
        second = "file size limit 99 exceeded for 'C:\\synthetic\\file-b'; accepted: synthetic-b"
        self.assertEqual(normalize_reason(first), normalize_reason(second))
        self.assertEqual(normalize_reason("path scope /synthetic/a excludes 12; accepted: synthetic"),
                         "path scope excludes")
        self.assertEqual(normalize_reason("path scope ./synthetic/a excludes 23. Next clause"),
                         "path scope excludes")
        self.save("codex", codex_call(1, "denied", BLOCKED + first))
        self.save("claude", claude_call(1, "denied", "PreToolUse:Bash hook error: " + second,
                                      error=True), agent="claude-code")
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual(len(group["top_reason_ids"]), 1)
        self.assertEqual(group["top_reason_ids"][0]["count"], 2)
        ids = group["top_reason_ids"]
        self.assertEqual(ids, self.report()["hook_denials"]["summary"]["top_reason_ids"])
        self.assertNotEqual(ids, self.report(salt=b"other-synthetic")["hook_denials"]["summary"]["top_reason_ids"])

    def test_top_ten_ties_empty_rates_and_ephemeral_ids(self):
        empty = self.report()["hook_denials"]["summary"]
        self.assertIsNone(empty["denials_per_100_tool_calls"])
        self.assertIsNone(empty["timeouts_per_100_tool_calls"])
        self.assertEqual(empty["top_reason_ids"], [])
        records = []
        for index, word in enumerate("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()):
            records += codex_call(index * 2 + 1, word, BLOCKED + "synthetic " + word)
        self.save("codex", records)
        group = self.report()["hook_denials"]["summary"]
        ids = [row["reason_id"] for row in group["top_reason_ids"]]
        self.assertEqual(len(ids), 10)
        self.assertEqual(ids, sorted(ids))
        ephemeral = self.report(salt=None)["hook_denials"]
        self.assertEqual(ephemeral["reason_ids"]["key"], "ephemeral")
        self.assertNotEqual(ephemeral["summary"]["top_reason_ids"],
                            self.report(salt=None)["hook_denials"]["summary"]["top_reason_ids"])

    def test_keyword_precedence_and_claude_text_blocks(self):
        cases = [("file size limit; accepted: synthetic", "size_limit"),
                 ("outside path scope; accepted: synthetic", "path_scope"),
                 ("tool not allowed; accepted: synthetic", "tool_not_allowed"),
                 ("slot wait timed out; accepted: synthetic", "wait_timeout"),
                 ("synthetic constraint; accepted: synthetic", "command_form"),
                 ("synthetic constraint", "other")]
        for reason, category in cases:
            with self.subTest(category=category):
                self.assertEqual(recognize(BLOCKED + reason, "codex", "exec")[0], category)
        self.save("claude", claude_call(1, "denied", [{"type": "text", "text":
                  "PreToolUse:Bash hook error: synthetic constraint"}], error=True), agent="claude-code")
        self.assertEqual(self.report()["hook_denials"]["summary"]["denials"], 1)

    def test_duplicate_records_and_call_id_types_do_not_add_denials(self):
        pair = codex_call(1, "denied", BLOCKED + "synthetic")
        records = pair + pair + codex_call(3, "retry", "completed")
        records += [response(5, "function_call", 17, name="exec", arguments="{}"),
                    response(6, "function_call_output", "17", output=BLOCKED + "synthetic")]
        self.save("codex", records)
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual((group["denials"], group["recovered"]), (1, 1))
        self.assertEqual(group["tool_calls"], 3)

    def test_running_calls_cannot_recover_and_unmatched_calls_consume_slots(self):
        records = [response(1, "function_call", "running", name="exec", arguments="{}")]
        records += codex_call(2, "denied", BLOCKED + "synthetic", name="exec")
        records += [response(4, "function_call_output", "running", output="completed")]
        for index in range(3):
            records += [response(5 + index, "function_call", "pending-" + str(index),
                                 name="read", arguments="{}")]
        records += codex_call(8, "late", "completed", name="exec")
        self.save("codex", records)
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual((group["recovered"], group["unrecovered"]), (0, 1))
        self.assertEqual(group["first_call_denials"]["sessions"], 1)

    def test_result_time_window_with_no_in_window_tool_starts(self):
        self.save("codex", codex_call(1, "denied", BLOCKED + "synthetic"))
        group = self.report(window=Window(timestamp(at(2)), timestamp(at(3))))["hook_denials"]["summary"]
        self.assertEqual((group["denials"], group["tool_calls"]), (1, 0))
        self.assertIsNone(group["denials_per_100_tool_calls"])

    def test_nested_rejected_exec_and_custom_string_results(self):
        nested = "Script error:\n" + json.dumps({"status": "rejected", "reason": BLOCKED + "synthetic"})
        self.save("codex", codex_call(1, "nested", nested, name="exec") +
                  codex_call(3, "custom", BLOCKED + "synthetic", custom=True))
        self.assertEqual(self.report()["hook_denials"]["summary"]["denials"], 2)

    def test_both_agents_share_an_ephemeral_reason_key_and_timeout_only_session(self):
        self.save("codex", codex_call(1, "denied", BLOCKED + "synthetic constraint"))
        self.save("claude", claude_call(1, "denied", "PreToolUse:Bash hook error: synthetic constraint",
                                      error=True), agent="claude-code")
        self.save("timeout", claude_call(1, "timeout",
                  "PreToolUse hook did not respond before its timeout while waiting", error=True),
                  agent="claude-code")
        group = self.report(salt=None)["hook_denials"]["summary"]
        self.assertEqual(len(group["top_reason_ids"]), 1)
        self.assertEqual(group["top_reason_ids"][0]["count"], 2)
        self.assertEqual((group["sessions_with_denials"], group["timeouts"]), (2, 1))

    def test_native_calls_without_text_inputs_count_toward_recovery_and_prior_success(self):
        def native(second, identity, phase):
            return {"type": "event_msg", "timestamp": at(second), "payload": {
                "type": "mcp_tool_call_" + phase, "call_id": identity, "output": "completed"}}
        records = [native(1, "prior", "begin"), native(2, "prior", "end")]
        records += codex_call(3, "denied", BLOCKED + "synthetic")
        for index in range(3):
            records += [native(5 + index * 2, "native-" + str(index), "begin"),
                        native(6 + index * 2, "native-" + str(index), "end")]
        records += codex_call(11, "late", "completed")
        self.save("codex", records)
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual(group["first_call_denials"]["sessions"], 0)
        self.assertEqual((group["recovered"], group["unrecovered"], group["tool_calls"]), (0, 1, 6))

    def test_claude_result_block_order_preserves_prior_success(self):
        one = claude_call(1, "first", "completed")
        two = claude_call(1, "second", "PreToolUse:Bash hook error: synthetic", error=True)
        starts = one[0]
        starts["message"]["content"] += two[0]["message"]["content"]
        results = one[1]
        results["message"]["content"] += two[1]["message"]["content"]
        self.save("claude", [starts, results], agent="claude-code")
        group = self.report()["hook_denials"]["summary"]
        self.assertEqual(group["denials"], 1)
        self.assertEqual(group["first_call_denials"]["sessions"], 0)

    def test_collect_cli_emits_json_and_text_without_reason_text(self):
        self.save("codex", codex_call(1, "denied", BLOCKED + "synthetic private reason"))
        key = self.root / "key"
        key.write_bytes(b"synthetic")
        result = subprocess.run([sys.executable, "-m", "sumbi", "collect", "--home", str(self.home),
            "--since", "2030-01-01T00:00:00Z", "--until", "2030-01-02T00:00:00Z",
            "--salt-file", str(key), "--json", "-"], capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["hook_denials"]["summary"]["denials"], 1)
        self.assertIn("Hook denials all", result.stderr)
        self.assertNotIn("synthetic private reason", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
