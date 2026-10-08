"""Delegation measurements from local session evidence, independent of outcomes."""

from collections import Counter
import math
from pathlib import PurePosixPath
from statistics import median

from sumbi.core.paths import execution_cwd
from sumbi.events.tool_paths import resolve_path


KINDS = ("new_input", "cache_write", "cache_read", "output", "total")
THRESHOLDS = (100_000, 200_000, 400_000)
SCRIPTS = {".sh", ".bash", ".py", ".ps1", ".js", ".mjs", ".cmd", ".bat"}


def identity(link):
    """Use the same project buckets as collect spend, with agent added."""
    return (link["bucket"], link["project_key"] if link["bucket"] == "project" else None,
        link["rule"])


def _total(values):
    return sum(values.get(kind) or 0 for kind in KINDS[:-1])


def _context(values):
    if values.get("new_input") is None:
        return None
    return sum(values.get(kind) or 0 for kind in KINDS[:3])


def _tokens(entries):
    result = {}
    for kind in KINDS:
        reported = [e["tokens"].get(kind) for e in entries if e["tokens"].get(kind) is not None]
        result[kind] = sum(reported) if reported else (0 if kind == "total" else None)
    return result


def _share(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator else None


def _p90(values):
    """Nearest-rank percentile, including small cohorts without interpolation."""
    return sorted(values)[math.ceil(len(values) * .9) - 1] if values else None


def _depth(session, index):
    if session.spawn_depth is not None:
        return str(session.spawn_depth)
    seen, depth = set(), 0
    while session.parent_raw_id:
        key = session.agent, session.raw_id
        if key in seen:
            return "unknown"
        seen.add(key)
        depth += 1
        session = index.get((session.agent, session.parent_raw_id))
        if session is None:
            return "unknown"
    return str(depth)


def _model(session, at):
    eligible = [(when, model) for when, model in session.model_history if when <= at]
    if not eligible:
        return "unknown"
    latest = max(when for when, _ in eligible)
    models = {model for when, model in eligible if when == latest}
    return next(iter(models)) if len(models) == 1 else "unknown"


def _request_records(session, window):
    return sorted((r for r in session.request_usage if window.contains(r[0])),
        key=lambda r: (r[0], r[1]))


def _estimate(session, parent, records):
    if parent is None or session.start_at is None or not records:
        return None
    prior = [r for r in parent.request_usage if r[0] <= session.start_at]
    child = sorted(session.request_usage, key=lambda r: (r[0], r[1]))
    if not prior or not child:
        return None
    parent_usage = [(at, order) for at, order, kind, _ in parent.attribution_events
        if kind == "usage" and at <= session.start_at]
    if parent_usage and max(parent_usage) not in {(r[0], r[1]) for r in prior}:
        return None
    child_usage = [(at, order) for at, order, kind, _ in session.attribution_events
        if kind == "usage"]
    if child_usage and min(child_usage) < (child[0][0], child[0][1]):
        return None
    latest = max(r[0] for r in prior)
    contexts = {_context(r[2]) for r in prior if r[0] == latest}
    if len(contexts) != 1:
        return None
    p, first = next(iter(contexts)), _context(child[0][2])
    values = [_context(r[2]) for r in records]
    if p is None or first is None or any(v is None for v in values) or not sum(values):
        return None
    return sum(p + value - first for value in values) / sum(values)


def _friction(entries, window, attributor):
    counts = Counter()
    for entry in entries:
        session = entry["session"]
        cwd = resolve_path(execution_cwd(session.start_cwd))
        checkout = attributor.repository_path(cwd) if cwd else None
        targets = [target for edit, at in session.edits.items() if window.contains(at)
            for target in session.edit_targets.get(edit, (None,))]
        states = []
        for target in targets:
            outside = (None if checkout is None or target is None else
                target != checkout and not target.startswith(checkout.rstrip("/") + "/"))
            states.append(outside)
            counts["edit_targets_unobserved"] += outside is None
            if outside:
                counts["edits_outside_checkout"] += 1
                counts["script_edits_outside_checkout"] += (
                    PurePosixPath(target).suffix.casefold() in SCRIPTS)
        counts["sessions_all_edits_outside_checkout"] += bool(states) and all(
            state is True for state in states)
        counts["tool_errors"] += session.counts["tool_errors"]
        counts["tool_results"] += session.counts["tool_results"]
    keys = ("sessions_all_edits_outside_checkout", "edits_outside_checkout",
        "script_edits_outside_checkout", "edit_targets_unobserved", "tool_errors", "tool_results")
    return {**{key: counts[key] for key in keys},
        "tool_failure_rate": _share(counts["tool_errors"], counts["tool_results"])}


def _contexts(entries, parents, window):
    averages, ratios, observed, skipped = [], [], [], 0
    above = dict.fromkeys(map(str, THRESHOLDS), 0)
    request_total = 0
    for entry in entries:
        session = entry["session"]
        records = [r for r in _request_records(session, window)
            if entry["keys"] is None or (r[0], r[1]) in entry["keys"]]
        contexts = [_context(r[2]) for r in records]
        complete = bool(records) and all(c is not None for c in contexts) and (
            entry["keys"] is None or entry["keys"] <= {(r[0], r[1]) for r in records})
        # Cumulative-only or partial usage cannot stand in for request observations.
        usage_keys = entry["usage_keys"]
        complete &= usage_keys <= {(r[0], r[1]) for r in records}
        if complete:
            averages.append(sum(contexts) / len(contexts))
            observed.append(session.id())
            for (_, _, values), context in zip(records, contexts):
                total = _total(values)
                request_total += total
                for threshold in THRESHOLDS:
                    if context > threshold:
                        above[str(threshold)] += total
        ratio = _estimate(session, parents.get((session.agent, session.parent_raw_id)), records
            ) if complete else None
        if ratio is None:
            skipped += 1
        else:
            ratios.append(ratio)
    return {"context": {"measurement": "observed", "observed_sessions": len(observed),
        "context_unobserved": len(entries) - len(observed),
        "session_average_median": median(averages) if averages else None,
        "session_average_p90": _p90(averages), "request_total_tokens": request_total,
        "sensitivity": {key: {"total_tokens": value,
            "share": _share(value, request_total)} for key, value in above.items()}},
        "estimate": {"measurement": "estimated", "observed_sessions": len(ratios),
            "context_unobserved": skipped, "median_inline_to_actual_context_ratio":
                median(ratios) if ratios else None,
            "sessions_ratio_below_one": sum(ratio < 1 for ratio in ratios),
            "share_ratio_below_one": _share(sum(ratio < 1 for ratio in ratios), len(ratios))}}


def _models(entries):
    rows, statuses = {}, {}
    for entry in entries:
        session = entry["session"]
        status = session.model_request_status
        status_row = statuses.setdefault(status, {"sessions": 0, "total_tokens": 0})
        status_row["sessions"] += 1
        status_row["total_tokens"] += entry["tokens"]["total"]
        model_tokens = Counter()
        for at, _, values, _, _, _ in entry["events"]:
            model_tokens[_model(session, at)] += _total(values)
        if not model_tokens:
            for model in session.models or {"unknown"}:
                model_tokens[model] = 0
        for model, total in model_tokens.items():
            row = rows.setdefault((model, status), {"model": model, "request": status,
                "sessions": 0, "total_tokens": 0})
            row["sessions"] += 1
            row["total_tokens"] += total
    return {"by_observed_model_and_request": [rows[key] for key in sorted(rows)],
        "by_request": {status: statuses.get(status, {"sessions": 0, "total_tokens": 0})
            for status in ("requested", "unrequested", "unknown")}}


def _group(children, selected, index, window, attributor):
    parent_keys = {(e["session"].agent, e["session"].parent_raw_id) for e in children}
    parent_entries = [selected[key] for key in sorted(parent_keys) if key in selected]
    child_keys = {(e["session"].agent, e["session"].raw_id) for e in children}
    union = children + [e for e in parent_entries if (e["session"].agent,
        e["session"].raw_id) not in child_keys]
    dispatched, parents, denominator = _tokens(children), _tokens(parent_entries), _tokens(union)
    depths = Counter(_depth(e["session"], index) for e in children)
    n = math.ceil(len(children) / 10)
    top = sum(sorted((e["tokens"]["total"] for e in children), reverse=True)[:n])
    return {"volume": {"dispatched_sessions": len(children),
        "dispatching_parent_sessions": len(parent_entries),
        "parent_unobserved": sum((e["session"].agent, e["session"].parent_raw_id)
            not in selected for e in children), "nesting_depth": dict(sorted(depths.items()))},
        "cost_share": {"measurement": "observed", "dispatched": dispatched, "parents": parents,
            "dispatched_share": {kind: _share(dispatched[kind], denominator[kind])
                for kind in KINDS}, "share_basis": "unique_sessions_union"},
        **_contexts(children, index, window),
        "concentration": {"top_decile_sessions": n, "top_decile_total_tokens": top,
            "top_decile_share": _share(top, dispatched["total"])},
        "model_request": _models(children),
        "workaround_friction": _friction(children, window, attributor)}


def _entry(session, row, events, group=None, *, member=True):
    chosen = [event for event in events if group is None or identity(event[3]) == group]
    tokens = row["tokens"] if group is None else _tokens(
        [{"tokens": a["tokens"]} for a in row["allocations"] if identity(a) == group])
    keys = {(at, order) for at, order, *_ in chosen}
    return {"session": session, "tokens": tokens, "events": chosen,
        "keys": None if group is None else keys, "usage_keys": keys, "member": member}


def measure(found, rows, window, attributor, idle_minutes, *, agents=()):
    """Build an overall view and matching agent/project allocation breakdowns."""
    index = {(s.agent, s.raw_id): s for s in found}
    selected, project_entries = {}, {}
    for session in found:
        row = rows.get(session.id())
        if row is None:
            continue
        events = list(session.event_allocations(window, attributor, idle_minutes))
        if row["allocations"]:
            allowed = {identity(a) for a in row["allocations"]}
            events = [event for event in events if identity(event[3]) in allowed]
        selected[session.agent, session.raw_id] = _entry(session, row, events)
        groups = {identity(a) for a in row["allocations"]} or {identity(row["project"])}
        for group in groups:
            project_entries.setdefault((*group, row["agent"]), {})[
                session.agent, session.raw_id] = _entry(session, row, events, group)
    # A parent remains observed when its billable events belong to another project.
    for (*group, agent), entries in project_entries.items():
        for entry in list(entries.values()):
            session = entry["session"]
            parent_key = session.agent, session.parent_raw_id
            if session.parent_raw_id and parent_key in selected and parent_key not in entries:
                parent = selected[parent_key]
                entries[parent_key] = _entry(parent["session"], rows[parent["session"].id()],
                    parent["events"], tuple(group), member=False)
    children = [entry for entry in selected.values() if entry["session"].parent_raw_id]
    agents = sorted(set(agents) | {s.public_agent() for s in found if s.id() in rows})
    return {"summary": _group(children, selected, index, window, attributor),
        "by_agent": {agent: _group([e for e in children if e["session"].public_agent() == agent],
            selected, index, window, attributor) for agent in agents},
        "by_project_and_agent": [{"bucket": bucket, "project_key": key, "rule": rule,
            "agent": agent, **_group([e for e in entries.values()
                if e["member"] and e["session"].parent_raw_id], entries, index, window, attributor)}
            for (bucket, key, rule, agent), entries in sorted(project_entries.items(),
                key=lambda item: tuple(value or "" for value in item[0]))]}


def _display(value):
    if isinstance(value, int):
        return str(value)
    return "unobserved" if value is None else format(value, ".6g")


def _group_text(name, group):
    volume, cost, context = (group[key] for key in ("volume", "cost_share", "context"))
    estimate, concentration = (group[key] for key in ("estimate", "concentration"))
    lines = [f"Delegation {name} (observed): dispatched {volume['dispatched_sessions']}; "
        f"dispatching parents {volume['dispatching_parent_sessions']}; "
        f"parent unobserved {volume['parent_unobserved']}",
        "  Nesting depth: " + ("; ".join(f"{k} {v}" for k, v in
            volume["nesting_depth"].items()) or "none")]
    for label in ("dispatched", "parents", "dispatched_share"):
        lines.append("  " + label + ": " + "; ".join(kind + " " + _display(value)
            for kind, value in cost[label].items()))
    lines.extend(["  Session average context: median "
        + _display(context['session_average_median']) + "; "
        f"p90 {_display(context['session_average_p90'])}; "
        f"observed {context['observed_sessions']}; unobserved {context['context_unobserved']}",
        f"  Context request total tokens: {context['request_total_tokens']}",
        "  Context sensitivity (strictly above): " + "; ".join(
            key + " total tokens " + str(value["total_tokens"]) + " share "
            + _display(value["share"]) for key, value in context["sensitivity"].items()),
        f"  Top decile: sessions {concentration['top_decile_sessions']}; "
        f"total tokens {concentration['top_decile_total_tokens']}; "
        f"share {_display(concentration['top_decile_share'])}"])
    for status, row in group["model_request"]["by_request"].items():
        lines.append(f"  Model request {status}: sessions {row['sessions']}; "
            f"total tokens {row['total_tokens']}")
    for row in group["model_request"]["by_observed_model_and_request"]:
        lines.append(f"  Model {row['model']} ({row['request']}): sessions {row['sessions']}; "
            f"total tokens {row['total_tokens']}")
    lines.extend(["  Inline/actual context (estimated): median "
        + _display(estimate["median_inline_to_actual_context_ratio"])
        + f"; observed {estimate['observed_sessions']}; unobserved {estimate['context_unobserved']}"
        + f"; sessions below one {estimate['sessions_ratio_below_one']}; share "
        + _display(estimate["share_ratio_below_one"]),
        "  Workaround friction (observed): " + "; ".join(key + " " + _display(value)
            for key, value in group["workaround_friction"].items())])
    return lines


def text_lines(report):
    lines = _group_text("all", report["summary"])
    for agent, group in report["by_agent"].items():
        lines.extend(_group_text(agent, group))
    for group in report["by_project_and_agent"]:
        name = " ".join(str(group[key]) for key in ("bucket", "rule", "project_key", "agent")
            if group[key] is not None)
        lines.extend(_group_text(name, group))
    lines.append("Delegation estimate measures context volume; it ignores model prices, "
        "output tokens and caching behaviour.")
    lines.append("Delegation shares use the unique union of children and direct parents; "
        "nested dispatchers enter that denominator once.")
    return lines
