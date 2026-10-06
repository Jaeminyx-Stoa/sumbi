"""Vendored, text-only practices. No catalog item executes project code."""

import json
from pathlib import Path

VERSION = "0.1.0"


def load_catalog() -> list[dict]:
    """Read the bundled catalog; discovery and external catalogs are out of scope."""
    return json.loads(
        Path(__file__).with_name("practices.json").read_text(encoding="utf-8"))["practices"]


def load_judgment_policy() -> dict:
    """Return the common prerequisites for a later comparison, not a verdict."""
    return json.loads(
        Path(__file__).with_name("practices.json").read_text(encoding="utf-8"))["judgment_policy"]
