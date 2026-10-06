"""Strict, local pre-registration schema; diagnostics never echo input."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile

from sumbi.outcomes.github.ledger import LABEL, utc
from sumbi.core.time import Window
from sumbi.core.json import unique_object
from sumbi.catalog import load_catalog


@dataclass(frozen=True)
class Registration:
    intervention_id: str
    applied_at: datetime
    registered_at: datetime
    predictions: tuple[dict, ...]
    margin_pp: float
    sample_size_per_arm: int
    follow_up_days: float
    before: Window
    after: Window
    confounders: tuple[dict, ...]
    outcome_source: str = "github"

    @property
    def preregistered(self):
        return self.registered_at <= min(self.applied_at, self.after.since)


def read_registration(path: Path) -> Registration:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        fields = {"intervention_id", "applied_at", "registered_at", "predictions",
            "non_inferiority_margin_pp", "sample_size_per_arm", "follow_up_days",
            "before", "after"}
        if not isinstance(raw,
            dict) or not fields <= set(raw) or set(raw) - fields - {"confounders",
                "outcome_source"}:
            raise ValueError
        source = raw.get("outcome_source", "github")
        if source not in ("github", "local-verify"):
            raise ValueError
        def bounded(value):
            return isinstance(value, str) and re.fullmatch(LABEL, value)
        def number(value):
            return type(value) in (int, float) and math.isfinite(value)
        if not bounded(raw["intervention_id"]):
            raise ValueError
        applied, registered = utc(raw["applied_at"], "Registration"), utc(raw["registered_at"],
            "Registration")
        windows = []
        for arm in ("before", "after"):
            w = raw[arm]
            if not isinstance(w, dict) or set(w) != {"since", "until"}:
                raise ValueError
            windows.append(Window(utc(w["since"], "Registration"), utc(w["until"], "Registration")))
        before, after = windows
        if (before.until > applied or after.since < applied
            or before.until - before.since != after.until - after.since):
            raise ValueError
        margin, size, days = (raw["non_inferiority_margin_pp"], raw["sample_size_per_arm"],
            raw["follow_up_days"])
        if (not number(margin) or not 0 < margin < 100 or type(size) is not int or size < 1
            or not number(days) or days <= 0):
            raise ValueError
        predictions = raw["predictions"]
        if not isinstance(predictions, list) or not predictions:
            raise ValueError
        safe_predictions = []
        for prediction in predictions:
            if (not isinstance(prediction, dict)
                or not {"metric", "direction", "rough_size_percent"} <= set(prediction)
                or set(prediction) - {"metric", "direction", "rough_size_percent", "condition"}
                or not bounded(prediction["metric"])
                or prediction["direction"] not in ("increase", "decrease")
                or not number(prediction["rough_size_percent"])
                or prediction["rough_size_percent"] < 0
                or ("condition" in prediction
                    and not isinstance(prediction["condition"], str))):
                raise ValueError
            safe_predictions.append({k: prediction[k]
                for k in ("metric", "direction", "rough_size_percent")})
        confounders = raw.get("confounders", [])
        if not isinstance(confounders, list):
            raise ValueError
        events = []
        for event in confounders:
            if not isinstance(event, dict) or set(event) != {"at",
                "label"} or not bounded(event["label"]):
                raise ValueError
            events.append({"at": utc(event["at"], "Registration"), "label": event["label"]})
        return Registration(raw["intervention_id"], applied, registered, tuple(safe_predictions),
            margin, size, days, before, after, tuple(events), source)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("Registration must match the documented comparison JSON schema") from None


def write_registration(path: Path, *, intervention_id, outcome_source, margin_pp,
    sample_size_per_arm, follow_up_days, window_days, applied_at, practice_ids):
    """Publish a validated catalog pre-registration exclusively, never replace."""
    catalog = {p["id"]: p for p in load_catalog()}
    if (not practice_ids or len(set(practice_ids)) != len(practice_ids)
        or any(p not in catalog for p in practice_ids)):
        raise ValueError("Predictions require unique bundled catalog practice IDs")
    if type(window_days) not in (int, float) or not math.isfinite(window_days) or window_days <= 0:
        raise ValueError("Window days must be positive and finite")
    try:
        planned = utc(applied_at, "Registration")
        duration = timedelta(days=window_days)
        raw = {"intervention_id": intervention_id, "outcome_source": outcome_source,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "applied_at": planned.isoformat(),
            "non_inferiority_margin_pp": margin_pp, "sample_size_per_arm": sample_size_per_arm,
            "follow_up_days": follow_up_days,
            "before": {"since": (planned - duration).isoformat(), "until": planned.isoformat()},
            "after": {"since": planned.isoformat(), "until": (planned + duration).isoformat()},
            "predictions": [catalog[p]["prediction"] for p in practice_ids]}
        data = json.dumps(raw, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    except (ValueError, TypeError, OverflowError):
        raise ValueError(
            "Registration flags must match the documented comparison JSON schema") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=".sumbi-register-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.chmod(0o600)
        registration = read_registration(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ValueError("Registration already exists; it will not be overwritten") from None
        return registration
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
