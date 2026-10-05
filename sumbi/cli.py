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
from sumbi.report import ADAPTERS, collect, text_summary


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
    configure_install(commands.add_parser("install", help="Inventory and seed repository practices offline"))
    configure_collection(commands.add_parser("collect", help="Measure local session logs without transcripts"))
    command = commands.add_parser("deliver", help="Join a dispatch ledger with offline outcomes and sessions")
    configure_collection(command, deliver=True)
    command.add_argument("--ledger", type=Path, required=True)
    command.add_argument("--outcomes", type=Path, required=True)
    command.add_argument("--follow-up-days", type=idle, default=7.0)
    return root


def configure_collection(command, *, deliver=False):
    command.add_argument("--since", type=utc, required=True)
    command.add_argument("--until", type=utc, required=True)
    command.add_argument("--home", type=Path, default=Path.home())
    command.add_argument("--agents", default="claude-code,codex")
    for option in ("--project", "--match-origin", "--match-path"):
        command.add_argument(option, action=RuleAction)
    command.add_argument("--idle-minutes", type=idle, default=5.0)
    command.add_argument("--json", default="out/deliver.json" if deliver else "out/collect.json", metavar="OUT")
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
    try:
        window = Window(args.since, args.until)
    except ValueError as exc:
        root.error(str(exc))
    agents = args.agents.split(",")
    if not agents or any(agent not in ADAPTERS for agent in agents) or len(set(agents)) != len(agents):
        root.error("Agents must be a unique comma-separated selection of claude-code,codex")
    rules = getattr(args, "rules", [])
    if any(not rule.origins and not rule.paths for rule in rules):
        root.error("Each project rule needs at least one matching pattern")
    destinations = ([Path(args.json)] if args.json != "-" else []) + ([args.local_review] if args.local_review else [])
    resolved = [p.resolve() for p in destinations]
    if len(set(resolved)) != len(resolved):
        root.error("JSON and local review must have distinct destinations")
    if args.salt_file is not None and args.salt_file.resolve() in resolved:
        root.error("Output destinations must not overwrite the pseudonym salt file")
    sources = [args.home / ".claude" / "projects", args.home / ".codex" / "sessions"]
    if any(p.is_relative_to(source.resolve()) for p in resolved for source in sources):
        root.error("Output destinations must be outside session-log directories")
    if args.command == "deliver":
        if any(p == args.ledger.resolve() or p.is_relative_to(args.outcomes.resolve()) for p in resolved):
            root.error("Deliver output must not overwrite ledger or outcome fixtures")
        from sumbi.deliver import deliver, text_summary as deliver_summary
        from sumbi.outcomes import FixtureOutcomes
        try:
            salt = read_salt(args.salt_file)
            report = deliver(args.home, window, args.ledger, FixtureOutcomes(args.outcomes),
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
