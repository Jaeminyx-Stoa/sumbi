"""Synthetic worker dispatch, own-session links, PR states and privacy boundaries."""

from datetime import timedelta
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from support import IsolatedTemporaryDirectory
from worker_github_fixtures import (REPO, at, codex_worker, claude_worker, pull,
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
            ("url-input", 1, "gh pr view https://github.com/example/sample/pull/1", None, None,
                "pr_url", "explicit"),
            ("url-output", 2, "gh pr create --head worker-2",
                "https://github.com/example/sample/pull/2", None, "pr_url", "explicit"),
            ("push-input", 3, "git push -u origin worker-3", None, None, "pushed_branch", "strong"),
            ("push-output", 4, "git push origin HEAD", " * [new branch] HEAD -> worker-4\n", None,
                "pushed_branch", "strong"),
            ("created", 5, "git switch -c worker-5", None, None, "created_branch", "strong"),
            ("cwd", 6, None, None, "worker-6", "cwd_branch", "weak"),
            ("query", 7, "git branch --show-current", "worker-7\n", None, "cwd_branch", "weak")]
        for identity, number, command, output, context, _, _ in cases:
            codex_worker(self.home, identity, self.repo, command=command, output=output,
                context_branch=context)
        report = self.report([pull(n) for _, n, *_ in cases])
        evidence = sorted((r["links"][0]["evidence"], r["links"][0]["strength"])
            for r in report["units"])
        self.assertEqual(evidence, sorted((e, s) for *_, e, s in cases))
        self.assertEqual(report["success_rate"]["denominator"], 5)
        public = json.dumps(report)
        for value in (str(self.repo), "worker-1", "example/sample", "/pull/1", "git push"):
            self.assertNotIn(value, public)

    def test_pr_opened_by_main_uses_worker_branch_without_borrowing_main_evidence(self):
        codex_worker(self.home, "worker", self.repo, command="git switch -c worker-1")
        codex_worker(self.home, "unlinked", self.repo)
        codex_worker(self.home, "main", self.repo, kind="vscode",
            command="gh pr create --head worker-1", output="https://github.com/example/sample/pull/1")
        report = self.report([pull(1)])
        self.assertEqual(report["states"]["success"], 1)
        self.assertEqual(report["states"]["no_pr"], 1)
        self.assertEqual(report["success_rate"]["denominator"], 2)
        self.assertEqual(next(r for r in report["units"] if r["state"] == "success")
            ["links"][0]["evidence"], "created_branch")

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
        codex_worker(self.home, "worker", self.repo, command="gh pr view "
            "https://github.com/example/sample/pull/1 https://github.com/example/sample/pull/2")
        report = self.report([pull(1), pull(2, merged=False)])
        self.assertEqual(report["states"]["failed"], 1)
        self.assertEqual(len(report["units"][0]["prs"]), 2)

    def test_explicit_pr_can_scope_worker_without_start_origin(self):
        missing = self.root / "missing"
        codex_worker(self.home, "worker", missing, command="gh pr view "
            "https://github.com/example/sample/pull/1")
        report = self.report([pull(1)])
        self.assertEqual(report["units"][0]["start_scope"], "own_reference")
        self.assertEqual(report["states"]["success"], 1)

    def test_explicit_repo_scopes_no_pr_without_a_checkout_origin(self):
        codex_worker(self.home, "worker", self.root / "missing",
            command="gh pr create --repo example/sample --head uncreated-worker")
        report = self.report([])
        self.assertEqual(report["units"][0]["start_scope"], "own_reference")
        self.assertEqual(report["states"]["no_pr"], 1)

    def test_structured_push_result_names_a_strong_branch(self):
        codex_worker(self.home, "worker", self.repo, command="git push origin HEAD",
            output={"branch": "worker-1"})
        report = self.report([pull(1)])
        self.assertEqual(report["units"][0]["links"][0]["evidence"], "pushed_branch")

    def test_strongest_link_wins_per_pr_but_other_weak_constituents_remain_weak(self):
        codex_worker(self.home, "same-pr", self.repo, 1, context_branch="worker-1",
            output="https://github.com/example/sample/pull/1")
        codex_worker(self.home, "mixed", self.repo, context_branch="worker-2",
            command="gh pr view https://github.com/example/sample/pull/1")
        report = self.report([pull(1), pull(2)])
        strong = next(r for r in report["units"] if not r["weak_link"])
        weak = next(r for r in report["units"] if r["weak_link"])
        self.assertEqual(strong["links"][0]["evidence"], "pr_url")
        self.assertEqual(len(strong["links"]), 1)
        self.assertEqual({r["strength"] for r in weak["links"]}, {"explicit", "weak"})
        self.assertEqual(report["success_rate"]["denominator"], 1)

    def test_branch_reuse_and_out_of_scope_pr_cannot_supply_success(self):
        codex_worker(self.home, "worker", self.repo, 1,
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
        codex_worker(self.home, "worker", self.repo,
            command="gh pr view https://github.com/example/sample/pull/1")
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


if __name__ == "__main__":
    unittest.main()
