"""Compose evidence observations and reviewed local change application."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from sumbi.install.errors import InstallError
from sumbi.install.improve import observations, build_improvement, apply_improvement
from sumbi.install.revert import revert_install
from sumbi.judge.registration import read_registration


def configure_parser(parser):
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--evidence", type=Path, help="Existing local collect JSON")
    parser.add_argument("--bundle", type=Path, help="Reviewed local change JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show local diff (default)")
    mode.add_argument("--apply", action="store_true", help="Apply exact reviewed bytes with backup")
    mode.add_argument("--revert", metavar="BACKUP_ID", help="Restore unchanged improvement targets")
    parser.add_argument("--reviewed-sha256", help="Exact SHA-256 of the reviewed bundle")
    parser.add_argument("--registration", type=Path,
        help="Optional existing compare pre-registration")
    parser.add_argument("--json", action="store_true", help="Print only allow-listed summary JSON")


def run(args):
    try:
        if args.revert:
            if any((args.evidence, args.bundle, args.reviewed_sha256, args.registration)):
                raise InstallError(
                    "Revert cannot be combined with evidence, bundle, or review options.")
            result = revert_install(args.root, args.revert)
            # Revert's file paths are local; public JSON substitutes counts only.
            print(json.dumps({"backup_id": result["backup_id"], "refused": result["refused"],
                "files": len(result["files"])} if args.json else result, sort_keys=True))
            return 1 if result["refused"] else 0
        if not args.evidence:
            raise InstallError("Improve requires --evidence from an existing collect report.")
        if args.apply and (not args.bundle or not args.reviewed_sha256):
            raise InstallError("Apply requires --bundle and --reviewed-sha256.")
        if args.reviewed_sha256 and not args.apply:
            raise InstallError("Review acknowledgement requires --apply.")
        if args.registration and not args.bundle:
            raise InstallError("Registration requires a reviewed bundle.")
        if not args.bundle:
            _, summary = observations(args.evidence)
            print(json.dumps(summary, sort_keys=True))
            return 0
        plan = build_improvement(args.root, args.evidence, args.bundle)
        if args.registration:
            registration = read_registration(args.registration)
            if (registration.intervention_id != plan.summary["intervention_id"]
                or registration.predictions != (plan.summary["prediction"],)
                or not registration.preregistered
                or args.apply and registration.registered_at > datetime.now(timezone.utc)):
                raise InstallError(
                    "Registration must pre-register this intervention and prediction.")
        result = apply_improvement(plan, args.reviewed_sha256) if args.apply else None
        summary = {**plan.summary, "status": "applied" if result else "proposed"}
        if result:
            summary["backup_id"] = result["backup_id"]
        print(json.dumps(summary, sort_keys=True))
        if not args.json:
            print("Local review only: diff may contain private source/configuration text.")
            print("Diagnosis is a reviewed hypothesis; collect counts do not establish causality.")
            print("Review and verification are owner-local attestations, "
                "not authenticated evidence.")
            for change in plan.changes:
                diff = change.diff()
                if diff:
                    sys.stdout.write(diff)
                else:
                    print("Line endings only: " + change.path
                        + " (exact before/after SHA-256 in summary).")
        return 0
    except (InstallError, OSError, ValueError) as error:
        message = (str(error) if isinstance(error, InstallError)
            else "Local improvement validation failed.")
        print("sumbi improve: " + message, file=sys.stderr)
        return 1
