"""Synthetic terminal unknown checks for ledger and worker GitHub judgments."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest

from support import IsolatedTemporaryDirectory
from worker_github_fixtures import REPO, at, pull, recording, save
from sumbi.core.time import Window
from sumbi.outcomes.github.deliver import judge
from sumbi.outcomes.github.ledger import Deliverable
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.outcomes.worker_github.workers import worker_judgment


def unknown(entry, reason):
    snapshot = entry["checks_at_merge"]
    snapshot["statuses"] = []
    if reason == "checks_none":
        snapshot["required"] = []
        entry.update(checks_at_merge=None,
            current_policy_evidence={"required": [], "results": snapshot})
    return entry


class UnverifiedTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.start = at(1) - timedelta(hours=1)
        self.lifetime = Window(self.start, at(31))

    def outcomes(self, raw):
        save(self.root / "repository.json", raw)
        return FixtureOutcomes(self.root)

    def judgments(self, outcomes, numbers=(1,)):
        prs = tuple(REPO + "#" + str(n) for n in numbers)
        deliverable = Deliverable("synthetic-unit", self.start, (REPO,), prs, ())
        session = SimpleNamespace(start_at=self.start, edits={"edit": at(1, 1)},
            local_evidence_gaps={}, id=lambda: "synthetic-unit")
        links = {pr: ("pr_created", "constituent", None) for pr in prs}
        return (judge(deliverable, outcomes),
            worker_judgment(session, self.lifetime, links, outcomes, 7))

    def test_mature_unknown_checks_are_terminal_with_original_reason(self):
        for reason in ("checks_missing_required", "checks_none"):
            with self.subTest(reason=reason):
                outcomes = self.outcomes(recording([unknown(pull(1), reason)]))
                for result in self.judgments(outcomes):
                    self.assertEqual((result["state"], result["reason"]), ("unverified", reason))
                    self.assertFalse(result["first_pass_success"])
                    self.assertEqual(result["closed_at"], at(1, 100))

    def test_follow_up_capture_must_be_mature_and_complete(self):
        for reason in ("checks_missing_required", "checks_none"):
            for field, value, expected_reason in (
                ("observed_at", at(2).isoformat(), "window_open"),
                ("pulls_complete", False, "outcome_coverage_incomplete"),
                ("commits_complete", False, "outcome_coverage_incomplete"),
                ("coverage_start", at(2).isoformat(), "outcome_coverage_incomplete")):
                with self.subTest(reason=reason, field=field):
                    raw = recording([unknown(pull(1), reason)])
                    raw[field] = value
                    # A missing start is represented by observation evidence;
                    # fixtures cannot contain PRs outside their capture bounds.
                    outcomes = self.outcomes(recording([unknown(pull(1), reason)]))
                    if field == "coverage_start":
                        outcomes.observations[REPO] = replace(outcomes.observation(REPO), start=at(2))
                    else:
                        outcomes = self.outcomes(raw)
                    for result in self.judgments(outcomes):
                        self.assertEqual((result["state"], result["reason"]),
                            ("immature", expected_reason))

    def test_current_policy_missing_required_checks_are_terminal(self):
        entry = unknown(pull(1), "checks_missing_required")
        snapshot = entry["checks_at_merge"]
        snapshot["required"] = []
        entry.update(checks_at_merge=None, current_policy_evidence={
            "required": [{"context": "check", "app_id": None}], "results": snapshot})
        outcomes = self.outcomes(recording([entry]))
        self.assertEqual(outcomes.pull(REPO + "#1").checks_basis, "current_policy")
        for result in self.judgments(outcomes):
            self.assertEqual((result["state"], result["reason"]),
                ("unverified", "checks_missing_required"))

    def test_exact_window_end_is_mature_and_disturbance_boundary_is_excluded(self):
        for reason in ("checks_missing_required", "checks_none"):
            raw = recording([unknown(pull(1), reason)], end=at(8, 100).isoformat())
            raw["commits"] = [{"sha": "e" * 40, "commit": {
                "message": "Fix #1", "committer": {"date": at(8, 100).isoformat()}}}]
            # Extend repository observation to include the boundary commit.
            outcomes = self.outcomes({**raw, "observed_at": at(9).isoformat()})
            outcomes.observations[REPO] = replace(outcomes.observation(REPO), until=at(8, 100))
            with self.subTest(reason=reason):
                for result in self.judgments(outcomes):
                    self.assertEqual((result["state"], result["reason"]), ("unverified", reason))

    def test_disturbances_fail_unknown_checks_even_before_window_closes(self):
        for reason in ("checks_missing_required", "checks_none"):
            for disturbance in ("revert_commit", "revert_pr", "fix_pr", "fix_commit"):
                raw = recording([unknown(pull(1), reason)], end=at(4).isoformat())
                if disturbance.endswith("pr"):
                    follow = pull(2, day=2)
                    follow["response"]["title"] = (
                        "Revert #1" if disturbance == "revert_pr" else "Fix #1")
                    raw["pulls"].append(follow)
                else:
                    raw["commits"] = [{"sha": "e" * 40, "commit": {
                        "message": "Revert #1" if disturbance == "revert_commit" else "Fix #1",
                        "committer": {"date": at(2).isoformat()}}}]
                with self.subTest(reason=reason, disturbance=disturbance):
                    for source, result in enumerate(self.judgments(self.outcomes(raw))):
                        self.assertEqual(result["state"], "failed")
                        self.assertEqual(result["reason"],
                            "reverted" if disturbance == "revert_commit" or (
                                disturbance == "revert_pr" and source == 0) else "follow_up_fix")

    def test_constituent_precedence_in_both_orders(self):
        for state in ("failed", "in_progress", "immature", "unverified", "success"):
            first = unknown(pull(1), "checks_missing_required")
            second = pull(2)
            if state == "failed":
                second = pull(2, merged=False)
            elif state == "in_progress":
                second = pull(2, merged=False, state="open")
            elif state == "immature":
                second = pull(2, day=30)
            elif state == "unverified":
                second = unknown(second, "checks_none")
            for numbers in ((1, 2), (2, 1)):
                with self.subTest(state=state, order=numbers):
                    for result in self.judgments(self.outcomes(recording([first, second])), numbers):
                        self.assertEqual(result["state"], "unverified" if state == "success" else state)
                        self.assertFalse(result["first_pass_success"])
        for result in self.judgments(self.outcomes(recording([pull(1), pull(2)])), (1, 2)):
            self.assertEqual(result["state"], "success")

    def test_capture_and_access_reasons_remain_pending(self):
        for reason in ("checks_unreadable", "checks_capture_incomplete",
            "pagination_incomplete", "checks_policy_unreadable"):
            with self.subTest(reason=reason):
                outcomes = self.outcomes(recording([unknown(pull(1), "checks_missing_required")]))
                identity = REPO + "#1"
                outcomes.pulls[identity] = replace(outcomes.pull(identity), checks_reason=reason)
                for result in self.judgments(outcomes):
                    self.assertEqual((result["state"], result["reason"]), ("in_progress", reason))

    def test_missing_result_capture_stays_pending_in_recorded_replay(self):
        for requirements, reason in (([], "checks_none"),
            ([{"context": "check", "app_id": None}], "checks_missing_required")):
            entry = pull(1)
            entry.update(checks_at_merge=None,
                current_policy_evidence={"required": requirements, "results": None})
            with self.subTest(reason=reason):
                for result in self.judgments(self.outcomes(recording([entry]))):
                    self.assertEqual((result["state"], result["reason"]), ("in_progress", reason))

    def test_legacy_null_results_stay_in_progress_in_recorded_replay(self):
        entry = pull(1)
        entry.update(checks_at_merge=None,
            current_policy_evidence={"required": [], "results": None})
        outcomes = self.outcomes(recording([entry]))
        self.assertFalse(outcomes.pull(REPO + "#1").checks_capture_complete)
        for result in self.judgments(outcomes):
            self.assertEqual((result["state"], result["reason"]),
                ("in_progress", "checks_none"))

    def test_pending_follow_up_and_missing_constituent_still_block(self):
        raw = recording([unknown(pull(1), "checks_missing_required")])
        follow = pull(2, day=2, merged=False, state="open")
        follow["response"]["title"] = "Fix #1"
        for entries, numbers in (([raw["pulls"][0], follow], (1,)), (raw["pulls"], (1, 3))):
            for result in self.judgments(self.outcomes(recording(deepcopy(entries))), numbers):
                self.assertEqual(result["state"], "in_progress")

    def test_unknown_repair_cannot_make_green_ledger_work_successful(self):
        follow = unknown(pull(2, day=2), "checks_none")
        follow["response"]["title"] = "Fix #1"
        ledger, worker = self.judgments(self.outcomes(recording([pull(1), follow])))
        self.assertEqual((ledger["state"], ledger["reason"]), ("unverified", "checks_none"))
        self.assertEqual(worker["state"], "failed")
