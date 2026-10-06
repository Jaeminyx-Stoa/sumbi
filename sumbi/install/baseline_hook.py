"""A narrow optional hook; the installer does not collect logs itself."""

import importlib
from pathlib import Path
import re

from .errors import InstallError

PENDING = "pending (collect not available)"


def baseline(repository: Path, *, run: bool = False, home: Path | None = None,
             salt: bytes | None = None) -> dict:
    try:
        module = importlib.import_module("sumbi.install.baseline")
    except ModuleNotFoundError as error:
        if error.name in {"sumbi.install.baseline", "sumbi"}:
            return {"status": PENDING}
        raise InstallError("Collect baseline import failed; no practices applied.") from None
    except Exception:
        raise InstallError("Collect baseline import failed; no practices applied.") from None
    entry = getattr(module, "baseline", None)
    if not callable(entry):
        return {"status": PENDING}
    if not run:
        return {"status": "available (runs before apply)"}
    try:
        options = {}
        if home is not None:
            options["home"] = home
        if salt is not None:
            options["salt"] = salt
        result = entry(repository=repository, **options)
    except Exception:
        raise InstallError("Collect baseline failed; no practices applied.") from None
    # Only recognized statuses and validated, relative artifact IDs cross into
    # committable intervention records; arbitrary collector text stays local.
    if (isinstance(result, dict) and isinstance(result.get("status"), str)
            and result["status"] in {"recorded", "no sessions found"}
            and isinstance(result.get("file"), str)
            and re.fullmatch(r"\.sumbi/baseline/[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\.json", result["file"])
            and type(result.get("sessions")) is int and result["sessions"] >= 0):
        return {key: result[key] for key in ("status", "file", "sessions")}
    return {"status": "recorded", "entry_point": "sumbi.collect.baseline"}
