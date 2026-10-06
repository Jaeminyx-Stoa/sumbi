"""Local apply-time evidence and conservative exposure accounting."""

import json
from pathlib import Path

from sumbi.ledger import utc
from sumbi.registration import _unique


def exposure_gap(registration, path: Path | None, intervention_id=None):
    if path is None:
        if intervention_id is not None:
            raise ValueError("Intervention ID requires an interventions file")
        return None
    identity = intervention_id or registration.intervention_id
    if identity != registration.intervention_id:
        raise ValueError("Intervention ID must match registration")
    try:
        times = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line, object_pairs_hook=_unique)
            if not isinstance(row, dict):
                raise ValueError
            if row.get("action") == "revert":
                continue
            # Older installations used a practice ID as the intervention ID.
            if row.get("intervention_id", row.get("practice_id")) == identity:
                times.add(utc(row["utc_time"], "Intervention"))
        # Several practices in one install share a timestamp. Repeated installs
        # at different times cannot silently select a convenient exposure date.
        if len(times) != 1:
            raise ValueError
        actual = times.pop()
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise ValueError("Interventions must identify one valid UTC apply time") from None
    planned = registration.applied_at
    since, until = sorted((planned, actual))
    return {"actual_write_at": actual.isoformat(), "since": since.isoformat(),
            "until": until.isoformat(), "bounds": "[since,until)",
            "seconds": (until - since).total_seconds(),
            "direction": "later" if actual > planned else "earlier" if actual < planned else "equal"}


def in_exposure_gap(when, gap):
    from sumbi.model import timestamp
    return bool(gap and when is not None and timestamp(gap["since"]) <= when < timestamp(gap["until"]))


def exposure_side(when, planned, gap):
    from sumbi.model import timestamp
    if in_exposure_gap(when, gap):
        return "mixed"
    boundary = timestamp(gap["actual_write_at"]) if gap else planned
    return "before" if when < boundary else "after"


def gap_summary(gap):
    return f"Exposure gap: {gap['seconds']:g} seconds; actual write {gap['direction']} than planned"
