"""Synthetic adapter accounting, provenance, privacy and conflict tests."""

import json
from pathlib import Path
from support import IsolatedTemporaryDirectory
from types import SimpleNamespace
import unittest

from sumbi.events.adapters import sumbi_events
from sumbi.sessions.builder import collect as collect_sessions
from sumbi.outcomes.github.deliver import linked_events
from sumbi.outcomes.local_verify.workers import state, token_measurement
from sumbi.measure.attribution import Attributor
from sumbi.core.records import Coverage
from sumbi.sessions.session import Session, TOKEN_KINDS
from sumbi.core.time import Window
from sumbi.core.values import timestamp
from sumbi.core.privacy import pseudonym, pseudonym_key


class OpenEventAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / ".sumbi/events"
        self.root.mkdir(parents=True)
        self.repo = (self.home / "repository").resolve()
        self.repo.mkdir()
        self.window = Window(timestamp("2030-01-01T00:00:00Z"), timestamp("2030-01-02T00:00:00Z"))

    def event(self, kind, event_id, seconds=0, *, session="worker", **fields):
        return {"schema_version": 1, "type": kind, "event_id": event_id, "session_id": session,
                "timestamp": f"2030-01-01T01:00:{seconds:02d}Z", **fields}

    def start(self, session="worker", **fields):
        return self.event("session_start", "start-" + session, session=session,
            **{"agent": "example-agent", "role": "worker", "cwd": str(self.repo), **fields})

    def save(self, events, name="stream.jsonl"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def read(self):
        coverage = Coverage()
        return collect_sessions(sumbi_events, self.home, self.window, coverage), coverage

    def worker(self):
        return [self.start(), self.event("file_edit", "edit", 1),
                self.event("command_execution", "command", 3, command=["bash", "scripts/check.sh"],
                           started_at="2030-01-01T01:00:02Z", cwd=str(self.repo), exit_code=0),
                self.event("session_end", "end", 4)]

    def outcome(self, session):
        return state(session, self.window, ("scripts/check.sh",), 5, self.repo)[0]

    def test_global_delta_duplicates_and_explicit_tool_pairs(self):
        tokens = dict(zip(TOKEN_KINDS, (5, 2, 3, 4, 1)))
        usage = self.event("token_usage", "usage", 2, tokens=tokens)
        events = [self.start(), usage, self.event("token_usage", "usage-2", 3, tokens=tokens),
                  self.event("tool_start", "tool-start", 1, tool_call_id="tool"),
                  self.event("tool_end", "tool-end", 4, tool_call_id="tool", error=True)]
        self.save(events)
        self.save([usage], "resume/stream.jsonl")
        sessions, coverage = self.read()
        session = sessions[0]
        self.assertEqual(coverage.duplicate_events, 1)
        self.assertEqual(session.tokens["new_input"], 10)
        self.assertEqual(session.counts["tool_calls"], 1)
        self.assertEqual(session.counts["tool_results"], 1)
        self.assertEqual(session.counts["tool_errors"], 1)
        self.assertEqual(session.as_dict(self.window, Attributor([]), 5)["tokens"]["complete_total"], 28)
        self.assertEqual(session.intervals[("tool", "tool")][1] - session.intervals[("tool", "tool")][0],
                         timestamp("2030-01-01T01:00:04Z") - timestamp("2030-01-01T01:00:01Z"))

    def test_missing_delta_components_stay_unknown_even_for_codex_emitter(self):
        self.save([self.start(agent="codex"), self.event("token_usage", "known", 1,
            tokens=dict(zip(TOKEN_KINDS, (5, 2, 3, 4, 1)))), self.event("token_usage", "partial", 2,
            tokens={"new_input": 5, "cache_read": 3, "output": 4})])
        session = self.read()[0][0]
        tokens, complete, observed = token_measurement(session, self.window)
        self.assertIsNone(tokens["cache_write"])
        self.assertIsNone(tokens["total"])
        self.assertFalse(complete)
        self.assertEqual(observed, 26)
        public = session.as_dict(self.window, Attributor([]), 5)["tokens"]
        self.assertEqual(public["total"], 26)
        self.assertIsNone(public["complete_total"])
        self.assertEqual(public["not_reported_events"]["cache_write"], 1)

    def test_zero_is_known_and_reasoning_is_a_subset(self):
        self.save([self.start(), self.event("token_usage", "zero", 1, tokens=dict.fromkeys(TOKEN_KINDS, 0))])
        session = self.read()[0][0]
        tokens, complete, observed = token_measurement(session, self.window)
        self.assertTrue(complete)
        self.assertEqual(tokens["total"], 0)
        self.assertEqual(observed, 0)
        self.assertEqual(session.as_dict(self.window, Attributor([]), 5)["tokens"]["complete_total"], 0)

    def test_invalid_tokens_reject_bool_negative_and_reasoning_over_output(self):
        for invalid in (True, -1, 1.5, "3"):
            with self.subTest(invalid=invalid):
                self.save([self.start(), self.event("token_usage", "invalid", 1, tokens={"output": invalid})])
                session, coverage = self.read()
                self.assertEqual(coverage.invalid_token_records, 1)
                self.assertEqual(self.outcome(session[0]), "unverified")
        self.save([self.start(), self.event("token_usage", "invalid", 1,
            tokens={"output": 1, "reasoning_output": 2})])
        self.assertEqual(self.read()[1].invalid_token_records, 1)

    def test_custom_labels_are_salted_and_parent_ids_cross_emitters(self):
        self.save([self.start("parent", role="orchestrator", agent="private parent", model="private-model"),
                   self.start(parent_session_id="parent", agent="private child", effort="private effort", cli_version="private-version")])
        with pseudonym_key(b"synthetic-key"):
            sessions, _ = self.read()
            rows = {s.raw_id: s.as_dict(self.window, Attributor([]), 5) for s in sessions}
            self.assertEqual(rows["worker"]["parent_id"], rows["parent"]["id"])
            self.assertEqual(rows["worker"]["agent"], "sumbi-events:" + pseudonym("agent", "private child"))
            public = json.dumps(rows)
            for value in ("private parent", "private child", "private-model", "private effort", "private-version"):
                self.assertNotIn(value, public)

    def test_duplicate_id_conflict_removes_both_records_and_blocks_success(self):
        events = self.worker()
        self.save(events)
        self.save([{**events[2], "exit_code": 2}], "resume/conflict.jsonl")
        sessions, coverage = self.read()
        self.assertFalse(sessions[0].commands)
        self.assertEqual(coverage.unknown_record_types["conflicting_event_id"], 1)
        self.assertEqual(self.outcome(sessions[0]), "unverified")

    def test_conflicting_start_withholds_dispatch_proof(self):
        first = self.start()
        self.save([first, {**first, "event_id": "other-start", "agent": "other-agent"}])
        sessions, coverage = self.read()
        self.assertIsNone(sessions[0].start_at)
        self.assertIsNone(sessions[0].parent_raw_id)
        self.assertEqual(coverage.unknown_record_types["conflicting_session_start"], 1)

    def test_conflicting_context_metadata_is_not_order_selected(self):
        self.save(self.worker() + [self.event("context", "context-a", 2, model="model-a"),
                                  self.event("context", "context-b", 2, model="model-b")])
        sessions, coverage = self.read()
        self.assertEqual(coverage.unknown_record_types["conflicting_context_metadata"], 1)
        self.assertEqual(self.outcome(sessions[0]), "unverified")

    def test_later_unknown_edits_or_commands_cannot_preserve_success(self):
        for kind, fields in (("file_edit", {}), ("command_execution", {
                "command": ["bash", "scripts/check.sh"], "exit_code": None, "started_at": None, "cwd": None})):
            with self.subTest(kind=kind):
                self.save(self.worker() + [self.event(kind, "unknown", timestamp=None, **fields)])
                session = self.read()[0][0]
                self.assertEqual(self.outcome(session), "unverified")

    def test_command_context_never_proves_cwd_or_reads_exit_prose(self):
        events = self.worker()
        for extra in ({"cwd": None}, {"exit_code": None, "stdout": "Process exited with code 0"},
                      {"exit_code": True}, {"started_at": None}):
            with self.subTest(extra=extra):
                self.save([events[0], events[1], {**events[2], **extra}, events[3]])
                self.assertEqual(self.outcome(self.read()[0][0]), "unverified")

    def test_later_unknown_command_content_cannot_preserve_success(self):
        self.save(self.worker() + [self.event("command_execution", "unknown-content", 5,
            started_at="2030-01-01T01:00:04Z", command=None, cwd=str(self.repo), exit_code=0)])
        self.assertEqual(self.outcome(self.read()[0][0]), "unverified")

    def test_explicit_usage_cwd_precedes_context_and_metadata_conflicts_clear_it(self):
        self.save([self.start(cwd="/fixture/first"), self.event("token_usage", "own-cwd", 1,
            cwd="/fixture/second", tokens={"new_input": 1})])
        session = self.read()[0][0]
        allocations = list(session.event_allocations(self.window, Attributor([]), 5))
        self.assertEqual(allocations[0][-1], "/fixture/second")
        self.save([self.start(cwd="/fixture/first"), self.event("context", "conflict", 0,
            cwd="/fixture/second"), self.event("token_usage", "usage", 1, tokens={"new_input": 1})])
        session = self.read()[0][0]
        allocations = list(session.event_allocations(self.window, Attributor([]), 5))
        self.assertEqual(allocations[0][-2], "unassigned")

    def test_timestamp_is_completion_and_extra_completion_is_ignored(self):
        events = self.worker()
        events[2]["completed_at"] = "2030-01-01T00:00:00Z"
        self.save(events)
        self.assertEqual(self.outcome(self.read()[0][0]), "success")

    def test_unknown_version_type_and_missing_role_count_fixed_categories(self):
        invalid = [{**self.start(), "schema_version": True},
                   self.event("private arbitrary type", "unknown", 2),
                   {key: value for key, value in self.start("other").items() if key != "role"}]
        self.save(invalid)
        sessions, coverage = self.read()
        self.assertEqual(coverage.unknown_record_types["unsupported_schema_version"], 1)
        self.assertEqual(coverage.unknown_record_types["unknown_event_type"], 1)
        self.assertEqual(coverage.unknown_record_types["invalid_session_start"], 1)
        self.assertNotIn("private arbitrary type", json.dumps(coverage.as_dict()))
        self.assertTrue(all(s.local_evidence_gaps for s in sessions))

    def test_structurally_malformed_fields_do_not_crash_or_inherit_proof(self):
        for fields in ({"model": {}}, {"effort": []}, {"cli_version": True}, {"cwd": []}):
            with self.subTest(fields=fields):
                self.save([self.start(**fields), {**self.start(**fields), "event_id": "duplicate-start"}])
                sessions, coverage = self.read()
                self.assertTrue(coverage.unknown_record_types)
                self.assertTrue(sessions[0].local_evidence_gaps)
        self.save(self.worker() + [self.event("token_usage", "invalid-cwd", 5,
            cwd="relative/private", tokens={"output": 4})])
        sessions, coverage = self.read()
        self.assertEqual(coverage.unknown_record_types["invalid_token_cwd"], 1)
        self.assertEqual(self.outcome(sessions[0]), "unverified")
        self.save([self.start(), self.event(["unknown"], "bad-type", 1)])
        self.assertEqual(self.read()[1].unknown_record_types["unknown_event_type"], 1)

    def test_discarded_cost_evidence_nulls_complete_cost_only_for_token_session(self):
        tokens = dict(zip(TOKEN_KINDS, (5, 2, 3, 4, 1)))
        good = self.event("token_usage", "known-cost", 1, tokens=tokens)
        bad = self.event("token_usage", "bad-cost", 2, tokens={"output": -1})
        for fields in ({}, {"schema_version": 2}):
            with self.subTest(fields=fields):
                self.save([self.start(), good, {**bad, **fields}])
                session = self.read()[0][0]
                public = session.as_dict(self.window, Attributor([]), 5)
                self.assertEqual(public["tokens"]["total"], 14)
                self.assertIsNone(public["tokens"]["complete_total"])
                self.assertTrue(public["tokens"]["evidence_incomplete"])
                self.assertIsNone(token_measurement(session, self.window)[0]["total"])
        shared = self.event("token_usage", "shared-id", 2, tokens=tokens)
        command = self.event("command_execution", "shared-id", 2, session="other",
            command=None, started_at=None, cwd=None, exit_code=None)
        self.save([self.start(), self.start("other"), good, {**good, "event_id": "other-cost", "session_id": "other"},
                   shared, command])
        sessions = {s.raw_id: s for s in self.read()[0]}
        self.assertTrue(sessions["worker"].token_evidence_incomplete)
        self.assertFalse(sessions["other"].token_evidence_incomplete)
        self.assertEqual(token_measurement(sessions["other"], self.window)[0]["total"], 14)

    def test_explicit_null_or_conflicting_context_blocks_previous_spend_fallback(self):
        first = self.event("token_usage", "first-cost", 1, tokens={"output": 1})
        unknown = self.event("context", "unknown-context", 2, cwd=None)
        known = self.event("context", "known-context", 2, cwd="/fixture/second")
        later = self.event("token_usage", "later-cost", 3, tokens={"output": 1})
        for contexts in ([unknown], [unknown, known], [known, unknown]):
            with self.subTest(contexts=contexts):
                self.save([self.start(cwd="/fixture/first"), first, *contexts, later,
                    self.event("context", "recovered", 4, cwd="/fixture/recovered"),
                    self.event("token_usage", "recovered-cost", 5, tokens={"output": 1})])
                session, coverage = self.read()
                events = list(session[0].event_allocations(self.window, Attributor([]), 5))
                self.assertEqual(events[1][-2], "unassigned")
                self.assertEqual(events[2][-1], "/fixture/recovered")
                self.assertEqual(bool(coverage.unknown_record_types), len(contexts) == 2)

    def test_github_ownership_is_not_inferred_from_folder_id(self):
        self.save([self.start(cwd="/fixture/D1"), self.event("token_usage", "cost", 1, tokens={"output": 10})])
        session = self.read()[0][0]
        dispatch = SimpleNamespace(id="D1", repos={"sample/example"}, branches=[],
            dispatched_at=timestamp("2030-01-01T00:00:00Z"))
        events = list(linked_events(session, self.window, Attributor([]), 5, [dispatch],
            {"D1": {"attempt_prs": [], "closed_at": None}}))
        self.assertIsNone(events[0][2])
        self.assertEqual(events[0][3], "unallocated")

    def test_generic_blank_lines_are_ignored_and_utc_overflow_is_invalid(self):
        self.save([self.start()])
        with (self.root / "stream.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("\n \t\n")
        sessions, coverage = self.read()
        self.assertEqual(coverage.lines_read, 3)
        self.assertEqual(coverage.broken_lines, 0)
        self.assertEqual(len(sessions), 1)
        self.assertIsNone(timestamp("0001-01-01T00:00:00+01:00"))
        self.save([self.start(timestamp="0001-01-01T00:00:00+01:00")])
        sessions, coverage = self.read()
        self.assertEqual(coverage.invalid_timestamps, 1)
        self.assertIsNone(sessions[0].start_at)

    def test_native_no_edit_with_unknown_command_keeps_native_no_change(self):
        for agent in ("codex", "claude-code"):
            with self.subTest(agent=agent):
                session = Session(agent, "synthetic-session")
                session.local_evidence_gaps["command_timestamp_missing"] = 1
                self.assertEqual(self.outcome(session), "no_change")

    def test_metadata_null_clears_prior_baseline_and_omission_keeps_it(self):
        known = {"model": "known-model", "effort": "known-effort", "cli_version": "known-version"}
        start = self.start(timestamp="2029-12-31T23:00:00Z", **known)
        for fields, expected in ((dict.fromkeys(known), False), ({}, True)):
            with self.subTest(fields=fields):
                self.save([start, self.event("context", "context", timestamp="2029-12-31T23:01:00Z", **fields),
                    self.event("token_usage", "cost", 1, tokens={"output": 1})])
                session, coverage = self.read()
                for values in (session[0].models, session[0].efforts, session[0].versions):
                    self.assertEqual(bool(values), expected)
                self.assertFalse(any(session[0].metadata_incomplete.values()))
                self.assertFalse(coverage.unknown_record_types)

    def test_metadata_partial_flags_preserve_observed_ids_without_affecting_execution(self):
        known = {"model": "known-model", "effort": "known-effort", "cli_version": "known-version"}
        for initial, later in ((known, dict.fromkeys(known)), ({}, known)):
            with self.subTest(initial=initial):
                events = self.worker()
                events[0].update(initial)
                self.save(events + [self.event("context", "metadata", 5, **later)])
                session, coverage = self.read()
                self.assertTrue(all(session[0].metadata_incomplete.values()))
                self.assertTrue(all((session[0].models, session[0].efforts, session[0].versions)))
                self.assertFalse(coverage.unknown_record_types)
                self.assertEqual(self.outcome(session[0]), "success")
        self.save(self.worker() + [self.event("context", "metadata", 5, **dict.fromkeys(known))])
        session = self.read()[0][0]
        self.assertFalse(any(session.metadata_incomplete.values()))
        self.assertFalse(any((session.models, session.efforts, session.versions)))

    def test_null_and_known_metadata_at_same_time_conflict_in_either_order(self):
        for fields in ({"model": "known"}, {"effort": "known"}, {"cli_version": "known"}):
            for reverse in (False, True):
                with self.subTest(fields=fields, reverse=reverse):
                    contexts = [self.event("context", "known", 2, **fields),
                        self.event("context", "unknown", 2, **dict.fromkeys(fields))]
                    self.save(self.worker() + (list(reversed(contexts)) if reverse else contexts))
                    session, coverage = self.read()
                    self.assertEqual(coverage.unknown_record_types["conflicting_context_metadata"], 1)
                    self.assertEqual(self.outcome(session[0]), "unverified")


if __name__ == "__main__":
    unittest.main()
