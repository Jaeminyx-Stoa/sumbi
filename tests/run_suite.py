"""Run discovery with an explicitly selected temporary-file parent."""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.temp_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[name] = str(root)
    tempfile.tempdir = str(root)
    tests = Path(__file__).resolve().parent
    sys.path.insert(0, str(tests.parent))
    sys.path.insert(0, str(tests))
    suite = unittest.defaultTestLoader.discover(str(tests))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
