"""Minimal standard-library PEP 517 backend for this pure-Python package."""

import base64
import csv
import hashlib
import io
from pathlib import Path, PurePosixPath
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
STEM = PROJECT["name"] + "-" + PROJECT["version"]
INFO = STEM + ".dist-info"


def readme():
    """Read only the declared, repository-relative long description."""
    name = PROJECT["readme"]
    if not isinstance(name, str):
        raise ValueError("Readme must name a repository-relative file")
    path = PurePosixPath(name)
    if (path.is_absolute() or "\\" in name or ":" in name
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("Readme must name a repository-relative file")
    source = ROOT
    for part in path.parts:
        source = source / part
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise ValueError("Readme links are not supported")
    if not source.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Readme must remain inside the source tree")
    content_type = {".md": "text/markdown", ".rst": "text/x-rst", ".txt": "text/plain"}.get(source.suffix)
    if content_type is None:
        raise ValueError("Readme must use a supported text format")
    return source.read_text(encoding="utf-8"), content_type


def source_files(directory, suffixes, names=()):
    """Select distribution inputs without following source-tree links."""
    selected = []
    for path in sorted((ROOT / directory).rglob("*")):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("Distribution source links are not supported")
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("Distribution sources must remain inside the source tree")
        if path.is_file() and (path.suffix in suffixes or path.name in names):
            selected.append(path)
    return selected


def metadata():
    description, content_type = readme()
    extra = ("Description-Content-Type: " + content_type + "\n"
             + "".join("Classifier: " + value + "\n" for value in PROJECT.get("classifiers", []))
             + "".join("Project-URL: " + name + ", " + value + "\n" for name, value in PROJECT.get("urls", {}).items()))
    return {
        "METADATA": ("Metadata-Version: 2.2\n" + "Name: " + PROJECT["name"] + "\n"
                     + "Version: " + PROJECT["version"] + "\n"
                     + "Summary: " + PROJECT["description"] + "\n"
                     + "Requires-Python: " + PROJECT["requires-python"] + "\n"
                     + "License: " + PROJECT["license"]["text"] + "\n" + extra + "\n" + description).encode(),
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
    entries = {p.relative_to(ROOT).as_posix(): p.read_bytes()
               for p in source_files("sumbi", (".py", ".json", ".md"))}
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
    readme()  # Validate the declared input before adding it to the archive.
    filename = STEM + ".tar.gz"
    destination = Path(sdist_directory)
    destination.mkdir(parents=True, exist_ok=True)
    sources = [ROOT / p for p in ("pyproject.toml", "sumbi_build.py", PROJECT["readme"], "LICENSE")]
    for directory in ("sumbi", "tests", "docs"):
        suffixes = (".py", ".md", ".json", ".jsonl", ".csv")
        if directory == "tests":
            suffixes += (".fixture", ".ini", ".toml", ".yml", ".yaml", ".cfg", ".txt", ".mdc")
        names = (".fixture", "Makefile", "CODEOWNERS", "pre-commit") if directory == "tests" else ()
        sources.extend(source_files(directory, suffixes, names=names))
    def anonymous_owner(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    with tarfile.open(destination / filename, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(set(sources)):
            archive.add(path, arcname=STEM + "/" + path.relative_to(ROOT).as_posix(),
                        recursive=False, filter=anonymous_owner)
        data = metadata()["METADATA"]
        info = tarfile.TarInfo(STEM + "/PKG-INFO")
        info.size = len(data)
        info.mode = 0o644
        archive.addfile(anonymous_owner(info), io.BytesIO(data))
    return filename
