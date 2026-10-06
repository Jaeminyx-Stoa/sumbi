"""Translators emit frozen observations without window or feature decisions."""

from dataclasses import FrozenInstanceError
import inspect
import json
from pathlib import Path
import unittest

from support import IsolatedTemporaryDirectory
from sumbi.core.records import Coverage
from sumbi.events.adapters import claude_code, codex, sumbi_events
from sumbi.events import schema as e


class TranslationTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def save(self, relative, records):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def test_all_translators_have_only_home_and_coverage_inputs(self):
        for adapter in (claude_code, codex, sumbi_events):
            with self.subTest(adapter=adapter.__name__):
                self.assertEqual(list(inspect.signature(adapter.collect).parameters), ["home", "coverage"])

    def test_claude_retains_streaming_snapshots_and_duplicate_observations(self):
        def message(output):
            return {"type": "assistant", "sessionId": "session", "timestamp": "2030-01-01T00:00:01Z",
                    "message": {"id": "message", "usage": {"input_tokens": 5, "output_tokens": output},
                                "content": []}}
        first, second = message(1), message(2)
        self.save(".claude/projects/group/session.jsonl", [first, second, first])
        coverage = Coverage()
        observations = list(claude_code.collect(self.home, coverage))
        usages = [event for record in observations for event in record.events if isinstance(event, e.TokenUsage)]
        self.assertEqual([event.tokens.output for event in usages], [1, 2, 1])
        self.assertTrue(all(event.message_id == "message" and event.selection == "maximal_output" for event in usages))
        self.assertEqual(coverage.duplicate_events, 0)
        self.assertEqual(observations[0].identity, observations[2].identity)

    def test_claude_tool_inputs_are_local_observations_without_derived_paths_or_links(self):
        self.save(".claude/projects/group/session.jsonl", [{"type": "assistant", "sessionId": "session",
            "message": {"content": [{"type": "tool_use", "id": "call", "name": "Bash",
                "input": {"command": "git -C /fixture/one branch --show-current", "cwd": "/fixture/two"}}]}}])
        record = list(claude_code.collect(self.home, Coverage()))[0]
        evidence = next(event for event in record.events if isinstance(event, e.ToolEvidence))
        self.assertEqual(e.thaw(evidence.inputs[0].arguments)["cwd"], "/fixture/two")
        self.assertFalse(hasattr(record, "attribution_events"))
        self.assertFalse(hasattr(record, "deliverable_events"))
        self.assertFalse(hasattr(evidence, "paths"))
        self.assertFalse(hasattr(evidence, "refs"))
        with self.assertRaises(FrozenInstanceError):
            record.events = ()

    def test_codex_snapshot_retains_total_input_without_changing_v1_new_input_meaning(self):
        self.save(".codex/sessions/group/rollout-session.jsonl", [
            {"type": "session_meta", "payload": {"id": "session"}},
            {"type": "event_msg", "timestamp": "2030-01-01T00:00:01Z", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 5}}}}])
        observations = list(codex.collect(self.home, Coverage()))
        usage = next(event for event in observations[-1].events if isinstance(event, e.TokenUsage))
        self.assertEqual(usage.tokens.new_input, 6)
        self.assertEqual(usage.tokens.cache_read, 4)
        self.assertEqual(usage.input_total, 10)
        self.assertIsNone(usage.tokens.cache_write)
        self.assertEqual(usage.selection, "cumulative_including_cache")

    def test_codex_inherited_header_does_not_emit_dispatch_or_context(self):
        self.save(".codex/sessions/group/rollout-session.jsonl", [
            {"type": "session_meta", "payload": {"id": "child", "cwd": "/fixture/child"}},
            {"type": "session_meta", "payload": {"id": "parent", "cwd": "/fixture/parent"}}])
        coverage = Coverage()
        observations = list(codex.collect(self.home, coverage))
        self.assertEqual([record.session_id for record in observations], ["child", "child"])
        self.assertFalse(any(isinstance(event, (e.SessionStart, e.Context)) for event in observations[-1].events))
        self.assertEqual(coverage.inherited_session_meta, 1)

    def test_codex_start_provenance_distinguishes_launcher_and_interactive(self):
        for identity, source, originator, expected in (
            ("exec", "exec", "codex_exec", "noninteractive_exec"),
            ("desktop", "vscode", "desktop", "interactive"),
            ("unknown", "exec", "desktop", "unknown")):
            self.save(".codex/sessions/rollout-" + identity + ".jsonl", [{
                "type": "session_meta", "timestamp": "2030-01-01T00:00:00Z", "payload": {
                    "id": identity, "cwd": "/fixture/workspace", "source": source,
                    "originator": originator}}])
        starts = {record.session_id: event for record in codex.collect(self.home, Coverage())
            for event in record.events if isinstance(event, e.SessionStart)}
        self.assertEqual({key: value.dispatch_kind for key, value in starts.items()},
            {"exec": "noninteractive_exec", "desktop": "interactive", "unknown": "unknown"})
        self.assertTrue(all(value.role == "orchestrator" for value in starts.values()))

    def test_open_usage_uses_the_same_event_type_as_native_usage(self):
        record = {"schema_version": 1, "type": "token_usage", "event_id": "usage", "session_id": "session",
                  "timestamp": "2030-01-01T00:00:01Z", "tokens": {"new_input": 6, "output": 2},
                  "ignored": "Harness friction: private ignored text"}
        self.save(".sumbi/events/stream.jsonl", [record, record])
        coverage = Coverage()
        observations = list(sumbi_events.collect(self.home, coverage))
        self.assertEqual(len(observations), 2)
        self.assertEqual(coverage.duplicate_events, 0)
        self.assertEqual(observations[0].events, (e.TokenUsage(e.Tokens(new_input=6, output=2)),))
        self.assertEqual(observations[0].identity.scope, "event_id")

    def test_open_validation_is_deferred_until_identity_conflicts_are_resolved(self):
        record = {"schema_version": 1, "type": "token_usage", "event_id": "usage", "session_id": "session",
                  "timestamp": None, "tokens": {"output": -1}}
        self.save(".sumbi/events/stream.jsonl", [record])
        coverage = Coverage()
        observation = list(sumbi_events.collect(self.home, coverage))[0]
        diagnostic = next(event for event in observation.events if isinstance(event, e.Diagnostic))
        self.assertEqual(diagnostic.counter, "invalid_token_records")
        self.assertTrue(diagnostic.token_gap)
        self.assertEqual(coverage.invalid_token_records, 0)
        self.assertEqual(coverage.invalid_timestamps, 0)

    def test_native_starts_include_agent_role_and_parent_observations(self):
        self.save(".claude/projects/group/session/subagents/agent-child.jsonl", [{
            "type": "user", "sessionId": "parent", "agentId": "child",
            "timestamp": "2030-01-01T00:00:00Z", "cwd": "/fixture/one"}])
        self.save(".codex/sessions/group/rollout-child.jsonl", [{
            "type": "session_meta", "timestamp": "2030-01-01T00:00:00Z", "payload": {
                "id": "child", "cwd": "/fixture/one", "source": {
                    "subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}}}])
        for adapter, agent in ((claude_code, "claude-code"), (codex, "codex")):
            with self.subTest(agent=agent):
                record = list(adapter.collect(self.home, Coverage()))[0]
                start = next(event for event in record.events if isinstance(event, e.SessionStart))
                self.assertEqual((start.agent, start.role, start.parent_session_id), (agent, "worker", "parent"))


if __name__ == "__main__":
    unittest.main()
