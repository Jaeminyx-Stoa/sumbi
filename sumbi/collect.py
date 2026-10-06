"""Repository-scoped, offline pre-install measurement."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from sumbi.model import Window
from sumbi.report import collect, log_roots


def baseline(*, repository: Path, home: Path | None = None,
             salt: bytes | None = None, now: datetime | None = None) -> dict:
    """Write a counts-only report for the previous 14 UTC days, before apply."""
    # Use the installer's checked, exclusive publication for repository metadata.
    from sumbi.install.apply import _write
    from sumbi.install.inventory import safe_path

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
