"""Per-role usage profile: the cost questions a before/after review asks of collect.

Rows reuse collect's session rows, project keys and delegation's nearest-rank
percentile. Tool names classify paired calls into fixed classes and never leave
this module; only class labels, counts and seconds are reported.
"""

from collections import Counter, defaultdict
import math
from statistics import median

from sumbi.core.values import TOKEN_KINDS
from sumbi.measure.calls import START_CALLS, contexts as call_contexts, measure as calls, summarize
from sumbi.measure.delegation import _percentile


ROLES = ("orchestrator", "worker")
KINDS = (*TOKEN_KINDS, "total")
COUNTS = ("tool_calls", "tool_results", "tool_errors", "compactions")
LONG_SESSION_MINUTES = 60.0
TOOL_CLASSES = ("read", "edit", "search", "shell", "agent", "web", "mcp", "other", "unnamed")
# Built-in tool names (case-folded, namespace removed) -> class. MCP tools use the
# "mcp__" prefix; any other named tool is "other"; calls without a name "unnamed".
TOOL_CLASS_TABLE = {
    "read": "read", "read_file": "read", "view_image": "read", "ls": "read",
    "list_dir": "read", "notebookread": "read",
    "write": "edit", "edit": "edit", "multiedit": "edit", "notebookedit": "edit",
    "write_file": "edit", "apply_patch": "edit", "filechange": "edit",
    "glob": "search", "grep": "search", "grep_files": "search", "toolsearch": "search",
    "bash": "shell", "bashoutput": "shell", "killshell": "shell", "killbash": "shell",
    "powershell": "shell", "shell": "shell", "exec": "shell", "exec_command": "shell",
    "commandexecution": "shell", "local_shell_call": "shell", "write_stdin": "shell",
    "task": "agent", "agent": "agent", "sendmessage": "agent", "taskoutput": "agent",
    "taskstop": "agent", "spawn_agent": "agent", "send_input": "agent",
    "close_agent": "agent", "resume_agent": "agent", "wait_agent": "agent",
    "webfetch": "web", "websearch": "web", "web_fetch": "web", "web_search": "web",
    "web_search_call": "web",
}
CALL_LEAVES = ("count", "sessions_with_calls", "per_session_median", "context_unobserved",
    "context_median", "context_p90", "context_max", "start_sessions", "first_context_median",
    "first5_context_sum_median", "first5_context_sum_total")
LATENCY_LEAVES = ("paired_calls", "median_seconds", "p90_seconds")
FAN_OUT_LEAVES = ("parents", "children", "children_max", "children_median")
# Numeric leaves an improve evidence pointer may name below roles/<role>/.
ROLE_LEAVES = ("sessions", "sessions_per_day", *(f"tokens/{k}" for k in KINDS),
    *(f"tokens_per_day/{k}" for k in KINDS), "session_total_tokens_median",
    *(f"counts/{k}" for k in COUNTS), "tool_calls_per_day", "tool_error_rate",
    "compactions_per_day", "cache_read_per_tool_call", "active_seconds",
    "active_seconds_per_day", "tool_seconds_per_paired_call",
    "compaction/sessions", "compaction/total_tokens", "compaction/token_share",
    "long_tail/sessions", "long_tail/total_tokens", "long_tail/token_share",
    "concentration/top_decile_sessions", "concentration/top_decile_total_tokens",
    "concentration/top_decile_share",
    *(f"tool_latency/{c}/{leaf}" for c in TOOL_CLASSES for leaf in LATENCY_LEAVES),
    *(f"calls/{leaf}" for leaf in CALL_LEAVES))


def tool_class(name) -> str:
    if not isinstance(name, str) or not name:
        return "unnamed"
    folded = name.casefold()
    if folded.startswith("mcp__"):
        return "mcp"
    return TOOL_CLASS_TABLE.get(folded.rsplit(".", 1)[-1], "other")


