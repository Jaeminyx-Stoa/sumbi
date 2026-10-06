"""Synthetic worker dispatch, own-session links, PR states and privacy boundaries."""

from datetime import timedelta
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from support import IsolatedTemporaryDirectory
from worker_github_fixtures import (REPO, at, codex_worker, claude_worker, execution, pull,
    recording, repository, save, stream)
from sumbi.core.records import Coverage
from sumbi.core.time import Window
from sumbi.events.adapters import codex
from sumbi.events.references import tool_refs
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.outcomes.worker_github.workers import deliver_workers
from sumbi.sessions.builder import collect


class WorkerGitHubTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.repo = self.root / "home", self.root / "repository"
        repository(self.repo)
        self.window = Window(at(1) - timedelta(hours=1), at(2) - timedelta(hours=1))
        for target in ("socket.socket", "socket.getaddrinfo", "socket.create_connection",
            "urllib.request.OpenerDirector.open"):
            guard = patch(target, side_effect=AssertionError("Tests cannot use the network"))
            guard.start()
            self.addCleanup(guard.stop)

    def outcomes(self, pulls, **options):
        directory = self.root / "outcomes"
        save(directory / "repository.json", recording(pulls, **options))
        return FixtureOutcomes(directory)

    def report(self, pulls, **options):
        return deliver_workers(self.home, self.window, [REPO], self.outcomes(pulls, **options),
            salt=b"synthetic-key")

    def test_subagent_exec_and_interactive_overhead_are_fixed_from_start(self):
        codex_worker(self.home, "child", self.repo, 1, kind="subagent")
        rows = codex_worker(self.home, "exec", self.repo, 2)
        # A later same-thread header and inherited parent cannot change provenance.
        later = {"type": "session_meta", "timestamp": at(1, 101).isoformat(),
            "payload": {"id": "exec", "cwd": str(self.repo), "source": "vscode"}}
        inherited = {**later, "payload": {"id": "parent", "cwd": str(self.repo),
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "other"}}}}}
        stream(self.home / ".codex/sessions/rollout-exec.jsonl", [*rows, later, inherited])
        codex_worker(self.home, "main", self.repo, 3, kind="vscode")
        report = self.report([pull(1), pull(2), pull(3)])
        self.assertEqual(report["states"]["success"], 2)
        self.assertEqual({r["dispatch_kind"] for r in report["units"]},
            {"subagent", "noninteractive_exec"})
        self.assertEqual(report["dispatch_overhead"]["sessions"], 1)
        self.assertEqual(report["cost"]["per_success"]["total"]["numerator"], 200)
        self.assertEqual(report["dispatch_overhead"]["observed_total"], 100)

    def test_exec_requires_both_start_markers_and_preserves_existing_worker_rule(self):
        rows = codex_worker(self.home, "unknown", self.repo, 1)
        rows[0]["payload"]["originator"] = "desktop"
        stream(self.home / ".codex/sessions/rollout-unknown.jsonl", rows)
        codex_worker(self.home, "exec", self.repo, 2)
        sessions = collect(codex, self.home, self.window, Coverage())
        kinds = {s.raw_id: s.dispatch_kind for s in sessions}
        self.assertEqual(kinds, {"exec": "noninteractive_exec", "unknown": "unknown"})
        self.assertFalse(any(s.is_worker for s in sessions))
        self.assertEqual(self.report([pull(1), pull(2)])["states"]["success"], 1)

    def test_sibling_worktree_with_same_origin_is_in_scope(self):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Synthetic",
            "-c", "user.email=synthetic@example.test", "commit", "--allow-empty", "-qm",
            "Synthetic root"], capture_output=True, check=True)
        sibling = self.root / "sibling"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-qb", "worker-1",
            str(sibling)], capture_output=True, check=True)
        codex_worker(self.home, "sibling", sibling, 1)
        report = self.report([pull(1)])
        self.assertEqual(report["units"][0]["start_scope"], "start_origin")
        self.assertEqual(report["states"]["success"], 1)

    def test_link_strength_and_priority_from_each_own_evidence_kind(self):
        cases = [
            ("url-output", 2, "gh pr create --head worker-2",
                "https://github.com/example/sample/pull/2", None, "pr_created", "strong"),
            ("push-input", 3, "git push -u origin worker-3", None, None, "pushed_branch", "strong"),
            ("push-output", 4, "git push origin HEAD", " * [new branch] HEAD -> worker-4\n", None,
                "pushed_branch", "strong"),
            ("committed", 5, "git commit -m 'Synthetic change'", "[worker-5 abc1234] Synthetic change\n",
                None, "committed_branch", "strong"),
            ("cwd", 6, None, None, "worker-6", "cwd_branch", "weak"),
            ("query", 7, "git branch --show-current", "worker-7\n", None, "cwd_branch", "weak")]
        for identity, number, command, output, context, _, _ in cases:
            codex_worker(self.home, identity, self.repo, command=command, output=output,
                context_branch=context)
        report = self.report([pull(n) for _, n, *_ in cases])
        evidence = sorted((r["links"][0]["evidence"], r["links"][0]["strength"])
            for r in report["units"])
        self.assertEqual(evidence, sorted((e, s) for *_, e, s in cases))
        self.assertEqual(report["success_rate"]["denominator"], 4)
        public = json.dumps(report)
        for value in (str(self.repo), "worker-1", "example/sample", "/pull/1", "git push"):
            self.assertNotIn(value, public)

    def test_pr_opened_by_main_uses_worker_branch_without_borrowing_main_evidence(self):
        codex_worker(self.home, "worker", self.repo, command="git push origin worker-1")
        codex_worker(self.home, "unlinked", self.repo)
        codex_worker(self.home, "main", self.repo, kind="vscode",
            command="gh pr create --head worker-1", output="https://github.com/example/sample/pull/1")
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["states"]["no_pr"], 1)
        self.assertEqual(report["success_rate"]["denominator"], 2)
        self.assertEqual(next(r for r in report["units"] if r["state"] == "success")
            ["links"][0]["evidence"], "pushed_branch")

    def test_all_six_states_keep_no_pr_and_exclude_no_change(self):
        for i in range(1, 5):
            codex_worker(self.home, str(i), self.repo, i)
        codex_worker(self.home, "no-pr", self.repo)
        codex_worker(self.home, "no-change", self.repo, edit=False)
        immature = pull(3)
        immature["response"].update(created_at="2030-01-30T01:00:00Z",
            closed_at="2030-01-30T02:00:00Z", merged_at="2030-01-30T02:00:00Z")
        snapshot = immature["checks_at_merge"]
        snapshot["captured_at"] = snapshot["statuses"][0]["updated_at"] = "2030-01-30T02:00:00Z"
        report = self.report([pull(1), pull(2, merged=False), immature,
            pull(4, merged=False, state="open")])
        self.assertEqual(report["states"], dict.fromkeys(
            ("success", "failed", "immature", "in_progress", "no_pr", "no_change"), 1))
        self.assertEqual(report["success_rate"]["denominator"], 5)
        self.assertEqual(report["cost"]["per_success"]["total"]["numerator"], 500)

    def test_red_revert_and_follow_up_fail_original_dispatch(self):
        for scenario in ("red", "revert", "fix-pr", "fix-commit"):
            with self.subTest(scenario=scenario):
                codex_worker(self.home, "worker", self.repo, 1)
                original = pull(1, checks="red" if scenario == "red" else "green")
                pulls, commits = [original], []
                if scenario == "fix-pr":
                    repair = pull(2, day=2)
                    repair["response"]["title"] = "Fix #1"
                    pulls.append(repair)
                elif scenario in ("revert", "fix-commit"):
                    commits.append({"sha": "f" * 40, "commit": {"message":
                        "Revert #1" if scenario == "revert" else "Fix #1",
                        "committer": {"date": at(2).isoformat()}}})
                report = self.report(pulls, commits=commits)
                self.assertEqual(report["states"]["failed"], 1)

    def test_multiple_constituents_and_failed_one_cannot_disappear(self):
        rows = codex_worker(self.home, "worker", self.repo, command="gh pr create",
            output="https://github.com/example/sample/pull/1")
        stream(self.home / ".codex/sessions/rollout-worker.jsonl", [*rows,
            *execution("gh pr create", "https://github.com/example/sample/pull/2", self.repo,
                identity="second", start=12, end=20)])
        report = self.report([pull(1), pull(2, merged=False)])
        self.assertEqual(report["states"]["failed"], 1)
        self.assertEqual(len(report["units"][0]["prs"]), 2)

    def test_explicit_pr_can_scope_worker_without_start_origin(self):
        missing = self.root / "missing"
        codex_worker(self.home, "worker", missing, command="gh pr create --repo example/sample",
            output="https://github.com/example/sample/pull/1")
        report = self.report([pull(1)])
        self.assertEqual(report["units"][0]["start_scope"], "own_reference")
        self.assertEqual(report["states"]["success"], 1)

    def test_explicit_repo_scopes_no_pr_without_a_checkout_origin(self):
        codex_worker(self.home, "worker", self.root / "missing",
            command="gh pr create --repo example/sample --head uncreated-worker")
        report = self.report([])
        self.assertEqual(report["units"][0]["start_scope"], "own_reference")
        self.assertEqual(report["states"]["no_pr"], 1)

    def test_anchored_push_result_names_a_strong_branch(self):
        codex_worker(self.home, "worker", self.repo, command="git push origin HEAD",
            output="To https://github.com/example/sample.git\n   abc1234..def5678 HEAD -> worker-1\n")
        report = self.report([pull(1)])
        self.assertEqual(report["units"][0]["links"][0]["evidence"], "pushed_branch")

    def test_strongest_link_wins_per_pr_but_other_weak_constituents_remain_weak(self):
        codex_worker(self.home, "same-pr", self.repo, command="gh pr create", context_branch="worker-1",
            output="https://github.com/example/sample/pull/1")
        codex_worker(self.home, "mixed", self.repo, context_branch="worker-2",
            command="gh pr create", output="https://github.com/example/sample/pull/1")
        report = self.report([pull(1), pull(2)])
        strong = next(r for r in report["units"] if not r["weak_link"])
        weak = next(r for r in report["units"] if r["weak_link"])
        self.assertEqual(strong["links"][0]["evidence"], "pr_created")
        self.assertEqual(len(strong["links"]), 1)
        self.assertEqual({r["strength"] for r in weak["links"]}, {"strong", "weak"})
        self.assertEqual(report["success_rate"]["denominator"], 1)

    def test_branch_reuse_and_out_of_scope_pr_cannot_supply_success(self):
        codex_worker(self.home, "worker", self.repo, command="gh pr create",
            output="https://github.com/other/sample/pull/2")
        previous = pull(1)
        previous["response"]["created_at"] = "2029-12-31T00:00:00Z"
        report = self.report([previous])
        self.assertEqual(report["states"]["no_pr"], 1)

    def test_no_change_is_excluded_even_with_a_strong_pr_reference(self):
        codex_worker(self.home, "unchanged", self.repo, 1, edit=False)
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["no_change"], 1)
        self.assertEqual(report["success_rate"]["denominator"], 0)
        self.assertEqual(len(report["units"][0]["links"]), 1)

    def test_missing_explicit_pr_is_pending_and_reported_for_coverage(self):
        codex_worker(self.home, "worker", self.repo, command="gh pr create",
            output="https://github.com/example/sample/pull/1")
        report = self.report([])
        self.assertEqual(report["states"]["in_progress"], 1)
        self.assertEqual(report["coverage"]["missing_prs"], 1)

    def test_mixed_agents_and_live_capture_happen_after_dispatch_selection(self):
        codex_worker(self.home, "exec", self.repo, 1)
        claude_worker(self.home, "child", self.repo, 2)
        recorded = self.outcomes([pull(1), pull(2)])
        def live(dispatches):
            self.assertEqual(len(dispatches), 1)
            self.assertEqual(dispatches[0].repos, (REPO,))
            return recorded
        report = deliver_workers(self.home, self.window, [REPO], live)
        self.assertEqual({r["agent"] for r in report["units"]}, {"codex", "claude-code"})
        self.assertEqual(report["states"]["success"], 2)

    def test_unsupported_shell_and_messages_cannot_create_branch_ownership(self):
        for command in ("git push --delete origin worker-1", "git push --dry-run origin worker-1",
            "git push origin :worker-1", "git branch -d worker-1",
            "git switch -c worker-1 && echo done", "git push origin $BRANCH"):
            refs = tool_refs("exec_command", {"cmd": command}, worker=True)
            self.assertFalse(any(k in ("branch_created", "branch_push_intent") for k, _ in refs))
        self.assertEqual(tool_refs("unknown", "https://github.com/example/sample/pull/1",
            output=True, worker=True), set())

    def test_views_old_pr_then_creates_own_pr(self):
        rows = codex_worker(self.home, "worker", self.repo,
            command="gh pr view https://github.com/example/sample/pull/1",
            output="https://github.com/example/sample/pull/1")
        rows.extend(execution("gh pr create --head worker-2",
            "https://github.com/example/sample/pull/2", self.repo,
            identity="create", start=20, end=30))
        stream(self.home / ".codex/sessions/rollout-worker.jsonl", rows)
        older = pull(1)
        older["response"]["created_at"] = "2029-12-31T00:00:00Z"
        report = self.report([older, pull(2)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual([r["evidence"] for r in report["units"][0]["links"]], ["pr_created"])
        self.assertEqual(report["coverage"]["evidence_gaps"]["pre_dispatch_pr_mentions"], 1)

    def test_push_and_commit_can_continue_older_pr(self):
        older = pull(1)
        older["response"]["created_at"] = "2029-12-31T00:00:00Z"
        codex_worker(self.home, "push", self.repo, 1)
        codex_worker(self.home, "commit", self.repo, command="git commit -m 'Synthetic fix'",
            output="[worker-1 abc1234] Synthetic fix\n")
        report = self.report([older])
        self.assertEqual(report["states"]["success"], 2)
        self.assertEqual({r["links"][0]["role"] for r in report["units"]}, {"continued"})
        self.assertEqual({r["dispatched_at"] for r in report["units"]}, {at(1).isoformat()})
        self.assertEqual(report["cost"]["per_success"]["total"]["numerator"], 200)

    def test_failed_or_unobserved_push_never_links(self):
        for code in (1, None):
            with self.subTest(exit_code=code):
                codex_worker(self.home, "worker", self.repo, 1, exit_code=code,
                    output="To https://github.com/example/sample.git\n"
                    " * [new branch] worker-1 -> worker-1\n")
                report = self.report([pull(1)])
                self.assertEqual(report["states"]["no_pr"], 1)
                self.assertEqual(report["units"][0]["links"], [])
                gap = "failed_authorship_commands" if code else "authorship_exit_unknown"
                self.assertEqual(report["coverage"]["evidence_gaps"][gap], 1)

    def test_brief_views_lists_and_branch_creation_are_not_authorship(self):
        for command, output in (("cat brief.md", "Review https://github.com/example/sample/pull/1"),
            ("gh pr list", "https://github.com/example/sample/pull/1"),
            ("gh pr view https://github.com/example/sample/pull/1", {"branch": "worker-1"}),
            ("git switch -c worker-1", "Switched to a new branch 'worker-1'")):
            with self.subTest(command=command):
                rows = codex_worker(self.home, "worker", self.repo, command=command, output=output)
                rows.append({"type": "response_item", "timestamp": at(1, 1).isoformat(),
                    "payload": {"type": "message", "role": "user", "content": [{
                        "type": "input_text", "text": "Brief: https://github.com/example/sample/pull/1"}]}})
                stream(self.home / ".codex/sessions/rollout-worker.jsonl", rows)
                report = self.report([pull(1)])
                self.assertEqual(report["units"][0]["links"], [])
                self.assertEqual(report["states"]["no_pr"], 1)

    def test_api_create_response_requires_successful_create_action(self):
        for command, code, expected in (("gh api repos/example/sample/pulls -X POST", 0, 1),
            ("gh api repos/example/sample/pulls -f title=Synthetic", 0, 1),
            ("gh api repos/example/sample/pulls", 0, 0),
            ("gh api repos/example/sample/pulls -X POST", 1, 0)):
            with self.subTest(command=command, code=code):
                codex_worker(self.home, "worker", self.repo, command=command, exit_code=code,
                    output=json.dumps({"html_url": "https://github.com/example/sample/pull/1"}))
                self.assertEqual(len(self.report([pull(1)])["units"][0]["links"]), expected)

    def test_creation_or_push_temporal_conflicts_do_not_abort_other_workers(self):
        older = pull(1)
        older["response"]["created_at"] = "2029-12-31T00:00:00Z"
        closed = pull(2, seconds=1)
        closed["response"]["created_at"] = "2029-12-31T00:00:00Z"
        codex_worker(self.home, "conflicting-create", self.repo, command="gh pr create",
            output="https://github.com/example/sample/pull/1")
        codex_worker(self.home, "closed-push", self.repo, 2)
        codex_worker(self.home, "healthy", self.repo, 3)
        report = self.report([older, closed, pull(3)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["states"]["no_pr"], 2)
        self.assertEqual(report["coverage"]["evidence_gaps"]["link_conflicts"], 2)

    def test_judgment_exception_is_a_unit_coverage_gap(self):
        codex_worker(self.home, "first", self.repo, 1)
        codex_worker(self.home, "second", self.repo, 2)
        from sumbi.outcomes.worker_github.workers import judge
        for exception in (ValueError("Synthetic conflict"), OverflowError(), RecursionError()):
            def conflicting(unit, outcomes, days):
                if REPO + "#1" in unit.prs:
                    raise exception
                return judge(unit, outcomes, days)
            with self.subTest(exception=type(exception).__name__), patch(
                "sumbi.outcomes.worker_github.workers.judge", side_effect=conflicting):
                report = self.report([pull(1), pull(2)])
                self.assertEqual(report["states"]["success"], 1)
                affected = next(r for r in report["units"] if r["evidence_incomplete"])
                self.assertEqual(affected["links"], [])
                self.assertEqual(affected["state"], "in_progress")
                self.assertEqual(report["coverage"]["evidence_gaps"]["judgment_conflicts"], 1)

    def test_invalid_follow_up_configuration_still_fails_fast(self):
        with self.assertRaisesRegex(ValueError, "timestamp range"):
            deliver_workers(self.home, self.window, [REPO], self.outcomes([]), follow_up_days=1e100)

    def test_codex_completed_item_commit_result_links_its_branch(self):
        rows = codex_worker(self.home, "worker", self.repo)
        rows.append({"type": "event_msg", "timestamp": at(1, 11).isoformat(), "payload": {
            "type": "item_completed", "started_at_ms": int(at(1, 3).timestamp() * 1000),
            "item": {"id": "commit", "type": "CommandExecution",
                "command": ["git", "commit", "-m", "Synthetic change"], "cwd": str(self.repo),
                "exit_code": 0, "aggregated_output": "[worker-1 abc1234] Synthetic change\n"}}})
        stream(self.home / ".codex/sessions/rollout-worker.jsonl", rows)
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["units"][0]["links"][0]["evidence"], "committed_branch")

    def test_launch_only_and_failed_create_output_cannot_link(self):
        for code in (1, None):
            codex_worker(self.home, "worker", self.repo, command="gh pr create", exit_code=code,
                output="https://github.com/example/sample/pull/1")
            self.assertEqual(self.report([pull(1)])["units"][0]["links"], [])
        rows = codex_worker(self.home, "worker", self.repo, 1)
        rows = [r for r in rows if r.get("payload", {}).get("type") != "exec_command_end"]
        stream(self.home / ".codex/sessions/rollout-worker.jsonl", rows)
        self.assertEqual(self.report([pull(1)])["units"][0]["links"], [])

    def test_push_repository_conflict_and_unknown_remote_cannot_link(self):
        for command, output in (("git push origin worker-1",
            "To https://github.com/other/sample.git\n * [new branch] worker-1 -> worker-1\n"),
            ("git push upstream worker-1", None),
            ("cd other && git push origin worker-1", None)):
            codex_worker(self.home, "worker", self.repo, command=command, output=output)
            self.assertEqual(self.report([pull(1)])["units"][0]["links"], [])

    def test_git_c_operand_uses_execution_repository(self):
        missing = self.root / "missing"
        codex_worker(self.home, "worker", missing,
            command=["git", "-C", str(self.repo), "push", "origin", "worker-1"])
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["units"][0]["start_scope"], "own_reference")

    def test_pre_dispatch_execution_and_weak_old_branch_cannot_continue_pr(self):
        older = pull(1)
        older["response"]["created_at"] = "2029-12-31T00:00:00Z"
        rows = codex_worker(self.home, "worker", self.repo, context_branch="worker-1")
        events = execution("git push origin worker-1", None, self.repo, start=-10, end=11)
        stream(self.home / ".codex/sessions/rollout-worker.jsonl", [*rows, *events])
        report = self.report([older])
        self.assertEqual(report["states"]["no_pr"], 1)
        self.assertEqual(report["units"][0]["links"], [])
        self.assertEqual(report["coverage"]["evidence_gaps"]["link_conflicts"], 1)

    def test_claude_failed_push_result_cannot_link(self):
        claude_worker(self.home, "worker", self.repo, 1)
        path = self.home / ".claude/projects/group/main/subagents/agent-worker.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[-1]["toolUseResult"]["exitCode"] = 1
        rows[-1]["message"]["content"][0].update(is_error=True,
            content="Exit code 1\nSynthetic rejection")
        stream(path, rows)
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["no_pr"], 1)
        self.assertEqual(report["units"][0]["links"], [])

    def test_malformed_execution_cwd_is_a_gap_not_a_global_abort(self):
        rows = codex_worker(self.home, "worker", self.repo, 1)
        launch = next(r for r in rows if r.get("payload", {}).get("type") == "exec_command_begin")
        launch["payload"]["cwd"] = {"unsupported": "Synthetic value"}
        stream(self.home / ".codex/sessions/rollout-worker.jsonl", rows)
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["no_pr"], 1)
        self.assertEqual(report["units"][0]["links"], [])
        self.assertEqual(report["coverage"]["evidence_gaps"]["link_conflicts"], 1)


if __name__ == "__main__":
    unittest.main()
