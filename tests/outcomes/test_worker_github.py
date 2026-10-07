"""Synthetic worker dispatch, own-session links, PR states and privacy boundaries."""

from datetime import timedelta
import base64
import io
import json
from pathlib import Path
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, patch

from support import IsolatedTemporaryDirectory, isolate_github_destinations
from worker_github_fixtures import (REPO, at, codex_worker, claude_worker, execution, pull,
    push_output, recording, repository, save, stream)
from sumbi.core.records import Coverage
from sumbi.core.time import Window
from sumbi.events.adapters import claude_code, codex
from sumbi.events.references import tool_refs
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.outcomes.github.live import GitHubOutcomes, RepositoryUnreadable
from sumbi.outcomes.worker_github.workers import deliver_workers, text_summary
from sumbi.outcomes.local_verify.workers import deliver_local
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

    def test_unverified_workers_retain_denominator_and_lifetime_spend(self):
        from outcomes.test_github_unverified import unknown
        for number in (1, 2, 3):
            codex_worker(self.home, f"unit-{number}", self.repo, number)
        report = self.report([pull(1), unknown(pull(2), "checks_missing_required"),
            unknown(pull(3), "checks_none")])
        self.assertEqual(report["states"]["unverified"], 2)
        self.assertEqual(report["success_rate"]["numerator"], 1)
        self.assertEqual(report["success_rate"]["denominator"], 3)
        self.assertEqual(report["cost"]["per_success"]["total"]["numerator"], 300)
        self.assertEqual(report["state_reasons"]["unverified"], {
            "checks_missing_required": 1, "checks_none": 1})
        self.assertIn("unverified: checks_none 1", text_summary(report))

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
            ("push-input", 3, "git push -u origin worker-3", push_output("worker-3"), None,
                "pushed_branch", "strong"),
            ("push-output", 4, "git push origin HEAD", "To https://github.com/example/sample.git\n"
                " * [new branch] HEAD -> worker-4\n", None,
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
        codex_worker(self.home, "worker", self.repo, command="git push origin worker-1",
            output=push_output("worker-1"))
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
        self.assertEqual(report["states"], {**dict.fromkeys(
            ("success", "failed", "immature", "in_progress", "no_pr", "no_change"), 1),
            "unverified": 0})
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

    def test_create_intent_cannot_scope_without_a_checkout_origin(self):
        codex_worker(self.home, "worker", self.root / "missing",
            command="gh pr create --repo example/sample --head uncreated-worker")
        report = self.report([])
        self.assertEqual(report["units"], [])
        self.assertEqual(report["coverage"]["excluded_scope"]["repository_unconfirmed"], 1)

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

    def test_remote_authorship_without_local_edits_is_retained(self):
        codex_worker(self.home, "unchanged", self.repo, 1, edit=False)
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["success_rate"]["denominator"], 1)
        self.assertEqual(len(report["units"][0]["links"]), 1)

    def test_remote_create_commit_and_continued_links_prove_changes(self):
        older = pull(1)
        older["response"]["created_at"] = "2029-12-31T00:00:00Z"
        codex_worker(self.home, "push", self.repo, 1, edit=False)
        codex_worker(self.home, "commit", self.repo, edit=False,
            command="git commit -m 'Synthetic fix'", output="[worker-1 abc1234] Synthetic fix\n")
        codex_worker(self.home, "create", self.root / "unavailable", edit=False,
            command="ssh build.example.test 'gh pr create'",
            output="https://github.com/example/sample/pull/2")
        report = self.report([older, pull(2)])
        self.assertEqual(report["states"]["success"], 3)
        self.assertEqual(sum(r["links"][0]["role"] == "continued" for r in report["units"]), 2)

    def test_unmatched_remote_push_is_no_pr_without_local_edits(self):
        codex_worker(self.home, "push", self.root / "unavailable", 1, edit=False)
        report = self.report([])
        self.assertEqual(report["states"]["no_pr"], 1)
        self.assertEqual(report["success_rate"]["denominator"], 1)

    def test_weak_or_no_authorship_without_edits_is_no_change(self):
        for name, branch in (("weak", "worker-1"), ("none", None)):
            codex_worker(self.home, name, self.repo, edit=False, context_branch=branch)
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["no_change"], 2)
        self.assertEqual(report["success_rate"]["denominator"], 0)

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

    def test_temporal_judgment_exception_is_a_unit_coverage_gap(self):
        codex_worker(self.home, "first", self.repo, 1)
        codex_worker(self.home, "second", self.repo, 2)
        from sumbi.outcomes.worker_github.workers import judge
        for exception in (OverflowError(), RecursionError()):
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

    def test_structural_judgment_errors_are_not_swallowed(self):
        codex_worker(self.home, "worker", self.repo, 1)
        with patch("sumbi.outcomes.worker_github.workers.judge",
            side_effect=ValueError("Synthetic structural error")), self.assertRaisesRegex(
                ValueError, "Synthetic structural error"):
            self.report([pull(1)])

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
            command=["git", "-C", str(self.repo), "push", "origin", "worker-1"],
            output=push_output("worker-1"))
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
        rows = codex_worker(self.home, "worker", self.repo,
            command="git commit -m 'Synthetic fix'", output="[worker-1 abc1234] Synthetic fix\n")
        launch = next(r for r in rows if r.get("payload", {}).get("type") == "exec_command_begin")
        launch["payload"]["cwd"] = {"unsupported": "Synthetic value"}
        stream(self.home / ".codex/sessions/rollout-worker.jsonl", rows)
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["no_pr"], 1)
        self.assertEqual(report["units"][0]["links"], [])
        self.assertEqual(report["coverage"]["evidence_gaps"]["link_conflicts"], 1)

    def test_remote_push_wrappers_link_from_output(self):
        script = "cd /srv/sample\ngit push origin HEAD:worker-1\n"
        encoded = base64.b64encode(script.encode()).decode()
        commands = [
            "ssh build.example.test 'cd /srv/sample && git push origin HEAD:worker-1'",
            "wsl.exe -d Ubuntu-24.04 -- ssh build.example.test 'git push origin HEAD:worker-1'",
            "$script = @'\n" + script + "'@\n"
                "$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))\n"
                "wsl.exe -- sh -c 'echo " + encoded + " | base64 -d | ssh build.example.test bash'",
            ["wsl.exe", "--", "ssh", "build.example.test", script]]
        for command in commands:
            with self.subTest(command=command):
                codex_worker(self.home, "remote", self.repo, command=command,
                    output=push_output("worker-1"))
                report = self.report([pull(1)])
                self.assertEqual(report["states"]["success"], 1)
                self.assertEqual(report["units"][0]["links"][0]["evidence"], "pushed_branch")

    def test_remote_create_printed_url_scopes_other_checkout(self):
        other = self.root / "coordination"
        repository(other)
        subprocess.run(["git", "-C", str(other), "remote", "set-url", "origin",
            "https://github.com/example/coordination.git"], capture_output=True, check=True)
        codex_worker(self.home, "remote", other,
            command="ssh build.example.test 'cd /srv/sample && gh pr create --head worker-1'",
            output="Creating pull request for worker-1 into main in example/sample\n\n"
                "https://github.com/example/sample/pull/1\n")
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["units"][0]["start_scope"], "own_reference")
        self.assertEqual(report["units"][0]["links"][0]["evidence"], "pr_created")

    def test_push_names_scope_even_with_other_measured_cwd(self):
        other = self.root / "coordination"
        repository(other)
        subprocess.run(["git", "-C", str(other), "remote", "set-url", "origin",
            "https://github.com/example/coordination.git"], capture_output=True, check=True)
        codex_worker(self.home, "remote", other,
            command="ssh build.example.test 'git push origin HEAD:worker-1'",
            output=push_output("worker-1"))
        outcomes = self.outcomes([pull(1)])
        save(self.root / "outcomes/coordination.json", {
            **recording([pull(1)]), "repository": "example/coordination"})
        report = deliver_workers(self.home, self.window, [REPO, "example/coordination"],
            FixtureOutcomes(self.root / "outcomes"), salt=b"synthetic-key")
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["units"][0]["start_scope"], "start_origin")
        expected = deliver_workers(self.home, self.window, [REPO], outcomes, salt=b"synthetic-key")
        self.assertEqual(report["units"][0]["links"], expected["units"][0]["links"])
        self.assertEqual(expected["units"][0]["start_scope"], "own_reference")

    def test_remote_push_without_a_matching_pr_still_scopes_unit(self):
        codex_worker(self.home, "remote", self.root / "unavailable",
            command="ssh build.example.test 'git push origin HEAD:worker-1'",
            output=push_output("worker-1"))
        report = self.report([])
        self.assertEqual(report["units"][0]["start_scope"], "own_reference")
        self.assertEqual(report["states"]["no_pr"], 1)

    def test_remote_push_can_continue_older_pr(self):
        older = pull(1)
        older["response"]["created_at"] = "2029-12-31T00:00:00Z"
        codex_worker(self.home, "remote", self.root / "unavailable",
            command="ssh build.example.test 'git push origin HEAD:worker-1'",
            output=push_output("worker-1"))
        report = self.report([older])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["units"][0]["links"][0]["role"], "continued")

    def test_rejected_up_to_date_deleted_and_tag_results_are_not_authorship(self):
        for line in (" ! [rejected] HEAD -> worker-1 (non-fast-forward)",
            " ! [remote rejected] HEAD -> worker-1 (pre-receive hook declined)",
            "Everything up-to-date", " = [up to date] HEAD -> worker-1",
            " - [deleted] (none) -> worker-1", " * [new tag] worker-1 -> worker-1",
            "   abc1234..def5678 refs/tags/source -> refs/tags/worker-1"):
            with self.subTest(line=line):
                codex_worker(self.home, "remote", self.repo, command="git push origin worker-1",
                    output="To https://github.com/example/sample.git\n" + line + "\n")
                report = self.report([pull(1)])
                self.assertEqual(report["states"]["no_pr"], 1)
                self.assertEqual(report["units"][0]["links"], [])

    def test_push_results_require_successful_completed_execution(self):
        for code in (1, None):
            with self.subTest(exit_code=code):
                codex_worker(self.home, "remote", self.repo, exit_code=code,
                    command="ssh build.example.test 'git push origin HEAD:worker-1'",
                    output=push_output("worker-1"))
                report = self.report([pull(1)])
                self.assertEqual(report["units"][0]["links"], [])
                self.assertEqual(report["states"]["no_pr"], 1)
        rows = codex_worker(self.home, "remote", self.repo,
            command="ssh build.example.test 'git push origin HEAD:worker-1'",
            output=push_output("worker-1"))
        next(r for r in rows if r.get("payload", {}).get("type") == "exec_command_end"
            )["payload"].pop("exit_code")
        stream(self.home / ".codex/sessions/rollout-remote.jsonl", rows)
        self.assertEqual(self.report([pull(1)])["units"][0]["links"], [])

    def test_push_result_remote_blocks_do_not_cross_link(self):
        codex_worker(self.home, "remote", self.repo,
            command="ssh build.example.test 'git push origin --all'",
            output=push_output("worker-1") + push_output("worker-2", "other/sample")
                + "To https://example.test/sample.git\n   abc1234..def5678 HEAD -> worker-3\n")
        report = self.report([pull(1), pull(2), pull(3)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(len(report["units"][0]["links"]), 1)

    def test_outside_push_result_cannot_scope_unit(self):
        codex_worker(self.home, "remote", self.root / "unavailable",
            command="ssh build.example.test 'git push origin worker-1'",
            output=push_output("worker-1", "other/sample"))
        report = self.report([pull(1)])
        self.assertEqual(report["units"], [])
        self.assertEqual(report["coverage"]["excluded_scope"]["repository_unconfirmed"], 1)

    def test_all_changed_destination_refs_link_with_exact_heads(self):
        codex_worker(self.home, "remote", self.repo, command="ssh build.example.test 'git push'",
            output="To git@github.com:example/sample.git\n"
                " * [new branch] source -> worker-1\n"
                "   abc1234..def5678 source -> worker-2\n"
                " + abc1234...def5678 HEAD -> refs/heads/worker-3 (forced update)\n"
                " ! [rejected] source -> worker-4 (non-fast-forward)\n")
        report = self.report([pull(1), pull(2), pull(3), pull(4), pull(5, head="source")])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(len(report["units"][0]["links"]), 3)

    def test_opaque_push_output_and_plain_url_mentions_cannot_link_or_scope(self):
        for command, output in (("git push origin worker-1", "Synthetic completion"),
            ("ssh build.example.test 'cat brief.md'", "https://github.com/example/sample/pull/1")):
            with self.subTest(command=command):
                codex_worker(self.home, "remote", self.root / "unavailable",
                    command=command, output=output)
                report = self.report([pull(1)])
                self.assertEqual(report["units"], [])

    def test_claude_remote_push_and_create_use_paired_results(self):
        for command, output, evidence in (("ssh build.example.test 'git push'",
            push_output("worker-1"), "pushed_branch"),
            ("ssh build.example.test 'gh pr create'",
                "Creating pull request\nhttps://github.com/example/sample/pull/1\n", "pr_created")):
            with self.subTest(evidence=evidence):
                claude_worker(self.home, "remote", self.root / "unavailable", 1, seconds=11)
                path = self.home / ".claude/projects/group/main/subagents/agent-remote.jsonl"
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                rows[-2]["message"]["content"][0]["input"]["command"] = command
                rows[-1]["toolUseResult"].update(stdout=output, stderr="")
                rows[-1]["message"]["content"][0]["content"] = output
                stream(path, rows)
                report = self.report([pull(1)])
                self.assertEqual(report["states"]["success"], 1)
                self.assertEqual(report["units"][0]["links"][0]["evidence"], evidence)
                public = json.dumps(report)
                for private in ("build.example.test", "worker-1", "example/sample", "git push",
                    "Creating pull request", str(self.root)):
                    self.assertNotIn(private, public)

    def test_codex_completed_item_remote_push_result_links(self):
        rows = codex_worker(self.home, "remote", self.root / "unavailable")
        rows.append({"type": "event_msg", "timestamp": at(1, 11).isoformat(), "payload": {
            "type": "item_completed", "started_at_ms": int(at(1, 3).timestamp() * 1000),
            "item": {"id": "push", "type": "CommandExecution",
                "command": ["ssh", "build.example.test", "git push"], "exit_code": 0,
                "aggregated_output": push_output("worker-1")}}})
        stream(self.home / ".codex/sessions/rollout-remote.jsonl", rows)
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["units"][0]["links"][0]["evidence"], "pushed_branch")

    def claude_result(self, command, output, *, error=False, edit=False, envelope=None,
        background=False):
        claude_worker(self.home, "remote", self.repo, 1, seconds=11)
        path = self.home / ".claude/projects/group/main/subagents/agent-remote.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if not edit:
            rows.pop(1)
        rows[-2]["message"]["content"][0]["input"].update(command=command,
            run_in_background=background)
        rows[-1].pop("toolUseResult")
        if envelope is not None:
            rows[-1]["toolUseResult"] = envelope
        result = rows[-1]["message"]["content"][0]
        result.update(content=output)
        result.pop("is_error")
        if error is not None:
            result["is_error"] = error
        stream(path, rows)

    def test_envelope_less_claude_accepted_push_and_creation_are_strong(self):
        for command, output, evidence in (("ssh build.example.test 'git push'",
            push_output("worker-1"), "pushed_branch_output"),
            ("ssh build.example.test 'gh pr create'",
                "https://github.com/example/sample/pull/1", "pr_created_output")):
            with self.subTest(evidence=evidence):
                self.claude_result(command, output)
                report = self.report([pull(1)])
                self.assertEqual(report["states"]["success"], 1)
                link = report["units"][0]["links"][0]
                self.assertEqual((link["evidence"], link["strength"]), (evidence, "strong"))
                self.assertNotIn("authorship_exit_unknown", report["coverage"]["evidence_gaps"])

    def test_envelope_less_new_branch_continues_old_pr(self):
        self.claude_result("ssh build.example.test 'git push'",
            "To https://github.com/example/sample.git\n * [new branch] HEAD -> worker-1\n")
        older = pull(1)
        older["response"]["created_at"] = "2029-12-31T00:00:00Z"
        report = self.report([older])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["units"][0]["links"][0]["role"], "continued")

    def test_envelope_less_rejected_up_to_date_and_plain_urls_cannot_link(self):
        for command, output in (("ssh build.example.test 'git push'",
            "To https://github.com/example/sample.git\n ! [rejected] HEAD -> worker-1\n"),
            ("git push", "To https://github.com/example/sample.git\n"
                " ! [remote rejected] HEAD -> worker-1\n"),
            ("git push", "Everything up-to-date\n"),
            ("gh pr view 1", "https://github.com/example/sample/pull/1"),
            ("git commit -m 'Synthetic change'", "[worker-1 abc1234] Synthetic change\n")):
            with self.subTest(command=command, output=output):
                self.claude_result(command, output)
                report = self.report([pull(1)])
                self.assertEqual(report["states"]["no_change"], 1)
                self.assertEqual(report["units"][0]["links"], [])

    def test_claude_error_unknown_error_nonzero_and_deferred_cannot_link(self):
        for options in ({"error": True}, {"error": None}, {"error": "false"},
            {"envelope": {"exitCode": 1}}, {"background": True},
            {"envelope": {"interrupted": True}}, {"envelope": {"backgroundTaskId": "task"}}):
            with self.subTest(options=options):
                self.claude_result("ssh build.example.test 'git push'", push_output("worker-1"),
                    **options)
                report = self.report([pull(1)])
                self.assertEqual(report["units"][0]["links"], [])
                self.assertEqual(report["states"]["no_change"], 1)

    def test_envelope_less_authorship_does_not_pass_local_verification(self):
        self.claude_result("bash scripts/check.sh", push_output("worker-1"), edit=True)
        report = deliver_local(self.home, self.window, self.repo, verify=["scripts/check.sh"],
            agents=["claude-code"], salt=b"synthetic-key")
        self.assertEqual(report["states"]["success"], 0)
        measured = collect(claude_code, self.home, self.window, Coverage(), worker_links=True)
        self.assertIsNone(next(iter(measured[0].commands.values())).exit_code)

    def test_owner_scope_measures_two_evidenced_repositories_lazily(self):
        other = "example/second"
        for name, repo in (("first", REPO), ("second", other)):
            codex_worker(self.home, name, self.root / "unavailable", edit=False,
                command="ssh build.example.test 'git push'", output=push_output("worker-1", repo))
        codex_worker(self.home, "foreign", self.root / "unavailable", edit=False,
            command="git push", output=push_output("worker-1", "foreign/sample"))
        codex_worker(self.home, "mention", self.root / "unavailable", edit=False,
            command="gh pr view 1", output="https://github.com/example/unmentioned/pull/1")
        codex_worker(self.home, "weak", self.repo, edit=False, context_branch="worker-1")
        recorded = self.outcomes([pull(1)])
        save(self.root / "outcomes/second.json", {**recording([pull(1)]), "repository": other})
        recorded = FixtureOutcomes(self.root / "outcomes")
        calls = []
        def live(dispatches):
            calls.extend(r for d in dispatches for r in d.repos)
            return recorded
        report = deliver_workers(self.home, self.window, [], live, repo_owners=["EXAMPLE"])
        self.assertEqual(sorted(calls), [REPO, other])
        self.assertEqual(report["states"]["success"], 2)
        self.assertEqual(report["states"]["no_change"], 1)
        self.assertEqual(report["coverage"]["excluded_scope"]["repository_unconfirmed"], 2)
        self.assertEqual(len(report["coverage"]["repositories"]), 2)
        for private in ("example/second", "foreign/sample", "build.example.test", "worker-1"):
            self.assertNotIn(private, json.dumps(report))

    def test_owner_scope_alone_does_not_admit_cwd_or_weak_evidence(self):
        codex_worker(self.home, "weak", self.repo, context_branch="worker-1")
        provider = Mock(side_effect=AssertionError("No outcome fetch expected"))
        report = deliver_workers(self.home, self.window, [], provider, repo_owners=["example"])
        self.assertEqual(report["units"], [])
        provider.assert_not_called()

    def test_owner_unreadable_repository_is_counted_and_readable_links_survive(self):
        denied = "example/second"
        codex_worker(self.home, "readable", self.repo, 1)
        codex_worker(self.home, "unreadable", self.repo, edit=False,
            command="gh pr create", output=f"https://github.com/{denied}/pull/1")
        codex_worker(self.home, "mixed", self.repo, edit=False, command="git push",
            output=push_output("worker-1", denied) + push_output("worker-1"))
        rows = codex_worker(self.home, "mixed-commit", self.repo, edit=False,
            command="git commit -m 'Synthetic change'", output="[worker-1 abc1234] Synthetic change")
        rows.extend(execution("git push", push_output("worker-1", denied), self.repo,
            identity="second-push", start=12, end=13))
        stream(self.home / ".codex/sessions/rollout-mixed-commit.jsonl", rows)
        recorded = self.outcomes([pull(1)])
        isolate_github_destinations(self)
        for code in (404, 403):
            with self.subTest(code=code):
                calls = []
                def provider(dispatches):
                    repo = dispatches[0].repos[0]
                    calls.append(repo)
                    if repo == denied:
                        return GitHubOutcomes(dispatches, cache=self.root / "cache", head_refs=True)
                    return recorded
                def response(request, **kwargs):
                    raise urllib.error.HTTPError(request.full_url, code, "Synthetic denied", {},
                        io.BytesIO(b'{"message":"Not accessible"}'))
                with patch("sumbi.outcomes.github.live.github_token", return_value="synthetic-token"), \
                    patch("urllib.request.OpenerDirector.open", side_effect=response), \
                    patch("sumbi.outcomes.github.live.time.sleep") as sleep:
                    report = deliver_workers(self.home, self.window, [], provider,
                        repo_owners=["example"], salt=b"synthetic-key")
                sleep.assert_not_called()
                self.assertEqual(calls.count(denied), 1)
                self.assertEqual(report["states"]["success"], 3)
                self.assertEqual(report["success_rate"]["denominator"], 3)
                self.assertEqual(report["coverage"]["excluded_scope"]["repository_unreadable"], 1)
                self.assertEqual(sum(r.get("reason") == "repository_unreadable"
                    for r in report["coverage"]["repositories"]), 1)
                excluded = next(r for r in report["units"] if r["reason"] == "repository_unreadable")
                self.assertEqual(excluded["links"], [])
                self.assertEqual(report["coverage"]["missing_prs"], 0)
                self.assertEqual(report["excluded_worker_spend"]["observed_total"], 100)
                for private in (REPO, denied, "worker-1", "Not accessible"):
                    self.assertNotIn(private, json.dumps(report))
                with patch("sumbi.outcomes.github.live.github_token", return_value="synthetic-token"), \
                    patch("urllib.request.OpenerDirector.open", side_effect=response):
                    with self.assertRaisesRegex(RepositoryUnreadable, f"HTTP {code}"):
                        deliver_workers(self.home, self.window, [denied], provider,
                            repo_owners=["example"])

    def test_owner_rate_limit_and_transient_failures_still_abort(self):
        codex_worker(self.home, "worker", self.repo, 1)
        isolate_github_destinations(self)
        for code in (403, 429, 503):
            with self.subTest(code=code):
                def response(request, **kwargs):
                    raise urllib.error.HTTPError(request.full_url, code, "Synthetic failure",
                        {"Retry-After": "0", "X-RateLimit-Remaining": "0"},
                        io.BytesIO(b'{"message":"rate limit exceeded"}'))
                with patch("sumbi.outcomes.github.live.github_token", return_value="synthetic-token"), \
                    patch("urllib.request.OpenerDirector.open", side_effect=response) as opened, \
                    patch("sumbi.outcomes.github.live.time.sleep"):
                    with self.assertRaisesRegex(ValueError, f"HTTP {code}") as raised:
                        deliver_workers(self.home, self.window, [], lambda ds:
                            GitHubOutcomes(ds, cache=self.root / "cache"), repo_owners=["example"])
                self.assertNotIsInstance(raised.exception, RepositoryUnreadable)
                self.assertEqual(opened.call_count, 4 if code in (403, 429) else 1)

    def test_owner_repositories_discovered_in_extended_lifetime_are_fetched_once(self):
        codex_worker(self.home, "worker", self.repo, 1, edit=False)
        path = self.home / ".codex/sessions/rollout-worker.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows.extend(execution("ssh build.example.test 'git push'",
            push_output("worker-2", "example/second"), self.repo, day=2,
            identity="later", end=11))
        stream(path, rows)
        recorded = self.outcomes([pull(1)])
        save(self.root / "outcomes/second.json", {
            **recording([pull(2, day=2)]), "repository": "example/second"})
        recorded = FixtureOutcomes(self.root / "outcomes")
        calls = []
        def live(dispatches):
            calls.append(tuple(r for d in dispatches for r in d.repos))
            return recorded
        report = deliver_workers(self.home, self.window, [], live, repo_owners=["example"])
        self.assertEqual(calls, [(REPO,), ("example/second",)])
        self.assertEqual(len(report["units"][0]["links"]), 2)
        self.assertEqual(report["states"]["success"], 1)

    def test_owner_first_authorship_after_dispatch_window_still_scopes_worker(self):
        rows = codex_worker(self.home, "late", self.root / "unavailable", edit=False)
        rows.extend(execution("ssh build.example.test 'git push'", push_output("worker-1"),
            self.repo, day=2))
        stream(self.home / ".codex/sessions/rollout-late.jsonl", rows)
        report = deliver_workers(self.home, self.window, [], self.outcomes([pull(1, day=2)]),
            repo_owners=["example"])
        self.assertEqual(report["states"]["success"], 1)

    def test_delivery_state_reason_breakdown_includes_pending_and_excluded_units(self):
        for i in (1, 2):
            codex_worker(self.home, str(i), self.repo, i, edit=False)
        codex_worker(self.home, "unchanged", self.repo, edit=False)
        pulls = [pull(i) for i in (1, 2)]
        for p in pulls:
            p["checks_at_merge"]["statuses"] = []
        report = self.report(pulls)
        self.assertEqual(report["state_reasons"]["unverified"], {"checks_missing_required": 2})
        self.assertEqual(report["state_reasons"]["in_progress"], {})
        self.assertEqual(report["state_reasons"]["no_change"], {"no_known_edit": 1})
        self.assertIn("unverified: checks_missing_required 2", text_summary(report))
        self.assertIn("no_change: no_known_edit 1", text_summary(report))


if __name__ == "__main__":
    unittest.main()
