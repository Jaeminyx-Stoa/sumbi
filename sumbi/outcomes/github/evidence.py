"""Validate evidence structure, then discard unusable at-merge items with diagnostics."""

from collections import defaultdict
import re

from sumbi.outcomes.github.ledger import utc

SHA = r"[0-9a-fA-F]{40}"
SNAPSHOT = {"head_sha", "captured_at", "required", "check_runs", "statuses"}


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(SHA, value):
        raise ValueError("Outcomes: invalid evidence SHA")


def names(values):
    if values is not None and (not isinstance(values, list) or any(
        not isinstance(n, str) or not n for n in values) or len(set(values)) != len(values)):
        raise ValueError("Outcomes: required checks must be an explicit unique name list")


def snapshot_structure(snapshot):
    """Missing documented fields, wrong types, labels and IDs are structural errors."""
    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT:
        raise ValueError("Outcomes: invalid checks_at_merge snapshot")
    sha(snapshot["head_sha"])
    utc(snapshot["captured_at"], "Outcomes checks captured_at")
    names(snapshot["required"])
    for kind in ("check_runs", "statuses"):
        if not isinstance(snapshot[kind], list):
            raise ValueError("Outcomes: check_runs and statuses must be arrays")
        ids = set()
        for item in snapshot[kind]:
            if not isinstance(item, dict):
                raise ValueError("Outcomes: evidence items must be objects")
            if "id" in item:
                if type(item["id"]) is not int or item["id"] <= 0 or item["id"] in ids:
                    raise ValueError("Outcomes: invalid or duplicate evidence ID")
                ids.add(item["id"])
            check_structure(item) if kind == "check_runs" else status_structure(item)


def check_structure(check):
    if not {"name", "head_sha", "started_at", "completed_at", "status", "conclusion"} <= set(
        check) or not isinstance(check["name"], str) or not check["name"] or check[
        "status"] not in ("queued", "in_progress", "completed") or check["conclusion"] not in (
            None, "success", "failure", "neutral", "cancelled", "skipped", "timed_out",
            "action_required", "stale", "startup_failure"):
        raise ValueError("Outcomes: invalid check-run state")
    sha(check["head_sha"])
    for key in ("started_at", "completed_at"):
        if check[key] is not None:
            utc(check[key], "Outcomes check " + key)
    app = check.get("app", {})
    if not isinstance(app, dict) or (app.get("id") is not None and (
        type(app["id"]) is not int or app["id"] <= 0)):
        raise ValueError("Outcomes: invalid check app ID")


def status_structure(status):
    if not {"context", "sha", "updated_at", "state"} <= set(status) or not isinstance(
        status["context"], str) or not status["context"] or status["state"] not in (
            "success", "pending", "failure", "error"):
        raise ValueError("Outcomes: invalid commit status")
    sha(status["sha"])
    utc(status["updated_at"], "Outcomes status updated_at")


def at_merge(snapshot, head, merged, gap, *, by_app=False):
    """Structure is checked even for evidence that will be discarded."""
    snapshot_structure(snapshot)
    valid = True
    if snapshot["head_sha"].lower() != head.lower():
        gap("snapshot_not_on_head", snapshot)
        valid = False
    if not merged or utc(snapshot["captured_at"], "Outcomes checks captured_at") != merged:
        gap("snapshot_not_at_merge", snapshot)
        valid = False
    runs, statuses = [], []
    for check in snapshot["check_runs"]:
        reason = check_gap(check, head, merged) if valid else "snapshot_evidence_excluded"
        if reason:
            if valid:
                gap(reason, check)
        else:
            runs.append(check)
    for status in snapshot["statuses"]:
        reason = ("status_not_on_head" if status["sha"].lower() != head.lower()
            else "status_after_merge" if merged and utc(status["updated_at"],
                "Outcomes status updated_at") > merged else None)
        if reason:
            if valid:
                gap(reason, status)
        elif valid:
            statuses.append(status)
    runs = without_conflicts(runs, "name", "started_at", "conflicting_check_evidence", gap, by_app)
    statuses = without_conflicts(statuses, "context", "updated_at",
        "conflicting_status_evidence", gap)
    return {**snapshot, "check_runs": runs, "statuses": statuses}, valid


def check_gap(check, head, merged):
    if check["head_sha"].lower() != head.lower():
        return "check_not_on_head"
    start = utc(check["started_at"], "Outcomes check started_at") if check["started_at"] else None
    end = utc(check["completed_at"], "Outcomes check completed_at") if check[
        "completed_at"] else None
    if merged and (start and start > merged or end and end > merged):
        return "check_after_merge"
    if (not start or end and end < start or (check["status"] == "completed") != bool(end)
        or check["status"] == "completed" and check["conclusion"] is None
        or check["status"] != "completed" and check["conclusion"] is not None):
        return "check_incomplete"
    return None


def without_conflicts(rows, name_key, time_key, reason, gap, by_app=False):
    groups, excluded = defaultdict(list), {}
    for row in rows:
        key = row[name_key], row.get("app", {}).get("id") if by_app else None
        groups[(key, utc(row[time_key], "Outcomes evidence time"))].append(row)
    for (key, when), items in groups.items():
        values = {item["state"] if name_key == "context" else (
            item["status"], item["conclusion"]) for item in items}
        if len(values) > 1:
            excluded[key] = max(when, excluded.get(key, when))
            gap(reason, items)
    return [r for r in rows if (key := (r[name_key], r.get("app", {}).get("id")
        if by_app else None)) not in excluded
        or utc(r[time_key], "Outcomes evidence time") > excluded[key]]


def verdict(snapshot):
    """Latest attempts win; both sources of an unpinned name must pass."""
    latest, statuses = {}, {}
    for check in snapshot["check_runs"]:
        when = utc(check["started_at"], "Outcomes check started_at")
        value = "green" if check["status"] == "completed" and check["conclusion"] in (
            "success", "neutral", "skipped") else "red"
        if check["name"] not in latest or when >= latest[check["name"]][0]:
            latest[check["name"]] = when, value
    for status in snapshot["statuses"]:
        when = utc(status["updated_at"], "Outcomes status updated_at")
        if status["context"] not in statuses or when >= statuses[status["context"]][0]:
            statuses[status["context"]] = when, "green" if status["state"] == "success" else "red"
    for name, (when, value) in statuses.items():
        if name in latest:
            value = "green" if value == latest[name][1] == "green" else "red"
        latest[name] = when, value
    if snapshot["required"] is None or any(n not in latest for n in snapshot["required"]):
        return "unknown"
    return "green" if all(latest[n][1] == "green" for n in snapshot["required"]) else "red"
