"""Catalog pre-registration command."""

import sys
from pathlib import Path
from sumbi.cli.common import idle, utc
from sumbi.judge.registration import write_registration

def run(args):
    try:
        write_registration(args.json, intervention_id=args.intervention_id,
            outcome_source=args.outcome_source,
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


def configure_parser(command):
    command.add_argument("--json", type=Path, required=True, metavar="OUT",
        help="New local registration JSON file")
    command.add_argument("--intervention-id", required=True, help="Public-safe intervention ID")
    command.add_argument("--outcome-source", required=True, choices=("github", "local-verify"))
    command.add_argument("--margin-pp", "--non-inferiority-margin-pp", dest="margin_pp",
        type=float, required=True,
        help="Acceptable success-rate drop in absolute percentage points "
        "(0 < margin < 100)")
    command.add_argument("--sample-size-per-arm", type=int, required=True,
        help="Positive registered sample per arm")
    command.add_argument("--follow-up-days", type=idle, required=True,
        help="Positive outcome follow-up days")
    command.add_argument("--window-days", type=idle, required=True,
        help="Equal adjacent dispatch windows around planned application")
    command.add_argument("--applied-at", type=utc, required=True,
        help="Planned UTC application time; registered_at is now")
    command.add_argument("--practice", action="append", required=True, metavar="ID",
        help="Catalog prediction practice ID; repeatable")
