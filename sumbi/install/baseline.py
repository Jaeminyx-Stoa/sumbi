"""A narrow optional hook; the installer does not collect logs itself."""

import importlib
from pathlib import Path

from .errors import InstallError

PENDING = "pending (collect not available)"


def baseline(repository: Path, *, run: bool = False) -> dict:
    try:
        module = importlib.import_module("sumbi.collect")
    except ModuleNotFoundError as error:
        if error.name in {"sumbi.collect", "sumbi"}:
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
        entry(repository=repository)
    except Exception:
        raise InstallError("Collect baseline failed; no practices applied.") from None
    # The collect entry point owns its aggregate artifact. Never echo its return
    # value: an unrecognized integration must not export free text or log data.
    return {"status": "recorded", "entry_point": "sumbi.collect.baseline"}
