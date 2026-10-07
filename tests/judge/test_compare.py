"""Literal synthetic round truths and offline decision/statistics boundaries."""

import contextlib
import csv
import io
import json
from pathlib import Path
import shutil
from support import IsolatedTemporaryDirectory
import unittest
from unittest.mock import patch

from sumbi.cli import main
from sumbi.judge.compare_github import compare, mix_distance, text_summary, verdict
from sumbi.judge.stats import bootstrap, classify_ratio, newcombe, sample_size
from sumbi.outcomes.github.deliver import wilson
from sumbi.outcomes.github.ledger import read_ledger
from sumbi.measure.attribution import ProjectRule
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.judge.registration import read_registration

FIXTURE = Path(__file__).parents[1] / "fixtures/compare"


class StatisticsTests(unittest.TestCase):
    def test_newcombe_published_table_ii_all_eight_contrasts(self):
        # Newcombe 1998 Table II, method 10 (score without CC). First
        # proportion is after; these are literal published four-place limits.
        cases = [(56, 70, 48, 80, .0524, .3339), (9, 10, 3, 10, .1705, .8090),
                 (6, 7, 2, 7, .0582, .8062), (5, 56, 0, 29, -.0381, .1926),
                 (0, 10, 0, 20, -.1611, .2775), (0, 10, 0, 10, -.2775, .2775),
                 (10, 10, 0, 20, .6791, 1.), (10, 10, 0, 10, .6075, 1.)]
        for a, an, b, bn, low, high in cases:
            with self.subTest(after=(a, an), before=(b, bn)):
                result = newcombe(b, bn, a, an)
                self.assertEqual([round(v, 4) for v in result["interval_95"]], [low, high])
                reverse = newcombe(a, an, b, bn)["interval_95"]
                self.assertAlmostEqual(reverse[0], -result["interval_95"][1])
                self.assertAlmostEqual(reverse[1], -result["interval_95"][0])

    def test_wilson_reuses_existing_worked_truth(self):
        self.assertAlmostEqual(wilson(1, 2)["wilson_95"][0], .094531205734)
        self.assertAlmostEqual(wilson(1, 2)["wilson_95"][1], .905468794266)
        self.assertIsNone(newcombe(0, 0, 1, 2)["interval_95"])

    def test_sample_size_formula_units_and_boundaries(self):
        self.assertEqual(sample_size(.5, 5), 1570)
        self.assertEqual(sample_size(.8, 50), 11)
        self.assertEqual(sample_size(0, 5), 0)
        self.assertEqual(sample_size(1, 5), 0)
        self.assertIsNone(sample_size(None, 5))
        for margin in (0, -1, 100, float("nan")):
            with self.assertRaises(ValueError):
                sample_size(.5, margin)

    def test_classification_strict_bounds(self):
        self.assertEqual(classify_ratio(.90, [.8, .99]), "improved")
        self.assertEqual(classify_ratio(.90, [.8, 1.]), "uncertain")
        self.assertEqual(classify_ratio(.9001, [.8, .99]), "uncertain")
        self.assertEqual(classify_ratio(1.1, [1., 1.2]), "uncertain")
        self.assertEqual(classify_ratio(1.1, [1.01, 1.2]), "worse")
        self.assertEqual(classify_ratio(.5, None), "uncertain")

    def test_undefined_bootstrap_draws_are_not_dropped(self):
        def row(state, spend):
            return {"state": state, "tokens": {"total": spend}, "elapsed_seconds": spend}
        before = [row("success", 10), row("failed", 10)]
        after = [row("success", 5)]
        costs, ratios = bootstrap(before, after, ("total",), resamples=100, seed=1)
        self.assertEqual(ratios["total"]["value"], .25)
        self.assertLess(ratios["total"]["defined_resamples"], 100)
        self.assertIsNone(ratios["total"]["interval_95"])
        self.assertIsNone(costs["before"]["total"]["interval_95"])
        _, ratios = bootstrap([row("success", 0)], after, ("total",), resamples=100)
        self.assertIsNone(ratios["total"]["value"])
        self.assertEqual(ratios["total"]["classification"], "uncertain")
        _, ratios = bootstrap([], [], ("total",), resamples=100)
        self.assertIsNone(ratios["total"]["value"])

    def test_total_variation_distance(self):
        from collections import Counter
        self.assertAlmostEqual(mix_distance(Counter(a=8, b=2), Counter(a=6, b=4)), .2)
        self.assertEqual(mix_distance(Counter(a=10), Counter(b=10)), 1)


