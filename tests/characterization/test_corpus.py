"""Byte-level CLI safety net, same-process and separate-process determinism."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

from sumbi.cli import parser
from support import IsolatedTemporaryDirectory
from .corpus import CASES, DIRECTORY, run_corpus
from .regenerate import difference, flatten, golden_files


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Tests never mutate goldens. Read the reviewed corpus once rather than
        # reopening every artifact for every case on mounted filesystems.
        cls.expected = golden_files()
        cls.observed = run_corpus()

    def assert_artifacts_equal(self, expected, actual):
        self.assertEqual(set(expected), set(actual), "The set of output artifacts changed")
        for name in sorted(expected):
            if expected[name] != actual[name]:
                self.fail(difference(expected[name], actual[name], name))

    def test_repeat_in_same_process(self):
        self.assert_artifacts_equal(flatten(self.observed), flatten(run_corpus()))

    def test_repeat_in_separate_processes(self):
        with IsolatedTemporaryDirectory(prefix="sumbi-processes-") as temporary:
            root = Path(temporary)
            snapshots = []
            for index in range(2):
                path = root / ("snapshot-" + str(index) + ".json")
                result = subprocess.run([sys.executable, str(DIRECTORY / "regenerate.py"),
                                         "--dump", str(path), "--temp-root", str(root)],
                                        capture_output=True, text=True, timeout=600)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                snapshots.append(flatten(json.loads(path.read_text(encoding="utf-8"))))
            self.assert_artifacts_equal(flatten(self.observed), snapshots[0])
            self.assert_artifacts_equal(snapshots[0], snapshots[1])

    def test_manifest_covers_help_commands_and_errors(self):
        self.assertEqual(set(self.expected), set(flatten(self.observed)),
                         "The complete set of reviewed output artifacts changed")
        actions = [action for action in parser()._actions if getattr(action, "choices", None)]
        commands = set(actions[0].choices)
        covered = {case["command"] for case in CASES} - {"root"}
        self.assertEqual(commands, covered, "Every subcommand in --help needs corpus cases")
        self.assertEqual(len(CASES), len({case["name"] for case in CASES}))
        for command in commands:
            rows = [case for case in CASES if case["command"] == command]
            self.assertTrue(any(case["expected_exit"] == 0 and "--help" not in case["argv"] for case in rows))
            failures = [case for case in rows if case["expected_exit"] != 0]
            self.assertTrue(failures)
            self.assertTrue(all(self.observed[case["name"]]["stderr.txt"] for case in failures))
        install_fixtures = {p.name for p in (DIRECTORY.parent / "install/fixtures").iterdir() if p.is_dir()}
        for fixture in install_fixtures:
            for suffix in ("dry-text", "dry-json", "apply-revert"):
                self.assertIn("install-" + fixture + "-" + suffix, self.observed)


def golden_test(case):
    def test(self):
        prefix = case["name"] + "/"
        expected = {name: value for name, value in self.expected.items() if name.startswith(prefix)}
        actual = {prefix + name: value for name, value in self.observed[case["name"]].items()}
        self.assert_artifacts_equal(expected, actual)
    return test


for case in CASES:
    setattr(CorpusTests, "test_golden_" + case["name"].replace("-", "_"), golden_test(case))
