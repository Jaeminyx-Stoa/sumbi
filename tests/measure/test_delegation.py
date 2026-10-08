"""Authored native log shapes for the delegation measurement contract."""

import json
from pathlib import Path
import subprocess
import unittest

from support import IsolatedTemporaryDirectory
from sumbi.core.time import Window
from sumbi.core.values import timestamp
from sumbi.measure.attribution import ProjectRule
from sumbi.measure.report import collect, text_summary


WINDOW = Window(timestamp("2030-01-01T00:00:00Z"), timestamp("2030-01-02T00:00:00Z"))


def at(minute):
    return f"2030-01-01T00:{minute:02d}:00Z"


class DelegationTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory(prefix="sumbi-delegation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(self.checkout)], check=True, capture_output=True)
        self.cwd = str(self.checkout)

    def save(self, relative, records):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    def claude(self, minute, identity, context, parent="parent", agent=None, output=10,
               model="model-a", blocks=None):
        record = {"type": "assistant", "timestamp": at(minute), "sessionId": parent,
                  "cwd": self.cwd, "message": {"id": identity, "model": model,
                  "usage": {"input_tokens": context - 30, "cache_creation_input_tokens": 10,
                            "cache_read_input_tokens": 20, "output_tokens": output},
                  "content": blocks or []}}
        if agent:
            record["agentId"] = agent
        return record

    def child(self, name, records, sidecar=None, parent="parent"):
        directory = parent.replace(":subagent:", "/subagents/agent-")
        path = self.save(f".claude/projects/group/{directory}/subagents/agent-{name}.jsonl", records)
        if sidecar is not None:
            path.with_suffix(".meta.json").write_text(json.dumps(sidecar), encoding="utf-8")
        return path

    def codex_meta(self, name, minute=0, parent=None):
        payload = {"id": name, "cwd": self.cwd, "source": "cli"}
        if parent:
            payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        return {"type": "session_meta", "timestamp": at(minute), "payload": payload}

    def codex_usage(self, minute, total, last=None):
        info = {"total_token_usage": {"input_tokens": total, "cached_input_tokens": 20,
                                     "output_tokens": 10, "reasoning_output_tokens": 3}}
        if last is not None:
            info["last_token_usage"] = {"input_tokens": last, "cached_input_tokens": 20,
                                       "cache_write_input_tokens": 999, "output_tokens": 10,
                                       "reasoning_output_tokens": 3, "total_tokens": last + 10}
        return {"type": "event_msg", "timestamp": at(minute),
                "payload": {"type": "token_count", "info": info}}

    def report(self, window=WINDOW, **kwargs):
        return collect(self.home, window, salt=b"synthetic", **kwargs)[0]

    def summary(self):
        return self.report()["delegation"]["summary"]

    def test_claude_foreground_background_nested_missing_and_model_sidecars(self):
        dispatches = [{"type": "tool_use", "id": "foreground", "name": "Agent",
                       "input": {"model": "sonnet"}},
                      {"type": "tool_use", "id": "background", "name": "Agent",
                       "input": {"run_in_background": True}}]
        self.save(".claude/projects/group/parent.jsonl", [
            self.claude(0, "parent-request", 100_000, blocks=dispatches)])
        first = self.claude(1, "first", 200_000, agent="foreground")
        repeat = {**first, "message": {**first["message"], "content": [{"type": "text", "text": "synthetic"}]}}
        self.child("foreground", [first, repeat,
            self.claude(3, "second", 500_000, agent="foreground")], {"model": "sonnet"})
        self.child("background", [self.claude(2, "background", 50_000, agent="background")], {})
        self.child("unknown", [self.claude(4, "unknown", 100_000, agent="unknown")])
        self.child("orphan", [self.claude(4, "orphan", 30_000, parent="missing", agent="orphan")], parent="missing")
        self.child("nested", [self.claude(2, "nested", 400_000, parent="parent:subagent:foreground", agent="nested")],
                   parent="parent:subagent:foreground")
        group = self.summary()
        self.assertEqual(group["volume"], {"dispatched_sessions": 5, "dispatching_parent_sessions": 2,
                                          "parent_unobserved": 1, "nesting_depth": {"1": 3, "2": 1, "unknown": 1}})
        self.assertEqual(group["cost_share"]["dispatched"]["total"], 1_280_060)
        self.assertEqual(group["cost_share"]["parents"]["total"], 800_030)
        self.assertAlmostEqual(group["cost_share"]["dispatched_share"]["total"], 1_280_060 / 1_380_070)
        context = group["context"]
        self.assertEqual(context["session_average_median"], 100_000)
        self.assertEqual(context["session_average_p90"], 400_000)
        self.assertEqual(context["sensitivity"]["100000"]["total_tokens"], 1_100_030)
        self.assertEqual(context["sensitivity"]["200000"]["total_tokens"], 900_020)
        self.assertEqual(context["sensitivity"]["400000"]["total_tokens"], 500_010)
        self.assertEqual(group["concentration"]["top_decile_sessions"], 1)
        self.assertEqual(group["concentration"]["top_decile_total_tokens"], 700_020)
        self.assertEqual(group["model_request"]["by_request"]["requested"], {"sessions": 1, "total_tokens": 700_020})
        self.assertEqual(group["model_request"]["by_request"]["unrequested"], {"sessions": 1, "total_tokens": 50_010})
        estimate = group["estimate"]
        self.assertEqual(estimate["observed_sessions"], 4)
        self.assertEqual(estimate["context_unobserved"], 1)
        self.assertEqual(estimate["sessions_ratio_below_one"], 2)
        self.assertEqual(estimate["share_ratio_below_one"], .5)
        self.assertEqual(estimate["median_inline_to_actual_context_ratio"], (5/7 + 1)/2)

    def test_codex_per_turn_context_keeps_existing_token_kinds_and_deduplicates(self):
        self.save(".codex/sessions/rollout-parent.jsonl", [self.codex_meta("parent"),
            self.codex_usage(0, 80_000, 80_000)])
        event = self.codex_usage(2, 150_000, 150_000)
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 1, "parent"),
            event, event, self.codex_usage(3, 400_000, 250_000)])
        group = self.summary()
        self.assertEqual(group["volume"]["dispatched_sessions"], 1)
        self.assertEqual(group["cost_share"]["dispatched"], {"new_input": 399_980, "cache_read": 20,
            "cache_write": None, "output": 10, "total": 400_010})
        self.assertEqual(group["context"]["session_average_median"], 200_000)
        self.assertEqual(group["context"]["request_total_tokens"], 400_020)
        self.assertAlmostEqual(group["estimate"]["median_inline_to_actual_context_ratio"], .65)
        self.assertEqual(group["model_request"]["by_request"]["unknown"]["sessions"], 1)

    def test_cumulative_only_and_partial_context_are_unobserved(self):
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 1, "missing"),
            self.codex_usage(2, 150_000)])
        group = self.summary()
        self.assertEqual(group["context"]["context_unobserved"], 1)
        self.assertIsNone(group["context"]["session_average_median"])
        self.assertIsNone(group["context"]["sensitivity"]["100000"]["share"])
        self.assertEqual(group["estimate"]["context_unobserved"], 1)

    def test_edits_inside_outside_scripts_and_tool_failures_emit_only_counts(self):
        outside = str(self.root / "scratch" / "helper.py")
        blocks = [{"type": "tool_use", "id": "write", "name": "Write", "input": {"file_path": outside}}]
        self.child("outside", [self.claude(1, "outside", 50_000, agent="outside", blocks=blocks),
            {"type": "user", "timestamp": at(2), "sessionId": "parent", "agentId": "outside",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "write", "is_error": True}]}}])
        changes = {str(self.checkout / "module.py"): {}, str(self.root / "scratch" / "helper.sh"): {},
                   str(self.root / "scratch" / "note.txt"): {}}
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 1, "parent"),
            {"type": "event_msg", "timestamp": at(2), "payload": {"type": "item_completed",
             "item": {"id": "change", "type": "FileChange", "changes": changes}}}])
        report = self.report()
        friction = report["delegation"]["summary"]["workaround_friction"]
        self.assertEqual(friction["sessions_all_edits_outside_checkout"], 1)
        self.assertEqual(friction["edits_outside_checkout"], 3)
        self.assertEqual(friction["script_edits_outside_checkout"], 2)
        self.assertEqual(friction["tool_errors"], 1)
        self.assertEqual(friction["tool_results"], 2)
        self.assertEqual(friction["tool_failure_rate"], .5)
        output = json.dumps(report) + text_summary(report)
        for private in (outside, "helper.py", "helper.sh", "note.txt", self.cwd):
            self.assertNotIn(private, output)

    def test_empty_window_has_counts_and_null_ratios(self):
        self.child("child", [self.claude(1, "child", 50_000, agent="child")])
        empty = Window(timestamp("2031-01-01T00:00:00Z"), timestamp("2031-01-02T00:00:00Z"))
        self.assertEqual(self.report(empty)["delegation"]["summary"]["volume"]["dispatched_sessions"], 0)
        # Also pin the shape for an empty source home.
        self.home = self.root / "empty-home"
        group = self.summary()
        self.assertEqual(group["volume"]["dispatched_sessions"], 0)
        self.assertEqual(group["cost_share"]["dispatched"]["total"], 0)
        self.assertIsNone(group["cost_share"]["dispatched_share"]["total"])
        self.assertEqual(group["concentration"]["top_decile_sessions"], 0)
        self.assertIsNone(group["estimate"]["median_inline_to_actual_context_ratio"])
        self.assertIn("Delegation all", text_summary(self.report()))

    def test_parent_outside_window_can_supply_context_but_not_parent_tokens(self):
        parent = self.claude(0, "parent", 50_000)
        parent["timestamp"] = "2029-12-31T23:59:00Z"
        self.save(".claude/projects/group/parent.jsonl", [parent])
        self.child("child", [self.claude(1, "child", 100_000, agent="child")])
        group = self.summary()
        self.assertEqual(group["volume"]["parent_unobserved"], 1)
        self.assertEqual(group["cost_share"]["parents"]["total"], 0)
        self.assertEqual(group["estimate"]["median_inline_to_actual_context_ratio"], .5)

    def test_unreadable_sidecar_does_not_read_or_emit_description(self):
        path = self.child("child", [self.claude(1, "child", 50_000, agent="child")])
        path.with_suffix(".meta.json").write_text("invalid", encoding="utf-8")
        self.assertEqual(self.summary()["model_request"]["by_request"]["unknown"]["sessions"], 1)
        path.with_suffix(".meta.json").write_text(json.dumps({"description": "synthetic-private-canary",
            "agentType": "synthetic-custom-agent", "model": "sonnet"}), encoding="utf-8")
        report = self.report()
        self.assertEqual(report["delegation"]["summary"]["model_request"]["by_request"]["requested"]["sessions"], 1)
        self.assertNotIn("synthetic-private-canary", json.dumps(report))
        self.assertNotIn("synthetic-custom-agent", json.dumps(report))

    def test_project_breakdowns_use_billable_event_attribution(self):
        other = self.root / "other-checkout"
        other.mkdir()
        first = self.claude(1, "one", 50_000, agent="child")
        second = self.claude(2, "two", 250_000, agent="child")
        second["cwd"] = str(other)
        self.child("child", [first, second])
        report = self.report(rules=[ProjectRule("sample", paths=[self.cwd]),
                                   ProjectRule("other", paths=[str(other)])])
        groups = {g["rule"]: g for g in report["delegation"]["by_project_and_agent"]}
        self.assertEqual(groups["sample"]["cost_share"]["dispatched"]["total"], 50_010)
        self.assertEqual(groups["other"]["cost_share"]["dispatched"]["total"], 250_010)
        self.assertEqual(groups["other"]["context"]["session_average_median"], 250_000)
        self.assertEqual(report["delegation"]["summary"]["cost_share"]["dispatched"]["total"], 300_020)

    def test_parent_in_another_project_remains_observed_without_extra_child_counts(self):
        other = self.root / "other-checkout"
        other.mkdir()
        parent = self.claude(0, "parent-request", 200_000, agent="dispatcher")
        self.child("dispatcher", [parent])
        child = self.claude(1, "child-request", 50_000,
                            parent="parent:subagent:dispatcher", agent="child")
        child["cwd"] = str(other)
        self.child("child", [child], parent="parent:subagent:dispatcher")
        report = self.report(rules=[ProjectRule("sample", paths=[self.cwd]),
                                   ProjectRule("other", paths=[str(other)])])
        group = next(g for g in report["delegation"]["by_project_and_agent"] if g["rule"] == "other")
        self.assertEqual(group["volume"]["dispatched_sessions"], 1)
        self.assertEqual(group["volume"]["dispatching_parent_sessions"], 1)
        self.assertEqual(group["volume"]["parent_unobserved"], 0)
        self.assertEqual(group["cost_share"]["parents"]["total"], 0)

    def test_nested_directory_can_identify_parent_from_root_session_id(self):
        self.save(".claude/projects/group/parent.jsonl", [self.claude(0, "parent", 50_000)])
        self.child("dispatcher", [self.claude(1, "dispatcher", 50_000, agent="dispatcher")])
        self.child("child", [self.claude(2, "child", 50_000, agent="child")],
                   parent="parent:subagent:dispatcher")
        self.assertEqual(self.summary()["volume"]["nesting_depth"], {"1": 1, "2": 1})

    def test_later_unobserved_parent_usage_prevents_stale_context_estimate(self):
        self.save(".codex/sessions/rollout-parent.jsonl", [self.codex_meta("parent"),
            self.codex_usage(0, 50_000, 50_000), self.codex_usage(1, 200_000)])
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 2, "parent"),
            self.codex_usage(3, 100_000, 100_000)])
        group = self.summary()
        self.assertEqual(group["context"]["observed_sessions"], 1)
        self.assertEqual(group["estimate"]["context_unobserved"], 1)
        self.assertIsNone(group["estimate"]["median_inline_to_actual_context_ratio"])

    def test_unobserved_first_child_context_prevents_estimate_in_later_window(self):
        self.save(".codex/sessions/rollout-parent.jsonl", [self.codex_meta("parent"),
            self.codex_usage(0, 50_000, 50_000)])
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 1, "parent"),
            self.codex_usage(2, 100_000), self.codex_usage(4, 200_000, 100_000)])
        group = self.report(Window(timestamp(at(3)), WINDOW.until))["delegation"]["summary"]
        self.assertEqual(group["context"]["observed_sessions"], 1)
        self.assertEqual(group["estimate"]["context_unobserved"], 1)

    def test_all_script_extensions_and_windows_path_boundaries(self):
        self.cwd = "R:/fixture/checkout/subdirectory"
        targets = {"R:/fixture/checkout/subdirectory/inside.py": {},
                   "R:/fixture/checkout/subdirectory-extra/outside.txt": {}}
        for extension in ("sh", "bash", "py", "ps1", "js", "mjs", "cmd", "bat"):
            targets[f"R:/fixture/scratch/helper.{extension.upper()}"] = {}
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 1, "missing"),
            {"type": "event_msg", "timestamp": at(2), "payload": {"type": "item_completed",
             "item": {"id": "change", "type": "FileChange", "changes": targets}}}])
        friction = self.summary()["workaround_friction"]
        self.assertEqual(friction["edits_outside_checkout"], 9)
        self.assertEqual(friction["script_edits_outside_checkout"], 8)
        self.assertEqual(friction["sessions_all_edits_outside_checkout"], 0)

    def test_background_result_can_arrive_after_child_spend(self):
        self.save(".claude/projects/group/parent.jsonl", [self.claude(0, "dispatch", 50_000,
            blocks=[{"type": "tool_use", "id": "dispatch", "name": "Agent",
                     "input": {"run_in_background": True}}]),
            {"type": "user", "timestamp": at(5), "sessionId": "parent", "cwd": self.cwd,
             "message": {"content": [{"type": "tool_result", "tool_use_id": "dispatch"}]}}])
        self.child("background", [self.claude(1, "child", 100_000, agent="background")], {})
        self.assertEqual(self.summary()["volume"]["dispatching_parent_sessions"], 1)
        self.assertEqual(self.summary()["estimate"]["median_inline_to_actual_context_ratio"], .5)

    def test_concentration_rounds_up_decile(self):
        self.save(".claude/projects/group/parent.jsonl", [self.claude(0, "parent", 50_000)])
        for index in range(11):
            self.child(str(index), [self.claude(1, str(index), 50_000 + index * 1000, agent=str(index))])
        group = self.summary()
        self.assertEqual(group["concentration"]["top_decile_sessions"], 2)
        self.assertEqual(group["concentration"]["top_decile_total_tokens"], 119_020)
        self.assertAlmostEqual(group["concentration"]["top_decile_share"], 119_020 / 605_110)

    def test_partly_overlapping_child_uses_only_in_window_request_tokens(self):
        self.save(".claude/projects/group/parent.jsonl", [self.claude(0, "parent", 200_000)])
        self.child("child", [self.claude(1, "first", 50_000, agent="child"),
            self.claude(3, "second", 100_000, agent="child"),
            self.claude(5, "third", 500_000, agent="child")])
        group = self.report(Window(timestamp(at(2)), timestamp(at(5))))["delegation"]["summary"]
        self.assertEqual(group["cost_share"]["dispatched"]["total"], 100_010)
        self.assertEqual(group["context"]["session_average_median"], 100_000)
        self.assertEqual(group["estimate"]["median_inline_to_actual_context_ratio"], 2.5)

    def test_model_tokens_split_and_session_count_can_appear_in_several_rows(self):
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 0, "missing"),
            {"type": "turn_context", "timestamp": at(1), "payload": {"cwd": self.cwd, "model": "model-a"}},
            self.codex_usage(2, 100_000, 100_000),
            {"type": "turn_context", "timestamp": at(3), "payload": {"cwd": self.cwd, "model": "model-b"}},
            self.codex_usage(4, 200_000, 100_000)])
        model = self.summary()["model_request"]
        rows = {row["model"]: row for row in model["by_observed_model_and_request"]}
        self.assertEqual(rows["model-a"]["total_tokens"], 100_010)
        self.assertEqual(rows["model-b"]["total_tokens"], 100_000)
        self.assertEqual(sum(row["sessions"] for row in rows.values()), 2)
        self.assertEqual(model["by_request"]["unknown"]["sessions"], 1)

    def test_child_with_model_but_no_usage_keeps_observed_model_count(self):
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 0, "missing"),
            {"type": "turn_context", "timestamp": at(1), "payload": {"cwd": self.cwd, "model": "model-a"}}])
        rows = self.summary()["model_request"]["by_observed_model_and_request"]
        self.assertEqual(rows, [{"model": "model-a", "request": "unknown", "sessions": 1, "total_tokens": 0}])

    def test_zero_context_is_observed_but_estimate_ratio_is_undefined(self):
        self.save(".claude/projects/group/parent.jsonl", [self.claude(0, "parent", 50_000)])
        child = self.claude(1, "child", 50_000, agent="child")
        child["message"]["usage"] = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": 0, "output_tokens": 10}
        self.child("child", [child])
        group = self.summary()
        self.assertEqual(group["context"]["session_average_median"], 0)
        self.assertEqual(group["context"]["sensitivity"]["100000"]["share"], 0)
        self.assertEqual(group["estimate"]["context_unobserved"], 1)

    def test_partial_context_excludes_entire_child_from_context_figures(self):
        self.save(".codex/sessions/rollout-child.jsonl", [self.codex_meta("child", 0, "missing"),
            self.codex_usage(1, 100_000, 100_000), self.codex_usage(2, 200_000)])
        group = self.summary()
        self.assertEqual(group["context"]["context_unobserved"], 1)
        self.assertEqual(group["context"]["request_total_tokens"], 0)
        self.assertIsNone(group["context"]["session_average_p90"])

    def test_unknown_edit_target_and_no_edits_do_not_count_as_all_outside(self):
        self.child("report", [self.claude(1, "report", 50_000, agent="report")])
        self.child("unknown", [self.claude(1, "unknown", 50_000, agent="unknown",
            blocks=[{"type": "tool_use", "id": "edit", "name": "Edit", "input": {}}])])
        group = self.summary()
        self.assertEqual(group["workaround_friction"]["sessions_all_edits_outside_checkout"], 0)
        self.assertEqual(group["workaround_friction"]["edit_targets_unobserved"], 1)
        self.assertIsNone(group["workaround_friction"]["tool_failure_rate"])
