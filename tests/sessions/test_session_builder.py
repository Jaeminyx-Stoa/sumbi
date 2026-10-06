"""Hand-computed fold rules using only normalized, synthetic observations."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import unittest

from sumbi.core.privacy import pseudonym, pseudonym_key
from sumbi.core.records import Coverage
from sumbi.core.time import Window
from sumbi.core.values import timestamp
from sumbi.events import schema as e
from sumbi.sessions.builder import build, own_context_at_start


START = timestamp("2030-01-01T00:00:00Z")
WINDOW = Window(START, START + timedelta(minutes=10))
ONE = "/fixture/one"
TWO = "/fixture/two"


class BuilderTests(unittest.TestCase):
    def record(self, index, *events, seconds=None, session="session", **fields):
        when = START + timedelta(seconds=index if seconds is None else seconds)
        return e.Record("native", session, when, e.Identity(str(index).encode()), tuple(events), **fields)

    def open_record(self, index, *events, **fields):
        record = self.record(index, *events, **fields)
        return replace(record, agent="sumbi-events", identity=e.Identity(str(index).encode(), str(index), "event_id"))

    def fold(self, *records, **options):
        coverage = Coverage()
        sessions = build(iter(records), WINDOW, coverage, **options)
        return sessions[0], coverage

    def snapshot(self, inputs, cached, output, reasoning=None):
        return e.TokenUsage(e.Tokens(new_input=inputs - cached, cache_read=cached,
            output=output, reasoning_output=reasoning), selection="cumulative_including_cache", input_total=inputs)

    def start(self, cwd=ONE, **fields):
        return e.SessionStart(cwd, "parent", "worker", "example-agent", **fields)

    def test_frozen_nested_tool_values_round_trip(self):
        raw = {"command": ["bash", "scripts/check.sh"], "cwd": ONE}
        event = e.CommandExecution("command", e.freeze(raw))
        raw["command"].append("changed")
        self.assertEqual(e.thaw(event.command)["command"], ["bash", "scripts/check.sh"])
        with self.assertRaises(FrozenInstanceError):
            event.cwd = TWO
        with self.assertRaises(FrozenInstanceError):
            event.command.items = ()

    def test_native_duplicate_does_not_repeat_time_counters_or_diagnostics(self):
        record = self.record(1, e.Counter("compactions"), e.Diagnostic("unknown-shape"))
        session, coverage = self.fold(record, record)
        self.assertEqual(session.counts["compactions"], 1)
        self.assertEqual(len(session.times), 1)
        self.assertEqual(coverage.duplicate_events, 1)
        self.assertEqual(coverage.unknown_record_types, {"unknown-shape": 1})

    def test_metadata_observed_before_duplicate_still_counts(self):
        record = self.record(1)
        repeated = replace(record, before_dedup=(e.Metadata(cli_version="v2", supplied=("cli_version",)),))
        session, coverage = self.fold(record, repeated)
        self.assertEqual(session.versions, {"v2"})
        self.assertEqual(coverage.duplicate_events, 1)

    def test_missing_native_timestamp_is_counted_only_when_supplied(self):
        absent = replace(self.record(1), timestamp=None, timestamp_supplied=False)
        null = replace(self.record(2), timestamp=None)
        session, coverage = self.fold(absent, null, null)
        self.assertEqual(coverage.invalid_timestamps, 1)
        self.assertEqual(coverage.duplicate_events, 1)
        self.assertFalse(session.times)

    def test_first_observed_start_and_cwd_have_independent_clocks(self):
        session, _ = self.fold(
            self.record(3, e.SessionStart(TWO, provenance="first-observed-cwd"), worker=True, parent_session_id="parent"),
            self.record(1, e.SessionStart(None, provenance="first-observed-cwd")),
            self.record(2, e.SessionStart(ONE, provenance="first-observed-cwd")))
        self.assertEqual(session.start_at, START + timedelta(seconds=1))
        self.assertEqual(session.start_cwd, ONE)
        self.assertEqual(session.start_evidence, "first-observed-cwd")
        self.assertTrue(session.is_worker)
        self.assertEqual(session.parent_raw_id, "parent")

    def test_header_start_keeps_earliest_header_and_accumulates_worker_role(self):
        session, _ = self.fold(
            self.record(3, e.SessionStart(TWO, provenance="session-header")),
            self.record(1, e.SessionStart(ONE, "parent", "worker", provenance="session-header")))
        self.assertEqual(session.start_at, START + timedelta(seconds=1))
        self.assertEqual(session.start_cwd, ONE)
        self.assertTrue(session.is_worker)
        self.assertEqual(session.parent_raw_id, "parent")

    def test_maximal_message_output_then_latest_timestamp_selects_all_components(self):
        def usage(output, inputs, cwd):
            return e.TokenUsage(e.Tokens(new_input=inputs, output=output), cwd, "maximal_output", "message")
        session, _ = self.fold(self.record(1, usage(4, 100, ONE)),
            self.record(3, usage(5, 30, TWO)), self.record(2, usage(5, 20, ONE)))
        self.assertEqual(session.tokens["new_input"], 30)
        self.assertEqual(session.tokens["output"], 5)
        usages = [row for row in session.attribution_events if row[2] == "usage"]
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0][3][1], TWO)

    def test_maximal_snapshot_outside_window_does_not_retain_earlier_partial_usage(self):
        session, _ = self.fold(
            self.record(1, e.TokenUsage(e.Tokens(output=4), selection="maximal_output", message_id="message")),
            self.record(2, e.TokenUsage(e.Tokens(output=5), selection="maximal_output", message_id="message"), seconds=600))
        self.assertIsNone(session.tokens["output"])

    def test_cumulative_usage_sorts_time_and_ordinal_and_uses_prior_baseline(self):
        session, _ = self.fold(self.record(2, self.snapshot(20, 8, 6, 2), seconds=10, ordinal=2),
            self.record(1, self.snapshot(10, 3, 2, 1), seconds=-1),
            self.record(3, self.snapshot(15, 5, 4, 1), seconds=10, ordinal=1))
        self.assertEqual({k: session.tokens[k] for k in ("new_input", "cache_read", "output", "reasoning_output")},
                         {"new_input": 5, "cache_read": 5, "output": 4, "reasoning_output": 1})
        self.assertIsNone(session.tokens["cache_write"])

    def test_cumulative_reset_rebaselines_every_component(self):
        session, _ = self.fold(self.record(1, self.snapshot(10, 2, 6, 2)),
                              self.record(2, self.snapshot(8, 4, 4, 1)))
        self.assertEqual(session.tokens["new_input"], 12)
        self.assertEqual(session.tokens["cache_read"], 6)
        self.assertEqual(session.tokens["output"], 10)
        self.assertEqual(session.tokens["reasoning_output"], 3)
        self.assertEqual(session.counts["counter_resets"], 1)

    def test_cumulative_cached_correction_exposes_gap_and_keeps_baseline(self):
        session, coverage = self.fold(self.record(1, self.snapshot(10, 2, 4)),
            self.record(2, self.snapshot(11, 5, 4)), self.record(3, self.snapshot(13, 5, 5)))
        self.assertEqual(coverage.invalid_token_records, 1)
        self.assertEqual(session.tokens["new_input"], 10)
        self.assertEqual(session.tokens["output"], 5)

    def test_cumulative_input_only_increment_is_not_dropped(self):
        session, _ = self.fold(self.record(1, self.snapshot(10, 0, 0)))
        self.assertEqual(session.tokens["new_input"], 10)
        self.assertEqual(session.tokens["output"], 0)

    def test_missing_reasoning_transition_does_not_invent_reasoning_delta(self):
        session, _ = self.fold(self.record(1, self.snapshot(10, 2, 4)),
                              self.record(2, self.snapshot(12, 3, 5, 2)))
        self.assertIsNone(session.tokens["reasoning_output"])
        self.assertEqual(session.tokens["output"], 5)

    def test_tool_counts_pair_once_and_request_intervals_remain_separate(self):
        session, _ = self.fold(self.record(1, e.ToolStart("tool"), e.Request("request", endpoint="start")),
            self.record(2, e.ToolStart("tool")), self.record(3, e.ToolEnd("tool", True), e.Request("request", endpoint="end")),
            self.record(4, e.ToolEnd("unmatched")))
        self.assertEqual(session.counts["tool_calls"], 1)
        self.assertEqual(session.counts["tool_results"], 2)
        self.assertEqual(session.counts["tool_errors"], 1)
        self.assertEqual(session.intervals["tool", "tool"], (START + timedelta(seconds=1), START + timedelta(seconds=3)))
        self.assertNotIn(("tool", "unmatched"), session.intervals)
        self.assertIn(("request", "request"), session.intervals)

    def test_machine_tool_interval_end_does_not_use_record_completion_time(self):
        session, _ = self.fold(self.record(3, e.ToolStart("tool", at=START, own_time=True),
            e.ToolEnd("tool", interval=False), e.ToolEnd("tool", at=START + timedelta(seconds=2), own_time=True, count=False)))
        self.assertEqual(session.intervals["tool", "tool"], (START, START + timedelta(seconds=2)))
        self.assertEqual(session.counts["tool_results"], 1)

    def test_deferred_launch_and_unmatched_required_pair_cannot_prove_execution(self):
        session, _ = self.fold(self.record(1, e.CommandExecution("call", e.freeze(["bash", "scripts/check.sh"]),
            started_at=START, cwd=ONE, phase="start", deferred=True)),
            self.record(2, e.CommandExecution("call", exit_code=0, phase="end", pairing="launch", require_pair=True)),
            self.record(3, e.CommandExecution("missing", exit_code=0, phase="end", pairing="launch", require_pair=True)))
        self.assertEqual(list(session.commands), ["call"])
        self.assertIsNone(session.commands["call"].exit_code)
        self.assertEqual(session.commands["call"].command, ["bash", "scripts/check.sh"])

    def test_execution_cwd_uses_context_at_start_even_when_context_is_recorded_late(self):
        session, _ = self.fold(self.record(1, e.SessionStart(ONE, provenance="session-header"),
            e.Context(ONE, execution_context=True)),
            self.record(2, e.CommandExecution("call", "check", started_at=START + timedelta(seconds=3),
                cwd_supplied=False, phase="start")),
            self.record(5, e.CommandExecution("call", exit_code=0, phase="end", pairing="launch", infer_cwd=True)),
            self.record(4, e.Context(TWO, execution_context=True), seconds=2))
        self.assertEqual(session.commands["call"].cwd, TWO)

    def test_conflicting_paired_cwd_remains_unknown(self):
        session, _ = self.fold(self.record(1, e.CommandExecution("call", started_at=START, cwd=ONE, phase="start")),
            self.record(2, e.CommandExecution("call", "check", 0, START, TWO, pairing="interval", infer_cwd=True)))
        self.assertIsNone(session.commands["call"].cwd)

    def test_context_at_start_rejects_conflicts_unknown_times_and_pre_lifetime_context(self):
        at = START + timedelta(seconds=1)
        self.assertIsNone(own_context_at_start([(START, ONE), (START, TWO)], at, START))
        self.assertIsNone(own_context_at_start([(None, ONE), (START, ONE)], at, START))
        self.assertIsNone(own_context_at_start([(START, ONE)], at, at))
        self.assertEqual(own_context_at_start([(START, ONE), (at + timedelta(seconds=1), TWO)], at, START), ONE)

    def test_explicit_execution_never_infers_cwd(self):
        session, _ = self.fold(self.open_record(1, self.start()),
            self.open_record(2, e.CommandExecution("command", "check", 0, START, None)))
        self.assertIsNone(session.commands["command"].cwd)
        self.assertEqual(session.start_evidence, "sumbi-events-v1")
        self.assertEqual(session.emitter_agent, "example-agent")

    def test_missing_edit_and_execution_times_are_enduring_gaps(self):
        record = replace(self.open_record(1, e.FileEdit("edit"), e.CommandExecution("command")), timestamp=None)
        session, coverage = self.fold(record)
        self.assertEqual(session.local_evidence_gaps["edit_timestamp_missing"], 1)
        self.assertEqual(session.local_evidence_gaps["command_timestamp_missing"], 1)
        self.assertEqual(session.local_evidence_gaps["command_content_unknown"], 1)
        self.assertEqual(coverage.invalid_timestamps, 1)

    def test_global_open_identity_conflict_discards_both_sessions_usage(self):
        first = self.open_record(1, e.TokenUsage(e.Tokens(new_input=10)), usage_record=True)
        second = replace(first, session_id="other", identity=e.Identity(b"different", "1", "event_id"))
        coverage = Coverage()
        sessions = build([first, second], WINDOW, coverage)
        self.assertEqual(len(sessions), 2)
        self.assertTrue(all(s.token_evidence_incomplete for s in sessions))
        self.assertTrue(all(not s.attribution_events for s in sessions))
        self.assertEqual(coverage.unknown_record_types["conflicting_event_id"], 1)

    def test_conflicting_dispatch_cannot_select_favorable_start(self):
        session, coverage = self.fold(self.open_record(1, self.start()), self.open_record(2, self.start(TWO)))
        self.assertIsNone(session.start_at)
        self.assertFalse(session.is_worker)
        self.assertEqual(coverage.unknown_record_types["conflicting_session_start"], 1)

    def test_open_cwd_conflict_clears_attribution_without_fallback(self):
        session, coverage = self.fold(self.open_record(1, e.Context(ONE)),
            self.open_record(2, e.Context(TWO), seconds=1))
        self.assertEqual([row[3] for row in session.attribution_events], [(None, True), (None, True)])
        self.assertEqual(coverage.unknown_record_types["conflicting_context_metadata"], 1)

    def test_open_metadata_retains_latest_prior_and_known_unknown_mix(self):
        def metadata(model):
            return e.Metadata(model=model, supplied=("model",), selection="explicit")
        with pseudonym_key(b"synthetic"):
            session, _ = self.fold(self.open_record(1, e.Context(metadata=metadata("old")), seconds=-10),
                self.open_record(2, e.Context(metadata=metadata("current")), seconds=-1),
                self.open_record(3, e.Context(metadata=metadata(None))))
            self.assertEqual(session.models, {pseudonym("model", "current")})
        self.assertTrue(session.metadata_incomplete["model"])

    def test_tool_paths_links_and_private_review_are_builder_features(self):
        evidence = e.ToolEvidence(inputs=(e.ToolInput("call", "Bash",
            e.freeze({"command": "git branch --show-current", "cwd": ONE})),))
        output = e.ToolEvidence(outputs=(e.ToolOutput("call", "feature/example"),))
        session, _ = self.fold(self.record(1, evidence, e.LocalText("Harness friction: synthetic")),
                              self.record(2, output), local_review=True, collect_links=True)
        self.assertEqual(session.review, {"Harness friction: synthetic"})
        self.assertEqual(session.attribution_events[0][3][0], ONE)
        self.assertIn(("branch_observed", "feature/example"), session.deliverable_events[-1][3])

    def test_completion_and_resume_follow_window_and_native_order(self):
        session, _ = self.fold(self.record(1, e.SessionEnd("observed")),
            self.record(2, e.Resume()), self.record(3, e.SessionEnd("observed"), seconds=600))
        self.assertIsNone(session.completed_at)

    def test_anonymous_tool_records_do_not_pair_reference_evidence_across_records(self):
        query = e.ToolEvidence(inputs=(e.ToolInput(None, "exec_command",
            e.freeze({"command": "git branch --show-current"})),), fallback_identity=True)
        output = e.ToolEvidence(outputs=(e.ToolOutput(None, "feature/example"),), fallback_identity=True)
        session, _ = self.fold(self.record(1, query), self.record(2, output), collect_links=True)
        self.assertFalse(session.deliverable_events)

    def test_pairing_retains_numeric_versus_string_source_keys(self):
        session, _ = self.fold(
            self.record(1, e.CommandExecution("1", "check", started_at=START, phase="start",
                source_identity=e.SourceIdentity(1))),
            self.record(2, e.CommandExecution("1", exit_code=0, phase="end", pairing="launch", require_pair=True,
                source_identity=e.SourceIdentity("1"))))
        self.assertFalse(session.commands)

    def test_missing_call_id_uses_numeric_record_sequence_for_pairing(self):
        session, _ = self.fold(self.record(1, e.CommandExecution(None, "check", phase="start")),
            self.record(2, e.CommandExecution("1", exit_code=0, phase="end", pairing="launch", require_pair=True)))
        self.assertFalse(session.commands)

    def test_invalid_open_cwd_and_null_are_both_unknown_for_conflict_comparison(self):
        session, coverage = self.fold(self.open_record(1, e.Context("relative", cwd_valid=False),
            e.Diagnostic("invalid_context_cwd", evidence_gap=True)),
            self.open_record(2, e.Context(None), seconds=1))
        self.assertEqual(coverage.unknown_record_types, {"invalid_context_cwd": 1})
        self.assertEqual(session.cwd_events, [(START + timedelta(seconds=1), "relative")])


if __name__ == "__main__":
    unittest.main()
