"""Verify dependency-free distributions and the console entry point."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

import sumbi_build


class BuildTests(unittest.TestCase):
    def test_wheel_metadata_entry_point_and_file_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            name = sumbi_build.build_wheel(directory)
            with zipfile.ZipFile(Path(directory) / name) as archive:
                metadata = archive.read(sumbi_build.INFO + "/METADATA").decode()
                self.assertIn("Requires-Python: >=3.11", metadata)
                self.assertNotIn("Requires-Dist:", metadata)
                self.assertEqual(archive.read(sumbi_build.INFO + "/LICENSE"), (sumbi_build.ROOT / "LICENSE").read_bytes())
                self.assertIn("sumbi = sumbi.cli:main", archive.read(sumbi_build.INFO + "/entry_points.txt").decode())
                self.assertIn("sumbi/adapters/codex.py", archive.namelist())
                self.assertIn("sumbi/__main__.py", archive.namelist())
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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = sumbi_build.build_sdist(root)
            with tarfile.open(root / name) as archive:
                self.assertIn(sumbi_build.STEM + "/docs/deliverables.template.csv", archive.getnames())
                self.assertTrue(all(p.name.startswith(sumbi_build.STEM + "/") for p in archive.getmembers()))
                # The archive was authored above from allow-listed relative source files.
                # Python 3.11's initial tarfile API predates extraction filters.
                for member in archive.getmembers():
                    self.assertTrue(member.isfile())
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

    def test_build_and_runtime_dependency_lists_are_empty(self):
        project = tomllib.loads((sumbi_build.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["build-system"]["requires"], [])
        self.assertEqual(project["project"]["dependencies"], [])
        self.assertEqual(sumbi_build.get_requires_for_build_wheel(), [])
        self.assertEqual(sumbi_build.get_requires_for_build_sdist(), [])


if __name__ == "__main__":
    unittest.main()
