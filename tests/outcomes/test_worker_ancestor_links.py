"""Synthetic in-process chains in a mixed-agent, multi-repository log home."""

from collections import Counter
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import unittest

from sumbi.core.values import timestamp

from support import IsolatedTemporaryDirectory
from worker_github_fixtures import (REPO, at, codex_worker, execution, pull,
    push_output, recording, repository, save, stream)
from sumbi.core.time import Window
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.outcomes.worker_github.chains import MAX_ANCESTORS, ancestor_chain
from sumbi.outcomes.worker_github.workers import deliver_workers
from sumbi.sessions.session import Session


class AncestorLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.repo = self.root / "home", self.root / "repository"
        repository(self.repo)
        self.window = Window(at(1) - timedelta(hours=1), at(2) - timedelta(hours=1))

    def codex(self, identity, parent=None, branch=None, start=0, **options):
        rows = codex_worker(self.home, identity, options.pop("cwd", self.repo),
            kind="subagent" if parent else "vscode", context_branch=branch, **options)
        if options.get("edit", True):
            rows[2]["payload"]["input"] = "*** Begin Patch\n*** Add File: synthetic.py\n+pass\n*** End Patch"
        if parent:
            rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = parent
        for row in rows:
            row["timestamp"] = (timestamp(row["timestamp"]) + timedelta(seconds=start)).isoformat()
        stream(self.home / f".codex/sessions/rollout-{identity}.jsonl", rows)
        return rows

    def claude(self, identity, parent=None, branch=None, command=None, output=None, start=0):
        raw = f"{parent}:subagent:{identity}" if parent else identity
        common = {"sessionId": parent or identity, "cwd": str(self.repo)}
        if parent:
            common["agentId"] = identity
        rows = [{**common, "type": "user", "timestamp": at(1, start).isoformat(),
            "gitBranch": branch, "message": {"content": "Synthetic dispatch"}},
            {**common, "type": "assistant", "timestamp": at(1, start + 2).isoformat(),
                "version": "1.0", "message": {"id": "edit", "model": "synthetic-claude",
                    "usage": {"input_tokens": 90, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0, "output_tokens": 10},
                    "content": [{"type": "tool_use", "id": "edit", "name": "Edit",
                        "input": {"file_path": "synthetic.py"}}]}}]
        if command:
            rows.extend([{**common, "type": "assistant", "timestamp": at(1, 20).isoformat(),
                "message": {"content": [{"type": "tool_use", "id": "ship", "name": "Bash",
                    "input": {"command": command}}]}},
                {**common, "type": "user", "timestamp": at(1, 21).isoformat(),
                    "toolUseResult": {"stdout": output, "stderr": "", "interrupted": False},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "ship",
                        "content": output, "is_error": False}]}}])
        path = (self.home / f".claude/projects/group/dispatch/subagents/agent-{identity}.jsonl"
            if parent else self.home / f".claude/projects/group/{identity}.jsonl")
        stream(path, rows)
        return raw

    def report(self, pulls=(), owners=False, extra=()):
        directory = self.root / "outcomes"
        save(directory / "repository.json", recording(list(pulls)))
        for repo, entries in extra:
            save(directory / (repo.split("/")[1] + ".json"),
                {**recording(entries), "repository": repo})
        return deliver_workers(self.home, self.window, [] if owners else [REPO, *[r for r, _ in extra]],
            FixtureOutcomes(directory), repo_owners=["example"] if owners else (), salt=b"synthetic-key")

    def unit(self, report, agent="codex"):
        return next(r for r in report["units"] if r["agent"] == agent)

    def test_mixed_home_links_each_child_only_within_its_agent(self):
        self.codex("root", command="ssh build.example.test 'git push'", output=push_output("worker-1"))
        self.codex("child", parent="root", branch="worker-1")
        self.claude("root", command="wsl sh -lc 'gh pr create'",
            output=f"https://github.com/{REPO}/pull/2")
        self.claude("child", parent="root", branch="worker-2")
        report = self.report([pull(1), pull(2)])
        self.assertEqual(report["states"]["success"], 2)
        self.assertEqual(report["coverage"]["link_evidence_counts"],
            {"ancestor_pr_created": 1, "ancestor_pushed_branch": 1})
        for row in report["units"]:
            self.assertEqual(row["links"][0]["strength"], "strong")
        public = json.dumps(report)
        for private in (str(self.root), REPO, "worker-1", "build.example.test", "git push"):
            self.assertNotIn(private, public)

    def test_grandchild_uses_root_shipping_for_both_native_agents(self):
        self.codex("root", command="powershell -Command 'git push'", output=push_output("worker-1"))
        self.codex("middle", parent="root", branch="worker-1")
        self.codex("leaf", parent="middle", branch="worker-1")
        self.claude("root", command="gh pr create", output=f"https://github.com/{REPO}/pull/2")
        middle = self.claude("middle", parent="root", branch="worker-2")
        self.claude("leaf", parent=middle, branch="worker-2")
        report = self.report([pull(1), pull(2)])
        self.assertEqual(report["states"]["success"], 4)
        self.assertTrue(all(len(r["links"]) == 1 for r in report["units"]))

    def test_two_parent_branches_keep_nonmatching_constituent_weak(self):
        self.codex("root", command="git push", output=push_output("worker-1") + push_output("worker-2"))
        self.codex("child", parent="root", branch="worker-1")
        row = self.unit(self.report([pull(1), pull(2, merged=False)]))
        self.assertEqual(row["state"], "failed")
        self.assertEqual(sorted(link["strength"] for link in row["links"]), ["strong", "weak"])
        self.assertTrue(row["weak_link"])

    def test_unknown_worker_branch_or_missing_creation_head_is_weak(self):
        self.codex("root", command="gh pr create", output=f"https://github.com/{REPO}/pull/1")
        self.codex("child", parent="root")
        entry = pull(1)
        row = self.unit(self.report([entry]))
        self.assertTrue(row["weak_link"])
        self.codex("child", parent="root", branch="worker-1")
        entry["response"]["head"].pop("ref")
        row = self.unit(self.report([entry]))
        self.assertTrue(row["weak_link"])

    def test_before_dispatch_and_scan_cutoff_cannot_supply_links(self):
        self.codex("root", command="git push", output=push_output("worker-1"))
        rows = self.codex("child", parent="root", start=12)
        # Retain own context and edit observations after its synthetic dispatch.
        for row in rows[1:]:
            row["timestamp"] = at(1, 14 if row.get("payload", {}).get("name") == "apply_patch" else 13).isoformat()
        stream(self.home / ".codex/sessions/rollout-child.jsonl", rows)
        row = self.unit(self.report([pull(1)]))
        self.assertEqual((row["state"], row["reason"], row["links"]), ("no_pr", "chain_unshipped", []))
        rows = self.codex("root", edit=False)
        rows.extend(execution("git push", push_output("worker-1"), self.repo, day=2, start=0, end=1))
        stream(self.home / ".codex/sessions/rollout-root.jsonl", rows)
        self.codex("child", parent="root")
        directory = self.root / "short"
        save(directory / "repository.json", recording([pull(1)], end=self.window.until.isoformat()))
        report = deliver_workers(self.home, self.window, [REPO], FixtureOutcomes(directory))
        self.assertEqual(self.unit(report)["reason"], "chain_unshipped")

    def test_command_started_before_dispatch_can_supply_a_later_result(self):
        self.codex("root", command="git push", output=push_output("worker-1"))
        self.codex("child", parent="root", branch="worker-1", start=5)
        self.assertEqual(self.unit(self.report([pull(1)]))["state"], "success")

    def test_result_at_dispatch_is_included_and_closed_pr_actions_are_excluded(self):
        self.codex("root", command="git push", output=push_output("worker-1"))
        self.codex("child", parent="root", branch="worker-1", start=11)
        self.assertEqual(self.unit(self.report([pull(1)]))["state"], "success")
        self.codex("child", parent="root")
        entry = pull(1, seconds=10)
        self.assertEqual(self.unit(self.report([entry]))["reason"], "no_linked_pr")

    def test_pre_dispatch_unknown_authorship_does_not_poison_observed_nonshipping(self):
        self.codex("root", command="git push", output=push_output("worker-1"), exit_code=None)
        self.codex("child", parent="root", start=12)
        self.assertEqual(self.unit(self.report())["reason"], "chain_unshipped")

    def test_unknown_authorship_after_dispatch_cannot_prove_nonshipping(self):
        self.codex("root", command="git push", output=push_output("worker-1"), exit_code=None)
        self.codex("child", parent="root")
        report = self.report()
        self.assertEqual(self.unit(report)["reason"], "no_linked_pr")
        self.assertEqual(report["coverage"]["evidence_gaps"]["authorship_exit_unknown"], 1)

    def test_successful_authorship_with_missing_result_stays_risky(self):
        self.codex("root", command="git push origin worker-1", output="")
        self.codex("child", parent="root")
        report = self.report()
        self.assertEqual(self.unit(report)["reason"], "no_linked_pr")
        self.assertEqual(report["coverage"]["evidence_gaps"], {"authorship_result_missing": 1})

    def test_other_repository_cannot_link_and_shipping_still_prevents_unshipped(self):
        other = "example/second"
        second = self.root / "second"
        repository(second)
        subprocess.run(["git", "-C", str(second), "remote", "set-url", "origin",
            f"https://github.com/{other}.git"], check=True, capture_output=True)
        self.codex("root", cwd=second, command="git push", output=push_output("worker-1", other))
        self.codex("child", parent="root", branch="worker-1")
        report = self.report([pull(1)], extra=[(other, [pull(1)])])
        row = self.unit(report)
        # Own cwd-branch remains weak; ancestor evidence from the other repository is absent.
        self.assertEqual([link["evidence"] for link in row["links"]], ["cwd_branch"])
        self.codex("child", parent="root")
        row = self.unit(self.report([pull(1)], extra=[(other, [pull(1)])]))
        self.assertEqual(row["reason"], "no_linked_pr")

    def test_failed_push_intent_cannot_widen_worker_repository_scope(self):
        other = "example/second"
        self.codex("root", command="git push", output=push_output("worker-1", other))
        self.codex("child", parent="root", branch="worker-1",
            command=f"git push https://github.com/{other}.git worker-1", output="", exit_code=1)
        row = self.unit(self.report([pull(1)], extra=[(other, [pull(1)])]))
        self.assertEqual([link["evidence"] for link in row["links"]], ["cwd_branch"])

    def test_missing_parent_and_parent_gaps_remain_linking_risk(self):
        self.codex("child", parent="missing")
        report = self.report()
        self.assertEqual(self.unit(report)["reason"], "no_linked_pr")
        self.assertEqual(report["coverage"]["evidence_gaps"], {"ancestor_log_missing": 1})
        rows = self.codex("missing")
        rows[2]["timestamp"] = None
        stream(self.home / ".codex/sessions/rollout-missing.jsonl", rows)
        self.assertEqual(self.unit(self.report())["reason"], "no_linked_pr")

    def test_subagent_without_parent_id_cannot_prove_a_root(self):
        rows = self.codex("child", parent="root")
        rows[0]["payload"]["source"]["subagent"]["thread_spawn"].pop("parent_thread_id")
        stream(self.home / ".codex/sessions/rollout-child.jsonl", rows)
        report = self.report()
        self.assertEqual(self.unit(report)["reason"], "no_linked_pr")
        self.assertEqual(report["coverage"]["evidence_gaps"], {"ancestor_log_missing": 1})

    def test_complete_chain_without_shipping_keeps_cost_and_denominator(self):
        self.codex("root")
        self.codex("child", parent="root")
        self.codex("leaf", parent="child")
        report = self.report()
        self.assertEqual(report["state_reasons"]["no_pr"], {"chain_unshipped": 2})
        self.assertEqual(report["success_rate"]["denominator"], 2)
        self.assertEqual(report["cost"]["per_success"]["total"]["numerator"], 200)

    def test_only_out_of_scope_authorship_does_not_prove_change(self):
        for command, output in (
            ("git push", push_output("worker-1", "example/unmeasured")),
            ("gh pr create", "https://github.com/example/unmeasured/pull/1")):
            with self.subTest(command=command):
                self.codex("child", parent="missing", edit=False,
                    command=command, output=output)
                row = self.unit(self.report())
                self.assertEqual((row["state"], row["reason"]), ("no_change", "no_known_edit"))

    def test_ancestor_command_gaps_are_counted_once_for_multiple_children(self):
        for code, output, expected in (
            (1, "", "failed_authorship_commands"),
            (None, "", "authorship_exit_unknown"),
            (0, "https://github.com/example/sample/pull/1\n"
                "https://github.com/example/sample/pull/2", "link_conflicts"),
            (0, "", "authorship_result_missing")):
            with self.subTest(reason=expected):
                self.codex("root", command="gh pr create", output=output, exit_code=code)
                self.codex("middle", parent="root")
                self.codex("child", parent="middle")
                self.codex("sibling", parent="root")
                report = self.report()
                self.assertEqual(report["coverage"]["evidence_gaps"].get(expected), 1)
                if code is None or expected in ("link_conflicts", "authorship_result_missing"):
                    self.assertTrue(all(r["reason"] == "no_linked_pr" for r in report["units"]))

    def test_in_scope_commit_proves_change_with_only_shell_edits(self):
        command = "python -c 'open(\"synthetic.py\", \"w\").write(\"pass\")' && git commit -m 'Synthetic change'"
        self.codex("child", parent="missing", edit=False, command=command,
            output="[unmatched abc1234] Synthetic change\n")
        row = self.unit(self.report())
        self.assertEqual((row["state"], row["reason"], row["links"]),
            ("no_pr", "no_linked_pr", []))

    def test_commit_scope_honors_git_directory_with_invisible_shell_edits(self):
        other = self.root / "other"
        repository(other)
        subprocess.run(["git", "-C", str(other), "remote", "set-url", "origin",
            "https://github.com/example/unmeasured.git"], check=True, capture_output=True)
        for directory, expected in ((self.repo, "no_pr"), (other, "no_change")):
            with self.subTest(scope=expected):
                command = ("python -c 'open(\"synthetic.py\", \"w\").write(\"pass\")' && "
                    f"git -C '{directory}' commit -m 'Synthetic change'")
                self.codex("child", parent="missing", edit=False, command=command,
                    output="[unmatched abc1234] Synthetic change\n")
                row = self.unit(self.report())
                self.assertEqual((row["state"], row["links"]), (expected, []))

    def test_dispatched_exec_without_linked_dispatcher_is_not_observed_root(self):
        rows = self.codex("child")
        rows[0]["payload"].update(source="exec", originator="codex_exec")
        stream(self.home / ".codex/sessions/rollout-child.jsonl", rows)
        report = self.report()
        self.assertEqual(self.unit(report)["reason"], "no_linked_pr")
        self.assertEqual(report["coverage"]["evidence_gaps"], {"dispatcher_unobserved": 1})
        self.codex("leaf", parent="child")
        report = self.report()
        self.assertTrue(all(r["reason"] == "no_linked_pr" for r in report["units"]))
        self.assertEqual(report["coverage"]["evidence_gaps"], {"dispatcher_unobserved": 2})

    def test_repeated_ancestor_link_conflict_counts_the_command_once(self):
        self.codex("root", command="git push", output=push_output("worker-1"))
        for identity in ("child", "sibling", "leaf"):
            self.codex(identity, parent="root")
        report = self.report([pull(1, seconds=10)])
        self.assertEqual(report["coverage"]["evidence_gaps"].get("link_conflicts"), 1)
        self.assertTrue(all(r["reason"] == "no_linked_pr" for r in report["units"]))

    def test_no_edits_still_means_no_change_for_an_unshipped_chain(self):
        self.codex("root")
        self.codex("child", parent="root", edit=False)
        self.assertEqual(self.unit(self.report())["state"], "no_change")

    def test_worker_local_gaps_cannot_prove_chain_unshipped(self):
        self.codex("root")
        rows = self.codex("child", parent="root")
        rows[2]["timestamp"] = None
        stream(self.home / ".codex/sessions/rollout-child.jsonl", rows)
        self.assertEqual(self.unit(self.report())["reason"], "no_linked_pr")

    def test_commit_authorship_prevents_unshipped_but_is_not_inherited(self):
        self.codex("root", command="git commit -m 'Synthetic change'",
            output="[worker-1 abc1234] Synthetic change\n")
        self.codex("child", parent="root")
        row = self.unit(self.report([pull(1)]))
        self.assertEqual((row["reason"], row["links"]), ("no_linked_pr", []))

    def test_own_authorship_wins_even_when_it_has_no_matching_pr(self):
        self.codex("root", command="git push", output=push_output("worker-1"))
        self.codex("child", parent="root", branch="worker-1",
            command="git push", output=push_output("worker-2"))
        row = self.unit(self.report([pull(1), pull(2)]))
        self.assertNotIn("ancestor_pushed_branch", [link["evidence"] for link in row["links"]])
        self.codex("child", parent="root", command="git push", output=push_output("unmatched"))
        row = self.unit(self.report([pull(1)]))
        self.assertEqual((row["reason"], row["links"]), ("no_linked_pr", []))

    def test_inherited_checks_and_disturbances_use_existing_judgment(self):
        self.codex("root", command="git push", output=push_output("worker-1"))
        self.codex("child", parent="root", branch="worker-1")
        entry = pull(1)
        entry["checks_at_merge"]["statuses"] = []
        self.assertEqual(self.unit(self.report([entry]))["state"], "unverified")
        self.assertEqual(self.unit(self.report([pull(1, checks="red")]))["state"], "failed")
        repair = pull(2, day=2)
        repair["response"]["title"] = "Fix #1"
        self.assertEqual(self.unit(self.report([pull(1), repair]))["reason"], "follow_up_fix")

    def test_owner_discovery_captures_parent_shipping_after_dispatch_window(self):
        rows = self.codex("root", edit=False)
        rows.extend(execution("wsl sh -lc 'git push'", push_output("worker-1"), self.repo,
            day=3, start=0, end=1))
        stream(self.home / ".codex/sessions/rollout-root.jsonl", rows)
        self.codex("child", parent="root", branch="worker-1")
        report = self.report([pull(1, day=3)], owners=True)
        self.assertEqual(self.unit(report)["state"], "success")
        self.assertEqual(len(report["coverage"]["repositories"]), 1)

    def test_cycles_depth_and_cross_agent_parent_collisions_are_counted(self):
        worker = Session("codex", "child", parent_raw_id="parent")
        for index, expected in (
            ({("claude-code", "parent"): Session("claude-code", "parent")}, "ancestor_log_missing"),
            ({("codex", "parent"): Session("codex", "parent", parent_raw_id="child")}, "ancestor_cycle"),
            ({("codex", str(i)): Session("codex", str(i), parent_raw_id=str(i + 1))
                for i in range(MAX_ANCESTORS + 1)}, "ancestor_depth_exceeded")):
            if expected == "ancestor_depth_exceeded":
                worker.parent_raw_id = "0"
            gaps = Counter()
            _, complete = ancestor_chain(worker, index, gaps)
            self.assertFalse(complete)
            self.assertEqual(gaps, {expected: 1})
        index = {("codex", str(i)): Session("codex", str(i),
            parent_raw_id=str(i + 1) if i + 1 < MAX_ANCESTORS else None)
            for i in range(MAX_ANCESTORS)}
        gaps = Counter()
        chain, complete = ancestor_chain(worker, index, gaps)
        self.assertTrue(complete)
        self.assertEqual((len(chain), gaps), (MAX_ANCESTORS, {}))