class DecisionOrderTests(unittest.TestCase):
    def decision(self, **overrides):
        args = {"coverage_reasons": [], "preregistered": True, "blocking_flags": [],
                "arms": {"before": [{"state": "success"}], "after": [{"state": "success"}]},
                "registered_size": 1, "success": {"interval_95": [-.01, .1]}, "margin_pp": 5,
                "ratios": {"total": {"classification": "improved"}, "time": {"classification": "uncertain"}}}
        args.update(overrides)
        return verdict(**args)

    def test_earlier_gates_win_over_later_rejection(self):
        bad = {"success": {"interval_95": [-.5, -.3]}}
        self.assertEqual(self.decision(**bad, coverage_reasons=["broken"])[1][0], "incomplete_coverage")
        self.assertEqual(self.decision(**bad, preregistered=False)[1], ["not_preregistered"])
        self.assertEqual(self.decision(**bad, blocking_flags=["model_mix_shift"])[1], ["not_comparable", "model_mix_shift"])
        self.assertEqual(self.decision(**bad, blocking_flags=["verifier_never_passed"]),
                         ("withhold", ["not_comparable", "verifier_never_passed"]))
        self.assertEqual(self.decision(**bad, registered_size=2)[1], ["insufficient_sample"])
        self.assertEqual(self.decision(**bad, arms={"before": [{"state": "immature"}], "after": []})[1], ["immature_or_in_progress"])

    def test_noninferiority_equality_is_inconclusive(self):
        self.assertEqual(self.decision(success={"interval_95": [-.05, .1]})[1], ["inconclusive_success"])
        self.assertEqual(self.decision(success={"interval_95": [-.1, -.05]})[1], ["inconclusive_success"])

    def test_cost_decisions_and_success_tradeoff(self):
        def ratios(a, b):
            return {"total": {"classification": a}, "time": {"classification": b}}
        self.assertEqual(self.decision(ratios=ratios("worse", "worse"))[0], "reject")
        self.assertEqual(self.decision(ratios=ratios("worse", "improved"))[0], "owner_decides")
        self.assertEqual(self.decision(ratios=ratios("uncertain", "uncertain"))[1], ["no_detectable_change"])
        self.assertEqual(self.decision(success={"interval_95": [.01, .2]}, ratios=ratios("worse", "worse"))[0], "owner_decides")
        self.assertEqual(self.decision(success={"interval_95": [.01, .2]}, ratios=ratios("worse", "uncertain"))[0], "owner_decides")


class CompareRoundTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "round"
        shutil.copytree(FIXTURE, self.root)
        self.ledger = self.root / "deliverables.csv"
        self.registration = self.root / "registration.json"
        self.outcome_path = self.root / "outcomes/repository.json"
        for target in ("socket.socket", "socket.getaddrinfo", "socket.create_connection", "urllib.request.OpenerDirector.open"):
            guard = patch(target, side_effect=AssertionError("Tests cannot use the network"))
            guard.start()
            self.addCleanup(guard.stop)

    def run_round(self, **kwargs):
        return compare(self.root / "home", self.ledger, FixtureOutcomes(self.root / "outcomes"),
                       self.registration, agents=kwargs.pop("agents", ["codex"]), salt=b"synthetic", resamples=1000, **kwargs)

    def edit_json(self, path, edit):
        raw = json.loads(path.read_text(encoding="utf-8"))
        edit(raw)
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

    def edit_ledger(self, edit):
        with self.ledger.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        edit(rows)
        with self.ledger.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def edit_log(self, identity, edit):
        path = self.root / "home/.codex/sessions" / ("rollout-" + identity + ".jsonl")
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        edit(events)
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def assert_proposal(self, report, proposal, reason):
        self.assertEqual(report["verdict"]["label"], "proposal")
        self.assertEqual(report["verdict"]["proposal"], proposal)
        self.assertIn(reason, report["verdict"]["reasons"])

    def test_round_adopt_literal_hand_truth(self):
        report = self.run_round()
        self.assert_proposal(report, "adopt", "non_inferior_with_cost_improvement")
        for arm, spend in (("before", 2000), ("after", 800)):
            row = report["arms"][arm]
            self.assertEqual(row["n"], 20)
            self.assertEqual(row["success_rate"]["numerator"], 16)
            self.assertEqual(row["success_rate"]["denominator"], 20)
            self.assertAlmostEqual(row["success_rate"]["wilson_95"][0], .5839825677481064)
            self.assertAlmostEqual(row["success_rate"]["wilson_95"][1], .919342337420202)
            self.assertEqual(row["cost"]["total"]["numerator"], spend)
            self.assertEqual(row["cost"]["time"]["numerator"], spend)
            self.assertEqual(row["cost"]["total"]["denominator"], 16)
        self.assertEqual(report["sample_size"]["needed_per_arm"], 11)
        self.assertEqual(report["success_difference"]["difference"], 0)
        self.assertAlmostEqual(report["success_difference"]["interval_95"][0], -.24679166221252044)
        self.assertAlmostEqual(report["success_difference"]["interval_95"][1], .24679166221252044)
        for kind in ("new_input", "cache_read", "output", "reasoning_output", "total", "time"):
            row = report["ratios"][kind]
            self.assertEqual(row["value"], .4)
            self.assertAlmostEqual(row["interval_95"][0], .28880952380952385)
            self.assertAlmostEqual(row["interval_95"][1], .5541666666666664)
            self.assertEqual(row["classification"], "improved")
        self.assertIsNone(report["ratios"]["cache_write"]["value"])
        self.assertEqual(report["flags"], [])
        self.assertTrue(all("required_checks_at_merge" not in p
            for arm in report["arms"].values() for row in arm["deliverables"] for p in row["pr_outcomes"]))
        self.assertEqual(report["bootstrap"], {"seed": 1729, "resamples": 1000, "unit": "deliverable", "quantiles": [.025, .975]})

    def test_unverified_rows_retain_costs_and_reach_a_verdict(self):
        def missing_checks(raw):
            for entry in (raw["pulls"][0], raw["pulls"][20]):
                entry["checks_at_merge"]["check_runs"] = []
                entry["checks_at_merge"]["statuses"] = []
        self.edit_json(self.outcome_path, missing_checks)
        report = self.run_round()
        self.assertEqual(report["verdict"]["proposal"], "adopt")
        for arm, spend in (("before", 2000), ("after", 800)):
            row = report["arms"][arm]
            self.assertEqual(row["success_rate"]["denominator"], 20)
            self.assertEqual(row["success_rate"]["numerator"], 15)
            self.assertEqual(row["cost"]["total"]["numerator"], spend)
            unverified = [r for r in row["deliverables"] if r["state"] == "unverified"]
            self.assertEqual(len(unverified), 1)
            self.assertEqual(unverified[0]["reason"], "checks_missing_required")
            self.assertFalse(unverified[0]["eventual_success"])
            self.assertEqual(report["exclusions"][arm]["share"]["numerator"], 0)

    def test_round_reject_inferior(self):
        def fail(rows):
            for r in rows:
                if r["id"].startswith("A"):
                    r["prs"] = ""
                    r["state_override"] = "abandoned@2030-01-08T00:00:40Z"
        self.edit_ledger(fail)
        self.edit_json(self.registration, lambda r: r.update(non_inferiority_margin_pp=10))
        report = self.run_round()
        self.assert_proposal(report, "reject", "success_inferior")
        self.assertEqual(report["success_difference"]["difference"], -.8)
        self.assertAlmostEqual(report["success_difference"]["interval_95"][0], -.919342337420202)
        self.assertAlmostEqual(report["success_difference"]["interval_95"][1], -.5305100232026292)
        self.assertEqual(report["arms"]["after"]["cost"]["total"]["numerator"], 800)
        self.assertIsNone(report["arms"]["after"]["cost"]["total"]["value"])

    def test_round_owner_decides_opposite_costs(self):
        def longer(raw):
            for p in raw["pulls"]:
                if p["response"]["number"] > 100:
                    end = "2030-01-08T00:03:20Z"
                    p["response"].update(merged_at=end, closed_at=end)
                    p["checks_at_merge"]["captured_at"] = end
                    p["checks_at_merge"]["statuses"][0]["updated_at"] = end
        self.edit_json(self.outcome_path, longer)
        self.edit_ledger(lambda rows: [r.update(state_override="abandoned@2030-01-08T00:03:20Z") for r in rows if r["id"].startswith("A") and not r["prs"]])
        report = self.run_round()
        self.assert_proposal(report, "owner_decides", "time_token_tradeoff")
        self.assertEqual(report["arms"]["after"]["cost"]["time"]["numerator"], 4000)
        self.assertEqual(report["arms"]["after"]["cost"]["time"]["value"], 250)
        self.assertEqual(report["ratios"]["time"]["value"], 2)
        self.assertAlmostEqual(report["ratios"]["time"]["interval_95"][0], 1.444047619047619)
        self.assertAlmostEqual(report["ratios"]["time"]["interval_95"][1], 2.770833333333332)
        self.assertEqual(report["ratios"]["time"]["classification"], "worse")
        self.assertEqual(report["ratios"]["total"]["classification"], "improved")

    def set_after_time(self, seconds):
        end = f"2030-01-08T00:{seconds // 60:02d}:{seconds % 60:02d}Z"
        def change(raw):
            for p in raw["pulls"]:
                if p["response"]["number"] > 100:
                    p["response"].update(merged_at=end, closed_at=end)
                    p["checks_at_merge"]["captured_at"] = end
                    p["checks_at_merge"]["statuses"][0]["updated_at"] = end
        self.edit_json(self.outcome_path, change)
        self.edit_ledger(lambda rows: [r.update(state_override="abandoned@" + end)
            for r in rows if r["id"].startswith("A") and not r["prs"]])

    def test_round_reject_tokens_worse_without_offset(self):
        self.set_after_time(100)
        for i in range(1, 21):
            self.edit_log(f"A{i:02d}", lambda e: e[2]["payload"]["info"]["total_token_usage"].update(
                input_tokens=160, cached_input_tokens=40, output_tokens=40, reasoning_output_tokens=10))
        report = self.run_round()
        self.assert_proposal(report, "reject", "tokens_worse_without_offset")
        self.assertEqual(report["arms"]["after"]["cost"]["total"]["numerator"], 4000)
        self.assertEqual(report["ratios"]["total"]["value"], 2)
        self.assertAlmostEqual(report["ratios"]["total"]["interval_95"][0], 1.444047619047619)
        self.assertAlmostEqual(report["ratios"]["total"]["interval_95"][1], 2.770833333333332)
        self.assertEqual(report["ratios"]["time"]["value"], 1)
        self.assertAlmostEqual(report["ratios"]["time"]["interval_95"][0], .7220238095238095)
        self.assertAlmostEqual(report["ratios"]["time"]["interval_95"][1], 1.385416666666666)
        self.assertEqual(report["ratios"]["time"]["classification"], "uncertain")
        self.assertLessEqual(report["success_difference"]["interval_95"][0], 0)

    def test_round_reject_time_worse_without_offset(self):
        self.set_after_time(200)
        for i in range(1, 21):
            self.edit_log(f"A{i:02d}", lambda e: e[2]["payload"]["info"]["total_token_usage"].update(
                input_tokens=80, cached_input_tokens=20, output_tokens=20, reasoning_output_tokens=5))
        report = self.run_round()
        self.assert_proposal(report, "reject", "time_worse_without_offset")
        self.assertEqual(report["ratios"]["time"]["value"], 2)
        self.assertAlmostEqual(report["ratios"]["time"]["interval_95"][0], 1.444047619047619)
        self.assertAlmostEqual(report["ratios"]["time"]["interval_95"][1], 2.770833333333332)
        self.assertEqual(report["ratios"]["total"]["value"], 1)
        self.assertAlmostEqual(report["ratios"]["total"]["interval_95"][0], .7220238095238095)
        self.assertAlmostEqual(report["ratios"]["total"]["interval_95"][1], 1.385416666666666)
        self.assertEqual(report["ratios"]["total"]["classification"], "uncertain")

    def test_round_withhold_coverage(self):
        self.edit_json(self.outcome_path, lambda r: r.update(commits_complete=False))
        self.assert_proposal(self.run_round(), "withhold", "incomplete_coverage")

    def test_round_withhold_not_preregistered(self):
        self.edit_json(self.registration, lambda r: r.update(registered_at="2030-01-08T00:00:01Z"))
        self.assert_proposal(self.run_round(), "withhold", "not_preregistered")

    def test_round_withhold_blocking_confounder(self):
        for i in range(1, 21):
            self.edit_log(f"A{i:02d}", lambda e: e[1]["payload"].update(model="synthetic-next"))
        report = self.run_round()
        self.assert_proposal(report, "withhold", "model_mix_shift")
        self.assertEqual(report["mixes"]["model"]["total_variation_distance"], 1)

    def test_round_withhold_immature(self):
        def recent(raw):
            p = raw["pulls"][16]
            end = "2030-02-09T00:00:00Z"
            p["response"].update(merged_at=end, closed_at=end)
            p["checks_at_merge"]["captured_at"] = end
            p["checks_at_merge"]["statuses"][0]["updated_at"] = end
        self.edit_json(self.outcome_path, recent)
        report = self.run_round()
        self.assert_proposal(report, "withhold", "immature_or_in_progress")
        self.assertEqual(report["arms"]["after"]["states"]["immature"], 1)

    def test_round_withhold_insufficient_sample(self):
        self.edit_json(self.registration, lambda r: r.update(sample_size_per_arm=21))
        self.assert_proposal(self.run_round(), "withhold", "insufficient_sample")

    def test_round_withhold_inconclusive(self):
        self.edit_json(self.registration, lambda r: r.update(non_inferiority_margin_pp=5))
        report = self.run_round()
        self.assert_proposal(report, "withhold", "inconclusive_success")
        self.assertEqual(report["sample_size"]["needed_per_arm"], 1005)

    def test_exposure_spanning_and_contradiction_excluded(self):
        for identity in ("B01", "B02", "B03"):
            def straddle(events):
                events.append({"type": "event_msg", "timestamp": "2030-01-08T00:00:04Z", "payload": {"type": "agent_message", "message": "Synthetic private canary"}})
            self.edit_log(identity, straddle)
        report = self.run_round()
        self.assertEqual(report["exclusions"]["before"]["counts"], {"exposure_mixed": 3})
        self.assertEqual(report["arms"]["before"]["n"], 17)
        self.assert_proposal(report, "withhold", "before_excluded_or_unlinked")
        # A session starting before application contradicts an after dispatch,
        # even when its sole spend event is entirely after application.
        self.edit_log("A01", lambda e: e[0].update(timestamp="2030-01-07T23:59:00Z"))
        report = self.run_round()
        self.assertEqual(report["exclusions"]["after"]["counts"], {"exposure_mixed": 1})

    def test_unlinked_deliverables_and_exact_threshold(self):
        for identity in ("A01", "A02"):
            (self.root / "home/.codex/sessions" / ("rollout-" + identity + ".jsonl")).unlink()
        report = self.run_round()
        self.assertEqual(report["exclusions"]["after"]["share"]["value"], .1)
        self.assertFalse(any(f["name"] == "after_excluded_or_unlinked" for f in report["flags"]))
        (self.root / "home/.codex/sessions/rollout-A03.jsonl").unlink()
        report = self.run_round()
        self.assert_proposal(report, "withhold", "after_excluded_or_unlinked")
        self.assertEqual(report["exclusions"]["after"]["counts"], {"unlinked": 3})

    def test_separate_sessions_on_opposite_sides_are_mixed(self):
        source = self.root / "home/.codex/sessions/rollout-B01.jsonl"
        events = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        for i, event in enumerate(events, 1):
            event["timestamp"] = f"2030-01-08T00:00:0{i}Z"
        events[0]["payload"]["id"] = "synthetic-late"
        source.with_name("rollout-late.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        report = self.run_round()
        self.assertEqual(report["exclusions"]["before"]["counts"], {"exposure_mixed": 1})
        self.assertEqual(report["arms"]["before"]["n"], 19)

    def test_dispatch_window_end_is_excluded(self):
        self.edit_ledger(lambda rows: rows.append({"id": "outside", "dispatched_at": "2030-01-15T00:00:00Z",
            "acceptance": "Synthetic acceptance", "repos": "example/sample", "prs": "", "branches": "",
            "state_override": "", "accepted_by_human": "", "notes": "", "task_type": "feature"}))
        report = self.run_round()
        self.assertEqual(report["arms"]["after"]["n"], 20)
        self.assertEqual(report["coverage"]["ledger_rows_read"], 40)
        self.assertEqual(report["verdict"]["proposal"], "adopt")

    def test_lifetime_retry_spend_and_final_elapsed(self):
        # A closed replacement attempt on B01 is not a second deliverable.
        self.edit_ledger(lambda rows: rows[0].update(prs="example/sample#99:retry;example/sample#1:constituent"))
        def retry(raw):
            raw["pulls"].append({"response": {"number": 99, "state": "closed", "created_at": "2030-01-01T00:00:00Z",
                "closed_at": "2030-01-01T00:00:30Z", "merged": False, "merged_at": None,
                "head": {"sha": "9" * 40}, "merge_commit_sha": None, "title": "Synthetic retry", "body": None},
                "checks_at_merge": None})
        self.edit_json(self.outcome_path, retry)
        self.edit_log("B01", lambda e: e[2]["payload"]["info"]["total_token_usage"].update(input_tokens=160, cached_input_tokens=40, output_tokens=40, reasoning_output_tokens=10))
        report = self.run_round()
        self.assertEqual(report["arms"]["before"]["n"], 20)
        self.assertEqual(report["arms"]["before"]["success_rate"]["numerator"], 16)
        self.assertEqual(report["arms"]["before"]["cost"]["total"]["numerator"], 2100)
        self.assertEqual(report["arms"]["before"]["cost"]["time"]["numerator"], 2000)
        self.assertFalse(report["arms"]["before"]["deliverables"][0]["first_pass_success"])

    def test_volume_shift_is_informational_and_threshold_strict(self):
        # Shrink the registry and its logs together; not missing coverage.
        self.edit_ledger(lambda rows: rows.__setitem__(slice(None), [r for r in rows if not r["id"].startswith("A") or int(r["id"][1:]) <= 12]))
        for i in range(13, 21):
            (self.root / "home/.codex/sessions" / f"rollout-A{i:02d}.jsonl").unlink()
        self.edit_json(self.registration, lambda r: r.update(sample_size_per_arm=12))
        report = self.run_round()
        flag = next(f for f in report["flags"] if f["name"] == "volume_shift")
        self.assertFalse(flag["blocking"])
        self.assertEqual(flag["evidence"]["numerator"], 8)
        self.assertEqual(flag["evidence"]["denominator"], 12)

    def test_task_missing_distinct_from_literal_not_reported(self):
        self.edit_ledger(lambda rows: (rows[0].update(task_type=""), rows[1].update(task_type="not_reported")))
        report = self.run_round()
        types = [r["task_type"] for r in report["task_type_breakdowns"]]
        self.assertIn(None, types)
        self.assertIn("not_reported", types)

    def test_metadata_tv_threshold_without_dominant_change(self):
        for identity in ("B17", "B18", "B19", "B20", "A13", "A14", "A15", "A16", "A17", "A18", "A19", "A20"):
            self.edit_log(identity, lambda e: e[1]["payload"].update(model="synthetic-next"))
        report = self.run_round()
        self.assertEqual(report["mixes"]["model"]["total_variation_distance"], .2)
        self.assertFalse(report["mixes"]["model"]["dominant_changed"])
        self.assertFalse(any(f["name"] == "model_mix_shift" for f in report["flags"]))
        self.edit_log("A12", lambda e: e[1]["payload"].update(model="synthetic-next"))
        self.assert_proposal(self.run_round(), "withhold", "model_mix_shift")

    def test_ledger_row_order_does_not_change_bootstrap(self):
        expected = self.run_round()
        self.edit_ledger(lambda rows: rows.reverse())
        actual = self.run_round()
        self.assertEqual(actual["ratios"], expected["ratios"])
        self.assertEqual(actual["arms"], expected["arms"])

    def test_mixed_checks_basis_blocks(self):
        def weaken(raw):
            p = raw["pulls"][16]
            snapshot = p["checks_at_merge"]
            snapshot["required"] = []
            p["checks_at_merge"] = None
            p["current_policy_evidence"] = {"required": [], "results": snapshot}
        self.edit_json(self.outcome_path, weaken)
        self.assert_proposal(self.run_round(), "withhold", "checks_basis_mixed_or_unknown")

    def test_registered_runner_and_informational_event_window_bounds(self):
        self.edit_json(self.registration, lambda r: r.update(confounders=[
            {"at": "2030-01-08T00:00:00Z", "label": "runner_change"},
            {"at": "2030-01-15T00:00:00Z", "label": "runner_change"},
            {"at": "2030-01-02T00:00:00Z", "label": "holiday"}]))
        report = self.run_round()
        events = [f for f in report["flags"] if f["name"] == "registered_event"]
        self.assertEqual(len(events), 2)
        self.assertEqual([f["blocking"] for f in events], [True, False])
        self.assertEqual(events[0]["evidence"]["arms"], ["after"])
        self.assert_proposal(report, "withhold", "registered_event")

    def test_effort_and_cli_changes_block(self):
        for i in range(1, 21):
            self.edit_log(f"A{i:02d}", lambda e: (e[0]["payload"].update(cli_version="2.0"), e[1]["payload"].update(effort="low")))
        report = self.run_round()
        self.assert_proposal(report, "withhold", "effort_mix_shift")
        self.assertIn("cli_version_mix_shift", report["verdict"]["reasons"])

    def test_task_mix_informational_descriptive_only(self):
        self.edit_ledger(lambda rows: [r.update(task_type="fix") for r in rows if r["id"].startswith("A")])
        report = self.run_round()
        self.assertEqual(report["verdict"]["proposal"], "adopt")
        flag = next(f for f in report["flags"] if f["name"] == "task_type_mix_shift")
        self.assertFalse(flag["blocking"])
        self.assertEqual(report["mixes"]["task_type"]["total_variation_distance"], 1)
        self.assertEqual(len(report["task_type_breakdowns"]), 2)
        self.assertTrue(all(r["descriptive_only"] and "verdict" not in r for r in report["task_type_breakdowns"]))

    def test_unattributed_spend_blocks(self):
        original = self.root / "home/.codex/sessions/rollout-B01.jsonl"
        target = original.with_name("rollout-unassigned.jsonl")
        events = [json.loads(line) for line in original.read_text(encoding="utf-8").splitlines()]
        events[0]["payload"] = {"id": "synthetic-unassigned", "cli_version": "1.0"}
        events[1]["payload"] = {"model": "synthetic-model", "effort": "high"}
        events[2]["payload"]["info"]["total_token_usage"].update(input_tokens=800, cached_input_tokens=200, output_tokens=200)
        target.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        report = self.run_round()
        self.assert_proposal(report, "withhold", "unattributed_spend")
        self.assertEqual(report["coverage"]["unattributed_lifetime_share"]["numerator"], 1000)
        self.assertEqual(report["coverage"]["unattributed_lifetime_share"]["denominator"], 3800)

    def test_missing_metadata_blocks_without_inventing_zero(self):
        self.add_claude_round()
        self.edit_log("A02", lambda e: e[1]["payload"].pop("effort"))
        report = self.run_round(agents=["claude-code", "codex"])
        self.assert_proposal(report, "withhold", "effort_metadata_partial")
        flag = next(f for f in report["flags"] if f["name"] == "effort_metadata_partial")
        self.assertEqual(flag["evidence"]["agents"], ["codex"])

    def add_claude_round(self):
        target = self.root / "home/.claude/projects/fixture"
        target.mkdir(parents=True)
        for identity in ("B01", "A01"):
            shutil.copyfile(self.root / "claude" / (identity + ".jsonl"), target / (identity + ".jsonl"))
            (self.root / "home/.codex/sessions" / ("rollout-" + identity + ".jsonl")).unlink()

    def test_claude_and_codex_unobservable_effort_reaches_verdict(self):
        self.add_claude_round()
        report = self.run_round(agents=["claude-code", "codex"])
        self.assert_proposal(report, "adopt", "non_inferior_with_cost_improvement")
        flag = next(f for f in report["flags"] if f["name"] == "effort_unobservable")
        self.assertEqual(flag, {"name": "effort_unobservable", "blocking": False,
            "evidence": {"agents": ["claude-code"]}})
        self.assertEqual({s["agent"] for s in report["session_metadata"]}, {"claude-code", "codex"})
        for arm in ("before", "after"):
            values = report["mixes"]["effort"][arm]["values"]
            self.assertEqual(values["high"]["numerator"], 19)
            self.assertNotIn("<not_reported>", values)
        self.edit_json(self.registration, lambda r: r.update(confounders=[
            {"at": "2030-01-09T00:00:00Z", "label": "effort_change"}]))
        self.assert_proposal(self.run_round(agents=["claude-code", "codex"]), "withhold", "registered_event")

    def test_asymmetric_effort_reporting_blocks(self):
        for i in range(1, 21):
            self.edit_log(f"A{i:02d}", lambda e: e[1]["payload"].pop("effort"))
        report = self.run_round()
        self.assert_proposal(report, "withhold", "effort_metadata_asymmetric")
        flag = next(f for f in report["flags"] if f["name"] == "effort_metadata_asymmetric")
        self.assertEqual(flag["evidence"]["agents"], ["codex"])

    def test_agent_only_in_one_arm_does_not_claim_metadata_asymmetry(self):
        target = self.root / "home/.claude/projects/fixture"
        target.mkdir(parents=True)
        shutil.copyfile(self.root / "claude/A01.jsonl", target / "A01.jsonl")
        (self.root / "home/.codex/sessions/rollout-A01.jsonl").unlink()
        report = self.run_round(agents=["claude-code", "codex"])
        flag = next(f for f in report["flags"] if f["name"] == "agent_mix_shift")
        self.assertEqual(flag, {"name": "agent_mix_shift", "blocking": True, "evidence": {"agents": ["claude-code"]}})
        self.assert_proposal(report, "withhold", "agent_mix_shift")
        self.assertFalse(any(f["name"].endswith("metadata_asymmetric") for f in report["flags"]))

    def test_intervention_apply_gap_later_earlier_and_absent(self):
        path = self.root / "interventions.jsonl"
        for actual, arm in (("2030-01-08T00:10:00Z", "after"), ("2030-01-01T00:00:00Z", "before")):
            with self.subTest(actual=actual):
                path.write_text(json.dumps({"practice_id": "round-01", "utc_time": actual}) + "\n", encoding="utf-8")
                report = self.run_round(interventions_path=path)
                self.assertEqual(report["exclusions"][arm]["counts"], {"exposure_gap": 20})
                self.assertEqual(report["arms"][arm]["n"], 0)
                self.assertGreater(report["exposure_gap"]["seconds"], 0)
                self.assertIn("Exposure gap:", text_summary(report))
        report = self.run_round()
        self.assertNotIn("exposure_gap", report)
        self.assertEqual(report["arms"]["after"]["n"], 20)

    def test_other_project_heavy_spend_is_context_only(self):
        source = self.root / "home/.codex/sessions/rollout-B01.jsonl"
        events = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        events[0]["payload"].update(id="synthetic-other", cwd="Z:/synthetic/other", git={})
        events[1]["payload"].update(cwd="Z:/synthetic/other", git={})
        events[2]["payload"]["info"]["total_token_usage"].update(
            input_tokens=800000, cached_input_tokens=200000, output_tokens=200000)
        source.with_name("rollout-other.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        report = self.run_round(rules=[ProjectRule("sample", paths=["Z:/synthetic/sample/*"])])
        self.assert_proposal(report, "adopt", "non_inferior_with_cost_improvement")
        share = report["coverage"]["unattributed_lifetime_share"]
        self.assertEqual((share["numerator"], share["denominator"], share["value"]), (0, 2800, 0))
        self.assertEqual(report["coverage"]["other_lifetime_tokens"], 1000000)
        self.assertIn("Other-project lifetime tokens (context): 1000000", text_summary(report))

    def test_partial_or_broken_token_coverage_withholds(self):
        self.edit_log("A01", lambda e: e[2]["payload"]["info"]["total_token_usage"].pop("output_tokens"))
        self.assert_proposal(self.run_round(), "withhold", "incomplete_coverage")

    def test_deterministic_private_json_and_summary(self):
        self.edit_json(self.registration, lambda r: r["predictions"][0].update(condition="private condition canary"))
        self.edit_ledger(lambda rows: rows[0].update(acceptance="private acceptance canary", notes="private notes canary", branches="private-branch-canary/B01"))
        report = self.run_round()
        self.assertEqual(report, self.run_round())
        public = json.dumps(report, allow_nan=False) + text_summary(report)
        for canary in ("private condition canary", "private acceptance canary", "private notes canary", "private-branch-canary", "Z:/synthetic", "example/sample", "synthetic-B01"):
            self.assertNotIn(canary, public)
        self.assertNotIn('"id": "B01"', public)
        self.assertIn("not reported", text_summary(report))
        self.assertIn("descriptive only", text_summary(report))

    def test_cli_json_and_text_offline(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["compare", "--ledger", str(self.ledger), "--outcomes", str(self.root / "outcomes"),
                         "--registration", str(self.registration), "--home", str(self.root / "home"),
                         "--agents", "codex", "--json", "-", "--resamples", "1000"])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["verdict"]["proposal"], "adopt")
        self.assertIn("Verdict proposal: adopt", stderr.getvalue())

    def test_cli_cannot_overwrite_registration(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["compare", "--ledger", str(self.ledger), "--outcomes", str(self.root / "outcomes"),
                  "--registration", str(self.registration), "--json", str(self.registration)])

    def test_task_type_optional_header_and_validation(self):
        self.assertEqual(read_ledger(self.ledger)[0].task_type, "feature")
        self.edit_ledger(lambda rows: rows[0].update(task_type="free text"))
        with self.assertRaisesRegex(ValueError, "task_type"):
            read_ledger(self.ledger)
        # The historical nine-column contract is still accepted.
        old = self.root / "old.csv"
        with self.ledger.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        with old.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows([r[:9] for r in rows])
        self.assertIsNone(read_ledger(old)[0].task_type)

    def test_registration_schema_rejects_invalid_or_ambiguous_inputs(self):
        raw = json.loads(self.registration.read_text(encoding="utf-8"))
        cases = [{**raw, "non_inferiority_margin_pp": 0}, {**raw, "sample_size_per_arm": True},
                 {**raw, "follow_up_days": float("nan")}, {**raw, "predictions": []},
                 {**raw, "intervention_id": "free text"}, {**raw, "unexpected": 1},
                 {**raw, "applied_at": "2030-01-08T09:00:00+09:00"},
                 {**raw, "after": {"since": "2030-01-07T00:00:00Z", "until": "2030-01-14T00:00:00Z"}},
                 {**raw, "after": {"since": "2030-01-08T00:00:00Z", "until": "2030-01-16T00:00:00Z"}}]
        for case in cases:
            self.registration.write_text(json.dumps(case), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema"):
                read_registration(self.registration)
        self.registration.write_text('{"intervention_id":"one","intervention_id":"two"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            read_registration(self.registration)

    def test_registration_after_application_before_after_window_still_late(self):
        self.edit_json(self.registration, lambda r: (r.update(registered_at="2030-01-08T01:00:00Z"),
            r.update(after={"since": "2030-01-09T00:00:00Z", "until": "2030-01-16T00:00:00Z"})))
        self.assertFalse(read_registration(self.registration).preregistered)


if __name__ == "__main__":
    unittest.main()