def role(session) -> str:
    """Open events keep their explicit role; native sessions use dispatch evidence."""
    if session.agent == "sumbi-events":
        return "worker" if session.is_worker else "orchestrator"
    return "worker" if session.parent_raw_id or session.is_worker else "orchestrator"


def project_ref(link) -> str:
    """Pointer-safe project reference: the pseudonymous key, else the bucket name."""
    return link["project_key"] if link["bucket"] == "project" and link["project_key"] else (
        link["bucket"])


def _latency(session, window):
    """Durations of name-paired calls started in the window (hook-call pairing)."""
    values = defaultdict(list)
    for at, _, name, result, _ in session.hook_calls.observations:
        if result is not None and window.contains(at):
            values[tool_class(name)].append((result[0] - at).total_seconds())
    return values


def _tokens(entries):
    result = {}
    for kind in KINDS:
        reported = [e["tokens"][kind] for e in entries if e["tokens"].get(kind) is not None]
        result[kind] = sum(reported) if reported else (0 if kind == "total" else None)
    return result


def _share(numerator, denominator):
    return numerator / denominator if denominator else None


def _per_day(value, days):
    return None if value is None else value / days


def _role(entries, days, long_minutes):
    tokens = _tokens(entries)
    total = tokens["total"]
    totals = [e["tokens"]["total"] for e in entries]
    counts = {k: sum(e["row"]["counts"][k] for e in entries) for k in COUNTS}
    active = sum(e["row"]["time"]["active"]["seconds"] for e in entries)
    tool = [e["row"]["time"]["durations"]["tool"] for e in entries]
    pairs = sum(d["paired_intervals"] for d in tool)
    long = [e["tokens"]["total"] for e in entries
        if e["row"]["time"]["active"]["seconds"] > long_minutes * 60]
    compacted = [e["tokens"]["total"] for e in entries if e["row"]["counts"]["compactions"]]
    top_n = math.ceil(len(entries) / 10)
    top = sum(sorted(totals, reverse=True)[:top_n])
    models = {}
    for e in entries:
        row = models.setdefault(tuple(e["row"]["models"]), [0, 0])
        row[0] += 1
        row[1] += e["tokens"]["total"]
    latency = defaultdict(list)
    for e in entries:
        for name, values in e["latency"].items():
            latency[name].extend(values)
    return {"sessions": len(entries), "sessions_per_day": len(entries) / days,
        "tokens": tokens, "tokens_per_day": {k: _per_day(v, days) for k, v in tokens.items()},
        "session_total_tokens_median": median(totals) if totals else None,
        "counts": counts, "tool_calls_per_day": counts["tool_calls"] / days,
        "tool_error_rate": _share(counts["tool_errors"], counts["tool_results"]),
        "compactions_per_day": counts["compactions"] / days,
        "cache_read_per_tool_call": _share(tokens["cache_read"], counts["tool_calls"])
            if tokens["cache_read"] is not None else None,
        "active_seconds": active, "active_seconds_per_day": active / days,
        "tool_seconds_per_paired_call": _share(sum(d["seconds"] for d in tool), pairs),
        "compaction": {"sessions": len(compacted), "total_tokens": sum(compacted),
            "token_share": _share(sum(compacted), total)},
        "long_tail": {"sessions": len(long), "total_tokens": sum(long),
            "token_share": _share(sum(long), total)},
        "concentration": {"top_decile_sessions": top_n, "top_decile_total_tokens": top,
            "top_decile_share": _share(top, total)},
        "models": [{"models": list(label), "sessions": n, "total_tokens": spent,
            "token_share": _share(spent, total)} for label, (n, spent) in
            sorted(models.items(), key=lambda item: (-item[1][1], item[0]))],
        "tool_latency": {name: {"paired_calls": len(latency[name]),
            "median_seconds": median(latency[name]) if latency[name] else None,
            "p90_seconds": _percentile(latency[name], .9)} for name in TOOL_CLASSES},
        "calls": summarize(entries)}


