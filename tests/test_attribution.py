"""Authored event attribution fixtures with conserved, hand-computed spend."""

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
from support import IsolatedTemporaryDirectory
import unittest
from unittest.mock import patch

from sumbi.events.tool_paths import resolve_path, shell_paths, tool_evidence
from sumbi.measure.attribution import Attributor, ProjectRule
from sumbi.core.time import Window
from sumbi.core.values import timestamp
from sumbi.measure.report import collect, text_summary

START = timestamp("2030-01-01T00:00:00Z")
WINDOW = Window(START, START + timedelta(minutes=30))


class EventAttributionTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.a, self.b = self.home / "alpha", self.home / "beta"
        for repo in (self.a, self.b):
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        self.rules = [ProjectRule("alpha", paths=[str(self.a) + "*"]),
                      ProjectRule("beta", paths=[str(self.b) + "*"])]
        for target in ("socket.socket", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("No network in fixtures"))
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, {}, clear=False)
        guard.start()
        self.addCleanup(guard.stop)
        os.environ.pop("SUMBI_SALT", None)

    def time(self, seconds):
        return (START + timedelta(seconds=seconds)).isoformat()

    def write(self, agent, events, name=None):
        root = self.home / (".claude/projects/fixture" if agent == "claude-code" else ".codex/sessions/fixture")
        root.mkdir(parents=True, exist_ok=True)
        path = root / (name or ("main.jsonl" if agent == "claude-code" else "rollout-main.jsonl"))
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    def message(self, seconds, identity, cwd=None, tools=(), output=2):
        event = {"type": "assistant", "timestamp": self.time(seconds), "sessionId": "fixture-session",
                 "message": {"id": identity, "content": list(tools), "usage": {
                     "input_tokens": 10, "cache_creation_input_tokens": 3,
                     "cache_read_input_tokens": 5, "output_tokens": output}}}
        if cwd is not None:
            event["cwd"] = str(cwd)
        return event

    def context(self, seconds, cwd=None, ordinal=None):
        event = {"type": "turn_context", "timestamp": self.time(seconds), "payload": {}}
        if cwd is not None:
            event["payload"]["cwd"] = str(cwd)
        if ordinal is not None:
            event["ordinal"] = ordinal
        return event

    def token(self, seconds, inputs, cached, output, ordinal=None):
        event = {"type": "event_msg", "timestamp": self.time(seconds), "payload": {"type": "token_count",
                 "info": {"total_token_usage": {"input_tokens": inputs, "cached_input_tokens": cached,
                         "output_tokens": output, "reasoning_output_tokens": 0}}}}
        if ordinal is not None:
            event["ordinal"] = ordinal
        return event

    def tool(self, name, arguments, seconds=0):
        return {"type": "response_item", "timestamp": self.time(seconds), "payload": {
            "type": "function_call", "name": name, "call_id": "tool-" + str(seconds),
            "arguments": json.dumps(arguments)}}

    def block(self, name, arguments):
        return {"type": "tool_use", "id": "tool", "name": name, "input": arguments}

    def report(self, **kwargs):
        return collect(self.home, WINDOW, rules=self.rules, **kwargs)[0]

    def rows(self, report):
        return {r["rule"] or r["bucket"]: r for r in report["coverage"]["spend"]}

    def conserved(self, report):
        total = report["summary"]["tokens"]["total"]
        self.assertEqual(sum(r["tokens"] for r in report["coverage"]["spend"]), total)
        self.assertEqual(sum(report["coverage"]["evidence_tokens"].values()), total)
        for session in report["sessions"]:
            self.assertEqual(sum(a["tokens"]["total"] for a in session["allocations"]), session["tokens"]["total"])
        for kind in ("new_input", "cache_write", "cache_read", "output", "reasoning_output"):
            self.assertEqual(sum(r["tokens_by_kind"][kind] or 0 for r in report["coverage"]["spend"]),
                             report["summary"]["tokens"][kind] or 0)

    def test_claude_one_session_moves_between_repositories(self):
        self.write("claude-code", [self.message(0, "a", self.a), self.message(60, "b", self.b, output=7)])
        report = self.report()
        rows = self.rows(report)
        self.assertEqual((rows["alpha"]["tokens"], rows["beta"]["tokens"]), (20, 25))
        self.assertEqual((rows["alpha"]["sessions"], rows["beta"]["sessions"]), (1, 1))
        self.assertEqual(rows["beta"]["tokens_by_kind"], {
            "new_input": 10, "cache_write": 3, "cache_read": 5, "output": 7, "reasoning_output": None})
        self.assertEqual(rows["alpha"]["evidence_counts"]["cwd"], 1)
        self.assertEqual(report["coverage"]["unassigned_share"], 0)
        self.conserved(report)

    def test_windows_and_posix_cwds_allocate_events_on_either_platform(self):
        self.rules = [ProjectRule("alpha", paths=["q:/synthetic/alpha"]),
                      ProjectRule("beta", paths=["/Synthetic/Beta"])]
        self.write("claude-code", [self.message(0, "a", r"Q:\Synthetic\Alpha"),
                                  self.message(1, "b", "/Synthetic/Beta")])
        report = self.report()
        rows = self.rows(report)
        self.assertEqual((rows["alpha"]["tokens"], rows["beta"]["tokens"]), (20, 20))
        self.conserved(report)

    def test_codex_delta_crossing_cwd_switch_uses_closing_context(self):
        self.write("codex", [self.context(0, self.a), self.token(60, 10, 3, 2),
                             self.context(90, self.b), self.token(120, 30, 8, 7)])
        report = self.report()
        # First total: 7 new + 3 cached + 2 output = 12. Second delta:
        # 15 new + 5 cached + 5 output = 25, all to beta at the closing snapshot.
        rows = self.rows(report)
        self.assertEqual((rows["alpha"]["tokens"], rows["beta"]["tokens"]), (12, 25))
        self.assertEqual(rows["beta"]["tokens_by_kind"]["cache_write"], None)
        self.conserved(report)

    def test_tool_paths_into_other_repo_do_not_override_own_cwd(self):
        self.write("claude-code", [self.message(0, "a", self.a,
            tools=[self.block("Read", {"file_path": str(self.b / "private.txt")})])])
        rows = self.rows(self.report())
        self.assertEqual(rows["alpha"]["tokens"], 20)
        self.assertNotIn("beta", rows)
        self.assertEqual(rows["alpha"]["evidence_counts"]["tool_path"], 0)

    def test_tool_paths_work_without_own_cwd_and_consolidate_files(self):
        self.write("claude-code", [self.message(0, "a", tools=[
            self.block("Read", {"file_path": str(self.b / "one.txt")}),
            self.block("Edit", {"file_path": str(self.b / "src/two.txt")})])])
        rows = self.rows(self.report())
        self.assertEqual(rows["beta"]["tokens"], 20)
        self.assertEqual(rows["beta"]["evidence_counts"]["tool_path"], 1)
        self.assertEqual(rows["beta"]["fallback_share"], 0)

    def test_relative_tool_path_uses_its_own_message_cwd(self):
        self.write("claude-code", [self.message(0, "a", self.a, tools=[
            self.block("Read", {"file_path": "src/one.txt"})]), self.message(600, "b")])
        rows = self.rows(self.report())
        self.assertEqual(rows["alpha"]["tokens"], 40)
        self.assertEqual(rows["alpha"]["evidence_counts"]["tool_path"], 1)
        self.assertEqual(rows["alpha"]["fallback_share"], 0)

    def test_new_user_turn_clears_previous_tool_paths(self):
        self.write("claude-code", [self.message(0, "a", tools=[
            self.block("Read", {"file_path": str(self.b / "one.txt")})]),
            {"type": "user", "sessionId": "fixture-session", "timestamp": self.time(600),
             "message": {"content": "Authored next turn"}}, self.message(601, "b")])
        rows = self.rows(self.report())
        self.assertEqual((rows["beta"]["tokens"], rows["unassigned"]["tokens"]), (20, 20))

    def test_leading_cd_and_git_c_in_shell_commands(self):
        for command in (f'cd "{self.b}" && python -m unittest', f'git -C "{self.b}" status'):
            with self.subTest(command=command):
                self.write("codex", [self.context(0), self.tool("exec_command", {"cmd": command}, 1),
                                     self.token(2, 10, 3, 2)])
                rows = self.rows(self.report())
                self.assertEqual(rows["beta"]["tokens"], 12)
                self.assertEqual(rows["beta"]["evidence_counts"]["tool_path"], 1)

    def test_command_workdir_overrides_turn_cwd_and_next_turn_resets_it(self):
        self.write("codex", [self.context(0, self.a),
            self.tool("exec_command", {"cmd": "git status", "workdir": str(self.b)}, 1),
            self.token(2, 10, 3, 2), self.context(3, self.a), self.token(4, 20, 6, 4)])
        rows = self.rows(self.report())
        self.assertEqual((rows["alpha"]["tokens"], rows["beta"]["tokens"]), (12, 12))
        self.assertEqual(rows["beta"]["evidence_counts"]["cwd"], 1)

    def test_apply_patch_absolute_and_relative_targets(self):
        # The user line supplies only a relative-path base, not an assistant cwd.
        user = {"type": "user", "timestamp": self.time(0), "cwd": str(self.b),
                "sessionId": "fixture-session", "message": {"content": "Authored prompt"}}
        self.write("claude-code", [user, self.message(1, "a", tools=[self.block("apply_patch",
            "*** Begin Patch\n*** Update File: src/one.txt\n@@\n+x\n*** End Patch")])])
        self.assertEqual(self.rows(self.report())["beta"]["evidence_counts"]["tool_path"], 1)

    def test_fallback_under_threshold_and_missing_after_idle(self):
        self.write("claude-code", [self.message(0, "a", self.a), self.message(299, "b"),
                                  self.message(599, "c"), self.message(600, "d")])
        report = self.report()
        rows = self.rows(report)
        self.assertEqual((rows["alpha"]["tokens"], rows["unassigned"]["tokens"]), (40, 40))
        self.assertEqual(rows["alpha"]["evidence_counts"]["previous_event"], 1)
        self.assertEqual(rows["alpha"]["fallback_share"], 0.5)
        self.assertEqual(rows["unassigned"]["evidence_counts"]["unassigned"], 2)
        self.conserved(report)

    def test_pre_window_event_can_supply_fallback_but_future_cannot(self):
        self.write("claude-code", [self.message(-60, "before", self.a), self.message(0, "inside"),
                                  self.message(1800, "after", self.b)])
        report = self.report()
        self.assertEqual(self.rows(report)["alpha"]["fallback_share"], 1)
        self.assertEqual(report["summary"]["tokens"]["total"], 20)

    def test_no_evidence_is_unassigned(self):
        self.write("codex", [self.token(0, 10, 3, 2)])
        report = self.report()
        self.assertEqual(report["coverage"]["unassigned_share"], 1)
        self.assertEqual(self.rows(report)["unassigned"]["evidence_counts"]["unassigned"], 1)

    def test_conflicting_tool_paths_do_not_use_previous_fallback(self):
        self.write("claude-code", [self.message(0, "a", self.a), self.message(1, "b", tools=[
            self.block("Read", {"file_path": str(self.a / "one.txt")}),
            self.block("Read", {"file_path": str(self.b / "two.txt")})])])
        rows = self.rows(self.report())
        self.assertEqual(rows["unassigned"]["tokens"], 20)
        self.assertEqual(rows["unassigned"]["evidence_counts"]["tool_path"], 1)

    def test_explicit_rule_counts_only_matching_events(self):
        self.rules = self.rules[:1]
        self.write("claude-code", [self.message(0, "a", self.a), self.message(1, "b", self.b)])
        report = self.report()
        rows = self.rows(report)
        self.assertEqual((rows["alpha"]["tokens"], rows["other"]["tokens"]), (20, 20))
        self.assertEqual(rows["alpha"]["events"], 1)
        self.assertEqual(rows["alpha"]["sessions"], 1)
        self.assertEqual(report["summary"]["sessions"], 1)
        self.assertEqual(report["coverage"]["unassigned_share"], 0)
        self.assertEqual(report["coverage"]["unattributed_share"], 0.5)

    def test_no_rules_publish_candidate_project_groups(self):
        self.rules = []
        self.write("claude-code", [self.message(0, "a", self.a), self.message(1, "b", self.b)])
        report = self.report()
        projects = [r for r in report["coverage"]["spend"] if r["bucket"] == "project"]
        self.assertEqual(len(projects), 2)
        self.assertEqual(report["coverage"]["unattributed_share"], 0)
        self.assertTrue(all(r["rule"] is None for r in projects))

    def test_duplicate_stream_snapshot_counts_one_selected_event(self):
        self.write("claude-code", [self.message(0, "a", self.a), self.message(1, "a", self.b, output=7)])
        report = self.report()
        self.assertEqual(self.rows(report)["beta"]["tokens"], 25)
        self.assertEqual(report["coverage"]["evidence_counts"]["cwd"], 1)
        self.assertNotIn("alpha", self.rows(report))

    def test_unselected_stream_snapshot_paths_do_not_conflict_with_selected_paths(self):
        self.write("claude-code", [self.message(0, "a", tools=[self.block("Read", {
            "file_path": str(self.a / "one.txt")})]), self.message(1, "a", output=7,
            tools=[self.block("Read", {"file_path": str(self.b / "two.txt")})])])
        report = self.report()
        self.assertEqual(self.rows(report)["beta"]["tokens"], 25)
        self.assertEqual(report["coverage"]["evidence_counts"]["tool_path"], 1)

    def test_future_tool_calls_cannot_change_earlier_delta(self):
        self.write("codex", [self.context(0), self.token(1, 10, 3, 2), self.tool("Read", {
            "file_path": str(self.b / "two.txt")}, 2), self.token(3, 20, 6, 4)])
        rows = self.rows(self.report())
        self.assertEqual((rows["unassigned"]["tokens"], rows["beta"]["tokens"]), (12, 12))

    def test_repeated_codex_totals_are_not_billable_events(self):
        self.write("codex", [self.context(0, self.a), self.token(1, 10, 3, 2),
                             self.context(2, self.b), self.token(3, 10, 3, 2)])
        report = self.report()
        self.assertEqual(report["coverage"]["evidence_counts"]["cwd"], 1)
        self.assertNotIn("beta", self.rows(report))

    def test_same_timestamp_ordinal_decides_closing_context(self):
        self.write("codex", [self.context(0, self.a, ordinal=1), self.context(1, self.b, ordinal=3),
                             self.token(1, 10, 3, 2, ordinal=2)])
        self.assertEqual(self.rows(self.report())["alpha"]["tokens"], 12)

    def test_same_timestamp_and_ordinal_use_accepted_record_order(self):
        self.write("codex", [self.context(0, self.a, ordinal=1), self.token(1, 10, 3, 2, ordinal=2),
                             self.context(1, self.b, ordinal=2)])
        self.assertEqual(self.rows(self.report())["alpha"]["tokens"], 12)

    def test_origin_scope_never_falls_back_to_a_matching_tool_path(self):
        self.rules = [ProjectRule("alpha", origins=["example.test/team/allowed"], paths=["*"])]
        subprocess.run(["git", "-C", str(self.b), "remote", "add", "origin",
                        "https://example.test/team/other.git"], check=True, capture_output=True)
        self.write("claude-code", [self.message(0, "a", tools=[
            self.block("Read", {"file_path": str(self.b / "private.txt")})])])
        report = self.report()
        self.assertEqual(self.rows(report)["other"]["tokens"], 20)
        self.assertEqual(report["coverage"]["unassigned_share"], 0)

    def test_salt_changes_keys_and_output_contains_no_private_operands(self):
        self.write("claude-code", [self.message(0, "a", tools=[
            self.block("Bash", {"command": f'cd "{self.b}" && echo SYNTHETIC_SECRET'})])])
        plain, salted = self.report(), self.report(salt=b"synthetic-salt")
        self.assertEqual(plain["summary"], salted["summary"])
        self.assertNotEqual(self.rows(plain)["beta"]["project_key"], self.rows(salted)["beta"]["project_key"])
        output = json.dumps(salted) + text_summary(salted)
        for private in (str(self.home), "SYNTHETIC_SECRET", "synthetic-salt", "private.txt", "fixture-session", "cd "):
            self.assertNotIn(private, output)


