"""Offline integration truths for commands, baseline scope and pseudonym keys."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sumbi import __version__
from sumbi.cli import main
from sumbi.collect import baseline
from sumbi.install import apply_plan, build_plan
from sumbi.install.errors import InstallError
from sumbi.model import RepositoryAttributor, Window, label, pseudonym, timestamp
from sumbi.report import collect, text_summary

NOW = timestamp("2030-01-15T00:00:00Z")
WINDOW = Window(NOW - timedelta(days=14), NOW)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        for name in ("socket.socket", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("Network access is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, {}, clear=False)
        guard.start()
        self.addCleanup(guard.stop)
        os.environ.pop("SUMBI_SALT", None)
        guard = patch.object(Path, "home", return_value=self.home)
        guard.start()
        self.addCleanup(guard.stop)

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = main(list(args))
        return result, output.getvalue(), errors.getvalue()

    def log(self, identity, paths, *, when="2030-01-10T00:00:00Z", parent=None):
        directory = self.home / ".claude/projects/authored-fixture"
        directory.mkdir(parents=True, exist_ok=True)
        events = [{"type": "assistant", "timestamp": when, "sessionId": identity,
                   "cwd": str(path), "message": {"id": identity + str(n), "model": "fixture-model",
                     "content": "SYNTHETIC_PRIVATE_PROMPT", "usage": {"input_tokens": 10,
                      "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3, "output_tokens": 4}}}
                  for n, path in enumerate(paths)]
        destination = directory / (identity + ".jsonl")
        if parent:
            destination = directory / parent / "subagents" / ("agent-" + identity + ".jsonl")
            destination.parent.mkdir(parents=True, exist_ok=True)
            for event in events:
                event.update(sessionId=parent, agentId=identity)
        destination.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return destination

    def git_repository(self, name, origin):
        path = self.root / name
        path.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin],
                       check=True, capture_output=True)
        return path

    def read_baseline(self, result):
        raw = (self.repository / result["file"]).read_text(encoding="utf-8")
        for canary in (str(self.root), "SYNTHETIC_PRIVATE_PROMPT", "example.test", "fixture-session"):
            self.assertNotIn(canary, raw)
        return json.loads(raw)

    def test_shared_help_version_and_module_entry_point(self):
        for flag in ("--help", "--version"):
            with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exit_result:
                main([flag])
            self.assertEqual(exit_result.exception.code, 0)
            if flag == "--help":
                self.assertIn("install", output.getvalue())
                self.assertIn("collect", output.getvalue())
            else:
                self.assertEqual(output.getvalue().strip(), "sumbi " + __version__)
        result = subprocess.run([sys.executable, "-m", "sumbi", "--version"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sumbi " + __version__)

    def test_install_dispatch_and_read_only_default(self):
        result, output, errors = self.run_cli("install", "--root", str(self.repository),
                                             "--home", str(self.home))
        self.assertEqual(result, 0, errors)
        self.assertIn("Practices:", output)
        self.assertIn("available (runs before apply)", output)
        self.assertEqual(list(self.repository.iterdir()), [])
        with patch("sumbi.cli.run_install", return_value=17) as install:
            self.assertEqual(self.run_cli("install", "--select", "handoff")[0], 17)
        self.assertEqual(install.call_args.args[0].select, "handoff")

    def test_collect_dispatch_and_unsalted_output(self):
        self.log("fixture-session", [self.repository])
        result, output, errors = self.run_cli("collect", "--since", "2030-01-01T00:00Z",
               "--until", "2030-01-15T00:00Z", "--home", str(self.home), "--json", "-")
        self.assertEqual(result, 0, errors)
        report = json.loads(output)
        self.assertEqual(report["summary"]["sessions"], 1)
        self.assertIn("keys: unsalted", errors)
        self.assertFalse(report["pseudonyms"]["salted"])

    def test_baseline_uses_fourteen_day_bounds_and_path_scope(self):
        nested = self.repository / "src"
        nested.mkdir()
        sibling = self.root / "repository-other"
        sibling.mkdir()
        self.log("fixture-session", [self.repository, nested])
        self.log("first-boundary", [self.repository], when="2030-01-01T00:00:00Z")
        self.log("last-boundary", [self.repository], when="2030-01-15T00:00:00Z")
        self.log("old", [self.repository], when="2029-12-31T23:59:59Z")
        self.log("other", [sibling])
        self.log("mixed", [self.repository, sibling])
        result = baseline(repository=self.repository, home=self.home, now=NOW)
        report = self.read_baseline(result)
        self.assertEqual((result["status"], result["sessions"]), ("recorded", 3))
        self.assertEqual(report["window"]["since"], "2030-01-01T00:00:00Z")
        self.assertEqual(report["window"]["until"], "2030-01-15T00:00:00Z")
        # The mixed session contributes its matching 19-token message only.
        self.assertEqual(report["summary"]["tokens"]["total"], 76)
        self.assertEqual(report["scope"], {"kind": "repository", "sessions_in_window": 4,
                                         "sessions_selected": 3, "sessions_excluded": 1})
        self.assertEqual(len({s["project"]["project_key"] for s in report["sessions"]}), 1)
        self.assertTrue(all(s["project"]["bucket"] == "project" for s in report["sessions"]))

    def test_baseline_exact_normalized_origin_and_no_wrong_origin_fallback(self):
        self.git_repository("repository", "https://example.test/team/fixture[one].git")
        clone = self.git_repository("clone", "git@example.test:team/fixture[one].git")
        wrong = self.git_repository("repository/nested-other", "https://example.test/team/fixtureo.git")
        missing = self.repository / "missing-worktree"
        self.log("fixture-session", [self.repository, clone])
        self.log("wrong", [wrong])
        self.log("path-fallback", [missing])
        report = self.read_baseline(baseline(repository=self.repository, home=self.home, now=NOW))
        self.assertEqual(report["summary"]["sessions"], 2)
        evidence = {s["project"]["evidence"] for s in report["sessions"]}
        # A missing worktree folder is resolved through its existing repository ancestor.
        self.assertEqual(evidence, {"git_origin_candidate"})

    def test_no_logs_and_no_matching_sessions_are_clear_statuses(self):
        result = baseline(repository=self.repository, home=self.home, now=NOW)
        report = self.read_baseline(result)
        self.assertEqual(result["status"], "no sessions found")
        self.assertEqual(report["summary"]["sessions"], 0)
        self.assertEqual(report["coverage"]["adapters"]["codex"]["files_scanned"], 0)
        self.log("other", [self.home])
        result = baseline(repository=self.repository, home=self.home, now=NOW + timedelta(seconds=1))
        self.assertEqual(result["status"], "no sessions found")
        self.assertEqual(self.read_baseline(result)["scope"]["sessions_excluded"], 1)

    def test_unavailable_origin_cannot_bypass_baseline_scope(self):
        self.git_repository("repository", "https://example.test/team/fixture.git")
        nested = self.repository / "nested"
        nested.mkdir()
        attributor = RepositoryAttributor(self.repository)
        with patch.object(attributor, "origin", return_value=(None, "origin_unavailable")):
            self.assertEqual(attributor.link(str(nested))["bucket"], "unassigned")

    def test_baseline_rejects_source_destination_and_missing_repository(self):
        source = self.home / ".codex/sessions/repository"
        source.mkdir(parents=True)
        with self.assertRaises(ValueError):
            baseline(repository=source, home=self.home, now=NOW)
        self.assertEqual(list(source.iterdir()), [])
        missing = self.root / "missing-repository"
        with self.assertRaises(ValueError):
            baseline(repository=missing, home=self.home, now=NOW)
        self.assertFalse(missing.exists())

    def test_baseline_does_not_overwrite_or_modify_source_logs(self):
        source = self.log("fixture-session", [self.repository])
        original = source.read_bytes()
        result = baseline(repository=self.repository, home=self.home, now=NOW)
        before = (self.repository / result["file"]).read_bytes()
        with self.assertRaises((InstallError, FileExistsError)):
            baseline(repository=self.repository, home=self.home, now=NOW)
        self.assertEqual((self.repository / result["file"]).read_bytes(), before)
        self.assertEqual(source.read_bytes(), original)

    def test_baseline_rejects_linked_metadata(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.repository / ".sumbi").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks are unavailable on this test host")
        with self.assertRaises(InstallError):
            baseline(repository=self.repository, home=self.home, now=NOW)
        self.assertEqual(list(outside.iterdir()), [])

    def test_apply_connects_fixture_baseline_before_practice_writes(self):
        # Replace only the authored fixture timestamp with a current UTC instant.
        from datetime import datetime, timezone
        when = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.log("fixture-session", [self.repository], when=when)
        result, output, errors = self.run_cli("install", "--root", str(self.repository), "--apply",
                    "--select", "handoff", "--home", str(self.home))
        self.assertEqual(result, 0, errors)
        ledger = json.loads((self.repository / ".sumbi/interventions.jsonl").read_text())
        self.assertEqual(ledger["baseline"]["status"], "recorded")
        self.assertEqual(ledger["baseline"]["sessions"], 1)
        self.assertIn(ledger["baseline"]["file"], output)

    def test_gitignore_creation_preservation_idempotency_and_git_visibility(self):
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True, capture_output=True)
        local = self.repository / ".sumbi"
        local.mkdir()
        original = b"# Owner's local pattern\nprivate.txt\n!backups/\n"
        (local / ".gitignore").write_bytes(original)
        result = apply_plan(build_plan(self.repository, select=["handoff"]), home=self.home)
        expected = original + b"backups/\nbaseline/\ninstall.lock\n"
        self.assertEqual((local / ".gitignore").read_bytes(), expected)
        before = {p.relative_to(local): p.read_bytes() for p in local.rglob("*") if p.is_file()}
        apply_plan(build_plan(self.repository, select=["handoff"]), home=self.home)
        self.assertEqual(before, {p.relative_to(local): p.read_bytes() for p in local.rglob("*") if p.is_file()})
        for name in ("backups/fixture", "baseline/fixture", "install.lock"):
            path = local / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Authored local artifact\n", encoding="utf-8")
            check = subprocess.run(["git", "-C", str(self.repository), "check-ignore", "-q",
                                    ".sumbi/" + name], capture_output=True)
            self.assertEqual(check.returncode, 0)
        check = subprocess.run(["git", "-C", str(self.repository), "check-ignore", "-q",
                                ".sumbi/interventions.jsonl"], capture_output=True)
        self.assertEqual(check.returncode, 1)
        self.assertEqual(result["baseline"]["status"], "no sessions found")

    def test_gitignore_is_created_on_first_apply(self):
        apply_plan(build_plan(self.repository, select=["handoff"]), home=self.home)
        self.assertEqual((self.repository / ".sumbi/.gitignore").read_bytes(),
                         b"backups/\nbaseline/\ninstall.lock\n")

    def test_hmac_truth_env_and_legacy_unsalted_keys(self):
        value = "authored-id"
        expected = "session_" + hashlib.sha256(value.encode()).hexdigest()[:24]
        self.assertEqual(pseudonym("session", value), expected)
        with patch.dict(os.environ, {"SUMBI_SALT": "authored-secret"}):
            salted = pseudonym("session", value)
            self.assertEqual(salted, "session_" + hmac.new(b"authored-secret", value.encode(),
                                                         hashlib.sha256).hexdigest()[:24])
            self.assertNotEqual(salted, expected)
        self.assertEqual(pseudonym("session", value), expected)

    def test_salt_changes_every_key_without_changing_counts(self):
        self.log("fixture-session", [self.repository])
        self.log("child", [self.repository], parent="fixture-session")
        plain = collect(self.home, WINDOW, repository=self.repository)[0]
        salted = collect(self.home, WINDOW, repository=self.repository, salt=b"authored-secret")[0]
        again = collect(self.home, WINDOW, repository=self.repository, salt=b"authored-secret")[0]
        self.assertEqual(salted, again)
        self.assertEqual(plain["summary"], salted["summary"])
        self.assertTrue(salted["pseudonyms"]["salted"])
        for field in ("id", "parent_id"):
            old = {s[field] for s in plain["sessions"] if s[field]}
            new = {s[field] for s in salted["sessions"] if s[field]}
            self.assertTrue(old.isdisjoint(new))
        self.assertNotEqual(plain["sessions"][0]["project"]["project_key"],
                            salted["sessions"][0]["project"]["project_key"])
        self.assertNotIn("authored-secret", json.dumps(salted))
        self.assertIn("keys: salted", text_summary(salted))
        with patch.dict(os.environ, {"SUMBI_SALT": "authored-secret"}):
            self.assertNotEqual(label("authored private label"), label_plain :=
                                "label_" + hashlib.sha256(b"authored private label").hexdigest()[:24])
            self.assertEqual(collect(self.home, WINDOW, repository=self.repository)[0], salted)
        self.assertEqual(label("authored private label"), label_plain)

    def test_salt_file_overrides_env_and_is_not_exported(self):
        self.log("fixture-session", [self.repository])
        salt_file = self.root / "key"
        salt_file.write_bytes(b"authored-file-secret")
        with patch.dict(os.environ, {"SUMBI_SALT": "different-env-secret"}):
            result, output, errors = self.run_cli("collect", "--since", "2030-01-01T00:00Z",
                  "--until", "2030-01-15T00:00Z", "--home", str(self.home),
                  "--salt-file", str(salt_file), "--json", "-")
        self.assertEqual(result, 0, errors)
        expected = collect(self.home, WINDOW, salt=b"authored-file-secret")[0]
        self.assertEqual(json.loads(output), expected)
        for canary in (str(salt_file), "authored-file-secret", "different-env-secret"):
            self.assertNotIn(canary, output + errors)

    def test_empty_or_unreadable_salt_fails_without_private_errors(self):
        salt_file = self.root / "private-salt-name"
        for data in (None, b""):
            if data is not None:
                salt_file.write_bytes(data)
            result, output, errors = self.run_cli("collect", "--since", "2030-01-01T00:00Z",
                  "--until", "2030-01-15T00:00Z", "--home", str(self.home),
                  "--salt-file", str(salt_file), "--json", "-")
            self.assertEqual(result, 1)
            self.assertNotIn(str(salt_file), output + errors)
        with patch.dict(os.environ, {"SUMBI_SALT": ""}):
            self.assertEqual(self.run_cli("install", "--root", str(self.repository), "--apply")[0], 1)
        self.assertEqual(list(self.repository.iterdir()), [])

    def test_salt_file_cannot_be_an_output_destination(self):
        salt_file = self.root / "key"
        salt_file.write_bytes(b"authored-secret")
        for destination in (("--json", str(salt_file)),
                            ("--json", "-", "--local-review", str(salt_file))):
            with self.assertRaises(SystemExit) as error, redirect_stderr(io.StringIO()):
                main(["collect", "--since", "2030-01-01T00:00Z", "--until", "2030-01-15T00:00Z",
                      "--home", str(self.home), "--salt-file", str(salt_file), *destination])
            self.assertEqual(error.exception.code, 2)
        self.assertEqual(salt_file.read_bytes(), b"authored-secret")
