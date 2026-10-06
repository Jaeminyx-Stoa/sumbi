"""Select the worker GitHub source without duplicating comparison policy."""

from sumbi.judge.compare import compare as compare_units
from sumbi.judge.compare import text_summary as comparison_summary
from sumbi.outcomes.worker_github.source import WorkerGitHubSource
from sumbi.outcomes.worker_github.workers import reason_lines


def compare_workers(home, repos, outcomes, registration_path, *, agents=None, salt=None,
    seed=1729, resamples=5000, interventions_path=None, intervention_id=None, repo_owners=()):
    return compare_units(home, registration_path, WorkerGitHubSource(repos, outcomes, repo_owners),
        agents=agents, salt=salt, seed=seed, resamples=resamples,
        interventions_path=interventions_path, intervention_id=intervention_id)


def text_summary(report):
    lines = [comparison_summary(report)]
    for arm, row in report["arms"].items():
        lines.extend(reason_lines(row["candidate_state_reasons"], prefix=f"{arm} candidate "))
    return "\n".join(lines)
