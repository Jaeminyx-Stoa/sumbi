"""Expose offline harness inventory, additive installation, and guarded reversal.
The baseline integration records read-only, offline aggregate measurements before target writes.
"""

from .inventory import inventory
from .gaps import find_gaps
from .planner import build_plan
from .apply import apply_plan
from .revert import revert_install

__all__ = ["inventory", "find_gaps", "build_plan", "apply_plan", "revert_install"]
