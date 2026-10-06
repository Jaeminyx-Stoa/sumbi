"""Structural errors remain fatal; semantic evidence gaps are item-local and counted."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from support import IsolatedTemporaryDirectory
from worker_github_fixtures import at, pull, recording, save
from sumbi.outcomes.github.recorded import FixtureOutcomes


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def outcomes(self, entries=None, raw=None):
        save(self.directory / "repository.json", raw or recording(entries or [pull(1)]))
        return FixtureOutcomes(self.directory)

    def check_entry(self):
        entry = pull(1)
        snapshot = entry["checks_at_merge"]
        snapshot["statuses"] = []
        snapshot["check_runs"] = [{"name": "check", "head_sha": snapshot["head_sha"],
            "started_at": at(1, 20).isoformat(), "completed_at": snapshot["captured_at"],
            "status": "completed", "conclusion": "success"}]
        return entry

    def test_conflicting_ties_cannot_fall_back_to_older_green(self):
        for kind in ("check_runs", "statuses"):
            entry = self.check_entry() if kind == "check_runs" else pull(1)
            rows = entry["checks_at_merge"][kind]
            older = deepcopy(rows[0])
            older["started_at" if kind == "check_runs" else "updated_at"] = at(1, 15).isoformat()
            rows.extend([older, {**rows[0], **({"conclusion": "failure"}
                if kind == "check_runs" else {"state": "failure"})}])
            with self.subTest(kind=kind):
                outcomes = self.outcomes([entry])
                self.assertEqual(outcomes.pull("example/sample#1").checks, "unknown")
                self.assertEqual(sum(outcomes.evidence_gaps.values()), 1)

    def test_remaining_evidence_determines_required_verdict(self):
        for state, expected in (("success", "green"), ("failure", "red")):
            entry = self.check_entry()
            snapshot = entry["checks_at_merge"]
            snapshot["check_runs"][0]["head_sha"] = "e" * 40
            snapshot["statuses"] = [{"context": "check", "sha": snapshot["head_sha"],
                "updated_at": snapshot["captured_at"], "state": state}]
            with self.subTest(state=state):
                outcomes = self.outcomes([entry])
                self.assertEqual(outcomes.pull("example/sample#1").checks, expected)
                self.assertEqual(outcomes.evidence_gaps, {"check_not_on_head": 1})

    def test_newer_reliable_attempt_survives_an_older_conflict(self):
        entry = self.check_entry()
        rows = entry["checks_at_merge"]["check_runs"]
        rows.extend([{**rows[0], "conclusion": "failure"},
            {**rows[0], "started_at": at(1, 30).isoformat()}])
        outcomes = self.outcomes([entry])
        self.assertEqual(outcomes.pull("example/sample#1").checks, "green")
        self.assertEqual(outcomes.evidence_gaps, {"conflicting_check_evidence": 1})

    def test_incomplete_historical_policy_is_not_empty_requirements(self):
        entry = pull(1)
        entry["checks_at_merge"]["required"] = None
        outcomes = self.outcomes([entry])
        self.assertEqual(outcomes.pull("example/sample#1").checks, "unknown")
        self.assertEqual(outcomes.evidence_gaps, {"policy_incomplete": 1})

    def test_policy_invalid_snapshot_cannot_make_empty_requirements_green(self):
        entry = pull(1)
        snapshot = entry["checks_at_merge"]
        entry["checks_at_merge"] = None
        snapshot["required"] = ["check"]
        entry["current_policy_evidence"] = {"required": [], "results": snapshot}
        outcomes = self.outcomes([entry])
        self.assertEqual(outcomes.pull("example/sample#1").checks, "unknown")
        self.assertEqual(outcomes.evidence_gaps, {"policy_incomplete": 1})

    def test_pr_and_commit_timing_are_counted_gaps(self):
        entry = pull(1)
        entry["response"]["closed_at"] = at(1, 90).isoformat()
        raw = recording([entry, pull(2)], commits=[{"sha": "e" * 40,
            "commit": {"message": "Synthetic change", "committer": {
                "date": "2029-12-31T00:00:00Z"}}}])
        outcomes = self.outcomes(raw=raw)
        self.assertIsNone(outcomes.pull("example/sample#1"))
        self.assertIsNotNone(outcomes.pull("example/sample#2"))
        self.assertFalse(outcomes.observation("example/sample").pulls_complete)
        self.assertEqual(outcomes.commits["example/sample"], [])
        self.assertEqual(outcomes.evidence_gaps, {
            "pr_evidence_inconsistent": 1, "commit_outside_coverage": 1})

    def test_unsupported_literal_branch_is_a_gap_but_wrong_type_is_structural(self):
        entry = pull(1)
        entry["response"]["head"]["ref"] = "_synthetic"
        outcomes = self.outcomes([entry])
        self.assertIsNone(outcomes.pull("example/sample#1").head_ref)
        self.assertEqual(outcomes.branch_pulls("example/sample", "_synthetic"), [])
        self.assertEqual(outcomes.evidence_gaps, {"head_ref_unsupported": 1})
        entry["response"]["head"]["ref"] = 1
        with self.assertRaises(ValueError):
            self.outcomes([entry])

    def test_structural_checks_are_validated_before_semantic_exclusions(self):
        mutations = [lambda s: s.pop("required"), lambda s: s.update(statuses={}),
            lambda s: s["check_runs"][0].update(status="unsupported"),
            lambda s: s["check_runs"][0].update(head_sha=5),
            lambda s: s["check_runs"][0].update(started_at="invalid"),
            lambda s: s["check_runs"][0].update(app={"id": "invalid"}),
            lambda s: s.update(required=["check", "check"])]
        for mutate in mutations:
            entry = self.check_entry()
            snapshot = entry["checks_at_merge"]
            snapshot["head_sha"] = "e" * 40
            mutate(snapshot)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(ValueError):
                self.outcomes([entry])

    def test_duplicate_ids_and_wrong_types_fail_fast(self):
        for kind in ("pull", "check", "status", "commit", "policy"):
            raw = recording([self.check_entry()])
            snapshot = raw["pulls"][0]["checks_at_merge"]
            if kind == "pull":
                raw["pulls"].append(deepcopy(raw["pulls"][0]))
            elif kind == "check":
                snapshot["check_runs"][0]["id"] = 1
                snapshot["check_runs"].append(deepcopy(snapshot["check_runs"][0]))
            elif kind == "status":
                snapshot["statuses"] = [deepcopy(pull(1)["checks_at_merge"]["statuses"][0])]
                snapshot["statuses"][0]["id"] = 1
                snapshot["statuses"].append(deepcopy(snapshot["statuses"][0]))
            elif kind == "commit":
                commit = {"sha": "e" * 40, "commit": {"message": "Synthetic change",
                    "committer": {"date": at(1).isoformat()}}}
                raw["commits"] = [commit, deepcopy(commit)]
            else:
                raw["pulls"][0]["checks_at_merge"] = None
                requirement = {"context": "check", "app_id": None}
                raw["pulls"][0]["current_policy_evidence"] = {
                    "required": [requirement, deepcopy(requirement)], "results": None}
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.outcomes(raw=raw)

    def test_missing_policy_fields_and_malformed_json_fail_fast(self):
        entry = pull(1)
        entry["checks_at_merge"] = None
        entry["current_policy_evidence"] = {"required": []}
        with self.assertRaises(ValueError):
            self.outcomes([entry])
        (self.directory / "repository.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            FixtureOutcomes(self.directory)

    def test_authoritative_snapshot_does_not_hide_malformed_optional_evidence(self):
        entry = pull(1)
        entry["current_policy_evidence"] = {"required": []}
        with self.assertRaises(ValueError):
            self.outcomes([entry])

    def test_gap_counts_do_not_grow_on_repeated_pull_access(self):
        entry = pull(1)
        entry["checks_at_merge"]["statuses"][0]["sha"] = "e" * 40
        outcomes = self.outcomes([entry])
        for _ in range(4):
            outcomes.pull("example/sample#1")
        self.assertEqual(outcomes.evidence_gaps, {"status_not_on_head": 1})
        self.assertNotIn("check", json.dumps(dict(outcomes.evidence_gaps)))
