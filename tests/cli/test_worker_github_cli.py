"""Compose worker GitHub CLI routing and fixed privacy-safe validation errors."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from support import IsolatedTemporaryDirectory
from worker_github_fixtures import REPO, build_round, save
from sumbi.cli import main


class WorkerCliTests(unittest.TestCase):
    def setUp(self):
        temporary = IsolatedTemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.repo, self.outcomes, self.registration = build_round(self.root)
        for target in ("socket.socket", "socket.getaddrinfo", "socket.create_connection",
            "urllib.request.OpenerDirector.open"):
            guard = patch(target, side_effect=AssertionError("Tests cannot use the network"))
            guard.start()
            self.addCleanup(guard.stop)

    def invoke(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                status = main(argv)
            except SystemExit as exc:
                status = exc.code
        return status, output.getvalue(), errors.getvalue()

    def args(self, command="deliver"):
        base = [command, "--outcome-source", "worker-github", "--repo", REPO,
            "--outcomes", str(self.outcomes), "--home", str(self.home), "--json", "-"]
        return base + (["--since", "2030-01-01T00:00:00Z", "--until", "2030-01-15T00:00:00Z"]
            if command == "deliver" else ["--registration", str(self.registration), "--resamples", "100"])

    def test_owner_scope_is_visible_in_help(self):
        for command in ("deliver", "compare"):
            with self.subTest(command=command):
                status, output, errors = self.invoke([command, "--help"])
                self.assertEqual(status, 0)
                self.assertEqual(errors, "")
                self.assertIn("--repo-owner OWNER", output)
                self.assertIn("evidenced repositories", output)

    def test_deliver_and_compare_stdout_json_and_repeatable_repository(self):
        for command in ("deliver", "compare"):
            status, output, errors = self.invoke(self.args(command) + ["--repo", REPO.upper()])
            self.assertEqual(status, 0, errors)
            report = json.loads(output)
            self.assertEqual(report["outcome_source"], "worker-github")
            self.assertIn("Outcome source: worker-github", errors)
            self.assertNotIn(REPO, output + errors)
            if command == "compare":
                self.assertEqual(report["verdict"]["proposal"], "adopt")

    def test_compare_selects_source_from_registration(self):
        args = self.args("compare")
        index = args.index("--outcome-source")
        del args[index:index + 2]
        self.assertEqual(self.invoke(args)[0], 0)

    def test_owner_only_and_repeatable_owners_work_for_delivery_and_comparison(self):
        for command in ("deliver", "compare"):
            args = self.args(command)
            index = args.index("--repo")
            del args[index:index + 2]
            status, output, errors = self.invoke(args + ["--repo-owner", "EXAMPLE",
                "--repo-owner", "example", "--repo-owner", "other"])
            self.assertEqual(status, 0, errors)
            report = json.loads(output)
            self.assertEqual(len(report["coverage"]["repositories"]), 1)
            self.assertNotIn(REPO, output + errors)
            if command == "compare":
                self.assertEqual(report["verdict"]["proposal"], "adopt")
                self.assertEqual(report["arms"]["before"]["state_reasons"],
                    {"success": {"accepted": 12}, "failed": {}, "immature": {},
                        "in_progress": {}, "unverified": {}, "no_pr": {}, "no_change": {}})
                self.assertIn("before candidate success: accepted 12", errors)
            else:
                self.assertIn("state_reasons", report)

    def test_invalid_owner_echoes_no_operands_and_non_worker_source_rejects_it(self):
        status, output, errors = self.invoke(self.args() + ["--repo-owner", "private/invalid"])
        self.assertEqual(status, 1)
        self.assertEqual(output, "")
        self.assertNotIn("private", errors)
        args = self.args()
        args[args.index("worker-github")] = "github"
        self.assertEqual(self.invoke(args + ["--repo-owner", "example"])[0], 2)

    def test_invalid_repo_and_conflicting_options_echo_no_operands(self):
        for extra in (["--repo", "private/path/invalid"], ["--ledger", "private-ledger.csv"],
            ["--repository", "private-checkout"], ["--verify", "private-check.sh"],
            ["--cache", "private-cache"], ["--project", "private", "--match-path", "private*"]):
            with self.subTest(extra=extra[0]):
                status, output, errors = self.invoke(self.args() + extra)
                self.assertIn(status, (1, 2))
                self.assertEqual(output, "")
                self.assertNotIn("private", errors)

    def test_output_cannot_overwrite_registration_or_recorded_outcomes(self):
        for command, destination in (("compare", self.registration),
            ("deliver", self.outcomes / "repository.json")):
            before = destination.read_bytes()
            status, _, _ = self.invoke(self.args(command) + ["--json", str(destination)])
            self.assertEqual(status, 2)
            self.assertEqual(destination.read_bytes(), before)

    def test_registration_source_override_is_rejected(self):
        raw = json.loads(self.registration.read_text(encoding="utf-8"))
        raw["outcome_source"] = "github"
        save(self.registration, raw)
        self.assertEqual(self.invoke(self.args("compare"))[0], 2)


if __name__ == "__main__":
    unittest.main()
