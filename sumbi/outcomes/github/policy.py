"""Current-policy judgment using structurally valid, usable at-merge evidence."""

from sumbi.outcomes.github.evidence import at_merge, verdict
from sumbi.outcomes.github.ledger import utc


def policy_checks(evidence, merged, shas, gap):
    if evidence is None:
        if merged:
            gap("policy_incomplete", evidence)
        return "unknown", "unknown", "checks_policy_unreadable" if merged else "not_merged"
    if not isinstance(evidence, dict) or set(evidence) != {"required", "results"}:
        raise ValueError("Outcomes: invalid policy check evidence")
    required, snapshot = evidence["required"], evidence["results"]
    if required is not None and (not isinstance(required, list) or any(
        not isinstance(r, dict) or set(r) != {"context", "app_id"}
        or not isinstance(r["context"], str) or not r["context"]
        or (r["app_id"] is not None and (type(r["app_id"]) is not int or r["app_id"] <= 0))
        for r in required)):
        raise ValueError("Outcomes: invalid policy requirements")
    if required is not None and len({(r["context"], r["app_id"]) for r in required}) != len(
        required):
        raise ValueError("Outcomes: duplicate policy requirement")
    values, selected = {}, False
    if snapshot is not None:
        for candidate in snapshot if isinstance(snapshot, list) else [snapshot]:
            head = candidate.get("head_sha") if isinstance(candidate, dict) else None
            clean, valid = at_merge(candidate, next((s for s in shas if s and head and
                isinstance(head, str) and s.lower() == head.lower()), shas[0]), merged, gap,
                by_app=True)
            if candidate["required"] != []:
                gap("policy_incomplete", candidate)
                valid = False
            if valid and not selected:
                values = policy_values(clean)
                selected = bool(values) or candidate_at_merge(candidate, merged)
    if not merged:
        gap("policy_not_at_merge", evidence)
        return "unknown", "unknown", "not_merged"
    if required is None:
        gap("policy_incomplete", evidence)
        return "unknown", "unknown", "checks_policy_unreadable"
    basis = "current_policy" if required else "all_visible"
    if not required:
        verdicts = list(values.values())
        if not verdicts:
            return "unknown", basis, "no_checks"
    else:
        verdicts = []
        for requirement in required:
            name, app_id = requirement["context"], requirement["app_id"]
            matches = [value for (n, app, kind), value in values.items() if n == name
                and (app_id is None or (kind == "check" and app == app_id))]
            verdicts.append("red" if "red" in matches else "unknown" if not matches
                or "unknown" in matches else "green")
    if "red" in verdicts:
        return "red", basis, "checks_red"
    if "unknown" in verdicts:
        return "unknown", basis, "checks_missing_required"
    return "green", basis, ""


def candidate_at_merge(snapshot, merged):
    """A pre-merge attempt with a late completion cannot be replaced by another SHA."""
    return bool(merged) and any(c["started_at"] and utc(c["started_at"],
        "Outcomes check started_at") <= merged and c["head_sha"].lower() == snapshot[
            "head_sha"].lower() for c in snapshot["check_runs"])


def policy_values(snapshot):
    values, groups = {}, {}
    for run in snapshot["check_runs"]:
        groups.setdefault((run["name"], run.get("app", {}).get("id")), []).append(run)
    for (name, app_id), runs in groups.items():
        values[(name, app_id, "check")] = verdict(
            {**snapshot, "required": [name], "check_runs": runs, "statuses": []})
    for name in {s["context"] for s in snapshot["statuses"]}:
        values[(name, None, "status")] = verdict({**snapshot, "required": [name],
            "check_runs": [],
            "statuses": [s for s in snapshot["statuses"] if s["context"] == name]})
    return values
