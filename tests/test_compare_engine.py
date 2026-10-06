"""Source-neutral dispatch, gate order, and statistical-input contracts."""

import ast
from collections import Counter
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from sumbi.core.stats import estimate
from sumbi.core.values import timestamp
from sumbi.judge.compare import arm_metadata, assign_arms, compare, metadata_mixes
from sumbi.judge.compare_github import compare as compare_github
from sumbi.judge.compare_local import compare_local
from sumbi.judge.stats import bootstrap
from sumbi.outcomes.github.source import GitHubSource
from sumbi.outcomes.local_verify.source import LocalVerifySource
from sumbi.outcomes.units import Unit, flag
from sumbi.sessions.session import TOKEN_KINDS

ROOT = Path(__file__).resolve().parent.parent
REGISTRATION = ROOT / "tests/fixtures/compare/registration.json"


def unit(identity, dispatch, *, state="success", order_key=None, exposure=None, **fields):
    when = timestamp(dispatch)
    return Unit(identity, when, state, "synthetic", (exposure or when,), fields.pop("session_metadata", ()),
                {**dict.fromkeys(TOKEN_KINDS, 10), "total": 40}, True, 30, None,
                {"id": identity}, order_key or identity, **fields)


class SyntheticSource:
    """An independent plug-in exercising the engine without either delivery path."""

    name = "github"
    registration_error = "synthetic source mismatch"
    mix_kinds = ("model", "effort", "cli_version")
    deduplicate_metadata = True
    exclusion_flag = "_excluded_or_unlinked"
    exclusion_collection = "deliverables"
    risky_exclusions = False
    sort_exclusion_counts = True
    task_breakdowns = True
    finalized_elapsed_only = True

    def __init__(self, rows, reasons=()):
        self.rows, self.reasons, self.stages = rows, reasons, []

    def measure(self, home, period, registration, windows, *, agents, salt):
        return self.rows, {"coverage": {"adapters": {}}}

    def source_gates(self, arms, *, candidates, report, stage):
        self.stages.append(stage)
        return [flag("synthetic_" + stage, True, {})]

    def coverage_reasons(self, arms, *, candidates, report, windows):
        return list(self.reasons)

    def public_fields(self, report, comparison):
        return comparison


