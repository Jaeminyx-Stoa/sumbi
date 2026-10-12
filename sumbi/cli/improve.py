"""Compose evidence observations and reviewed local change application."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from sumbi.catalog.improve_guide import improvement_guide, guide_text
from sumbi.cli.common import atomic_write, idle, inside_log_roots, utc
from sumbi.install.errors import InstallError
from sumbi.install.external import (check_external, parse_expectation, register_external)
from sumbi.install.improve import observations, build_improvement, apply_improvement
from sumbi.install.revert import backup_targets, revert_install
from sumbi.judge.registration import read_registration


def configure_parser(parser):
    parser.add_argument("--guide", action="store_true",
        help="Read the default improvement work approach; no evidence required")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--evidence", type=Path, help="Existing local collect JSON")
    parser.add_argument("--bundle", type=Path, help="Reviewed local change JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show local diff (default)")
    mode.add_argument("--apply", action="store_true", help="Apply exact reviewed bytes with backup")
    mode.add_argument("--revert", metavar="BACKUP_ID", help="Restore unchanged improvement targets")
    mode.add_argument("--register-external", metavar="ID",
        help="Append an intervention applied outside a bundle to the intervention ledger")
    mode.add_argument("--check", metavar="ID",
        help="Show a registered external intervention's metrics before and after")
    parser.add_argument("--reviewed-sha256", help="Exact SHA-256 of the reviewed bundle")
    parser.add_argument("--registration", type=Path,
        help="Optional existing compare pre-registration")
    parser.add_argument("--json", action="store_true", help="Print only allow-listed summary JSON")
    # collect writes files with --json OUT; improve's --json is already a format flag.
    parser.add_argument("--json-out", type=Path, metavar="OUT",
        help="Also write the allow-listed summary JSON to OUT (UTF-8, no BOM)")
    external = parser.add_argument_group("external interventions")
    external.add_argument("--applied-at", type=utc, help="UTC time the change took effect")
    external.add_argument("--change", action="append", metavar="LABEL",
        help="Bounded label naming what changed; repeatable; no free text")
    external.add_argument("--predict", action="append", metavar="METRIC:DIRECTION:PERCENT",
        help="Predicted metric change; repeatable")
    external.add_argument("--revert-if", action="append", metavar="METRIC:DIRECTION:PERCENT",
        help="Observed change that should trigger a revert review; repeatable")
    external.add_argument("--before-days", type=idle, help="Window length before applied-at")
    external.add_argument("--after-days", type=idle, help="Window length after applied-at")
    external.add_argument("--before", type=Path, metavar="COLLECT_JSON",
        help="Collect report for the window before the change")
    external.add_argument("--after", type=Path, metavar="COLLECT_JSON",
        help="Collect report for the window after the change")
    external.add_argument("--signals", type=Path, metavar="FILE",
        help="External JSONL with ts and event labels; counts only")


REGISTER_OPTIONS = ("applied_at", "change", "predict", "revert_if", "before_days", "after_days")
CHECK_OPTIONS = ("before", "after", "signals")


def _public(args, summary=None, kind="observations"):
    """The exact object --json prints for this mode."""
    if args.guide:
        guide = improvement_guide()
        return guide if summary is None else {"guide": guide, kind: summary}
    return summary


def _print_report(args, summary=None, kind="observations"):
    if args.guide:
        if args.json:
            print(json.dumps(_public(args, summary, kind), sort_keys=True))
            return
        print(guide_text(improvement_guide()))
    if summary is not None:
        print(json.dumps(summary, sort_keys=True))


def _write_json(args, value):
    if args.json_out is not None:
        try:
            atomic_write(args.json_out, json.dumps(value, sort_keys=True) + "\n")
        except OSError:
            raise InstallError("JSON output could not be written.") from None


def _check_destination(args, *protected):
    """Refuse output over inputs, local install state or session-log directories."""
    if args.json_out is None:
        return
    target = args.json_out.resolve()
    inputs = [args.evidence, args.bundle, args.registration, args.before, args.after,
        args.signals, *protected]

    def same(path):
        path = Path(path).resolve()
        if os.path.normcase(path) == os.path.normcase(target):
            return True
        try:
            return target.exists() and path.exists() and os.path.samefile(path, target)
        except OSError:
            return True  # Unknown identity is treated as a collision.

    if (any(path is not None and same(path) for path in inputs)
        or target.is_relative_to((args.root / ".sumbi").resolve())
        or os.path.normcase(target).startswith(
            os.path.normcase((args.root / ".sumbi").resolve()) + os.sep)
        or inside_log_roots(target, Path.home())):
        raise InstallError("JSON output must not overwrite inputs, .sumbi state "
            "or session-log directories.")


def _refuse_mixed(args, allowed):
    options = {"evidence": args.evidence, "bundle": args.bundle, "apply": args.apply,
        "reviewed_sha256": args.reviewed_sha256, "registration": args.registration,
        "guide": args.guide, **{name: getattr(args, name)
            for name in (*REGISTER_OPTIONS, *CHECK_OPTIONS)}}
    if any(value for name, value in options.items() if name not in allowed):
        raise InstallError("This improve mode cannot be combined with those options.")


def _register(args):
    _refuse_mixed(args, REGISTER_OPTIONS)
    if any(getattr(args, name) in (None, []) for name in REGISTER_OPTIONS):
        raise InstallError("Registration requires --applied-at, --change, --predict, "
            "--revert-if, --before-days and --after-days.")
    _check_destination(args)
    record = register_external(args.root, intervention_id=args.register_external,
        applied_at=args.applied_at, changes=args.change,
        predictions=[parse_expectation(p, "rough_size_percent") for p in args.predict],
        revert_if=[parse_expectation(r, "threshold_percent") for r in args.revert_if],
        before_days=args.before_days, after_days=args.after_days)
    _write_json(args, record)
    print(json.dumps(record, sort_keys=True))
    if not args.json:
        print("Registered in the intervention ledger; labels and numbers only.")
        if not record["preregistered"]:
            print("Registered after the apply time: predictions are not pre-registered.")
    return 0


def _check(args):
    _refuse_mixed(args, CHECK_OPTIONS)
    if args.before is None or args.after is None:
        raise InstallError("Check requires --before and --after collect reports.")
    _check_destination(args)
    summary = check_external(args.root, args.check, args.before, args.after, args.signals)
    _write_json(args, summary)
    print(json.dumps(summary, sort_keys=True))
    if not args.json:
        print("Observation, not causation: other changes in the same windows are not removed.")
        print("Success-based metrics are not available from collect reports.")
    return 0


def _revert(args):
    if any((args.evidence, args.bundle, args.reviewed_sha256, args.registration)):
        raise InstallError(
            "Revert cannot be combined with evidence, bundle, or review options.")
    if args.json_out is not None:
        # Refuse before restoring: a restored or refused target must survive.
        root = args.root.resolve()
        _check_destination(args, *(root / name for name in backup_targets(root, args.revert)))
    result = revert_install(args.root, args.revert)
    # Revert's file paths are local; public JSON substitutes counts only.
    public = {"backup_id": result["backup_id"], "refused": result["refused"],
        "files": len(result["files"])}
    _write_json(args, public)
    print(json.dumps(public if args.json else result, sort_keys=True))
    return 1 if result["refused"] else 0


def run(args):
    try:
        if args.register_external:
            return _register(args)
        if args.check:
            return _check(args)
        if any(getattr(args, name) not in (None, []) for name in (*REGISTER_OPTIONS,
            *CHECK_OPTIONS)):
            raise InstallError("External intervention options need --register-external "
                "or --check.")
        if args.guide and not any((args.evidence, args.bundle, args.apply, args.revert,
            args.reviewed_sha256, args.registration)):
            _check_destination(args)
            _write_json(args, _public(args))
            _print_report(args)
            return 0
        if args.revert:
            return _revert(args)
        if not args.evidence:
            raise InstallError("Improve requires --evidence from an existing collect report.")
        if args.apply and (not args.bundle or not args.reviewed_sha256):
            raise InstallError("Apply requires --bundle and --reviewed-sha256.")
        if args.reviewed_sha256 and not args.apply:
            raise InstallError("Review acknowledgement requires --apply.")
        if args.registration and not args.bundle:
            raise InstallError("Registration requires a reviewed bundle.")
        if not args.bundle:
            _check_destination(args)
            _, summary = observations(args.evidence)
            _write_json(args, _public(args, summary))
            _print_report(args, summary)
            return 0
        plan = build_improvement(args.root, args.evidence, args.bundle)
        _check_destination(args, *(plan.root / change.path for change in plan.changes))
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
        _write_json(args, _public(args, summary, "proposal"))
        _print_report(args, summary, "proposal")
        if not args.json:
            print("Use sumbi improve --guide to design and review the intervention.")
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
