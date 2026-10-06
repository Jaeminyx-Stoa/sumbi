"""Shared command-line entry point for offline install and collection."""

from __future__ import annotations

import argparse
from pathlib import Path

from sumbi.core.time import Window
from sumbi import __version__
from sumbi.cli.install import configure_parser as configure_install, run as run_install
from sumbi.events.registry import ADAPTERS, log_roots


from sumbi.cli.common import configure_collection
from sumbi.cli.collect import run as run_collect
from sumbi.cli.deliver import run as run_deliver, configure_parser as configure_deliver
from sumbi.cli.compare import run as run_compare, configure_parser as configure_compare
from sumbi.cli.register import run as run_register, configure_parser as configure_register
from sumbi.judge.registration import read_registration

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sumbi")
    root.add_argument("--version", action="version", version="sumbi " + __version__)
    commands = root.add_subparsers(dest="command", required=True)
    configure_register(commands.add_parser("register", help="Create a validated catalog pre-registration; never overwrite"))
    configure_install(commands.add_parser("install", help="Inventory and seed repository practices offline"))
    configure_collection(commands.add_parser("collect", help="Measure local session logs without transcripts"))
    configure_deliver(commands.add_parser("deliver", help="Measure GitHub deliverables or local worker verification"))
    configure_compare(commands.add_parser("compare", help="Propose a verdict for one pre-registered intervention"))
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command == "install":
        return run_install(args)
    if args.command == "register":
        return run_register(args)
    try:
        if args.command == "compare":
            registration = read_registration(args.registration)
            window = Window(registration.before.since, registration.after.until)
        else:
            window = Window(args.since, args.until)
    except ValueError as exc:
        root.error(str(exc))
    agents = args.agents.split(",")
    if not agents or any(agent not in ADAPTERS for agent in agents) or len(set(agents)) != len(agents):
        root.error("Agents must be a unique comma-separated selection of " + ",".join(ADAPTERS))
    rules = getattr(args, "rules", [])
    if any(not rule.origins and not rule.paths for rule in rules):
        root.error("Each project rule needs at least one matching pattern")
    destinations = ([Path(args.json)] if args.json != "-" else []) + ([args.local_review] if args.local_review else [])
    resolved = [p.resolve() for p in destinations]
    if len(set(resolved)) != len(resolved):
        root.error("JSON and local review must have distinct destinations")
    if args.salt_file is not None and args.salt_file.resolve() in resolved:
        root.error("Output destinations must not overwrite the pseudonym salt file")
    sources = log_roots(args.home)
    if args.command == "compare":
        if args.intervention_id is not None and args.interventions is None:
            root.error("--intervention-id requires --interventions")
        if args.interventions is not None and args.interventions.resolve() in resolved:
            root.error("Compare output must not overwrite interventions")
    if any(p.is_relative_to(source.resolve()) for p in resolved for source in sources):
        root.error("Output destinations must be outside session-log directories")
    if args.command == "compare":
        return run_compare(args, root, window, agents, rules, resolved, sources, registration)
    if args.command == "deliver":
        return run_deliver(args, root, window, agents, rules, resolved, sources)
    return run_collect(args, window, agents, rules)
