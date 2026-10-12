"""Test package marker.

Test modules import shared helpers (``support``, ``worker_github_fixtures``) as
top-level modules, which is how ``unittest discover -s tests`` sees them. When a
module is run by dotted name from the repo root (``python -m unittest
tests.cli.test_x``) this makes the same top-level names importable.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
