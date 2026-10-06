"""Read bounded local owner declarations, including the practice language."""

import tomllib
from pathlib import Path

from .errors import InstallError
from .files import read_bytes


def load_configuration(root: Path) -> dict:
    raw = read_bytes(root, ".sumbi/config.toml")
    try:
        config = tomllib.loads(raw.decode("utf-8-sig")) if raw is not None else {}
    except (ValueError, UnicodeError):
        raise InstallError("Invalid install configuration.") from None
    if "language" in config and config["language"] not in ("en", "ko"):
        raise InstallError('Install language must be "en" or "ko".')
    return config
