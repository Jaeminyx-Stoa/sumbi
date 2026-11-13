"""Conservative decisions for version 1 fleet snapshots; no external effects."""

from datetime import datetime, timezone
import math

MAX_AGE_SECONDS = 600
ENGINES = {"claude", "codex", "opencode"}


def _time(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            dt.astimezone(timezone.utc)
            if dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0
            else None
        )
    except (ValueError, TypeError, AttributeError):
        return None


def _fresh(value, now):
    dt = _time(value)
    return dt is not None and 0 <= (now - dt).total_seconds() <= MAX_AGE_SECONDS


def _number(v, minimum=0, maximum=None):
    return (
        not isinstance(v, bool)
        and isinstance(v, (int, float))
        and math.isfinite(v)
        and v >= minimum
        and (maximum is None or v <= maximum)
    )


def quota(previous, current, now):
    """Return a status and reason, with slope in percentage points per hour."""
    unknown = lambda reason: {
        "status": "unknown",
        "reason": reason,
        "unit": "percentage_points_per_hour",
    }
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return unknown("missing_sample")
    if any(
        x.get("status") != "ok" or not _fresh(x.get("observed_at"), now)
        for x in (previous, current)
    ):
        return unknown("stale_or_invalid_sample")
    if (
        any(not isinstance(current.get(k), str) or not current[k] for k in ("account", "engine"))
        or current["engine"] not in ENGINES
    ):
        return unknown("invalid_identity")
    if any(
        previous.get(k) != current.get(k)
        for k in ("account", "engine", "resets_at", "window_minutes")
    ):
        return unknown("identity_or_reset_changed")
    reset = _time(current.get("resets_at"))
    a, b = _time(previous.get("observed_at")), _time(current.get("observed_at"))
    if (
        not reset
        or reset <= now
        or b <= a
        or not _number(current.get("window_minutes"), 1)
        or not all(_number(x.get("used_percent"), 0, 100) for x in (previous, current))
    ):
        return unknown("invalid_reset_or_sample")
    delta = current["used_percent"] - previous["used_percent"]
    if delta <= 0:
        return unknown("nonpositive_slope")
    slope = delta * 3600 / (b - a).total_seconds()
    seconds = (100 - current["used_percent"]) * 3600 / slope
    remaining = (reset - now).total_seconds()
    predicted = seconds <= remaining and remaining - seconds <= 3600
    return {
        "status": "signal" if predicted else "clear",
        "reason": "quota_exhaustion_before_reset" if predicted else "outside_reset_hour",
        "slope": slope,
        "unit": "percentage_points_per_hour",
        "seconds_to_exhaustion": seconds,
    }


def plan(snapshot, previous=None, now=None):
    """Evaluate windows and account samples, returning labeled decisions."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("unsupported snapshot")
    observed = _time(snapshot.get("observed_at"))
    if (
        observed is None
        or not isinstance(snapshot.get("windows"), list)
        or not isinstance(snapshot.get("accounts"), list)
    ):
        raise ValueError("invalid snapshot")
    reference = now or datetime.now(timezone.utc)
    snapshot_fresh = _fresh(snapshot.get("observed_at"), reference)
    now = observed
    return _windows(snapshot, now, snapshot_fresh) + _accounts(
        snapshot, previous, now, snapshot_fresh
    )


def _windows(snapshot, now, snapshot_fresh):
    results = []
    for row in snapshot["windows"]:
        if not isinstance(row, dict):
            results.append({"status": "unknown", "reason": "invalid_window"})
            continue
        identity = {k: row.get(k) for k in ("lane", "engine", "account", "session_id")}
        valid = (
            snapshot_fresh
            and row.get("status") == "ok"
            and _fresh(row.get("observed_at"), now)
            and row.get("engine") in ENGINES
            and all(
                isinstance(row.get(k), str) and row[k] for k in ("lane", "account", "session_id")
            )
        )
        for kind in ("context", "calls"):
            result = dict(identity, kind=kind)
            if not valid:
                result.update(status="unknown", reason="invalid_identity_or_stale")
            elif kind == "context":
                v = row.get("context_tokens")
                if not _number(v) or not isinstance(v, int):
                    result.update(status="unknown", reason="missing_context")
                else:
                    result.update(
                        status="signal" if v > 200000 else "clear",
                        reason="context_above_200000" if v > 200000 else "context_within_limit",
                        value=v,
                        threshold=200000,
                        unit="tokens",
                    )
            else:
                a, b = row.get("previous_calls_1h"), row.get("calls_1h")
                if not all(_number(v) and isinstance(v, int) for v in (a, b)):
                    result.update(status="unknown", reason="comparison_unavailable")
                else:
                    result.update(
                        status="signal" if a >= 10 and b >= 30 and b >= 2 * a else "clear",
                        reason="call_surge"
                        if a >= 10 and b >= 30 and b >= 2 * a
                        else "calls_within_limit",
                        previous=a,
                        value=b,
                        unit="calls_per_hour",
                        threshold="previous>=10 and current>=30 and current>=2*previous",
                    )
            results.append(result)
    return results


def _accounts(snapshot, previous, now, snapshot_fresh):
    results = []
    old = (
        previous.get("accounts", [])
        if isinstance(previous, dict) and previous.get("schema_version") == 1
        else []
    )
    for account in snapshot["accounts"]:
        if not isinstance(account, dict):
            results.append({"kind": "quota", "status": "unknown", "reason": "invalid_account"})
            continue
        matches = [
            a
            for a in old
            if isinstance(a, dict)
            and a.get("account") == account.get("account")
            and a.get("engine") == account.get("engine")
        ]
        current_matches = [
            a
            for a in snapshot["accounts"]
            if isinstance(a, dict)
            and a.get("account") == account.get("account")
            and a.get("engine") == account.get("engine")
        ]
        decision = quota(
            matches[0]
            if len(matches) == 1 and len(current_matches) == 1 and snapshot_fresh
            else None,
            account,
            now,
        )
        targets = [
            w
            for w in snapshot["windows"]
            if isinstance(w, dict)
            and w.get("account") == account.get("account")
            and w.get("engine") == account.get("engine")
            and w.get("status") == "ok"
            and _fresh(w.get("observed_at"), now)
            and w.get("lane")
            and w.get("session_id")
        ]
        if decision["status"] == "signal" and targets:
            for target in targets:
                results.append(
                    dict(
                        decision,
                        kind="quota",
                        account=account.get("account"),
                        engine=account.get("engine"),
                        lane=target["lane"],
                        session_id=target["session_id"],
                    )
                )
        else:
            if decision["status"] == "signal":
                decision = {
                    "status": "unknown",
                    "reason": "no_matching_window",
                    "unit": "percentage_points_per_hour",
                }
            results.append(
                dict(
                    decision,
                    kind="quota",
                    account=account.get("account"),
                    engine=account.get("engine"),
                )
            )
    return results
