"""Shared command-line entry point for offline install and collection."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

from sumbi.model import ProjectRule, Window, timestamp
from sumbi import __version__
from sumbi.install.__main__ import configure_parser as configure_install, run as run_install
from sumbi.privacy import read_salt
from sumbi.report import ADAPTERS, collect, log_roots, text_summary


class RuleAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        rules = getattr(namespace, "rules", None)
        if rules is None:
            rules = []
            namespace.rules = rules
        if option_string == "--project":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", values):
                parser.error("Project rule names must be bounded labels")
            if any(rule.name == values for rule in rules):
                parser.error("Project rule names must be unique")
            rules.append(ProjectRule(values))
        else:
            if not rules:
                parser.error("A matching pattern must follow --project")
            getattr(rules[-1], "origins" if option_string == "--match-origin" else "paths").append(values)


def utc(value: str):
    parsed = timestamp(value)
    # Reject offset input even if it can be converted to UTC.
    if parsed is None or not (value.endswith("Z") or value.endswith("+00:00")):
        raise argparse.ArgumentTypeError("Expected a UTC ISO timestamp ending in Z or +00:00")
    return parsed


def idle(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Idle minutes must be a positive finite number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Idle minutes must be a positive finite number")
    return number


def atomic_write(path: Path, text: str, *, private: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=".sumbi-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        if private:
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sumbi")
    root.add_argument("--version", action="version", version="sumbi " + __version__)
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("register", help="Create a validated catalog pre-registration; never overwrite")
    command.add_argument("--json", type=Path, required=True, metavar="OUT", help="New local registration JSON file")
    command.add_argument("--intervention-id", required=True, help="Public-safe intervention ID")
    command.add_argument("--outcome-source", required=True, choices=("github", "local-verify"))
    command.add_argument("--margin-pp", "--non-inferiority-margin-pp", dest="margin_pp", type=float, required=True,
                         help="Acceptable success-rate drop in absolute percentage points (0 < margin < 100)")
    command.add_argument("--sample-size-per-arm", type=int, required=True, help="Positive registered sample per arm")
    command.add_argument("--follow-up-days", type=idle, required=True, help="Positive outcome follow-up days")
    command.add_argument("--window-days", type=idle, required=True, help="Equal adjacent dispatch windows around planned application")
    command.add_argument("--applied-at", type=utc, required=True, help="Planned UTC application time; registered_at is now")
    command.add_argument("--practice", action="append", required=True, metavar="ID", help="Catalog prediction practice ID; repeatable")
    configure_install(commands.add_parser("install", help="Inventory and seed repository practices offline"))
    configure_collection(commands.add_parser("collect", help="Measure local session logs without transcripts"))
    command = commands.add_parser("deliver", help="Measure GitHub deliverables or local worker verification")
    configure_collection(command, deliver=True)
    configure_outcome_source(command)
    command.add_argument("--cache", type=Path, metavar="DIR", help="Private GitHub response cache outside repositories")
    command.add_argument("--record", type=Path, metavar="DIR", help="Record filtered GitHub responses for offline replay")
    command.add_argument("--follow-up-days", type=idle, default=7.0)
    command = commands.add_parser("compare", help="Propose a verdict for one pre-registered intervention")
    configure_collection(command, deliver=True, comparison=True)
    configure_outcome_source(command)
    command.add_argument("--cache", type=Path, metavar="DIR")
    command.add_argument("--record", type=Path, metavar="DIR")
    command.add_argument("--registration", type=Path, required=True)
    command.add_argument("--interventions", type=Path, metavar="FILE",
                         help="Optional install intervention JSONL; exclude and count actual apply-time exposure gaps")
    command.add_argument("--intervention-id", help="Apply record ID (defaults to registration ID; must match it)")
    command.add_argument("--seed", type=int, default=1729)
    command.add_argument("--resamples", type=int, default=5000)
    return root


def configure_outcome_source(command):
    command.add_argument("--outcome-source", choices=("github", "local-verify"),
                         help="Local verification reports verifier_never_passed with exit-code counts; compare blocks before non-inferiority")
    command.add_argument("--ledger", type=Path)
    command.add_argument("--outcomes", metavar="github|DIR")
    command.add_argument("--repository", type=Path, help="Repository scope for local verification units")
    command.add_argument("--verify", action="append", help="Repository-relative executed verification script")
    command.add_argument("--scan-until", type=utc, help="Lifetime observation end for local verification")
    command.add_argument("--active-minutes", type=idle, default=5.0)


def configure_collection(command, *, deliver=False, comparison=False):
    if not comparison:
        command.add_argument("--since", type=utc, required=True)
        command.add_argument("--until", type=utc, required=True)
    command.add_argument("--home", type=Path, default=Path.home())
    command.add_argument("--agents", default="claude-code,codex")
    for option in ("--project", "--match-origin", "--match-path"):
        command.add_argument(option, action=RuleAction)
    command.add_argument("--idle-minutes", type=idle, default=5.0)
    command.add_argument("--json", default="out/compare.json" if comparison else "out/deliver.json" if deliver else "out/collect.json", metavar="OUT")
    if not deliver:
        command.add_argument("--local-review", type=Path, metavar="OUT")
    else:
        command.set_defaults(local_review=None)
    command.add_argument("--salt-file", type=Path, help="Local pseudonym key file (overrides SUMBI_SALT)")


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command == "install":
        return run_install(args)
    if args.command == "register":
        try:
            from sumbi.registration import write_registration
            write_registration(args.json, intervention_id=args.intervention_id, outcome_source=args.outcome_source,
                               margin_pp=args.margin_pp, sample_size_per_arm=args.sample_size_per_arm,
                               follow_up_days=args.follow_up_days, window_days=args.window_days,
                               applied_at=args.applied_at.isoformat(), practice_ids=args.practice)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except OSError:
            print("Registration write failed; existing files were not replaced.", file=sys.stderr)
            return 1
        print("Pre-registration written; registered_at is now; predictions copied from catalog.")
        return 0
    try:
        if args.command == "compare":
            from sumbi.registration import read_registration
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
    if args.command in ("deliver", "compare"):
        source = args.outcome_source or (registration.outcome_source if args.command == "compare" else "github")
        if args.command == "compare" and registration.outcome_source != source:
            root.error("Registration and both arms must use the same outcome source")
        if source == "local-verify":
            if args.repository is None:
                root.error("Local verification requires --repository")
            if any(v is not None for v in (args.ledger, args.outcomes, args.cache, args.record)):
                root.error("Local verification uses worker sessions rather than ledger or GitHub options")
            protected = [args.repository / ".sumbi" / "config.toml"]
            if args.command == "compare":
                protected.append(args.registration)
            if any(p.resolve() in resolved for p in protected):
                root.error("Local output must not overwrite config or registration")
            try:
                from sumbi.local_outcomes import deliver_local, text_summary as local_summary
                salt = read_salt(args.salt_file)
                options = dict(agents=agents, verify=args.verify, scan_until=args.scan_until,
                               active_minutes=args.active_minutes, salt=salt)
                if args.command == "compare":
                    from sumbi.local_compare import compare_local, text_summary as local_summary
                    report = compare_local(args.home, args.repository, args.registration,
                                           **options, seed=args.seed, resamples=args.resamples,
                                           interventions_path=args.interventions, intervention_id=args.intervention_id)
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
        if args.ledger is None or args.outcomes is None:
            root.error("GitHub outcomes require --ledger and --outcomes")
        if args.repository is not None or args.verify is not None or args.scan_until is not None:
            root.error("Local verification options require local-verify outcome source")
        live = args.outcomes == "github"
        outcome_dir = None if live else Path(args.outcomes)
        if not live and (args.cache is not None or args.record is not None):
            root.error("Cache and record options require --outcomes github")
        if any(p == args.ledger.resolve() or (outcome_dir and p.is_relative_to(outcome_dir.resolve())) for p in resolved):
            root.error("Deliver output must not overwrite ledger or outcome fixtures")
        if args.command == "compare" and args.registration.resolve() in resolved:
            root.error("Compare output must not overwrite registration")
        from sumbi.deliver import deliver, text_summary as deliver_summary
        from sumbi.outcomes import FixtureOutcomes
        try:
            salt = read_salt(args.salt_file)
            if live:
                from sumbi.github_outcomes import GitHubOutcomes, outside_repository
                from sumbi.ledger import read_ledger
                cache = args.cache if args.cache is not None else Path.home() / ".cache/sumbi/outcomes"
                stores = [outside_repository(p) for p in (cache, args.record) if p is not None]
                for destination in resolved:
                    outside_repository(destination)
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
                outcomes = GitHubOutcomes(read_ledger(args.ledger), cache=cache, record=args.record)
            else:
                outcomes = FixtureOutcomes(outcome_dir)
            if args.command == "compare":
                from sumbi.compare import compare, text_summary as deliver_summary
                report = compare(args.home, args.ledger, outcomes, args.registration,
                                 agents=agents, rules=rules, idle_minutes=args.idle_minutes,
                                 salt=salt, seed=args.seed, resamples=args.resamples,
                                 interventions_path=args.interventions, intervention_id=args.intervention_id)
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
    try:
        salt = read_salt(args.salt_file)
        report, review = collect(args.home, window, agents=agents, rules=rules,
                                 idle_minutes=args.idle_minutes, local_review=bool(args.local_review), salt=salt)
        data = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if args.json == "-":
            print(data, end="")
        else:
            atomic_write(Path(args.json), data)
        if args.local_review:
            atomic_write(args.local_review, review, private=True)
    except (OSError, ValueError):
        print("Collection or output failed; no source log was modified.", file=sys.stderr)
        return 1
    print(text_summary(report), file=sys.stderr if args.json == "-" else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
