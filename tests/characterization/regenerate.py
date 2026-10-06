"""Explicitly check or write reviewed CLI golden files (never called by tests)."""

import argparse
import difflib
import json
import os
from pathlib import Path
import sys
import tempfile

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))

from characterization.corpus import DIRECTORY, run_corpus
from characterization.argparse_contract import CASES, check_output, is_argparse_artifact


def difference(expected, actual, name):
    return "".join(difflib.unified_diff(expected.splitlines(keepends=True),
                                    actual.splitlines(keepends=True),
                                    fromfile="golden/" + name, tofile="actual/" + name))


def golden_files():
    return {p.relative_to(DIRECTORY / "golden").as_posix(): p.read_text(encoding="utf-8")
            for p in (DIRECTORY / "golden").rglob("*") if p.is_file()}


def flatten(corpus):
    return {case + "/" + name: text for case, artifacts in corpus.items() for name, text in artifacts.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Replace goldens for an explicitly intended output change")
    mode.add_argument("--dump", type=Path, help="Write a private determinism snapshot without modifying goldens")
    parser.add_argument("--temp-root", type=Path)
    args = parser.parse_args()
    if args.temp_root:
        root = args.temp_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(root)
        for name in ("TMPDIR", "TEMP", "TMP"):
            os.environ[name] = str(root)
    corpus = run_corpus()
    for case in CASES:
        check_output(case, corpus[case["name"]])
    if args.dump:
        args.dump.write_text(json.dumps(corpus, indent=2) + "\n", encoding="utf-8", newline="\n")
        return 0
    actual = flatten(corpus)
    if args.write:
        repeated = flatten(run_corpus())
        if repeated != actual:
            raise SystemExit("Corpus is nondeterministic; no goldens written")
        previous = golden_files()
        for name in sorted(previous.keys() - actual.keys()):
            (DIRECTORY / "golden" / name).unlink()
        for name, text in actual.items():
            path = DIRECTORY / "golden" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote {len(actual)} files for {len(corpus)} cases")
        return 0
    expected = golden_files()
    failed = False
    for name in sorted(actual.keys() | expected.keys()):
        if name not in expected or name not in actual:
            print("Unexpected artifact set: " + name)
            failed = True
        elif not is_argparse_artifact(name) and actual[name] != expected[name]:
            print(difference(expected[name], actual[name], name), end="")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
