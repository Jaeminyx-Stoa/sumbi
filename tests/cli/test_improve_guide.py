"""Installed improvement approach and backward-compatible report composition."""

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import sumbi_build
from sumbi.cli import main

ROOT = Path(__file__).resolve().parents[2]


def invoke(*args):
    output, errors = StringIO(), StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = main(["improve", *map(str, args)])
    return code, output.getvalue(), errors.getvalue()


class ImproveGuideTests(unittest.TestCase):
    def test_guide_requires_no_evidence_repository_or_file_reads(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("No evidence read")):
            code, output, errors = invoke("--guide", "--root", "missing-repository")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Ask: ", output)
        self.assertIn("Change: ", output)
        self.assertIn("resource-heavy-job", output)
        self.assertIn("small-static-fix", output)
        self.assertIn("money-or-security-path", output)

    def test_legacy_json_bytes_unchanged_and_guide_composes_observations(self):
        evidence = ROOT / "tests/characterization/fixtures/improve/collect.json"
        expected = (ROOT / "tests/characterization/golden/improve-observations/stdout.txt").read_text()
        code, output, errors = invoke("--evidence", evidence, "--json")
        self.assertEqual((code, errors, output), (0, "", expected))
        code, output, errors = invoke("--guide", "--evidence", evidence, "--json")
        self.assertEqual((code, errors), (0, ""))
        combined = json.loads(output)
        self.assertEqual(combined["observations"], json.loads(expected))
        self.assertEqual(combined["guide"]["version"], 1)
        self.assertEqual(combined["observations"]["causality"], "not-established")

    def test_guide_is_accessible_from_built_package_without_source_or_docs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            installed = base / "installed"
            installed.mkdir()
            wheel = base / sumbi_build.build_wheel(base)
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if name.startswith("sumbi/"):
                        target = installed / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(name))
            isolated = base / "unrelated"
            isolated.mkdir()
            probe = ("import sys; from pathlib import Path; "
                "sys.path.insert(0,sys.argv[1]); import sumbi; "
                "assert Path(sumbi.__file__).is_relative_to(Path(sys.argv[1])); "
                "assert not Path(sys.argv[1],'docs').exists(); "
                "from sumbi.cli import main; "
                "raise SystemExit(main(['improve','--guide','--json']))")
            result = subprocess.run([sys.executable, "-I", "-c", probe, str(installed)],
                cwd=isolated, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            guide = json.loads(result.stdout)
            contexts = {row["context"] for row in guide["worked_example"]["different_contexts"]}
            self.assertEqual(contexts, {"resource-heavy-job", "small-static-fix",
                "money-or-security-path"})
            self.assertTrue(all(row["question"] and row["changed_behavior"]
                for row in guide["principles"]))


if __name__ == "__main__":
    unittest.main()
