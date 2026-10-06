"""Compatibility entry point for GitHub comparisons."""

from pathlib import Path

from sumbi.judge.compare import (DECISION_ORDER, EXCLUDED_SHARE, METRICS, MIX_DISTANCE,
                                 UNATTRIBUTED_SHARE, agent_only_in_one_arm, fraction,
                                 mix_distance, mix_report, text_summary, verdict)
from sumbi.judge.compare import compare as compare_units
from sumbi.outcomes.github.source import GitHubSource


def compare(home: Path, ledger_path: Path, outcomes, registration_path: Path, *,
            agents=None, rules=None, idle_minutes=5, salt=None, seed=1729, resamples=5000,
            interventions_path=None, intervention_id=None):
    source = GitHubSource(ledger_path, outcomes, rules, idle_minutes)
    return compare_units(home, registration_path, source, agents=agents, salt=salt, seed=seed,
                         resamples=resamples, interventions_path=interventions_path, intervention_id=intervention_id)
