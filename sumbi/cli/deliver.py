"""Validate outcome command inputs and dispatch GitHub or local verification reports."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from sumbi.core.privacy import read_salt


from sumbi.cli.common import atomic_write, configure_collection, configure_outcome_source, idle
from sumbi.outcomes.local_verify.workers import deliver_local, text_summary as local_deliver_summary
from sumbi.judge.compare_local import compare_local, text_summary as compare_local_summary
from sumbi.outcomes.github.deliver import deliver, text_summary as github_deliver_summary
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.outcomes.github import live as github_outcomes
from sumbi.outcomes.github.ledger import read_ledger
from sumbi.judge.compare_github import compare, text_summary as compare_summary
from sumbi.cli.worker_github import run as run_workers


def run(args, root, window, agents, rules, resolved, sources, registration=None):
    deliver_summary = github_deliver_summary
    source = args.outcome_source or (registration.outcome_source if args.command == "compare"
        else "github")
    if args.command == "compare" and registration.outcome_source != source:
        root.error("Registration and both arms must use the same outcome source")
    if source == "worker-github":
        return run_workers(args, root, window, agents, rules, resolved, sources, registration)
    if args.repo is not None or args.repo_owner is not None:
        root.error("Remote repository options require worker-github outcome source")
    if source == "local-verify":
        return _run_local(args, root, window, agents, resolved, registration)
    if args.ledger is None or args.outcomes is None:
        root.error("GitHub outcomes require --ledger and --outcomes")
    if args.repository is not None or args.verify is not None or args.scan_until is not None:
        root.error("Local verification options require local-verify outcome source")
    live = args.outcomes == "github"
    outcome_dir = None if live else Path(args.outcomes)
    if not live and (args.cache is not None or args.record is not None):
        root.error("Cache and record options require --outcomes github")
    if any(p == args.ledger.resolve()
        or (outcome_dir and p.is_relative_to(outcome_dir.resolve())) for p in resolved):
        root.error("Deliver output must not overwrite ledger or outcome fixtures")
    if args.command == "compare" and args.registration.resolve() in resolved:
        root.error("Compare output must not overwrite registration")
    try:
        salt = read_salt(args.salt_file)
        if live:
            cache = args.cache if args.cache is not None else Path.home() / ".cache/sumbi/outcomes"
            stores = [github_outcomes.outside_repository(p) for p in (cache, args.record)
                if p is not None]
            for destination in resolved:
                github_outcomes.outside_repository(destination)
            protected = [args.ledger.resolve(), *resolved]
            if args.command == "compare":
                protected.append(args.registration.resolve())
                if args.interventions:
                    protected.append(args.interventions.resolve())
            if args.salt_file:
                protected.append(args.salt_file.resolve())
            if any(p.is_relative_to(store) for p in protected for store in stores) or any(
                store.is_relative_to(source.resolve()) or source.resolve().is_relative_to(store)
                for store in stores for source in sources):
                raise ValueError("GitHub cache and record must not overlap inputs or output")
            outcomes = github_outcomes.GitHubOutcomes(read_ledger(args.ledger), cache=cache,
                record=args.record)
        else:
            outcomes = FixtureOutcomes(outcome_dir)
        if args.command == "compare":
            deliver_summary = compare_summary
            report = compare(args.home, args.ledger, outcomes, args.registration,
                agents=agents, rules=rules, idle_minutes=args.idle_minutes,
                salt=salt, seed=args.seed, resamples=args.resamples,
                interventions_path=args.interventions,
                intervention_id=args.intervention_id)
        else:
            report = deliver(args.home, window, args.ledger, outcomes,
                agents=agents, rules=rules, idle_minutes=args.idle_minutes,
                salt=salt, follow_up_days=args.follow_up_days)
        data = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if args.json == "-":
            print(data, end="")
        else:
            atomic_write(Path(args.json), data)
    except ValueError as exc:
        # These modules emit fixed field diagnostics, never input values.
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Deliver collection or output failed; no source was modified.", file=sys.stderr)
        return 1
    print(deliver_summary(report), file=sys.stderr if args.json == "-" else sys.stdout)
    return 0


def configure_parser(command):
    configure_collection(command, deliver=True)
    configure_outcome_source(command)
    command.add_argument("--cache", type=Path, metavar="DIR",
        help="Private GitHub response cache outside repositories")
    command.add_argument("--record", type=Path, metavar="DIR",
        help="Record filtered GitHub responses for offline replay")
    command.add_argument("--follow-up-days", type=idle, default=7.0)


def _run_local(args, root, window, agents, resolved, registration):
    local_summary = local_deliver_summary
    if args.repository is None:
        root.error("Local verification requires --repository")
    if any(v is not None for v in (args.ledger, args.outcomes, args.cache, args.record)):
        root.error(
            "Local verification uses worker sessions rather than ledger or GitHub options")
    protected = [args.repository / ".sumbi" / "config.toml"]
    if args.command == "compare":
        protected.append(args.registration)
    if any(p.resolve() in resolved for p in protected):
        root.error("Local output must not overwrite config or registration")
    try:
        salt = read_salt(args.salt_file)
        options = dict(agents=agents, verify=args.verify, scan_until=args.scan_until,
            active_minutes=args.active_minutes, salt=salt)
        if args.command == "compare":
            local_summary = compare_local_summary
            report = compare_local(args.home, args.repository, args.registration,
                **options, seed=args.seed, resamples=args.resamples,
                interventions_path=args.interventions,
                intervention_id=args.intervention_id)
        else:
            report = deliver_local(args.home, window, args.repository, **options)
        data = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if args.json == "-":
            print(data, end="")
        else:
            atomic_write(Path(args.json), data)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Local collection or output failed; no source was modified.", file=sys.stderr)
        return 1
    print(local_summary(report), file=sys.stderr if args.json == "-" else sys.stdout)
    return 0
