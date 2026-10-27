"""Register interventions applied outside improve bundles and observe them later.

Registrations append to the existing intervention ledger; checks read two collect
reports and an optional external signal file. Output is labels, counts and
numbers only: no free text, signal payload fields or causal verdicts.
"""

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat

from sumbi.core.json import unique_object
from sumbi.core.values import epoch
from .errors import InstallError
from .files import _checked, _write, read_bytes, safe_path
from sumbi.measure.profile import ROLES, TOOL_CLASSES
from .improve import (LABEL, MAX_BYTES, PROJECT_REF as IMPROVE_PROJECT_REF, _load, _lookup,
    _number, project)
from .planner import digest
from .revert import LEDGER

MAX_ITEMS = 16
MAX_SIGNAL_BYTES = 64 * MAX_BYTES
DAY = 86400.0
SIGNAL = re.compile(r"[a-z][a-z0-9-]{0,63}")
SUCCESS_METRICS = ("tokens-per-success", "time-per-success", "success-rate")
# Per-day cost metrics computed from existing collect summary fields.
PER_DAY = {
    "sessions-per-day": "/summary/sessions",
    "tokens-per-day": "/summary/tokens/total",
    "new-input-tokens-per-day": "/summary/tokens/new_input",
    "cache-write-tokens-per-day": "/summary/tokens/cache_write",
    "cache-read-tokens-per-day": "/summary/tokens/cache_read",
    "output-tokens-per-day": "/summary/tokens/output",
    "reasoning-output-subset-tokens-per-day": "/summary/tokens/reasoning_output",
    "compactions-per-day": "/summary/counts/compactions",
    "estimated-active-seconds-per-day": "/summary/time/active_seconds/value",
    # Present only when collect ran with --price-table.
    "estimated-value-per-day": "/value/summary/amount",
}
RATIOS = {"tool-error-rate": ("/summary/counts/tool_errors", "/summary/counts/tool_results")}
# Role metrics read the collect profile as reported (already per day or a share),
# from /profile/summary or, with a project reference, /profile/projects/<ref>.
PROFILE = {
    **{f"{role}-{name}": f"roles/{role}/{leaf}" for role in ROLES for name, leaf in (
        ("sessions-per-day", "sessions_per_day"), ("tokens-per-day", "tokens_per_day/total"),
        ("tool-calls-per-day", "tool_calls_per_day"), ("tool-error-rate", "tool_error_rate"),
        ("compactions-per-day", "compactions_per_day"),
        ("active-seconds-per-day", "active_seconds_per_day"),
        ("median-session-tokens", "session_total_tokens_median"),
        ("tool-seconds-per-paired-call", "tool_seconds_per_paired_call"),
        ("long-tail-share", "long_tail/token_share"),
        ("compacted-session-share", "compaction/token_share"),
        ("top-decile-share", "concentration/top_decile_share"),
        ("calls-per-session-median", "calls/per_session_median"),
        ("call-context-median", "calls/context_median"),
        ("call-context-p90", "calls/context_p90"),
        ("first-call-context-median", "calls/first_context_median"),
        ("first5-context-median", "calls/first5_context_sum_median"),
        *((f"{c}-tool-median-seconds", f"tool_latency/{c}/median_seconds")
            for c in TOOL_CLASSES))},
    "children-per-parent-max": "fan_out/children_max",
    "children-per-parent-median": "fan_out/children_median",
}
PROJECT_REF = re.compile(IMPROVE_PROJECT_REF)
COST_METRICS = (*PER_DAY, *RATIOS, *PROFILE)


