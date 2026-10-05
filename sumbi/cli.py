"""Command-line entry point for the M1a collection core."""

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
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("collect", help="Measure local session logs without transcripts")
    command.add_argument("--since", type=utc, required=True)
    command.add_argument("--until", type=utc, required=True)
    command.add_argument("--home", type=Path, default=Path.home())
    command.add_argument("--agents", default="claude-code,codex")
    for option in ("--project", "--match-origin", "--match-path"):
        command.add_argument(option, action=RuleAction)
    command.add_argument("--idle-minutes", type=idle, default=5.0)
    command.add_argument("--json", default="out/collect.json", metavar="OUT")
    command.add_argument("--local-review", type=Path, metavar="OUT")
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
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
    sources = [args.home / ".claude" / "projects", args.home / ".codex" / "sessions"]
    if any(p.is_relative_to(source.resolve()) for p in resolved for source in sources):
        root.error("Output destinations must be outside session-log directories")
    try:
        report, review = collect(args.home, window, agents=agents, rules=rules,
                                 idle_minutes=args.idle_minutes, local_review=bool(args.local_review))
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
