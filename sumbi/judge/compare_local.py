"""Compatibility entry point for local-verification comparisons."""

from pathlib import Path

from sumbi.judge.compare import compare as compare_units, text_summary
from sumbi.outcomes.local_verify.source import LocalVerifySource


def compare_local(home: Path, repository: Path, registration_path: Path, *, agents=None,
                  verify=None, scan_until=None, active_minutes=5, salt=None, seed=1729, resamples=5000,
                  interventions_path=None, intervention_id=None):
    source = LocalVerifySource(repository, verify, scan_until, active_minutes)
    return compare_units(home, registration_path, source, agents=agents, salt=salt, seed=seed,
                         resamples=resamples, interventions_path=interventions_path, intervention_id=intervention_id)
