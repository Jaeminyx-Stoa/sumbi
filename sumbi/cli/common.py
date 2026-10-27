"""Parse shared CLI values and project rules, and publish command output atomically."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import re
import tempfile

from sumbi.events.registry import log_roots
from sumbi.measure.attribution import ProjectRule
from sumbi.core.values import timestamp


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
            getattr(rules[-1], "origins" if option_string == "--match-origin"
                else "paths").append(values)


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


def inside_log_roots(path: Path, home: Path) -> bool:
    """True when a resolved output path is inside a local agent-log directory."""
    return any(path.is_relative_to(source.resolve()) for source in log_roots(home))


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


def configure_outcome_source(command):
    command.add_argument("--outcome-source", choices=("github", "local-verify", "worker-github"),
        help="Local verification reports verifier_never_passed with "
        "exit-code counts; compare blocks before non-inferiority")
    command.add_argument("--ledger", type=Path)
    command.add_argument("--outcomes", metavar="github|DIR")
    command.add_argument("--repo", action="append", metavar="owner/name",
        help="Measured remote repository for worker GitHub outcomes; repeatable")
    command.add_argument("--repo-owner", action="append", metavar="OWNER",
        help="Include evidenced repositories under this owner for worker-github (repeatable)")
    command.add_argument("--repository", type=Path,
        help="Repository scope for local verification units")
    command.add_argument("--verify", action="append",
        help="Repository-relative executed verification script")
    command.add_argument("--scan-until", type=utc,
        help="Lifetime observation end for local verification")
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
    command.add_argument("--json", default="out/compare.json" if comparison
        else "out/deliver.json" if deliver else "out/collect.json", metavar="OUT")
    if not deliver and not comparison:
        command.add_argument("--long-session-minutes", type=idle, default=60.0,
            help="Active minutes above which a session counts as long (profile)")
        command.add_argument("--session-labels", type=Path, metavar="FILE",
            help="JSONL session labels (lane, account, start_kind, host) by session ID prefix")
        command.add_argument("--forbid-model", action="append", default=[], metavar="PATTERN",
            help="Report spend on model labels matching this glob; repeatable")
        command.add_argument("--price-table", type=Path, metavar="FILE",
            help="JSON prices per million tokens by model glob; adds estimated value")
        command.add_argument("--cache", type=Path, metavar="DIR",
            help="Private local cache of per-file probes; unchanged files are not rescanned")
    if not deliver:
        command.add_argument("--local-review", type=Path, metavar="OUT")
    else:
        command.set_defaults(local_review=None)
    command.add_argument("--salt-file", type=Path,
        help="Local pseudonym key file (overrides SUMBI_SALT)")