def _fan_out(entries):
    children = Counter(e["row"]["parent_id"] for e in entries if e["row"]["parent_id"])
    sizes = sorted(children.values())
    return {"parents": len(sizes), "children": sum(sizes),
        "children_max": sizes[-1] if sizes else None,
        "children_median": median(sizes) if sizes else None}


def _group(entries, days, long_minutes):
    return {"roles": {name: _role([e for e in entries if e["role"] == name], days,
        long_minutes) for name in ROLES}, "fan_out": _fan_out(entries)}


def measure(found, rows, window, *, long_session_minutes=LONG_SESSION_MINUTES, agents=()):
    days = (window.until - window.since).total_seconds() / 86400
    entries, projects = [], {}
    for session in found:
        row = rows.get(session.id())
        if row is None:
            continue
        base = {"row": row, "role": role(session), "latency": _latency(session, window),
            "tokens": row["tokens"], "calls": calls(session, window),
            "contexts": call_contexts(session, window)}
        entries.append(base)
        groups = {}
        for allocation in row["allocations"]:
            groups.setdefault(project_ref(allocation), []).append(allocation)
        if not groups:
            groups[project_ref(row["project"])] = []
        for ref, allocations in groups.items():
            project = projects.setdefault(ref, {"bucket": (allocations[0] if allocations
                else row["project"])["bucket"], "rules": set(), "entries": []})
            # Without usage events the session project candidate supplies the rule label.
            project["rules"].update(a["rule"] for a in (allocations or [row["project"]])
                if a.get("rule"))
            project["entries"].append({**base, "tokens": _tokens(
                [{"tokens": a["tokens"]} for a in allocations]) if allocations else row["tokens"]})
    agents = sorted(set(agents) | {e["row"]["agent"] for e in entries})

    def grouped(selected):
        return {**_group(selected, days, long_session_minutes), "agents": {
            agent: _group([e for e in selected if e["row"]["agent"] == agent], days,
                long_session_minutes)
            for agent in sorted({e["row"]["agent"] for e in selected})}}

    return {"measurement": "observed", "window_days": days,
        "long_session_minutes": long_session_minutes,
        "role_rule": "parent_or_worker_header; sumbi-events explicit role",
        "summary": _group(entries, days, long_session_minutes),
        "by_agent": {agent: _group([e for e in entries if e["row"]["agent"] == agent], days,
            long_session_minutes) for agent in agents},
        "projects": {ref: {"bucket": project["bucket"], "rules": sorted(project["rules"]),
            **grouped(project["entries"])} for ref, project in sorted(projects.items())}}


def _show(value):
    if isinstance(value, int):
        return str(value)
    return "unobserved" if value is None else format(value, ".6g")


def _role_line(name, row):
    return (f"Profile {name}: sessions {row['sessions']} ({_show(row['sessions_per_day'])}/day); "
        f"tokens {row['tokens']['total']} ({_show(row['tokens_per_day']['total'])}/day); "
        f"tool calls {row['counts']['tool_calls']}; error rate "
        f"{_show(row['tool_error_rate'])}; compactions {row['counts']['compactions']}; "
        f"active seconds/day {_show(row['active_seconds_per_day'])}; long sessions "
        f"{row['long_tail']['sessions']} (token share "
        f"{_show(row['long_tail']['token_share'])}); top decile share "
        f"{_show(row['concentration']['top_decile_share'])}")


def _latency_line(name, row):
    parts = [f"{c} {_show(v['median_seconds'])}/{_show(v['p90_seconds'])} n {v['paired_calls']}"
        for c, v in row["tool_latency"].items() if v["paired_calls"]]
    return f"  Tool latency {name} (median/p90 seconds): " + ("; ".join(parts) or "none")


