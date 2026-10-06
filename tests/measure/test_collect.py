"""Hand-computed synthetic truths for the measurement core."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
from support import IsolatedTemporaryDirectory
import unittest
from unittest.mock import patch

from sumbi.events.adapters import claude_code, codex
from sumbi.cli import main, parser
from sumbi.measure.attribution import Attributor, ProjectRule
from sumbi.core.records import Coverage
from sumbi.sessions.session import Session
from sumbi.sessions.builder import collect as collect_sessions
from sumbi.core.time import Window
from sumbi.core.paths import normalize_origin, normalize_path
from sumbi.core.privacy import pseudonym
from sumbi.core.values import timestamp
from sumbi.measure.report import collect, text_summary

FIXTURES = Path(__file__).parents[1] / "fixtures"
WINDOW = Window(timestamp("2030-01-01T00:00:00Z"), timestamp("2030-01-01T00:10:00Z"))


class SyntheticHome(unittest.TestCase):
    def setUp(self):
        self.temporary = IsolatedTemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.one = self.home / "one"
        self.two = self.home / "two"
        self.one.mkdir()
        self.two.mkdir()
        self.rules = [ProjectRule("sample", paths=[str(self.one)])]

    def write(self, agent, name, events):
        root = self.home / (".claude/projects/group" if agent == "claude-code" else ".codex/sessions/group")
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = events if isinstance(events, str) else "\n".join(json.dumps(e) for e in events) + "\n"
        path.write_text(data, encoding="utf-8")
        return path

    def fixture(self, agent, source, name):
        data = (FIXTURES / source).read_text(encoding="utf-8")
        data = data.replace("fixture-one", json.dumps(str(self.one))[1:-1])
        return self.write(agent, name, data)

    def install(self):
        self.fixture("claude-code", "claude/main.jsonl", "session-c.jsonl")
        self.fixture("claude-code", "claude/child.jsonl", "session-c/subagents/child-one.jsonl")
        self.fixture("codex", "codex/main.jsonl", "rollout-main.jsonl")
        self.fixture("codex", "codex/resumed.jsonl", "rollout-resume.jsonl")

    def report(self, **kwargs):
        return collect(self.home, WINDOW, rules=self.rules, **kwargs)[0]

    def claude_event(self, when, identity="one", usage=None, **kwargs):
        return {"type": "assistant", "uuid": identity, "sessionId": "session-c", "timestamp": when,
                "cwd": str(self.one), "message": {"id": identity, "model": "model-a", "content": [],
                                                    "usage": usage or {"input_tokens": 1, "output_tokens": 2}}, **kwargs}

    def meta(self, cwd=None, identity="session-x"):
        return {"type": "session_meta", "timestamp": "2029-12-31T23:00:00Z",
                "payload": {"id": identity, "cwd": str(cwd or self.one), "cli_version": "0.1.0"}}

    def token(self, when, inputs, cached, output, reasoning=0, **extra):
        return {"type": "event_msg", "timestamp": when, "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": inputs, "cached_input_tokens": cached,
                                  "output_tokens": output, "reasoning_output_tokens": reasoning, **extra}}}}


class AccountingTests(SyntheticHome):
    def setUp(self):
        super().setUp()
        self.install()

    def test_claude_maximal_stream_snapshot_and_missing_reasoning(self):
        sessions = self.report()["sessions"]
        parent = next(s for s in sessions if s["agent"] == "claude-code" and s["parent_id"] is None)
        self.assertEqual({k: parent["tokens"][k] for k in ("new_input", "cache_write", "cache_read", "output", "total")},
                         {"new_input": 13, "cache_write": 25, "cache_read": 37, "output": 14, "total": 89})
        self.assertIsNone(parent["tokens"]["reasoning_output"])
        self.assertEqual(parent["models"], ["model-a", "model-b"])

    def test_subagent_is_separate_and_linked_to_parent_session(self):
        sessions = [s for s in self.report()["sessions"] if s["agent"] == "claude-code"]
        child = next(s for s in sessions if s["parent_id"])
        parent = next(s for s in sessions if not s["parent_id"])
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertNotEqual(child["id"], parent["id"])
        self.assertEqual(child["tokens"]["total"], 6)
        self.assertEqual(child["tokens"]["cache_write"], 0)
        self.assertEqual(child["cli_versions"], ["2.0.1"])

    def test_codex_cumulative_reset_resume_and_reasoning_subset(self):
        session = next(s for s in self.report()["sessions"] if s["agent"] == "codex")
        self.assertEqual({k: session["tokens"][k] for k in ("new_input", "cache_read", "output", "reasoning_output", "total")},
                         {"new_input": 65, "cache_read": 65, "output": 38, "reasoning_output": 12, "total": 168})
        self.assertIsNone(session["tokens"]["cache_write"])
        self.assertEqual(session["counts"]["counter_resets"], 1)
        self.assertEqual(session["models"], ["model-a", "model-b"])
        self.assertEqual(session["efforts"], ["high", "low", "medium"])
        self.assertEqual(self.report()["coverage"]["adapters"]["codex"]["sessions_read"], 1)

    def test_counts_are_deduplicated_across_representations(self):
        agents = self.report()["by_agent"]
        for agent in agents:
            self.assertEqual(agents[agent]["counts"]["tool_calls"], 3)
            self.assertEqual(agents[agent]["counts"]["tool_results"], 3)
            self.assertEqual(agents[agent]["counts"]["compactions"], 1)
            self.assertEqual(agents[agent]["counts"]["user_input_requests"], 1)
        self.assertEqual(agents["claude-code"]["counts"]["tool_errors"], 2)
        self.assertEqual(agents["claude-code"]["counts"]["api_errors"], 1)
        self.assertEqual(agents["codex"]["counts"]["tool_errors"], 1)

    def test_time_has_observed_pairs_and_estimated_sensitivity(self):
        sessions = self.report()["sessions"]
        for session in (s for s in sessions if not s["parent_id"]):
            self.assertEqual(session["time"]["wall_span"], {"measurement": "observed", "seconds": 600.0})
            self.assertEqual(session["time"]["active"]["measurement"], "estimated")
            self.assertEqual(session["time"]["active"]["sensitivity_seconds"], {"2": 600.0, "5": 600.0, "10": 600.0})
            tool = session["time"]["durations"]["tool"]
            self.assertEqual(tool["measurement"], "observed")
            self.assertEqual(tool["paired_intervals"], 3)
            self.assertEqual(tool["seconds"], 300 if session["agent"] == "claude-code" else 180)
        codex_session = next(s for s in sessions if s["agent"] == "codex")
        self.assertEqual(codex_session["time"]["durations"]["request"]["seconds"], 120)

    def test_coverage_exposes_broken_duplicate_unknown_and_read_counts(self):
        coverage = self.report()["coverage"]
        self.assertEqual(coverage["sessions_in_window"], 3)
        c = coverage["adapters"]["claude-code"]
        x = coverage["adapters"]["codex"]
        self.assertEqual((c["files_scanned"], c["lines_read"], c["broken_lines"], c["duplicate_events"]), (2, 18, 1, 1))
        self.assertEqual((x["files_scanned"], x["lines_read"], x["broken_lines"], x["duplicate_events"]), (2, 25, 0, 3))
        self.assertEqual(c["unknown_record_types"], {"fixture_record": 1})
        self.assertEqual(x["unknown_record_types"], {"fixture_record": 1})

    def test_spend_shares_and_partial_field_coverage(self):
        report = self.report()
        self.assertEqual(report["summary"]["tokens"]["total"], 263)
        self.assertEqual(report["summary"]["tokens"]["cache_write"], 25)
        self.assertEqual(report["summary"]["tokens"]["not_reported_sessions"]["cache_write"], 1)
        self.assertEqual(report["summary"]["tokens"]["not_reported_sessions"]["reasoning_output"], 2)
        project = next(r for r in report["coverage"]["spend"] if r["bucket"] == "project")
        self.assertEqual((project["tokens"], project["share"]), (263, 1.0))
        self.assertEqual(report["coverage"]["unattributed_share"], 0)

    def test_privacy_and_optional_local_friction(self):
        report, review = collect(self.home, WINDOW, rules=self.rules, local_review=True)
        emitted = json.dumps(report) + text_summary(report)
        for private in (str(self.home), "fixture-one", "Synthetic", "canary", "Harness friction:",
                        "session-c", "session-x", "ExampleTool", "tool-a", "group"):
            self.assertNotIn(private, emitted)
        self.assertIn("Harness friction: synthetic local note", review)
        self.assertEqual(collect(self.home, WINDOW, rules=self.rules)[1], "")


class EdgeTests(SyntheticHome):
    def check_old_layout(self, parent_name):
        self.fixture("claude-code", "claude/main.jsonl", parent_name)
        self.fixture("claude-code", "claude/child.jsonl", "agent-child-one.jsonl")
        sessions = {s.raw_id: s for s in collect_sessions(claude_code, self.home, WINDOW, Coverage())}
        self.assertEqual(set(sessions), {"session-c", "session-c:subagent:child-one"})
        parent, child = sessions["session-c"], sessions["session-c:subagent:child-one"]
        self.assertFalse(parent.is_worker)
        self.assertIsNone(parent.parent_raw_id)
        self.assertTrue(child.is_worker)
        self.assertEqual(child.parent_raw_id, parent.raw_id)
        self.assertEqual(parent.tokens, {"new_input": 13, "cache_write": 25,
                                        "cache_read": 37, "output": 14, "reasoning_output": None})
        self.assertEqual(child.tokens, {"new_input": 1, "cache_write": 0,
                                       "cache_read": 2, "output": 3, "reasoning_output": None})

    def test_old_layout_agent_sorts_before_parent(self):
        self.check_old_layout("z-parent.jsonl")

    def test_old_layout_agent_sorts_after_parent(self):
        self.check_old_layout("a-parent.jsonl")

    def test_claude_records_cannot_change_stream_role(self):
        for name, child in (("parent.jsonl", False), ("parent/subagents/agent-child.jsonl", True)):
            records = [self.claude_event("2030-01-01T00:01:00Z", identity=name + "first", isSidechain=child),
                       self.claude_event("2030-01-01T00:02:00Z", identity=name + "last", isSidechain=not child)]
            self.write("claude-code", name, records)
        sessions = collect_sessions(claude_code, self.home, WINDOW, Coverage())
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            self.assertEqual(session.is_worker, session.parent_raw_id is not None)

    def test_nested_claude_subagent_history_is_scanned_and_deduplicated(self):
        event = self.claude_event("2030-01-01T00:01:00Z", agentId="child-one")
        self.write("claude-code", "session-c/subagents/agent-child-one.jsonl", [event])
        self.write("claude-code", "session-c/subagents/agent-child-one/history/part.jsonl", [event])
        report = self.report()
        self.assertEqual(report["summary"]["sessions"], 1)
        self.assertEqual(report["summary"]["tokens"]["total"], 3)
        self.assertEqual(report["coverage"]["adapters"]["claude-code"]["files_scanned"], 2)
        self.assertEqual(report["coverage"]["adapters"]["claude-code"]["duplicate_events"], 1)

    def test_claude_subagent_records_without_agent_id_keep_file_identity(self):
        first = self.claude_event("2030-01-01T00:01:00Z", agentId="child-one")
        second = {"type": "pr-link", "timestamp": "2030-01-01T00:02:00Z", "sessionId": "session-c"}
        self.write("claude-code", "session-c/subagents/agent-child-one.jsonl", [first, second])
        self.assertEqual(self.report()["summary"]["sessions"], 1)

    def test_codex_child_keeps_first_identity_despite_inherited_parent_meta(self):
        child = self.meta(identity="child-one")
        child["payload"]["session_id"] = "parent-one"
        self.write("codex", "rollout-child.jsonl", [child, self.meta(identity="parent-one"),
            self.token("2030-01-01T00:01:00Z", 20, 5, 2)])
        self.write("codex", "rollout-parent.jsonl", [self.meta(identity="parent-one"),
            self.token("2030-01-01T00:01:30Z", 100, 50, 10)])
        report = self.report()
        self.assertEqual(report["summary"]["sessions"], 2)
        self.assertEqual(report["summary"]["tokens"]["total"], 132)
        self.assertEqual(report["summary"]["counts"]["counter_resets"], 0)
        self.assertEqual(report["coverage"]["adapters"]["codex"]["inherited_session_meta"], 1)
        self.assertEqual({s["id"] for s in report["sessions"]},
                         {pseudonym("session", "codex:child-one"), pseudonym("session", "codex:parent-one")})

    def test_codex_async_user_input_call_is_counted(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            {"type": "response_item", "timestamp": "2030-01-01T00:01:00Z", "payload": {
                "type": "function_call", "call_id": "one", "name": "functions.request_user_input_async"}}])
        self.assertEqual(self.report()["summary"]["counts"]["user_input_requests"], 1)

    def test_empty_home_has_zero_coverage_and_null_unsupported_fields(self):
        report = self.report()
        self.assertEqual(report["summary"]["sessions"], 0)
        self.assertEqual(report["summary"]["tokens"]["total"], 0)
        self.assertIsNone(report["summary"]["tokens"]["cache_write"])
        self.assertEqual(report["coverage"]["unattributed_share"], 0)
        self.assertEqual(report["coverage"]["adapters"]["codex"]["files_scanned"], 0)

    def test_claude_both_window_boundaries(self):
        self.write("claude-code", "main.jsonl", [
            self.claude_event("2029-12-31T23:59:59Z", "before"),
            self.claude_event("2030-01-01T00:00:00Z", "start"),
            self.claude_event("2030-01-01T00:09:59.999Z", "inside"),
            self.claude_event("2030-01-01T00:10:00Z", "end")])
        self.assertEqual(self.report()["summary"]["tokens"]["total"], 6)

    def test_codex_both_boundaries_and_pre_window_baseline(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            self.token("2029-12-31T23:59:59Z", 100, 60, 10),
            self.token("2030-01-01T00:00:00Z", 120, 70, 20),
            self.token("2030-01-01T00:09:59.999Z", 130, 70, 25),
            self.token("2030-01-01T00:10:00Z", 999, 100, 999)])
        self.assertEqual(self.report()["summary"]["tokens"]["total"], 45)

    def test_claude_equal_output_uses_latest_snapshot_window(self):
        events = [self.claude_event("2030-01-01T00:09:00Z", "stream"),
                  self.claude_event("2030-01-01T00:10:00Z", "stream")]
        self.write("claude-code", "main.jsonl", events)
        self.assertEqual(self.report()["summary"]["tokens"]["total"], 0)

    def test_claude_decreasing_stream_output_preserves_maximum(self):
        events = [self.claude_event("2030-01-01T00:01:00Z", "stream", {"input_tokens": 10, "output_tokens": 7}),
                  self.claude_event("2030-01-01T00:02:00Z", "stream", {"input_tokens": 10, "output_tokens": 2})]
        self.write("claude-code", "main.jsonl", events)
        self.assertEqual(self.report()["summary"]["tokens"]["total"], 17)

    def test_codex_multiple_resets_count_new_counter_values(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            self.token("2029-12-31T23:59:00Z", 100, 60, 10),
            self.token("2030-01-01T00:01:00Z", 20, 5, 2),
            self.token("2030-01-01T00:02:00Z", 30, 10, 4),
            self.token("2030-01-01T00:03:00Z", 5, 1, 1)])
        report = self.report()
        self.assertEqual(report["summary"]["tokens"]["total"], 40)
        self.assertEqual(report["summary"]["counts"]["counter_resets"], 2)

    def test_codex_reset_before_window_supplies_correct_new_baseline(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            self.token("2029-12-31T23:58:00Z", 100, 60, 10),
            self.token("2029-12-31T23:59:00Z", 20, 5, 2),
            self.token("2030-01-01T00:01:00Z", 30, 10, 4)])
        report = self.report()
        self.assertEqual(report["summary"]["tokens"]["total"], 12)
        self.assertEqual(report["summary"]["counts"]["counter_resets"], 0)

    def test_codex_repeated_totals_at_distinct_timestamps_are_zero_cost(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            self.token("2030-01-01T00:01:00Z", 20, 5, 2),
            self.token("2030-01-01T00:02:00Z", 20, 5, 2)])
        self.assertEqual(self.report()["summary"]["tokens"]["total"], 22)

    def test_codex_unknown_cache_write_stays_null_even_if_counter_present(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            self.token("2030-01-01T00:01:00Z", 20, 5, 2, cache_write_input_tokens=99)])
        self.assertIsNone(self.report()["sessions"][0]["tokens"]["cache_write"])

    def test_missing_and_invalid_usage_are_exposed(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            self.token("2030-01-01T00:01:00Z", 1, 2, 3),
            self.token("2030-01-01T00:02:00Z", -1, 0, 3),
            self.token("2030-01-01T00:03:00Z", 10, 2, 3)])
        report = self.report()
        self.assertEqual(report["coverage"]["adapters"]["codex"]["invalid_token_records"], 2)
        self.assertEqual(report["summary"]["tokens"]["total"], 13)

    def test_inconsistent_cached_delta_is_exposed_instead_of_clamped(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            self.token("2029-12-31T23:59:00Z", 100, 10, 3),
            self.token("2030-01-01T00:01:00Z", 110, 40, 4)])
        self.assertEqual(self.report()["coverage"]["adapters"]["codex"]["invalid_token_records"], 1)
        self.assertIsNone(self.report()["summary"]["tokens"]["new_input"])

    def test_broken_json_and_invalid_utf8_do_not_stop_scan(self):
        path = self.write("codex", "rollout-one.jsonl", [self.meta()])
        with path.open("ab") as stream:
            stream.write(b'{broken\n\xff\n[]\n')
            stream.write(json.dumps(self.token("2030-01-01T00:01:00Z", 10, 5, 2)).encode() + b"\n")
        report = self.report()
        self.assertEqual(report["summary"]["tokens"]["total"], 12)
        self.assertEqual(report["coverage"]["adapters"]["codex"]["broken_lines"], 3)

    def test_model_after_first_four_hundred_lines_and_version(self):
        events = [self.meta()] + [{"type": "world_state", "timestamp": "2030-01-01T00:00:00Z", "payload": {"index": i}}
                                 for i in range(405)]
        events += [{"type": "turn_context", "timestamp": "2030-01-01T00:01:00Z",
                    "payload": {"model": "model-late", "effort": "high", "cwd": str(self.one)}},
                   self.token("2030-01-01T00:02:00Z", 10, 5, 2)]
        self.write("codex", "rollout-one.jsonl", events)
        session = self.report()["sessions"][0]
        self.assertEqual(session["models"], ["model-late"])
        self.assertEqual(session["cli_versions"], ["0.1.0"])

    def test_active_gap_threshold_sensitivity_and_custom_threshold(self):
        self.write("claude-code", "main.jsonl", [self.claude_event(t, str(i)) for i, t in enumerate(
            ["2030-01-01T00:00:00Z", "2030-01-01T00:01:00Z", "2030-01-01T00:04:00Z", "2030-01-01T00:10:00Z"])])
        time = self.report(idle_minutes=4)["sessions"][0]["time"]
        self.assertEqual(time["active"]["sensitivity_seconds"], {"2": 60.0, "4": 240.0, "5": 240.0, "10": 600.0})
        self.assertEqual(time["active"]["seconds"], 240)
        self.assertEqual(time["durations"]["tool"]["seconds"], 0)

    def test_only_duration_value_without_pair_is_not_observed(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            {"type": "event_msg", "timestamp": "2030-01-01T00:01:00Z",
             "payload": {"type": "item_completed", "item": {"id": "tool", "type": "CommandExecution", "duration": 900, "exit_code": 1}}}])
        self.assertEqual(self.report()["sessions"][0]["time"]["durations"]["tool"]["seconds"], 0)

    def test_zero_duration_pair_and_missing_endpoints_are_exposed(self):
        session = Session("codex", "one")
        session.interval("tool", "zero", WINDOW.since, WINDOW.since)
        session.interval("tool", "unfinished", WINDOW.since, None)
        session.interval("tool", "missing-start", None, WINDOW.since)
        duration = session.as_dict(WINDOW, Attributor([]), 5)["time"]["durations"]["tool"]
        self.assertEqual(duration, {"measurement": "observed", "seconds": 0.0, "paired_intervals": 1,
                                   "unpaired_starts": 1, "unpaired_ends": 1})

    def test_resumed_pairs_are_order_independent(self):
        session = Session("codex", "one")
        early = timestamp("2030-01-01T00:01:00Z")
        late = timestamp("2030-01-01T00:04:00Z")
        session.interval("tool", "one", late, late)
        session.interval("tool", "one", early, early)
        self.assertEqual(session.as_dict(WINDOW, Attributor([]), 5)["time"]["durations"]["tool"]["seconds"], 180)

    def test_summary_time_sensitivity_matches_session_sum(self):
        self.install()
        time = self.report(idle_minutes=4)["summary"]["time"]
        self.assertEqual(time["active_sensitivity_seconds"],
                         {"measurement": "estimated", "values": {"2": 1200.0, "4": 1200.0, "5": 1200.0, "10": 1200.0}})
        self.assertEqual(time["durations"]["tool"]["seconds"], 480)
        self.assertEqual(time["durations"]["request"]["seconds"], 120)

    def test_pair_spanning_window_is_included_without_in_window_event(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            {"type": "event_msg", "timestamp": "2030-01-01T00:11:00Z",
             "payload": {"type": "task_complete", "turn_id": "one", "started_at": 1893455940, "completed_at": 1893456660}}])
        report = self.report()
        self.assertEqual(report["summary"]["sessions"], 1)
        self.assertEqual(report["sessions"][0]["time"]["durations"]["request"]["seconds"], 600)

    def test_unknown_nested_types_and_invalid_timestamp_are_covered(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            {"type": "event_msg", "timestamp": "invalid", "payload": {"type": "new_event"}},
            {"type": "response_item", "timestamp": "2030-01-01T00:01:00Z", "payload": {"type": "new_response"}}])
        coverage = self.report()["coverage"]["adapters"]["codex"]
        self.assertEqual(coverage["invalid_timestamps"], 1)
        self.assertEqual(coverage["unknown_record_types"], {"event_msg:new_event": 1, "response_item:new_response": 1})

    def test_free_text_identifier_is_hashed(self):
        event = self.claude_event("2030-01-01T00:01:00Z", sessionId="Synthetic private identifier")
        event["message"]["model"] = "Synthetic model canary with spaces"
        self.write("claude-code", "main.jsonl", [event])
        emitted = json.dumps(self.report())
        self.assertNotIn("Synthetic", emitted)

    def test_escaped_lone_surrogate_in_identifier_does_not_abort_collection(self):
        self.write("claude-code", "main.jsonl", [self.claude_event("2030-01-01T00:01:00Z", sessionId="\ud800")])
        self.assertEqual(self.report()["summary"]["tokens"]["total"], 3)


class AttributionTests(SyntheticHome):
    def test_https_ssh_origin_equivalence_and_credentials_removed(self):
        self.assertEqual(normalize_origin("https://credential@example.test/sample/repo.git"), "example.test/sample/repo")
        self.assertEqual(normalize_origin("git@example.test:sample/repo.git"), "example.test/sample/repo")
        self.assertEqual(normalize_origin("ssh://git@example.test:22/sample/repo.git"), "example.test/sample/repo")

    def test_origin_rule_wins_and_records_candidate_evidence(self):
        attributor = Attributor([ProjectRule("sample", origins=["https://example.test/sample/repo.git"], paths=["*"])])
        with patch.object(attributor, "origin", return_value=("example.test/sample/repo", "origin")):
            link = attributor.session_link({str(self.one)})
        self.assertEqual(link["rule"], "sample")
        self.assertEqual(link["evidence"], "git_origin_candidate")
        self.assertNotIn(str(self.one), json.dumps(link))

    def test_explicitly_out_of_scope_origin_never_uses_path_rule(self):
        attributor = Attributor([ProjectRule("sample", origins=["example.test/sample/allowed"], paths=["*"])])
        with patch.object(attributor, "origin", return_value=("example.test/sample/other", "origin")):
            link = attributor.session_link({str(self.one)})
        self.assertEqual(link["bucket"], "other")
        self.assertIsNone(link["rule"])
        self.assertEqual(link["evidence"], "git_origin_out_of_scope")

    def test_path_only_configuration_can_match_existing_origin(self):
        attributor = Attributor(self.rules)
        with patch.object(attributor, "origin", return_value=("example.test/sample/repo", "origin")):
            link = attributor.session_link({str(self.one)})
        self.assertEqual(link["bucket"], "project")
        self.assertEqual(link["evidence"], "path_pattern_origin")

    def test_two_cwds_in_one_origin_are_one_project(self):
        attributor = Attributor([ProjectRule("sample", origins=["example.test/sample/*"])])
        with patch.object(attributor, "origin", return_value=("example.test/sample/repo", "origin")):
            link = attributor.session_link({str(self.one), str(self.two)})
        self.assertEqual(link["bucket"], "project")
        self.assertEqual(len(link["links"]), 2)

    def test_session_candidates_do_not_override_event_spend(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(),
            {"type": "turn_context", "timestamp": "2030-01-01T00:01:00Z", "payload": {"cwd": str(self.one)}},
            self.token("2030-01-01T00:02:00Z", 10, 2, 3),
            {"type": "turn_context", "timestamp": "2030-01-01T00:03:00Z", "payload": {"cwd": str(self.two)}}])
        self.rules = [ProjectRule("first", paths=[str(self.one)]), ProjectRule("second", paths=[str(self.two)])]
        report = self.report()
        link = report["sessions"][0]["project"]
        self.assertEqual((link["bucket"], link["evidence"]), ("unassigned", "multiple_projects"))
        self.assertEqual(len(link["links"]), 2)
        self.assertEqual(report["coverage"]["unattributed_share"], 0)
        self.assertEqual(report["sessions"][0]["allocations"][0]["tokens"]["total"], 13)

    def test_previous_cwd_applies_before_a_later_in_window_switch(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(), self.token("2030-01-01T00:01:00Z", 10, 2, 3),
            {"type": "turn_context", "timestamp": "2030-01-01T00:03:00Z", "payload": {"cwd": str(self.two)}}])
        self.assertEqual(self.report()["sessions"][0]["project"]["evidence"], "multiple_projects")

    def test_first_event_cwd_replaces_old_context_and_future_cwd_is_excluded(self):
        self.write("codex", "rollout-one.jsonl", [self.meta(cwd=self.two),
            {"type": "turn_context", "timestamp": "2030-01-01T00:01:00Z", "payload": {"cwd": str(self.one)}},
            self.token("2030-01-01T00:02:00Z", 10, 2, 3),
            {"type": "turn_context", "timestamp": "2030-01-01T00:10:00Z", "payload": {"cwd": str(self.two)}}])
        self.assertEqual(self.report()["sessions"][0]["project"]["bucket"], "project")

    def test_windows_path_normalization_and_case_insensitive_matching(self):
        windows = "Q:" + "\\synthetic\\sample\\..\\sample\\workspace"
        expected = "q:" + "/synthetic/sample/workspace"
        self.assertEqual(normalize_path(windows), expected)
        attributor = Attributor([ProjectRule("sample", paths=["q:" + "/SYNTHETIC/*/workspace"])])
        self.assertEqual(attributor.session_link({windows})["bucket"], "project")

    def test_unmatched_and_missing_cwd_are_separate_buckets(self):
        attributor = Attributor(self.rules)
        self.assertEqual(attributor.session_link({str(self.two)})["bucket"], "other")
        self.assertEqual(attributor.session_link(set())["bucket"], "unassigned")

    def test_conflicting_rules_are_unassigned(self):
        attributor = Attributor([ProjectRule("one", paths=["*"]), ProjectRule("two", paths=["*"])])
        link = attributor.session_link({str(self.one)})
        self.assertEqual(link["bucket"], "unassigned")
        self.assertEqual(link["evidence"], "ambiguous_rules")

    def test_git_origin_lookup_is_local_cached_and_read_only(self):
        result = subprocess.CompletedProcess([], 0, "git@example.test:sample/repo.git\n", "")
        repo = subprocess.CompletedProcess([], 0, "true\n", "")
        attributor = Attributor(self.rules)
        with patch("sumbi.measure.attribution.subprocess.run", side_effect=[repo, result]) as run:
            self.assertEqual(attributor.origin(str(self.one))[0], "example.test/sample/repo")
            attributor.origin(str(self.one))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][-3:], ["config", "--get", "remote.origin.url"])
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_non_repository_does_not_read_global_origin_configuration(self):
        attributor = Attributor(self.rules)
        with patch("sumbi.measure.attribution.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")) as run:
            self.assertEqual(attributor.origin(str(self.one)), (None, "path_only"))
        run.assert_called_once()

    def test_unreadable_origin_does_not_bypass_origin_scope(self):
        attributor = Attributor([ProjectRule("sample", origins=["example.test/sample/*"], paths=["*"])])
        with patch.object(attributor, "origin", return_value=(None, "origin_unavailable")):
            link = attributor.session_link({str(self.one)})
        self.assertEqual((link["bucket"], link["evidence"]), ("unassigned", "origin_unavailable"))


class CliTests(SyntheticHome):
    def invoke(self, extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["collect", "--since", "2030-01-01T00:00:00Z", "--until", "2030-01-01T00:10:00Z",
                           "--home", str(self.home), *extra])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_cli_writes_versioned_json_and_text_and_private_local_review(self):
        self.install()
        output, review = self.home / "report.json", self.home / "review.txt"
        result, stdout, stderr = self.invoke(["--project", "sample", "--match-path", str(self.one),
                                               "--json", str(output), "--local-review", str(review)])
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Reported tokens (observed): 263", stdout)
        self.assertEqual(json.loads(output.read_text())["schema_version"], "1.1")
        self.assertIn("Harness friction: synthetic local note", review.read_text())
        self.assertNotIn("Harness friction:", stdout)

    def test_stdout_json_keeps_text_summary_on_stderr(self):
        result, stdout, stderr = self.invoke(["--json", "-", "--agents", "codex"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout)["schema_version"], "1.1")
        self.assertIn("Sessions in window: 0", stderr)
        self.assertEqual(list(json.loads(stdout)["by_agent"]), ["codex"])

    def test_ordered_multiple_project_rules_are_configuration(self):
        args = parser().parse_args(["collect", "--since", "2030-01-01T00:00:00Z", "--until", "2030-01-01T00:10:00Z",
                                    "--project", "one", "--match-origin", "example.test/sample/*", "--match-path", "*one*",
                                    "--project", "two", "--match-path", "*two*"])
        self.assertEqual([(r.name, r.origins, r.paths) for r in args.rules],
                         [("one", ["example.test/sample/*"], ["*one*"]), ("two", [], ["*two*"])])

    def test_invalid_window_and_agent_selection_rejected(self):
        for extra in (["--until", "2029-01-01T00:00:00Z"], ["--agents", "unknown"], ["--agents", "codex,codex"],
                      ["--since", "2030-01-01"], ["--since", "2030-01-01T01:00:00+01:00"], ["--idle-minutes", "nan"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as error:
                self.invoke(["--json", "-", *extra])
            self.assertEqual(error.exception.code, 2)

    def test_rules_need_preceding_project_and_a_pattern(self):
        for extra in (["--match-path", "*"], ["--project", "sample"], ["--project", "private label", "--match-path", "*"],
                      ["--project", "one", "--match-path", "*", "--project", "one", "--match-path", "*"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.invoke(["--json", "-", *extra])

    def test_outputs_cannot_overwrite_sources_or_each_other(self):
        destination = self.home / ".codex/sessions/report.json"
        for extra in (["--json", str(destination)], ["--json", str(self.home / "out"), "--local-review", str(self.home / "out")]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.invoke(extra)
        self.assertFalse(destination.exists())

    def test_collection_does_not_modify_source_files(self):
        self.install()
        paths = list(self.home.rglob("*.jsonl"))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        self.invoke(["--json", str(self.home / "report.json")])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})

    def test_output_failure_returns_error_without_exposing_paths(self):
        with patch("sumbi.cli.collect.atomic_write", side_effect=OSError("Synthetic private path canary")):
            result, stdout, stderr = self.invoke(["--json", str(self.home / "report.json")])
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn("canary", stderr)
        self.assertNotIn(str(self.home), stderr)


class ContractTests(unittest.TestCase):
    def test_programmatic_rule_names_obey_output_privacy_contract(self):
        with self.assertRaises(ValueError):
            ProjectRule("Synthetic private path with spaces")

    def test_window_requires_utc_and_order(self):
        with self.assertRaises(ValueError):
            Window(datetime(2030, 1, 1), datetime(2030, 1, 2))
        with self.assertRaises(ValueError):
            Window(WINDOW.until, WINDOW.since)

    def test_in_window_session_with_no_usage_does_not_fabricate_zero_kinds(self):
        session = Session("codex", "one")
        session.times.add(WINDOW.since)
        data = session.as_dict(WINDOW, Attributor([]), 5)
        self.assertIsNone(data["tokens"]["new_input"])
        self.assertEqual(data["tokens"]["total"], 0)

    def test_offline_core_has_no_project_rules_and_no_network_access(self):
        # All links depend on caller configuration; adapters expose no project defaults.
        self.assertEqual(Attributor([]).rules, [])
        for path in (Path(__file__).parents[2] / "sumbi").rglob("*.py"):
            if path.relative_to(Path(__file__).parents[2]).as_posix() == "sumbi/outcomes/github/live.py":
                continue  # Only the explicitly selected live outcome adapter uses HTTP.
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("import socket", source)
            self.assertNotIn("urllib.request", source)
            self.assertNotIn("requests", source.replace("user_input_requests", ""))


if __name__ == "__main__":
    unittest.main()
