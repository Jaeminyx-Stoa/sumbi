"""Comparison command dispatch."""

from pathlib import Path
from sumbi.cli.common import configure_collection, configure_outcome_source

from sumbi.cli.deliver import run as run_outcomes


def run(args, root, window, agents, rules, resolved, sources, registration):
    return run_outcomes(args, root, window, agents, rules, resolved, sources, registration)


def configure_parser(command):
    configure_collection(command, deliver=True, comparison=True)
    configure_outcome_source(command)
    command.add_argument("--cache", type=Path, metavar="DIR")
    command.add_argument("--record", type=Path, metavar="DIR")
    command.add_argument("--registration", type=Path, required=True)
    command.add_argument("--interventions", type=Path, metavar="FILE",
        help="Optional install intervention JSONL; exclude and count "
        "actual apply-time exposure gaps")
    command.add_argument("--intervention-id",
        help="Apply record ID (defaults to registration ID; must match it)")
    command.add_argument("--seed", type=int, default=1729)
    command.add_argument("--resamples", type=int, default=5000)
