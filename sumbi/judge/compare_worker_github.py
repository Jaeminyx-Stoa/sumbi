"""Select the worker GitHub source without duplicating comparison policy."""

from sumbi.judge.compare import compare as compare_units
from sumbi.judge.compare import text_summary
from sumbi.outcomes.worker_github.source import WorkerGitHubSource


def compare_workers(home, repos, outcomes, registration_path, *, agents=None, salt=None,
    seed=1729, resamples=5000, interventions_path=None, intervention_id=None):
    return compare_units(home, registration_path, WorkerGitHubSource(repos, outcomes),
        agents=agents, salt=salt, seed=seed, resamples=resamples,
        interventions_path=interventions_path, intervention_id=intervention_id)