class ComparisonEngineTests(unittest.TestCase):
    def test_metadata_observations_keep_each_sources_historical_counting_unit(self):
        metadata = {"id": "shared", "agent": "codex", "model": ["m"], "effort": [], "cli_version": []}
        row = unit("u1", "2030-01-02T00:00:00Z", session_metadata=(metadata,))
        arms = {"before": [row, replace(row, id="u2")], "after": []}
        for source, expected in ((GitHubSource(Path("ledger"), object()), 1),
                                 (LocalVerifySource(Path("repository")), 2)):
            with self.subTest(source=source.name):
                observations = arm_metadata(arms, deduplicate=source.deduplicate_metadata)
                mixes = metadata_mixes(observations, source.mix_kinds, [])
                self.assertEqual(mixes["model"]["before"]["values"]["m"]["numerator"], expected)

    def test_statistical_primitives_accept_units_without_zero_filling(self):
        row = unit("u1", "2030-01-02T00:00:00Z")
        self.assertEqual(estimate([row], "total")["value"], 40)
        missing = replace(row, tokens={**row.tokens, "total": None}, tokens_complete=False)
        self.assertIsNone(estimate([row, missing], "total")["numerator"])
        costs, ratios = bootstrap([row], [row], ("total", "time"), resamples=100)
        self.assertEqual(costs["before"]["total"]["interval_95"], [40, 40])
        self.assertEqual(ratios["time"]["value"], 1)
        with self.assertRaises(KeyError):
            row["public"]

    def test_third_source_uses_shared_dispatch_statistics_and_ordered_gates(self):
        rows = [unit("public-a", "2030-01-02T00:00:00Z", order_key="z"),
                unit("public-z", "2030-01-03T00:00:00Z", order_key="a"),
                unit("after", "2030-01-09T00:00:00Z"),
                unit("hiatus", "2030-02-01T00:00:00Z")]
        source = SyntheticSource(rows)
        result = compare(Path("synthetic"), REGISTRATION, source, salt=b"synthetic", resamples=100)
        self.assertEqual(source.stages, ["candidates", "retained", "coverage"])
        self.assertEqual([u.id for u in result["arms"]["before"]], ["public-z", "public-a"])
        self.assertEqual(result["rates"]["before"]["denominator"], 2)
        self.assertEqual([f["name"] for f in result["flags"]],
                         ["synthetic_candidates", "volume_shift", "synthetic_retained", "synthetic_coverage"])
        self.assertEqual(result["verdict"]["reasons"],
                         ["not_comparable", "synthetic_candidates", "synthetic_coverage", "synthetic_retained"])

    def test_coverage_precedes_source_gates_and_statistical_rejection(self):
        source = SyntheticSource([unit("failed", "2030-01-09T00:00:00Z", state="failed")],
                                 reasons=("synthetic_gap",))
        result = compare(Path("synthetic"), REGISTRATION, source, salt=b"synthetic", resamples=100)
        self.assertEqual(result["verdict"], {"label": "proposal", "proposal": "withhold",
                                           "reasons": ["incomplete_coverage", "synthetic_gap"]})

    def test_exclusion_precedence_and_no_change_risk_share(self):
        rows = [unit("gap", "2030-01-08T00:00:00Z", state="no_change", excluded_state="no_change"),
                unit("no-change", "2030-01-09T00:00:00Z", state="no_change", excluded_state="no_change",
                     exposure=timestamp("2030-01-02T00:00:00Z")),
                unit("mixed", "2030-01-09T00:00:00Z", exposure=timestamp("2030-01-02T00:00:00Z"))]
        gap = {"since": "2030-01-08T00:00:00Z", "until": "2030-01-08T01:00:00Z",
               "actual_write_at": "2030-01-08T01:00:00Z"}
        flags = []
        arms, excluded = assign_arms({"before": [], "after": rows}, timestamp("2030-01-08T00:00:00Z"),
                                     gap, LocalVerifySource(Path("synthetic")), flags)
        self.assertEqual(arms, {"before": [], "after": []})
        self.assertEqual([r["reason"] for r in excluded["after"]["units"]],
                         ["exposure_gap", "no_change", "exposure_mixed"])
        self.assertEqual(excluded["after"]["share"]["value"], 1)
        self.assertEqual(excluded["after"]["risky_share"]["value"], 2 / 3)
        self.assertEqual(flags[0]["name"], "after_excluded_or_mixed")

    def test_one_observability_policy_preserves_incomplete_and_absent_agents(self):
        metadata = {"before": [{"id": "b", "agent": "codex", "model": ["m"], "effort": [],
                                "cli_version": [], "metadata_incomplete": {"model": 1}}],
                    "after": [{"id": "a", "agent": "claude", "model": [], "effort": [], "cli_version": []}]}
        flags = []
        metadata_mixes(metadata, ("model", "effort", "cli_version"), flags)
        self.assertEqual([f["name"] for f in flags], ["agent_mix_shift", "model_unobservable",
                         "model_metadata_partial", "effort_unobservable", "cli_version_unobservable"])
        self.assertNotIn("model_metadata_asymmetric", Counter(f["name"] for f in flags))

    def test_compatibility_entries_delegate_to_the_same_engine(self):
        args = (Path("home"), Path("input"), Path("registration"))
        with patch("sumbi.judge.compare_github.compare_units", return_value={}) as engine:
            compare_github(args[0], args[1], object(), args[2], resamples=100)
            self.assertIsInstance(engine.call_args.args[2], GitHubSource)
        with patch("sumbi.judge.compare_local.compare_units", return_value={}) as engine:
            compare_local(*args, resamples=100)
            self.assertIsInstance(engine.call_args.args[2], LocalVerifySource)

    def test_judge_and_new_source_functions_fit_eighty_lines(self):
        paths = list((ROOT / "sumbi/judge").rglob("*.py")) + [ROOT / p for p in (
            "sumbi/outcomes/units.py", "sumbi/outcomes/github/source.py", "sumbi/outcomes/local_verify/source.py")]
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    with self.subTest(module=path.relative_to(ROOT).as_posix(), function=node.name):
                        self.assertLessEqual(node.end_lineno - node.lineno + 1, 80)


if __name__ == "__main__":
    unittest.main()
