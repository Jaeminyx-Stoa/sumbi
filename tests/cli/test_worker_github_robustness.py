"""A synthetic owner round completes despite heterogeneous unusable evidence."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

from support import IsolatedTemporaryDirectory, isolate_github_destinations
from worker_github_fixtures import (at, build_round, codex_worker, execution, pull,
    push_output, recording, save, stream)
from sumbi.cli import main
from sumbi.outcomes.github.live import GitHubOutcomes
from sumbi.outcomes.github.recorded import FixtureOutcomes


def run_entry(number, day):
    entry = pull(number, day=day)
    snapshot = entry["checks_at_merge"]
    snapshot["statuses"] = []
    snapshot["check_runs"] = [{"name": "check", "head_sha": snapshot["head_sha"],
        "started_at": at(day, 20).isoformat(), "completed_at": snapshot["captured_at"],
        "status": "completed", "conclusion": "success"}]
    return entry


def gap_entry(number, day, reason):
    entry = run_entry(number, day)
    snapshot = entry["checks_at_merge"]
    check = snapshot["check_runs"][0]
    if reason == "check_after_merge":
        check["completed_at"] = at(day, 101).isoformat()
    elif reason == "check_not_on_head":
        check["head_sha"] = "e" * 40
    elif reason == "conflicting_check_evidence":
        snapshot["check_runs"].append({**check, "conclusion": "failure"})
    elif reason == "policy_incomplete":
        entry["checks_at_merge"] = None
        entry["current_policy_evidence"] = {"required": None, "results": snapshot}
        snapshot["required"] = []
    elif reason.startswith("status_") or reason == "conflicting_status_evidence":
        snapshot["check_runs"] = []
        status = {"context": "check", "sha": snapshot["head_sha"],
            "updated_at": snapshot["captured_at"], "state": "success"}
        snapshot["statuses"] = [status]
        if reason == "status_after_merge":
            status["updated_at"] = at(day, 101).isoformat()
        elif reason == "status_not_on_head":
            status["sha"] = "e" * 40
        else:
            snapshot["statuses"].append({**status, "state": "failure"})
    elif reason == "snapshot_not_on_head":
        snapshot["head_sha"] = "e" * 40
    elif reason == "snapshot_not_at_merge":
        snapshot["captured_at"] = at(day, 101).isoformat()
    elif reason == "check_incomplete":
        check["conclusion"] = None
    return entry


class WorkerRobustnessTests(unittest.TestCase):
    def test_owner_deliver_and_compare_complete_with_all_gap_counters(self):
        with IsolatedTemporaryDirectory() as temporary:
            self.root = Path(temporary)
            _, _, _, registration = build_round(self.root / "registration-source", count=6)
            home = self.root / "home"
            reasons = ["check_after_merge", "check_not_on_head", "status_after_merge",
                "conflicting_check_evidence", "policy_incomplete", "status_not_on_head",
                "conflicting_status_evidence", "snapshot_not_on_head", "snapshot_not_at_merge",
                "check_incomplete", None, None]
            repositories = ("example/first", "example/second", "example/denied")
            directories = {}
            for index, repo in enumerate(repositories[:2]):
                entries = []
                for offset in range(6):
                    number, day = index * 6 + offset + 1, 1 if offset < 3 else 8
                    reason = reasons[number - 1]
                    entry = gap_entry(number, day, reason) if reason else run_entry(number, day)
                    if number == 11:
                        entry["response"]["created_at"] = "2029-12-31T00:00:00Z"
                    entries.append(entry)
                    rows = codex_worker(home, f"unit-{number}", self.root / "coordination",
                        day=day, edit=False, command="ssh build.example.test 'git push'",
                        output=push_output(f"worker-{number}", repo))
                    if number == 11:
                        rows.extend(execution("gh pr view 11", f"https://github.com/{repo}/pull/11",
                            self.root / "coordination", day=day, identity="mention", start=12, end=13))
                        stream(home / f".codex/sessions/rollout-unit-{number}.jsonl", rows)
                directory = self.root / str(index)
                save(directory / "repository.json", {**recording(entries), "repository": repo})
                directories[repo] = directory
            codex_worker(home, "denied", self.root / "coordination", day=1, edit=False,
                command="git push", output=push_output("worker-1", repositories[2]))
            isolate_github_destinations(self)
            def provider(dispatches):
                repo = dispatches[0].repos[0]
                if repo == repositories[2]:
                    return GitHubOutcomes(dispatches, cache=self.root / "cache", head_refs=True)
                return FixtureOutcomes(directories[repo])
            def denied(request, **kwargs):
                raise urllib.error.HTTPError(request.full_url, 404, "Synthetic denial", {}, io.BytesIO())
            for command in ("deliver", "compare"):
                args = [command, "--outcome-source", "worker-github", "--repo-owner", "example",
                    "--outcomes", "github", "--home", str(home), "--json", "-", "--agents", "codex"]
                args += (["--since", "2030-01-01T00:00:00Z", "--until", "2030-01-15T00:00:00Z"]
                    if command == "deliver" else ["--registration", str(registration), "--resamples", "100"])
                output, errors = io.StringIO(), io.StringIO()
                with patch("sumbi.cli.worker_github.live_factory", return_value=provider), \
                    patch("sumbi.outcomes.github.live.github_token", return_value="synthetic-token"), \
                    patch("urllib.request.OpenerDirector.open", side_effect=denied), \
                    patch("sumbi.cli.worker_github.read_salt", return_value=b"synthetic-key"), \
                    redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(main(args), 0, errors.getvalue())
                report = json.loads(output.getvalue())
                self.assertEqual(report["coverage"]["evidence_gaps"], {
                    **{reason: 1 for reason in reasons if reason}, "pre_dispatch_pr_mentions": 1})
                self.assertEqual(report["coverage"]["excluded_scope"], {"repository_unreadable": 1})
                self.assertEqual(len(report["coverage"]["repositories"]), 3)
                if command == "deliver":
                    self.assertEqual(report["states"]["success"], 2)
                    self.assertEqual(report["states"]["in_progress"], 2)
                    self.assertEqual(report["states"]["unverified"], 9)
                    self.assertEqual(report["success_rate"]["denominator"], 12)
                    self.assertEqual(report["state_reasons"]["in_progress"], {
                        "checks_policy_unreadable": 1,
                        "repository_unreadable": 1})
                    self.assertEqual(report["state_reasons"]["unverified"],
                        {"checks_missing_required": 9})
                    links = [link for unit in report["units"] for link in unit["links"]]
                    self.assertEqual(sum(link["role"] == "continued" for link in links), 1)
                    self.assertEqual(len(links), 12)
                else:
                    self.assertEqual(report["verdict"]["proposal"], "withhold")
                    self.assertEqual(report["exclusions"]["before"]["counts"], {"repository_unreadable": 1})
                    self.assertIn("before_excluded_or_unlinked", report["verdict"]["reasons"])
                    self.assertEqual(sum(arm["success_rate"]["numerator"]
                        for arm in report["arms"].values()), 2)
                for private in (*repositories, "worker-11", "build.example.test", str(self.root)):
                    self.assertNotIn(private, output.getvalue() + errors.getvalue())
