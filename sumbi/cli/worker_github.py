"""Validate and publish worker GitHub reports using existing private live capture."""

import json
from pathlib import Path
import sys

from sumbi.cli.common import atomic_write
from sumbi.core.privacy import read_salt
from sumbi.judge.compare_worker_github import compare_workers, text_summary as compare_summary
from sumbi.outcomes.github import live as github
from sumbi.outcomes.github.recorded import FixtureOutcomes
from sumbi.outcomes.worker_github.links import repositories
from sumbi.outcomes.worker_github.workers import deliver_workers, text_summary


def live_factory(args, resolved, sources):
    cache = args.cache if args.cache is not None else Path.home() / ".cache/sumbi/outcomes"
    stores = [github.outside_repository(p) for p in (cache, args.record) if p is not None]
    for destination in resolved:
        github.outside_repository(destination)
    protected = [*resolved]
    for option in ("registration", "interventions", "salt_file"):
        path = getattr(args, option, None)
        if path is not None:
            protected.append(path.resolve())
    if any(p.is_relative_to(store) for p in protected for store in stores) or any(
        store.is_relative_to(source.resolve()) or source.resolve().is_relative_to(store)
        for store in stores for source in sources):
        raise ValueError("GitHub cache and record must not overlap inputs or output")
    return lambda dispatches: github.GitHubOutcomes(dispatches, cache=cache,
        record=args.record, head_refs=True)


def run(args, root, window, agents, rules, resolved, sources, registration):
    if args.repo is None or args.outcomes is None:
        root.error("Worker GitHub outcomes require --repo and --outcomes")
    if any(v is not None for v in (args.ledger, args.repository, args.verify, args.scan_until)):
        root.error("Worker GitHub uses remote repositories rather than ledger "
            "or local verification")
    if rules:
        root.error("Worker GitHub scope uses --repo rather than project matching rules")
    live = args.outcomes == "github"
    outcome_dir = None if live else Path(args.outcomes)
    if not live and (args.cache is not None or args.record is not None):
        root.error("Cache and record options require --outcomes github")
    if outcome_dir and any(p.is_relative_to(outcome_dir.resolve()) for p in resolved):
        root.error("Worker output must not overwrite outcome fixtures")
    if registration and args.registration.resolve() in resolved:
        root.error("Compare output must not overwrite registration")
    try:
        repos = repositories(args.repo)
        salt = read_salt(args.salt_file)
        outcomes = live_factory(args, resolved, sources) if live else FixtureOutcomes(outcome_dir)
        summary = text_summary
        if registration:
            summary = compare_summary
            report = compare_workers(args.home, repos, outcomes, args.registration,
                agents=agents, salt=salt, seed=args.seed, resamples=args.resamples,
                interventions_path=args.interventions, intervention_id=args.intervention_id)
        else:
            report = deliver_workers(args.home, window, repos, outcomes, agents=agents,
                salt=salt, follow_up_days=args.follow_up_days)
        data = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if args.json == "-":
            print(data, end="")
        else:
            atomic_write(Path(args.json), data)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Worker collection or output failed; no source was modified.", file=sys.stderr)
        return 1
    print(summary(report), file=sys.stderr if args.json == "-" else sys.stdout)
    return 0