def _utc_text(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_expectation(value: str, size_key: str) -> dict:
    """Parse METRIC:DIRECTION:PERCENT; reject anything but bounded labels and numbers."""
    parts = value.split(":") if isinstance(value, str) else []
    if len(parts) != 3 or not LABEL.fullmatch(parts[0]) or parts[1] not in (
        "increase", "decrease"):
        raise InstallError("Expectations must be METRIC:increase|decrease:PERCENT labels.")
    try:
        size = float(parts[2])
    except ValueError:
        size = math.nan
    if not math.isfinite(size) or size < 0:
        raise InstallError("Expectation percentages must be finite and nonnegative.")
    return {"metric": parts[0], "direction": parts[1], size_key: size}


def _history(root: Path) -> tuple[bytes | None, list[dict]]:
    ledger = read_bytes(root, LEDGER)
    _checked(root, LEDGER, ledger)
    try:
        if ledger and not ledger.endswith(b"\n"):
            raise ValueError
        history = [json.loads(line, object_pairs_hook=unique_object)
            for line in (ledger or b"").splitlines()]
        if any(not isinstance(record, dict) for record in history):
            raise ValueError
    except (ValueError, UnicodeError):
        raise InstallError("Intervention ledger is invalid; nothing registered.") from None
    return ledger, history


def register_external(repository: Path | str, *, intervention_id: str, applied_at: datetime,
    changes: list[str], predictions: list[dict], revert_if: list[dict], before_days: float,
    after_days: float, now: datetime | None = None) -> dict:
    """Append one registration row to the intervention ledger under the install lock."""
    root = Path(repository).resolve()
    if not root.is_dir():
        raise InstallError("Registration requires an existing workspace directory.")
    record = _record(intervention_id, applied_at, changes, predictions, revert_if,
        before_days, after_days, now or datetime.now(timezone.utc))
    local = safe_path(root, ".sumbi")
    local.mkdir(mode=0o700, exist_ok=True)
    lock = safe_path(root, ".sumbi/install.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise InstallError("Another install is active or a recovery lock remains.") from None
    os.close(descriptor)
    try:
        ledger, history = _history(root)
        if any(row.get("intervention_id", row.get("practice_id")) == intervention_id
            for row in history):
            raise InstallError("Intervention ID already exists; use a new intervention ID.")
        path = safe_path(root, LEDGER)
        mode = stat.S_IMODE(path.stat().st_mode) if ledger is not None else 0o600
        _write(root, LEDGER, ledger, (ledger or b"")
            + (json.dumps(record, sort_keys=True) + "\n").encode(), mode)
        return record
    finally:
        lock.unlink()


def _record(identity, applied_at, changes, predictions, revert_if, before_days, after_days,
    now):
    if not isinstance(identity, str) or not LABEL.fullmatch(identity):
        raise InstallError("Intervention ID must be a bounded public-safe label.")
    if (not changes or len(changes) > MAX_ITEMS or len(set(changes)) != len(changes)
        or any(not isinstance(c, str) or not LABEL.fullmatch(c) for c in changes)):
        raise InstallError("Change labels must be 1-16 unique bounded labels.")
    for name, items, size_key in (("Predictions", predictions, "rough_size_percent"),
        ("Revert criteria", revert_if, "threshold_percent")):
        if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
            raise InstallError(name + " need 1-16 entries.")
        try:
            for item in items:
                _expectation(item, size_key)
        except ValueError:
            # Extra fields could carry free text into the ledger.
            raise InstallError(name + " must contain only metric, direction and "
                "percentage fields.") from None
    for days in (before_days, after_days):
        if type(days) not in (int, float) or not math.isfinite(days) or days <= 0:
            raise InstallError("Window days must be positive and finite.")
    try:
        before = (applied_at - timedelta(days=before_days), applied_at)
        after = (applied_at, applied_at + timedelta(days=after_days))
    except OverflowError:
        raise InstallError("Window days must be positive and finite.") from None
    applied = _utc_text(applied_at)
    return {"action": "register", "practice_id": "external-change",
        "intervention_id": identity, "changes": list(changes),
        "predictions": [dict(p) for p in predictions], "revert_if": [dict(r) for r in revert_if],
        "windows": {"before_days": before_days, "after_days": after_days,
            "before": {"since": _utc_text(before[0]), "until": _utc_text(before[1])},
            "after": {"since": _utc_text(after[0]), "until": _utc_text(after[1])},
            "bounds": "[since,until)"},
        "applied_at": applied, "registered_at": _utc_text(now),
        "preregistered": now <= applied_at,
        # Comparison exposure reads utc_time as the actual apply time.
        "utc_time": applied}


def read_external(repository: Path | str, intervention_id: str) -> dict:
    """Return the one validated registration row for an intervention ID."""
    root = Path(repository).resolve()
    if not isinstance(intervention_id, str) or not LABEL.fullmatch(intervention_id):
        raise InstallError("Intervention ID must be a bounded public-safe label.")
    _, history = _history(root)
    rows = [row for row in history if row.get("action") == "register"
        and row.get("intervention_id") == intervention_id]
    if len(rows) != 1:
        raise InstallError("Expected exactly one registration for this intervention ID.")
    row = rows[0]
    try:
        applied = _when(row["applied_at"])
        registered = _when(row["registered_at"])
        windows = row["windows"]
        rebuilt = _record(intervention_id, applied, row["changes"],
            [_expectation(p, "rough_size_percent") for p in row["predictions"]],
            [_expectation(r, "threshold_percent") for r in row["revert_if"]],
            windows["before_days"], windows["after_days"], registered)
        if rebuilt != row:
            raise ValueError
    except (InstallError, ValueError, TypeError, KeyError, AttributeError):
        raise InstallError("Registration row must match the documented schema.") from None
    return row


def _when(value):
    parsed = epoch(value) if isinstance(value, str) else None
    if parsed is None:
        raise ValueError
    return parsed


def _expectation(value, size_key):
    if (not isinstance(value, dict) or set(value) != {"metric", "direction", size_key}
        or not isinstance(value["metric"], str) or not LABEL.fullmatch(value["metric"])
        or value["direction"] not in ("increase", "decrease")
        or not _number(value[size_key])):
        raise ValueError
    return value


def _report(path: Path, scope: str = "/profile/summary/"):
    """Validate, hash and measure one byte snapshot with the observation projection."""
    data, report = _load(Path(path), 16 * MAX_BYTES)
    project(data, report)
    window = report["window"]
    since, until = _when(window["since"]), _when(window["until"])
    values = {}
    for pointer in {*PER_DAY.values(), *(p for pair in RATIOS.values() for p in pair),
        *(scope + leaf for leaf in PROFILE.values())}:
        value = _lookup(report, pointer)
        if value is not None and not _number(value):
            raise InstallError(
                "Evidence must be a supported collect report with numeric metrics.")
        values[pointer] = value
    for metric, leaf in PROFILE.items():
        values[metric] = values.pop(scope + leaf)
    return {"sha256": digest(data), "since": since, "until": until,
        "days": (until - since).total_seconds() / DAY, "values": values}


def _metric(report, metric):
    if metric in PROFILE:
        return report["values"][metric]
    if metric in PER_DAY:
        value = report["values"][PER_DAY[metric]]
        return None if value is None else value / report["days"]
    numerator, denominator = (report["values"][p] for p in RATIOS[metric])
    return None if numerator is None or not denominator else numerator / denominator


def _change(before, after):
    if before is None or after is None:
        return None, None
    direction = "increase" if after > before else "decrease" if after < before else "unchanged"
    return ((after - before) / before * 100 if before else None), direction


def _expected(item, size_key, before, after):
    metric = item["metric"]
    row = dict(item)
    if metric in SUCCESS_METRICS:
        return {**row, "status": "not-available", "reason": "no-success-denominator"}
    if metric not in COST_METRICS:
        return {**row, "status": "not-available", "reason": "not-in-collect"}
    first, second = _metric(before, metric), _metric(after, metric)
    if first is None or second is None:
        return {**row, "status": "not-available", "reason": "not-reported"}
    percent, direction = _change(first, second)
    row.update(status="observed", before=first, after=second, change_percent=percent,
        observed_direction=direction)
    if size_key == "threshold_percent":
        row["crossed"] = (None if percent is None else direction == item["direction"]
            and abs(percent) >= item[size_key])
    return row


def count_signals(path: Path, before: tuple, after: tuple) -> dict:
    """Count bounded event labels per window; never read or echo other fields."""
    try:
        source = Path(path)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_SIGNAL_BYTES:
            raise ValueError
        data = source.read_bytes()
        if len(data) > MAX_SIGNAL_BYTES:
            raise ValueError
        text = data.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        raise InstallError("Signals must be a bounded UTF-8 JSONL regular file.") from None
    arms = {"before": {"labels": {}, "invalid": 0}, "after": {"labels": {}, "invalid": 0}}
    unreadable = outside = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            unreadable += 1
            continue
        try:
            when = epoch(row.get("ts")) if isinstance(row, dict) else None
        except OverflowError:
            when = None  # An integer too large for a float, such as 10**400.
        if when is None:
            unreadable += 1
            continue
        arm = ("before" if before[0] <= when < before[1]
            else "after" if after[0] <= when < after[1] else None)
        if arm is None:
            outside += 1
            continue
        label = row.get("event")
        if isinstance(label, str) and SIGNAL.fullmatch(label):
            labels = arms[arm]["labels"]
            labels[label] = labels.get(label, 0) + 1
        else:
            arms[arm]["invalid"] += 1
    for arm in arms.values():
        arm["labels"] = dict(sorted(arm["labels"].items()))
    return {**arms, "unreadable_lines": unreadable, "outside_windows": outside,
        "basis": "external-signal-file-not-sumbi-events"}


def check_external(repository: Path | str, intervention_id: str, before_path: Path,
    after_path: Path, signals: Path | None = None, project_ref: str | None = None) -> dict:
    """Show registered predictions and base cost metrics side by side; no causal claim."""
    row = read_external(repository, intervention_id)
    if project_ref is not None and not (isinstance(project_ref, str)
        and PROJECT_REF.fullmatch(project_ref)):
        raise InstallError("Project must be a collect profile project key or bucket.")
    scope = ("/profile/summary/" if project_ref is None
        else "/profile/projects/" + project_ref + "/")
    before, after = _report(before_path, scope), _report(after_path, scope)
    applied = _when(row["applied_at"])
    if before["until"] > applied or after["since"] < applied:
        raise InstallError("Before evidence must end, and after evidence start, "
            "on their own side of the apply time.")
    windows = {}
    for arm, report in (("before", before), ("after", after)):
        registered = row["windows"][arm]
        windows[arm] = {"since": _utc_text(report["since"]), "until": _utc_text(report["until"]),
            "days": report["days"], "matches_registration":
            _utc_text(report["since"]) == registered["since"]
            and _utc_text(report["until"]) == registered["until"]}
    metrics = []
    for metric in COST_METRICS:
        first, second = _metric(before, metric), _metric(after, metric)
        percent, direction = _change(first, second)
        metrics.append({"id": metric, "before": first, "after": second,
            "change_percent": percent, "observed_direction": direction})
    summary = {"version": 1, "intervention_id": intervention_id, "changes": row["changes"],
        "applied_at": row["applied_at"], "registered_at": row["registered_at"],
        "preregistered": row["preregistered"],
        "evidence": {"before_sha256": before["sha256"], "after_sha256": after["sha256"]},
        "windows": windows, "metrics": metrics,
        "role_metrics_scope": "all" if project_ref is None else project_ref,
        "predictions": [_expected(p, "rough_size_percent", before, after)
            for p in row["predictions"]],
        "revert_if": [_expected(r, "threshold_percent", before, after) for r in row["revert_if"]],
        "success": {"status": "not-available", "reason": "no-success-denominator"},
        "verdict": "observation-not-causal", "causality": "not-established"}
    if signals is not None:
        summary["signals"] = count_signals(signals, (before["since"], before["until"]),
            (after["since"], after["until"]))
    return summary
