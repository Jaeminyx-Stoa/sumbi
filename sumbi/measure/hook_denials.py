"""Allow-listed hook friction summaries using collect's existing session grouping."""

from collections import Counter

from sumbi.events.hook_denials import CLASSES
from sumbi.measure.delegation import identity


def _group(entries):
    classes, reasons = Counter(), Counter()
    denials = timeouts = recovered = affected = first_call = calls = inner_denials = 0
    for row, events in entries:
        calls += row["counts"]["tool_calls"]
        denied = [event for event in events if not event[2]]
        affected += bool(denied)
        first_call += bool(denied) and denied[0][4]
        for category, reason_id, timeout, recovery, _, inner in events:
            if timeout:
                timeouts += 1
            else:
                denials += 1
                inner_denials += inner
                classes[category] += 1
                reasons[reason_id] += 1
                recovered += recovery
    return {"measurement": "observed", "sessions": len(entries), "tool_calls": calls,
        "sessions_with_denials": affected, "denials": denials, "timeouts": timeouts,
        "denials_per_100_tool_calls": denials * 100 / calls if calls else None,
        "timeouts_per_100_tool_calls": timeouts * 100 / calls if calls else None,
        "inner_denials_in_exec": inner_denials,
        "denials_by_class": {key: classes[key] for key in CLASSES},
        "top_reason_ids": [{"reason_id": key, "count": value} for key, value in
            sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:10]],
        "first_call_denials": {"sessions": first_call, "denied_sessions": affected,
            "share": first_call / affected if affected else None},
        "recovered": recovered, "unrecovered": denials - recovered}


def measure(found, rows, window, *, agents=(), persistent_ids=False):
    entries, projects = [], {}
    for session in found:
        row = rows.get(session.id())
        if row is None:
            continue
        entry = row, list(session.hook_calls.events(window))
        entries.append(entry)
        groups = {identity(a) for a in row["allocations"]} or {identity(row["project"])}
        for group in groups:
            projects.setdefault((*group, row["agent"]), []).append(entry)
    return {"reason_ids": {"algorithm": "hmac-sha256", "key": "supplied"
            if persistent_ids else "ephemeral", "normalization": "first-clause-v1"},
        "summary": _group(entries),
        "by_agent": {agent: _group([e for e in entries if e[0]["agent"] == agent])
            for agent in sorted(set(agents) | {e[0]["agent"] for e in entries})},
        "by_project_and_agent": [{"bucket": bucket, "project_key": key, "rule": rule,
            "agent": agent, **_group(group)} for (bucket, key, rule, agent), group in
            sorted(projects.items(), key=lambda item: tuple(v or "" for v in item[0]))]}


def _display(value):
    return "unobserved" if value is None else format(value, ".6g")


def _text(name, group):
    first = group["first_call_denials"]
    return [f"Hook denials {name} (observed): sessions {group['sessions_with_denials']}; "
        f"denials {group['denials']}; timeouts {group['timeouts']}",
        "  Per 100 tool calls: denials " + _display(group['denials_per_100_tool_calls'])
        + "; timeouts " + _display(group['timeouts_per_100_tool_calls'])
        + "; inner_denials_in_exec " + str(group['inner_denials_in_exec']),
        "  Denials by class: " + "; ".join(f"{k} {v}"
            for k, v in group["denials_by_class"].items()),
        "  Top reason IDs: " + ("; ".join(f"{r['reason_id']} {r['count']}"
            for r in group["top_reason_ids"]) or "none"),
        f"  First-call denials: sessions {first['sessions']}; denied sessions "
        + str(first['denied_sessions']) + "; share " + _display(first['share']),
        f"  Recovery: recovered {group['recovered']}; unrecovered {group['unrecovered']}"]


def text_lines(report):
    lines = _text("all", report["summary"])
    for agent, group in report["by_agent"].items():
        lines.extend(_text(agent, group))
    for group in report["by_project_and_agent"]:
        name = " ".join(str(group[k]) for k in ("bucket", "rule", "project_key", "agent")
            if group[k] is not None)
        lines.extend(_text(name, group))
    lines.append("Hook reason IDs: " + report["reason_ids"]["key"] + " key (HMAC-SHA256).")
    lines.append("Hook classes are a heuristic; other hook events are invisible. "
        "Reason text stays local and is never emitted.")
    return lines
