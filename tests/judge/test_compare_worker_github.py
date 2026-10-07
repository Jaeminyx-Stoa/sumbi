"""Recorded mixed-agent rounds reuse the common exposure and verdict gates."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from support import IsolatedTemporaryDirectory
from worker_github_fixtures import REPO, build_round, save
from sumbi.judge.compare_worker_github import compare_workers, text_summary
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.outcomes.github.live import RepositoryUnreadable


class WorkerCompareTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.repo, self.directory, self.registration = build_round(self.root)
        self.recording = self.directory / "repository.json"
        for target in ("socket.socket", "socket.getaddrinfo", "socket.create_connection",
            "urllib.request.OpenerDirector.open"):
            guard = patch(target, side_effect=AssertionError("Tests cannot use the network"))
            guard.start()
            self.addCleanup(guard.stop)

    def compare(self):
        return compare_workers(self.home, [REPO], FixtureOutcomes(self.directory), self.registration,
            salt=b"synthetic-key", resamples=100)

    def edit_stream(self, number, edit):
        if number % 100 % 3 == 0:
            path = self.home / f".claude/projects/group/main/subagents/agent-worker-{number}.jsonl"
        else:
            path = self.home / f".codex/sessions/rollout-worker-{number}.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows = edit(rows)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def test_recorded_mixed_agent_round_adopts_with_worker_only_costs(self):
        report = self.compare()
        self.assertEqual(report["verdict"]["proposal"], "adopt")
        self.assertEqual(report["sample_size"]["actual"], {"before": 12, "after": 12})
        self.assertEqual(report["ratios"]["total"]["value"], .5)
        self.assertEqual(report["ratios"]["time"]["value"], .5)
        self.assertEqual(report["bootstrap"]["unit"], "worker_session")
        self.assertEqual(report["dispatch_overhead"]["sessions"], 2)
        self.assertEqual(report["arms"]["before"]["checks_basis_counts"], {"historical": 12})
        self.assertIn("Outcome source: worker-github", text_summary(report))
        self.assertEqual(report["coverage"]["incomplete_reasons"], [])

    def test_no_pr_is_retained_but_over_ten_percent_unlinked_withholds(self):
        def without_refs(rows):
            return [r for r in rows if r.get("payload", {}).get("call_id") != "shell"]
        for number in (101, 102):
            self.edit_stream(number, without_refs)
        report = self.compare()
        self.assertEqual(report["verdict"]["proposal"], "withhold")
        self.assertIn("before_excluded_or_unlinked", report["verdict"]["reasons"])
        self.assertEqual(report["arms"]["before"]["n"], 12)
        self.assertEqual(report["arms"]["before"]["success_rate"]["numerator"], 10)
        self.assertEqual(report["arms"]["before"]["cost"]["total"]["numerator"], 1200)
        self.assertEqual(report["exclusions"]["before"]["share"]["numerator"], 0)

    def test_unreadable_owner_workers_count_toward_risky_exclusion_share(self):
        denied = "example/second"
        def unreadable(rows):
            return [json.loads(json.dumps(row).replace(REPO, denied)) for row in rows]
        for number in (101, 102):
            self.edit_stream(number, unreadable)
        recorded = FixtureOutcomes(self.directory)
        def provider(dispatches):
            if dispatches[0].repos == (denied,):
                raise RepositoryUnreadable("GitHub request failed (HTTP 404)")
            return recorded
        report = compare_workers(self.home, [], provider, self.registration,
            repo_owners=["example"], salt=b"synthetic-key", resamples=100)
        self.assertEqual(report["exclusions"]["before"]["counts"], {"repository_unreadable": 2})
        self.assertEqual(report["exclusions"]["before"]["risky_share"],
            {"numerator": 2, "denominator": 12, "value": 2 / 12,
                "interval_95": None, "interval_method": "not_applicable_census"})
        self.assertEqual(report["arms"]["before"]["n"], 10)
        self.assertEqual(report["arms"]["after"]["n"], 12)
        self.assertEqual(report["coverage"]["incomplete_reasons"], [])
        self.assertIn("before_excluded_or_unlinked", report["verdict"]["reasons"])
        self.assertEqual(report["verdict"]["proposal"], "withhold")
        self.assertNotIn(denied, json.dumps(report))

    def test_weak_branch_links_are_excluded_and_block_share(self):
        def weak(rows):
            rows[0]["payload"]["git"] = {"branch": "worker-101"}
            return [r for r in rows if r.get("payload", {}).get("call_id") != "shell"]
        self.edit_stream(101, weak)
        def weak_second(rows):
            rows[0]["payload"]["git"] = {"branch": "worker-102"}
            return [r for r in rows if r.get("payload", {}).get("call_id") != "shell"]
        self.edit_stream(102, weak_second)
        report = self.compare()
        self.assertEqual(report["exclusions"]["before"]["counts"], {"unlinked": 2})
        self.assertEqual(report["verdict"]["proposal"], "withhold")

    def test_unlinked_and_exposure_share_counts_union_once(self):
        def unlinked(rows):
            return [r for r in rows if r.get("payload", {}).get("call_id") != "shell"]
        self.edit_stream(101, unlinked)
        def spanning(rows):
            rows[-1]["timestamp"] = "2030-01-08T00:00:00Z"
            return rows
        self.edit_stream(102, spanning)
        report = self.compare()
        gate = next(f for f in report["flags"] if f["name"] == "before_excluded_or_unlinked")
        self.assertEqual(gate["evidence"]["numerator"], 2)
        self.edit_stream(101, spanning)
        report = self.compare()
        gate = next(f for f in report["flags"] if f["name"] == "before_excluded_or_unlinked")
        self.assertEqual(gate["evidence"]["numerator"], 2)

    def test_no_pr_and_no_change_exposure_gap_share_is_combined(self):
        def unlinked(rows):
            return [r for r in rows if r.get("payload", {}).get("call_id") != "shell"]
        self.edit_stream(101, unlinked)
        def unchanged(rows):
            return [r for r in rows if r.get("payload", {}).get("name") != "apply_patch"
                and r.get("payload", {}).get("call_id") != "shell"]
        self.edit_stream(102, unchanged)
        raw = json.loads(self.registration.read_text(encoding="utf-8"))
        raw["applied_at"] = "2030-01-07T00:00:00Z"
        raw["before"] = {"since": "2029-12-31T00:00:00Z", "until": "2030-01-07T00:00:00Z"}
        raw["after"] = {"since": "2030-01-07T00:00:00Z", "until": "2030-01-14T00:00:00Z"}
        # Move only the unchanged unit into the actual apply gap.
        def in_gap(rows):
            for row in rows:
                row["timestamp"] = row["timestamp"].replace("2030-01-01", "2030-01-06")
            return rows
        self.edit_stream(102, in_gap)
        save(self.registration, raw)
        recorded = json.loads(self.recording.read_text(encoding="utf-8"))
        recorded["coverage_start"] = "2029-12-31T00:00:00Z"
        save(self.recording, recorded)
        interventions = self.root / "interventions.jsonl"
        interventions.write_text(json.dumps({"intervention_id": "synthetic-round",
            "practice_id": "handoff", "utc_time": "2030-01-06T00:00:00Z"}) + "\n", encoding="utf-8")
        report = compare_workers(self.home, [REPO], FixtureOutcomes(self.directory), self.registration,
            resamples=100, interventions_path=interventions)
        gate = next(f for f in report["flags"] if f["name"] == "before_excluded_or_unlinked")
        self.assertEqual(gate["evidence"]["numerator"], 2)

    def test_exactly_ten_percent_unlinked_does_not_block(self):
        root = self.root / "boundary"
        home, _, directory, registration = build_round(root, count=10)
        path = home / ".codex/sessions/rollout-worker-101.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows = [r for r in rows if r.get("payload", {}).get("call_id") != "shell"]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report = compare_workers(home, [REPO], FixtureOutcomes(directory), registration,
            resamples=100, salt=b"synthetic-key")
        self.assertNotIn("before_excluded_or_unlinked", [f["name"] for f in report["flags"]])
        self.assertEqual(report["arms"]["before"]["n"], 10)

    def test_no_change_shift_is_visible_and_never_hides_worker_counts(self):
        def unchanged(rows):
            return [r for r in rows if r.get("payload", {}).get("name") != "apply_patch"
                and r.get("payload", {}).get("call_id") != "shell"]
        for number in (101, 102, 104):
            self.edit_stream(number, unchanged)
        report = self.compare()
        self.assertIn("no_change_share_shift", report["verdict"]["reasons"])
        self.assertEqual(report["exclusions"]["before"]["counts"], {"no_change": 3})
        self.assertEqual(report["arms"]["before"]["candidate_states"]["no_change"], 3)

    def test_checks_basis_mixture_including_constituents_blocks(self):
        raw = json.loads(self.recording.read_text(encoding="utf-8"))
        entry = raw["pulls"][0]
        snapshot = entry["checks_at_merge"]
        snapshot["required"] = []
        entry.update(checks_at_merge=None, current_policy_evidence={"required": [], "results": snapshot})
        save(self.recording, raw)
        report = self.compare()
        self.assertIn("checks_basis_mixed_or_unknown", report["verdict"]["reasons"])

    def test_missing_recorded_outcomes_withhold_before_comparability(self):
        raw = json.loads(self.recording.read_text(encoding="utf-8"))
        raw["commits_complete"] = False
        save(self.recording, raw)
        report = self.compare()
        self.assertEqual(report["verdict"]["reasons"][0], "incomplete_coverage")
        self.assertIn("outcome_coverage_incomplete", report["verdict"]["reasons"])

    def test_metadata_observability_and_mixed_exposure_use_shared_gates(self):
        def metadata_gap(rows):
            rows[1]["payload"].pop("effort")
            return rows
        self.edit_stream(101, metadata_gap)
        report = self.compare()
        self.assertIn("effort_metadata_partial", report["verdict"]["reasons"])
        def spanning(rows):
            rows[-1]["timestamp"] = "2030-01-08T00:00:00Z"
            return rows
        self.edit_stream(102, spanning)
        report = self.compare()
        self.assertEqual(report["exclusions"]["before"]["counts"]["exposure_mixed"], 1)

    def test_actual_apply_gap_and_registration_source_validation(self):
        interventions = self.root / "interventions.jsonl"
        interventions.write_text(json.dumps({"intervention_id": "synthetic-round",
            "practice_id": "handoff", "utc_time": "2030-01-08T01:00:01Z"}) + "\n", encoding="utf-8")
        report = compare_workers(self.home, [REPO], FixtureOutcomes(self.directory), self.registration,
            resamples=100, interventions_path=interventions)
        self.assertEqual(report["exclusions"]["after"]["counts"], {"exposure_gap": 12})
        self.assertEqual(report["verdict"]["proposal"], "withhold")
        raw = json.loads(self.registration.read_text(encoding="utf-8"))
        raw["outcome_source"] = "github"
        save(self.registration, raw)
        with self.assertRaisesRegex(ValueError, "worker-github"):
            self.compare()

    def test_insufficient_sample_comes_after_completeness(self):
        raw = json.loads(self.registration.read_text(encoding="utf-8"))
        raw["sample_size_per_arm"] = 13
        save(self.registration, raw)
        self.assertEqual(self.compare()["verdict"]["reasons"], ["insufficient_sample"])


if __name__ == "__main__":
    unittest.main()
