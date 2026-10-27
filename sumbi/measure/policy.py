"""Forbidden-model check: spend on model labels an operator pattern rules out.

Each in-window usage event takes the model label in force at its time (the
latest observed model at or before it, else the session's only model). Patterns
are case-insensitive shell globs over model labels.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from sumbi.core.values import TOKEN_KINDS
from sumbi.measure.labels import DIMENSIONS, in_force, model_at, usage_events
from sumbi.measure.pricing import add as add_value, empty as empty_value, finish as finish_value


def _add(row, session_id, values, value=None):
    row["sessions"].add(session_id)
    if value is not None:
        add_value(row.setdefault("spend_value", empty_value()), *value)
    for kind in TOKEN_KINDS:
        if values.get(kind) is not None:
            row["tokens"][kind] = (row["tokens"][kind] or 0) + values[kind]


def _empty():
    return {"sessions": set(), "tokens": dict.fromkeys(TOKEN_KINDS)}


def _out(row, **extra):
    total = sum(row["tokens"][k] or 0 for k in TOKEN_KINDS if k != "reasoning_output")
    result = {**extra, "sessions": len(row["sessions"]), "tokens": {**row["tokens"],
        "total": total}}
    if "spend_value" in row:
        result["spend_value"] = finish_value(row["spend_value"])
    return result


def measure(found, rows, window, patterns, labels=None, pricer=None):
    results = []
    for pattern in patterns:
        folded = pattern.casefold()
        whole, models, by = _empty(), {}, {d: {} for d in DIMENSIONS}
        unobserved = 0
        for session in found:
            if session.id() not in rows:
                continue
            segments = labels.segments(session) if labels is not None else []
            for when, _, values in usage_events(session, window):
                model = model_at(session, when)
                if model is None:
                    unobserved += 1
                    continue
                if not fnmatchcase(model.casefold(), folded):
                    continue
                value = pricer.value(model, values) if pricer is not None else None
                _add(whole, session.id(), values, value)
                _add(models.setdefault(model, _empty()), session.id(), values, value)
                segment = in_force(segments, when)
                for dimension in DIMENSIONS:
                    label = segment["values"][dimension] if segment else None
                    _add(by[dimension].setdefault(label, _empty()), session.id(), values, value)
        result = _out(whole, pattern=pattern, model_unobserved_events=unobserved)
        result["models"] = sorted((_out(r, model=m) for m, r in models.items()),
            key=lambda r: -r["tokens"]["total"])
        if labels is not None:
            result["by_label"] = {d: sorted((_out(r, value=v) for v, r in table.items()),
                key=lambda r: -r["tokens"]["total"]) for d, table in by.items()}
        results.append(result)
    return {"measurement": "observed", "rule": "model_in_force_at_usage_event; "
        "case_insensitive_glob", "forbidden_models": results}


def text_lines(report):
    lines = []
    for row in report["forbidden_models"]:
        lines.append(f"Forbidden model {row['pattern']}: sessions {row['sessions']}; tokens "
            f"{row['tokens']['total']}" + (f"; value {row['spend_value']['amount']:.2f}"
                if "spend_value" in row else "") + "".join(f"; {m['model']} {m['tokens']['total']}"
                for m in row["models"]))
        for dimension, values in row.get("by_label", {}).items():
            if values:
                lines.append(f"  by {dimension}: " + "; ".join(
                    f"{v['value'] if v['value'] is not None else '(none)'} sessions "
                    f"{v['sessions']} tokens {v['tokens']['total']}" for v in values[:8]))
    return lines
