"""Verify dependency-free distributions and the console entry point."""

import base64
import csv
from email.parser import BytesParser
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from support import IsolatedTemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch
import zipfile

import sumbi_build


class BuildTests(unittest.TestCase):
    def test_wheel_metadata_entry_point_and_file_integrity(self):
        with IsolatedTemporaryDirectory() as directory:
            name = sumbi_build.build_wheel(directory)
            with zipfile.ZipFile(Path(directory) / name) as archive:
                metadata = archive.read(sumbi_build.INFO + "/METADATA").decode()
                self.assertIn("Metadata-Version: 2.2\n", metadata)
                self.assertIn("Requires-Python: >=3.11", metadata)
                self.assertIn("Description-Content-Type: text/markdown\n", metadata)
                self.assertIn("Classifier: Development Status :: 3 - Alpha\n", metadata)
                self.assertIn("Project-URL: Repository, https://github.com/Jaeminyx-Stoa/sumbi\n", metadata)
                self.assertEqual(metadata.split("\n\n", 1)[1],
                                 (sumbi_build.ROOT / "README.md").read_text(encoding="utf-8"))
                self.assertNotIn("Requires-Dist:", metadata)
                self.assertEqual(archive.read(sumbi_build.INFO + "/LICENSE"), (sumbi_build.ROOT / "LICENSE").read_bytes())
                self.assertIn("sumbi = sumbi.cli:main", archive.read(sumbi_build.INFO + "/entry_points.txt").decode())
                self.assertIn("sumbi/adapters/codex.py", archive.namelist())
                self.assertIn("sumbi/__main__.py", archive.namelist())
                for module in ("compare", "compare_stats", "registration"):
                    self.assertIn("sumbi/" + module + ".py", archive.namelist())
                self.assertEqual(len(json.loads(archive.read("sumbi/catalog/practices.json"))["practices"]), 8)
                self.assertIn("2026-10-05", archive.read("sumbi/catalog/CREDITS.md").decode())
                self.assertFalse(any(name.startswith("tests/") for name in archive.namelist()))
                records = list(csv.reader(io.StringIO(archive.read(sumbi_build.INFO + "/RECORD").decode())))
                self.assertEqual({row[0] for row in records}, set(archive.namelist()))
                for name, digest, size in records:
                    if digest:
                        data = archive.read(name)
                        expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                        self.assertEqual(digest, "sha256=" + expected)
                        self.assertEqual(int(size), len(data))

    def test_sdist_can_rebuild_wheel_without_external_backend(self):
        with IsolatedTemporaryDirectory() as directory:
            root = Path(directory)
            name = sumbi_build.build_sdist(root)
            with tarfile.open(root / name) as archive:
                package_info = archive.extractfile(sumbi_build.STEM + "/PKG-INFO").read()
                self.assertEqual(package_info, sumbi_build.metadata()["METADATA"])
                fields = BytesParser().parsebytes(package_info)
                self.assertEqual(fields["Metadata-Version"], "2.2")
                self.assertEqual(fields["Name"], sumbi_build.PROJECT["name"])
                self.assertEqual(fields["Version"], sumbi_build.PROJECT["version"])
                self.assertEqual(sumbi_build.STEM, fields["Name"] + "-" + fields["Version"])
                self.assertIsNone(fields["Dynamic"])
                self.assertIn(sumbi_build.STEM + "/docs/deliverables.template.csv", archive.getnames())
                fixtures = {p.relative_to(sumbi_build.ROOT).as_posix()
                            for parent in ("tests/fixtures", "tests/install/fixtures")
                            for p in (sumbi_build.ROOT / parent).rglob("*") if p.is_file()}
                self.assertTrue({sumbi_build.STEM + "/" + p for p in fixtures} <= set(archive.getnames()))
                self.assertEqual(len(archive.getnames()), len(set(archive.getnames())))
                self.assertFalse(any("/local/" in p or "/out/" in p or "__pycache__" in p
                                     for p in archive.getnames()))
                self.assertTrue(all(p.name.startswith(sumbi_build.STEM + "/") for p in archive.getmembers()))
                # The archive was authored above from allow-listed relative source files.
                # Python 3.11's initial tarfile API predates extraction filters.
                for member in archive.getmembers():
                    self.assertTrue(member.isfile())
                    self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, "", ""))
                    self.assertNotIn("..", Path(member.name).parts)
                    destination = root / "source" / member.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.extractfile(member).read())
            source = root / "source" / sumbi_build.STEM
            spec = importlib.util.spec_from_file_location("rebuild_backend", source / "sumbi_build.py")
            backend = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(backend)
            with zipfile.ZipFile(root / backend.build_wheel(root)) as archive:
                self.assertEqual(len(json.loads(archive.read("sumbi/catalog/practices.json"))["practices"]), 8)
                self.assertEqual(package_info, archive.read(backend.INFO + "/METADATA"))
            # Exercise the fixtures that extension-only packaging used to omit.
            probe = ("import sys, unittest; sys.path.insert(0, 'tests'); "
                     "suite=unittest.defaultTestLoader.loadTestsFromNames(["
                     "'install.test_inventory.InventoryTests.test_empty_truth',"
                     "'install.test_inventory.InventoryTests.test_configured_truth']); "
                     "result=unittest.TextTestRunner().run(suite); "
                     "sys.exit(not result.wasSuccessful())")
            result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=source,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_readme_rejects_absolute_traversing_and_unsupported_inputs(self):
        for name in ("../README.md", "/synthetic/README.md", "q:/synthetic/README.md",
                     "folder\\README.md", "", "README.bin", {"file": "README.md"}):
            with self.subTest(name=name), patch.dict(sumbi_build.PROJECT, {"readme": name}):
                with self.assertRaises(ValueError):
                    sumbi_build.metadata()

    def test_distribution_sources_do_not_follow_links(self):
        with IsolatedTemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sumbi").mkdir()
            outside = root / "synthetic-private.json"
            outside.write_text("{}", encoding="utf-8")
            try:
                (root / "sumbi/link.json").symlink_to(outside)
            except OSError:
                self.skipTest("Symlink creation is unavailable on this test host.")
            with patch.object(sumbi_build, "ROOT", root), self.assertRaises(ValueError):
                sumbi_build.source_files("sumbi", (".json",))

    def test_build_and_runtime_dependency_lists_are_empty(self):
        project = tomllib.loads((sumbi_build.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["build-system"]["requires"], [])
        self.assertEqual(project["project"]["dependencies"], [])
        self.assertEqual(sumbi_build.get_requires_for_build_wheel(), [])
        self.assertEqual(sumbi_build.get_requires_for_build_sdist(), [])


if __name__ == "__main__":
    unittest.main()
