"""Offline install command, also available through the shared CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from sumbi.core.privacy import read_salt

from sumbi.install.apply import apply_plan
from sumbi.install.baseline_hook import baseline
from sumbi.install.errors import InstallError
from sumbi.install.inventory import safe_path
from sumbi.install.exclusions import GitIgnore
from sumbi.install.planner import build_plan
from sumbi.install.placement import annotate_placement, read_starts
from sumbi.install.revert import revert_install


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Repository to inspect (default: current directory).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print additive unified diffs (the default).")
    mode.add_argument("--apply", action="store_true", help="Apply additions with backups and an intervention ledger.")
    mode.add_argument("--revert", metavar="BACKUP_ID", help="Restore unchanged applied files from an install backup.")
    parser.add_argument("--select", help="Comma-separated catalog IDs; apply only matching gap candidates.")
    parser.add_argument("--intervention-id", help="Public-safe pre-registration ID to attach to all applied practices")
    parser.add_argument("--budget", type=int, default=2000, help="Instruction token budget (default: 2000).")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="Exclude repository-relative paths and descendants; repeatable.")
    parser.add_argument("--json", metavar="PATH", help="Create a JSON plan at a repository-relative path; never overwrite.")
    parser.add_argument("--home", type=Path, default=Path.home(), help="Local agent-log home for start placement and baseline.")
    parser.add_argument("--salt-file", type=Path, help="Local pseudonym key file (overrides SUMBI_SALT).")


def run(args: argparse.Namespace) -> int:
    try:
        if args.intervention_id is not None and not args.apply:
            raise InstallError("Intervention ID requires --apply.")
        if args.revert:
            if args.select is not None or args.json or args.exclude:
                raise InstallError("Revert cannot be combined with selection, plan output, or exclusions.")
            result = revert_install(Path(args.root), args.revert)
            print("Revert: " + json.dumps(result, sort_keys=True))
            return 1 if result["refused"] else 0
        try:
            salt = read_salt(args.salt_file)
        except (OSError, ValueError):
            raise InstallError("Cannot read a nonempty pseudonym salt.") from None
        selection = [p.strip() for p in args.select.split(",")] if args.select is not None else None
        plan = build_plan(Path(args.root), budget=args.budget, select=selection, exclude=args.exclude)
        sessions, window, coverage = read_starts(args.home)
        annotate_placement(plan, sessions, window, coverage)
        status = baseline(plan.root)
        if args.json:
            if args.json.split("/", 1)[0].lower() in {".git", ".sumbi"}:
                raise InstallError("Plan JSON cannot be written into repository metadata.")
            if args.json.casefold() in {c.path.casefold() for c in plan.changes}:
                raise InstallError("Plan JSON conflicts with a planned target.")
            destination = safe_path(plan.root, args.json)
            if GitIgnore(destination.parent).check(destination.name):
                raise InstallError("Plan JSON cannot be written into an ignored path.")
            if destination.exists():
                raise InstallError("Plan JSON already exists; it will not be overwritten.")
            # Parent must already exist: dry-run does not create directories.
            with destination.open("x", encoding="utf-8") as stream:
                json.dump({**plan.as_dict(), "baseline": status}, stream, indent=2)
                stream.write("\n")
        report = plan.report
        print("Inventory (offline; paths and parsed labels only):")
        print(f"  excluded roots: {report['exclusions']['count']}")
        print(f"  git-ignored roots: {report['exclusions']['gitignore_count']}")
        nested = report["versioning"]["nested_repositories"]
        print(f"  nested git repositories: {nested['count']}" + (" (" + ", ".join(nested["paths"]) + ")" if nested["paths"] else ""))
        print("  observed starts: " + json.dumps(report["observed_starts"], sort_keys=True))
        for entry in report["exclusions"]["patterns"]:
            print(f"  exclude {entry['pattern']}: {entry['count']}")
        for name, entry in report["instructions"].items():
            if entry["count"]:
                print(f"  {name}: {entry['count']} ({', '.join(entry['paths'])})")
        print("  capabilities: " + ", ".join(f"{key}={value['count']}" for key, value in report["capabilities"].items()))
        for name, entry in report["capabilities"].items():
            if entry["paths"]:
                print(f"  {name} paths: " + ", ".join(entry["paths"]))
        for entry in report["configurations"]:
            print(f"  config {entry['path']}: hooks={entry['hooks']}, permissions={entry['permission_entries']}, mcp={entry['mcp_servers']}, approval_policy={entry['approval_policy_present']}, sandbox_mode={entry['sandbox_mode_present']}")
        for entry in report["claude_imports"]:
            destination_label = entry.get("path", "excluded" if entry["status"] == "excluded-not-read" else "external")
            print(f"  import {entry['source']} -> {destination_label}: {entry['status']}")
        enforcement = report["enforcement"]
        print(f"  workflows={len(enforcement['workflows'])}, codeowners={enforcement['codeowners']['count']}, test_sources={len(enforcement['test_sources'])}")
        for workflow in enforcement["workflows"]:
            print(f"  workflow {workflow['path']}: " + ", ".join(j.get("name", j["id"]) + " (" + j["id"] + ")" for j in workflow["jobs"]))
        for name in ("codeowners", "pr_templates", "git_hooks", "lint", "format", "type_check"):
            entry = enforcement[name]
            print(f"  {name}: {entry['count']}" + (" (" + ", ".join(entry["paths"]) + ")" if entry["paths"] else ""))
        for source in enforcement["test_sources"]:
            print(f"  test source {source['path']}: {source['kind']}")
        print("  required checks: unknown (offline); rulesets: unknown (offline)")
        print("  estimate: " + report["cost"]["estimate_rule"])
        print("  instruction tokens (root/largest scope): " + ", ".join(f"{agent}={cost['root_estimated_tokens']}/{cost['estimated_tokens']}" for agent, cost in report["cost"]["instructions"].items()))
        for agent, cost in report["cost"]["conditional_instructions"].items():
            print(f"  conditional instruction tokens {agent}: {cost['estimated_tokens']} ({len(cost['paths'])} rules)")
        for key, convention in report["conventions"].items():
            print(f"  convention {key}: {convention['status']} " + json.dumps(convention["evidence"], sort_keys=True))
        unknown = [key for key, convention in report["conventions"].items() if convention["status"] == "unknown"]
        if unknown:
            print("Unknown conventions: " + ", ".join(unknown))
            print('  Declare owner guidance in .sumbi/config.toml under [conventions], '
                  'using repository-relative existing paths, for example: ' + unknown[0] + ' = ["docs/process.md"]')
        print(f"  budget={args.budget}; warnings={len(report['warnings'])}")
        for entry in report["cost"]["skill_descriptions"]:
            print(f"  skill description {entry['path']}: characters={entry['characters']}, tokens={entry['estimated_tokens']}")
        for entry in report["cost"]["every_prompt_hooks"]:
            print(f"  every-prompt hooks {entry['path']}: {entry['count']}")
        for warning in report["warnings"]:
            print("  warning: " + json.dumps(warning, sort_keys=True))
        print("Gaps: " + (", ".join(dict.fromkeys(g["id"] for g in plan.gaps)) or "none"))
        for gap in plan.gaps:
            print("  " + gap["id"] + ": " + json.dumps(gap["evidence"], sort_keys=True) + "; candidates=" + ",".join(gap["candidates"]))
        print("Practices: " + (", ".join(p["id"] for p in plan.practices) or "none"))
        print("baseline: " + status["status"])
        for change in plan.changes:
            sys.stdout.write(change.diff())
        if args.apply:
            result = apply_plan(plan, home=args.home, salt=salt, intervention_id=args.intervention_id)
            print("Applied: " + (", ".join(result["applied"]) or "none"))
            print("baseline: " + result["baseline"]["status"])
            if "file" in result["baseline"]:
                print("Baseline file: " + result["baseline"]["file"])
            if "backup" in result:
                print("Backup: " + result["backup"])
                print("Backup ID: " + result["backup_id"])
        elif not plan.changes:
            print("No changes proposed.")
        return 0
    except (InstallError, OSError) as error:
        message = str(error) if isinstance(error, InstallError) else "Repository operation failed."
        print("sumbi install: " + message, file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory and seed text-only repository practices offline.")
    configure_parser(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