def _calls_line(name, row):
    calls = row["calls"]
    return (f"  Calls {name}: {calls['count']} (per session median "
        f"{_show(calls['per_session_median'])}); context per call median/p90/max "
        f"{_show(calls['context_median'])}/{_show(calls['context_p90'])}/"
        f"{_show(calls['context_max'])}; starts {calls['start_sessions']}: first call median "
        f"{_show(calls['first_context_median'])}, first {START_CALLS} calls sum median "
        f"{_show(calls['first5_context_sum_median'])}")


def _fan_line(name, fan):
    return (f"Profile {name} fan-out: parents {fan['parents']}; children {fan['children']}; "
        f"per parent max {_show(fan['children_max'])}; median {_show(fan['children_median'])}")


MODEL_LINE_ROWS = 5


def _models_line(models):
    """Largest rows by name; the rest fold into an explicit count and token share."""
    parts = [("+".join(m["models"]) or "none") + f" sessions {m['sessions']} token share "
        + _show(m["token_share"]) for m in models[:MODEL_LINE_ROWS]]
    rest = models[MODEL_LINE_ROWS:]
    if rest:
        shares = [m["token_share"] for m in rest]
        parts.append(f"{len(rest)} more model sets sessions {sum(m['sessions'] for m in rest)} "
            "token share " + _show(None if None in shares else sum(shares)))
    return "  Models: " + ("; ".join(parts) or "none")


def _detail(name, group):
    lines = []
    for role_name, row in group["roles"].items():
        if row["sessions"]:
            lines.append(_role_line(f"{name} {role_name}", row))
            lines.append(_models_line(row["models"]))
            lines.append(_calls_line(role_name, row))
            lines.append(_latency_line(role_name, row))
    lines.append(_fan_line(name, group["fan_out"]))
    return lines


def text_lines(report):
    lines = [f"Profile window {_show(report['window_days'])} days; long sessions over "
        f"{_show(report['long_session_minutes'])} active minutes; roles by parent or worker "
        "header (open events: explicit role)"]
    lines.extend(_detail("all", report["summary"]))
    for agent, group in report["by_agent"].items():
        lines.extend(_detail(agent, group))
    for ref, project in report["projects"].items():
        name = " ".join([project["bucket"], *project["rules"], *(
            [ref] if ref != project["bucket"] else [])])
        for role_name, row in project["roles"].items():
            if row["sessions"]:
                lines.append(_role_line(f"{name} {role_name}", row))
        if project["fan_out"]["parents"]:
            lines.append(_fan_line(name, project["fan_out"]))
    lines.append("Project profile tokens are that project's allocated tokens; counts, time "
        "and tool latency are whole-session values and repeat across projects.")
    return lines


def tail_lines(report, profile):
    """A short closing recap so `tail` shows the headline numbers."""
    summary = report["summary"]
    lines = ["Key summary: window " + report["window"]["since"] + " to "
        + report["window"]["until"] + f" ({_show(profile['window_days'])} days); sessions "
        f"{summary['sessions']}; reported tokens {summary['tokens']['total']}"]
    for role_name, row in profile["summary"]["roles"].items():
        lines.append(f"  {role_name}: sessions {row['sessions']}; tokens/day "
            f"{_show(row['tokens_per_day']['total'])}; long sessions "
            f"{row['long_tail']['sessions']}/{row['sessions']} (token share "
            f"{_show(row['long_tail']['token_share'])}); top decile share "
            f"{_show(row['concentration']['top_decile_share'])}")
    fan = profile["summary"]["fan_out"]
    lines.append(f"  Fan-out: parents {fan['parents']}; children per parent max "
        f"{_show(fan['children_max'])}; median {_show(fan['children_median'])}")
    for role_name, row in profile["summary"]["roles"].items():
        lines.append(_latency_line(role_name, row))
    for role_name, row in profile["summary"]["roles"].items():
        lines.append(_calls_line(role_name, row))
    return lines
