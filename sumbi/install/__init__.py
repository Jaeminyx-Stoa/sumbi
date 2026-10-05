"""Offline harness inventory and additive installation.

The collect integration point is ``sumbi.collect.baseline(repository=Path)``.
It must be read-only and offline, and return JSON-safe aggregate metadata.
"""

from .inventory import inventory
from .gaps import find_gaps
from .planner import build_plan
from .apply import apply_plan

__all__ = ["inventory", "find_gaps", "build_plan", "apply_plan"]