class PathEvidenceTests(unittest.TestCase):
    def test_unc_paths_are_candidates_without_network_filesystem_access(self):
        with patch.object(Path, "exists", side_effect=AssertionError("No UNC probing")), \
                patch.object(Path, "is_dir", side_effect=AssertionError("No UNC probing")), \
                patch("sumbi.measure.attribution.subprocess.run", side_effect=AssertionError("No UNC Git commands")):
            link = Attributor([ProjectRule("alpha", paths=["//synthetic/alpha/*"])]).event_link(
                r"\\synthetic\alpha\folder")
        self.assertEqual(link["bucket"], "project")

    def test_windows_posix_and_relative_paths(self):
        self.assertEqual(resolve_path(r"Q:\Synthetic\Alpha\..\Beta\file.txt"), "q:/synthetic/beta/file.txt")
        self.assertEqual(resolve_path("../Beta/file.txt", "/Synthetic/Alpha"), "/Synthetic/Beta/file.txt")
        self.assertEqual(resolve_path(r"..\Beta\file.txt", r"Q:\Synthetic\Alpha"), "q:/synthetic/beta/file.txt")
        self.assertEqual(resolve_path("/Synthetic/Beta/file.txt"), "/Synthetic/Beta/file.txt")

    def test_quoted_shell_paths_and_windows_cd(self):
        self.assertEqual(shell_paths('cd /d "Q:\\Synthetic Space\\Beta" && git status'), [r"Q:\Synthetic Space\Beta"])
        self.assertEqual(shell_paths("git -C '/synthetic space/beta' status"), ["/synthetic space/beta"])
        self.assertEqual(shell_paths(["git", "-C", "/synthetic space/beta", "status"]), ["/synthetic space/beta"])

    def test_unrecognized_or_unsafe_inputs_supply_no_evidence(self):
        for command in ("echo cd /synthetic/beta", "echo x; cd /synthetic/beta", "cd $(pwd) && git status"):
            self.assertEqual([resolve_path(p) for p in shell_paths(command) if resolve_path(p)], [])
        self.assertEqual(tool_evidence("UnknownTool", {"path": "/synthetic/beta"}), (None, []))
        self.assertEqual(tool_evidence("exec_command", "not json"), (None, []))
        self.assertIsNone(resolve_path("~/.private"))
        self.assertIsNone(resolve_path("relative.txt"))

    def test_git_operations_inside_quoted_arguments_are_not_evidence(self):
        for command in ("echo '; git -C /synthetic/beta'", 'echo "x && git -C /synthetic/beta"',
                        "echo x # ; git -C /synthetic/beta", r"echo x\; git -C /synthetic/beta",
                        "echo x^& git -C /synthetic/beta", "echo $x; git -C /synthetic/beta"):
            self.assertEqual(shell_paths(command), [])

    def test_relative_git_operand_after_leading_cd_uses_that_directory(self):
        self.assertEqual(shell_paths("cd /synthetic/beta && git -C src status"),
                         ["/synthetic/beta", "/synthetic/beta/src"])

    def test_patch_targets_are_extracted_without_patch_content(self):
        _, paths = tool_evidence("apply_patch", "*** Begin Patch\n*** Add File: /synthetic/a\n+private\n"
                                "*** Update File: /synthetic/b\n*** Move to: /synthetic/c\n*** Delete File: /synthetic/d\n*** End Patch")
        self.assertEqual(paths, ["/synthetic/a", "/synthetic/b", "/synthetic/c", "/synthetic/d"])

    def test_patch_json_arguments_are_supported(self):
        _, paths = tool_evidence("apply_patch", json.dumps({"patch": "*** Begin Patch\n"
                                 "*** Add File: /synthetic/a\n+x\n*** End Patch"}))
        self.assertEqual(paths, ["/synthetic/a"])


if __name__ == "__main__":
    unittest.main()
