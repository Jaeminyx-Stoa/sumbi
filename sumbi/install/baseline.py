"""Repository-scoped, offline pre-install measurement."""

from datetime import datetime, timedelta, timezone
import json
import re
from .errors import InstallError
from pathlib import Path

from sumbi.core.time import Window
from sumbi.measure.report import collect
from sumbi.events.registry import log_roots
from .files import _write, safe_path


# Accept only the baseline artifact timestamp grammar.
BASELINE_PATH = re.compile(r"\.sumbi/baseline/[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\.json")


def record_baseline(*, repository: Path, home: Path | None = None,
    salt: bytes | None = None, now: datetime | None = None) -> dict:
    """Write a counts-only report for the previous 14 UTC days, before apply."""
    # Use the installer's checked, exclusive publication for repository metadata.

    repository = repository.resolve()
    if not repository.is_dir():
        raise ValueError("Baseline requires an existing repository directory")
    home = home if home is not None else Path.home()
    until = now or datetime.now(timezone.utc)
    if until.tzinfo is None:
        raise ValueError("Baseline time must include a timezone")
    until = until.astimezone(timezone.utc)
    window = Window(until - timedelta(days=14), until)
    relative = ".sumbi/baseline/" + until.strftime("%Y%m%dT%H%M%S.%fZ") + ".json"
    destination = safe_path(repository, relative)
    sources = log_roots(home)
    if any(destination.resolve().is_relative_to(source.resolve()) for source in sources):
        raise ValueError("Baseline must be outside session-log directories")
    report, _ = collect(home, window, repository=repository, salt=salt)
    data = (json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    _write(repository, relative, None, data, 0o600)
    sessions = report["summary"]["sessions"]
    return {"status": "recorded" if sessions else "no sessions found", "file": relative,
        "sessions": sessions}


PENDING = "pending (collect not available)"


def baseline(repository: Path, *, run: bool = False, home: Path | None = None,
    salt: bytes | None = None) -> dict:
    entry = record_baseline
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
        and BASELINE_PATH.fullmatch(result["file"])
        and type(result.get("sessions")) is int and result["sessions"] >= 0):
        return {key: result[key] for key in ("status", "file", "sessions")}
    return {"status": "recorded", "entry_point": "sumbi.collect.baseline"}
