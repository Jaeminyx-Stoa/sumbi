"""Minimal standard-library PEP 517 backend for this pure-Python package."""

import base64
import csv
import hashlib
import io
from pathlib import Path
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
STEM = PROJECT["name"] + "-" + PROJECT["version"]
INFO = STEM + ".dist-info"


def metadata():
    return {
        "METADATA": ("Metadata-Version: 2.1\n" + "Name: " + PROJECT["name"] + "\n"
                     + "Version: " + PROJECT["version"] + "\n"
                     + "Summary: " + PROJECT["description"] + "\n"
                     + "Requires-Python: " + PROJECT["requires-python"] + "\n"
                     + "License: " + PROJECT["license"]["text"] + "\n\n").encode(),
        "WHEEL": b"Wheel-Version: 1.0\nGenerator: sumbi_build\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "LICENSE": (ROOT / "LICENSE").read_bytes(),
        "entry_points.txt": ("[console_scripts]\n" + "".join(
            name + " = " + entry + "\n" for name, entry in PROJECT["scripts"].items())).encode(),
    }


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    destination = Path(metadata_directory) / INFO
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in metadata().items():
        (destination / name).write_bytes(data)
    return INFO


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    filename = STEM + "-py3-none-any.whl"
    destination = Path(wheel_directory)
    destination.mkdir(parents=True, exist_ok=True)
    entries = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in sorted((ROOT / "sumbi").rglob("*"))
               if p.is_file() and p.suffix in (".py", ".json", ".md")}
    entries.update({INFO + "/" + name: data for name, data in metadata().items()})
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for name, data in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, len(data)])
    writer.writerow([INFO + "/RECORD", "", ""])
    entries[INFO + "/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(destination / filename, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return filename


def build_sdist(sdist_directory, config_settings=None):
    filename = STEM + ".tar.gz"
    destination = Path(sdist_directory)
    destination.mkdir(parents=True, exist_ok=True)
    sources = [ROOT / p for p in ("pyproject.toml", "sumbi_build.py", "README.md", "LICENSE")]
    for directory in ("sumbi", "tests", "docs"):
        sources.extend(p for p in sorted((ROOT / directory).rglob("*"))
                       if p.is_file() and p.suffix in (".py", ".md", ".json", ".jsonl"))
    with tarfile.open(destination / filename, "w:gz") as archive:
        for path in sources:
            archive.add(path, arcname=STEM + "/" + path.relative_to(ROOT).as_posix(), recursive=False)
    return filename
