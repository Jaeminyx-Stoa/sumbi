"""Synthetic scratch-only, ignored, multi-checkout and mixed-agent worker edits."""

from datetime import timedelta
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from support import IsolatedTemporaryDirectory
from worker_github_fixtures import (REPO, at, codex_worker, pull, push_output,
    recording, repository, save, stream)
from sumbi.core.records import Coverage
from sumbi.core.paths import normalize_path
from sumbi.core.time import Window
from sumbi.events.adapters import codex
from sumbi.measure.attribution import Attributor
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.outcomes.worker_github.edits import EditScope
from sumbi.outcomes.worker_github.workers import deliver_workers
from sumbi.sessions.builder import collect


class WorkerEditScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.repo = self.root / "home", self.root / "repository"
        repository(self.repo)
        self.window = Window(at(1) - timedelta(hours=1), at(2) - timedelta(hours=1))

    def claude(self, identity, targets, *, tool="Write", ship=False):
        common = {"sessionId": "main", "agentId": identity, "cwd": str(self.repo)}
        rows = [{**common, "type": "user", "timestamp": at(1).isoformat(),
            "message": {"content": "Synthetic dispatch"}}]
        for index, target in enumerate(targets):
            args = {} if target is None else {
                "notebook_path" if tool == "NotebookEdit" else "file_path": str(target)}
            rows.append({**common, "type": "assistant", "timestamp": at(1, 2 + index).isoformat(),
                "message": {"id": str(index), "content": [{"type": "tool_use", "name": tool,
                    "id": "edit-" + str(index), "input": args}]}})
        if ship:
            rows.extend([{**common, "type": "assistant", "timestamp": at(1, 10).isoformat(),
                "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "ship",
                    "input": {"command": "git push"}}]}},
                {**common, "type": "user", "timestamp": at(1, 11).isoformat(),
                    "toolUseResult": {"stdout": push_output("worker-1"), "stderr": "",
                        "interrupted": False}, "message": {"content": [{"type": "tool_result",
                            "tool_use_id": "ship", "content": push_output("worker-1"),
                            "is_error": False}]}}])
        stream(self.home / f".claude/projects/group/main/subagents/agent-{identity}.jsonl", rows)

    def codex(self, identity, patch_text, *, cwd=None, arguments=False):
        rows = codex_worker(self.home, identity, self.repo, kind="subagent")
        payload = rows[2]["payload"]
        if arguments:
            payload.update(type="function_call", arguments=json.dumps({
                "patch": patch_text, "cwd": str(cwd or self.repo)}))
            payload.pop("input")
        else:
            payload["input"] = patch_text
        stream(self.home / f".codex/sessions/rollout-{identity}.jsonl", rows)

    def report(self, *, owners=False, extra=()):
        directory = self.root / "outcomes"
        save(directory / "repository.json", recording([pull(1)]))
        for repo in extra:
            save(directory / (repo.split("/")[1] + ".json"), {**recording([]), "repository": repo})
        return deliver_workers(self.home, self.window, [] if owners else [REPO, *extra],
            FixtureOutcomes(directory), repo_owners=["example"] if owners else ())

    def test_claude_tools_writing_only_scratch_are_no_change(self):
        for index, tool in enumerate(("Edit", "Write", "MultiEdit", "NotebookEdit")):
            self.claude(str(index), [self.root / "temporary" / "brief.txt"], tool=tool)
        report = self.report()
        self.assertEqual(report["states"]["no_change"], 4)
        self.assertEqual(report["coverage"]["evidence_gaps"]["edit_outside_scope"], 4)

    def test_ignored_missing_folder_is_no_change_but_tracked_plus_scratch_is_changed(self):
        (self.repo / ".gitignore").write_text("ignored/\ntracked.py\n", encoding="utf-8")
        (self.repo / "tracked.py").write_text("pass\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-f", "tracked.py"], check=True,
            capture_output=True)
        self.claude("ignored", [self.repo / "ignored" / "deleted.txt"])
        self.claude("mixed", ["tracked.py", self.root / "scratch" / "brief.txt"])
        report = self.report()
        self.assertEqual((report["states"]["no_change"], report["states"]["no_pr"]), (1, 1))
        gaps = report["coverage"]["evidence_gaps"]
        self.assertEqual((gaps["edit_ignored"], gaps["edit_outside_scope"]), (1, 1))

    def test_relative_patch_extracts_add_update_delete_and_move_targets(self):
        text = ("*** Begin Patch\n*** Add File: added.py\n+pass\n"
            "*** Update File: source.py\n*** Move to: moved.py\n@@\n-old\n+new\n"
            "*** Delete File: deleted.py\n*** End Patch")
        self.codex("multi", text)
        session = collect(codex, self.home, self.window, Coverage())[0]
        self.assertEqual(session.edit_targets["edit"], tuple(normalize_path(str(self.repo / name))
            for name in ("added.py", "source.py", "moved.py", "deleted.py")))
        self.assertEqual(session.edits, {"edit": at(1, 2)})
        self.assertEqual(self.report()["states"]["no_pr"], 1)

    def test_clone_worktree_and_unmeasured_checkout(self):
        clone = self.root / "clone"
        repository(clone)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Synthetic",
            "-c", "user.email=synthetic@example.test", "commit", "--allow-empty", "-qm", "Synthetic"],
            check=True, capture_output=True)
        sibling = self.root / "sibling"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-qb", "synthetic", str(sibling)],
            check=True, capture_output=True)
        other = self.root / "other"
        repository(other)
        subprocess.run(["git", "-C", str(other), "remote", "set-url", "origin",
            "https://github.com/example/unmeasured.git"], check=True, capture_output=True)
        self.claude("clone", [clone / "code.py"])
        self.claude("sibling", [sibling / "code.py"])
        self.claude("other", [other / "code.py"])
        self.assertEqual((self.report()["states"]["no_pr"], self.report()["states"]["no_change"]), (2, 1))

    def test_unknown_targets_remain_changes_and_never_prove_unshipped(self):
        self.claude("missing", [None])
        self.codex("malformed", "*** Begin Patch\n*** Add File: code.py\ninvalid\n*** End Patch")
        # Add an observed interactive root so only target uncertainty prevents unshipped.
        codex_worker(self.home, "main", self.repo, kind="vscode", edit=False)
        report = self.report()
        self.assertEqual(report["states"]["no_pr"], 2)
        self.assertTrue(all(r["reason"] == "no_linked_pr" for r in report["units"]))
        self.assertEqual(report["coverage"]["evidence_gaps"]["edit_target_unknown"], 2)

    def test_mixed_agents_repositories_and_call_cwd(self):
        second = self.root / "second"
        repository(second)
        subprocess.run(["git", "-C", str(second), "remote", "set-url", "origin",
            "https://github.com/example/second.git"], check=True, capture_output=True)
        self.claude("scratch", [self.root / "scratch.txt"])
        self.claude("second", [second / "code.py"])
        self.codex("code", "*** Begin Patch\n*** Add File: code.py\n+pass\n*** End Patch",
            cwd=second, arguments=True)
        report = self.report(extra=["example/second"])
        self.assertEqual((report["states"]["no_pr"], report["states"]["no_change"]), (2, 1))
        public = json.dumps(report)
        for private in (str(self.root), REPO, "example/second", "scratch.txt", "code.py"):
            self.assertNotIn(private, public)

    def test_strong_shipping_still_changes_scratch_only_workers_and_owner_scope(self):
        self.claude("shipped", [self.root / "scratch.txt"], ship=True)
        self.claude("scratch", [self.root / "notes.txt"])
        report = self.report(owners=True)
        self.assertEqual((report["states"]["success"], report["states"]["no_change"]), (1, 1))

    def test_known_in_scope_edit_with_complete_root_proves_unshipped(self):
        codex_worker(self.home, "main", self.repo, kind="vscode", edit=False)
        self.codex("code", "*** Begin Patch\n*** Update File: code.py\n@@\n-old\n+new\n*** End Patch")
        self.assertEqual(self.report()["units"][0]["reason"], "chain_unshipped")

    def test_git_failures_and_unreadable_targets_remain_unknown_and_queries_are_cached(self):
        scope = EditScope(Attributor([]), [REPO])
        target = str(self.repo / "code.py")
        with patch("subprocess.run", side_effect=OSError("Synthetic query failure")) as query:
            self.assertEqual(scope.target(target), "edit_target_unknown")
            self.assertEqual(scope.target(target), "edit_target_unknown")
            self.assertEqual(query.call_count, 1)
        scope = EditScope(Attributor([]), [REPO])
        with patch.object(Path, "stat", side_effect=PermissionError("Synthetic unreadable checkout")):
            self.assertEqual(scope.target(target), "edit_target_unknown")
        scope = EditScope(Attributor([]), [REPO])
        scope.attributor.origin(str(self.repo))
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 128, "", "Synthetic failure")):
            self.assertEqual(scope.target(target), "edit_target_unknown")

    def test_broken_checkout_and_unparsable_origin_are_not_outside_scope(self):
        target = str(self.repo / "code.py")
        for results in (
            [subprocess.CompletedProcess([], 128, "", "fatal: not a git repository: synthetic")],
            [subprocess.CompletedProcess([], 0, "true\n", ""),
                subprocess.CompletedProcess([], 0, "unsupported-synthetic-origin\n", "")],
            [subprocess.CompletedProcess([], 0, "true\n", ""),
                subprocess.CompletedProcess([], 1, "", "")]):
            scope = EditScope(Attributor([]), [REPO])
            with patch("subprocess.run", side_effect=results):
                self.assertEqual(scope.target(target), "edit_target_unknown")

    def test_multi_target_coverage_counts_once_per_edit_and_ignore_queries_are_cached(self):
        (self.repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        self.codex("multi", "*** Begin Patch\n*** Add File: ignored/a.py\n+pass\n"
            "*** Add File: ignored/b.py\n+pass\n*** End Patch")
        report = self.report()
        self.assertEqual(report["states"]["no_change"], 1)
        self.assertEqual(report["coverage"]["evidence_gaps"]["edit_ignored"], 1)
        scope = EditScope(Attributor([]), [REPO])
        with patch("subprocess.run", wraps=subprocess.run) as query:
            for _ in range(2):
                self.assertEqual(scope.target(str(self.repo / "ignored/a.py")), "edit_ignored")
            self.assertEqual(sum("check-ignore" in call.args[0] for call in query.call_args_list), 1)
