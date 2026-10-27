"""Operator-supplied session labels and per-label spend, calls and start cost.

A label file is JSONL. Each line names a session by its native ID or an ID
prefix (at least eight characters), optionally an agent and a UTC `at` time from
which the line applies, and bounded label values. Claude Code subagent IDs
extend their parent's ID, so a parent prefix also labels its subagents. Usage
events and calls go to the latest line in force at their time; earlier events,
and sessions with no line, are unlabeled. Only label values, counts and token
sums are reported; IDs in the file never leave this module.
"""

from __future__ import annotations

import json
from statistics import median

from sumbi.core.values import TOKEN_KINDS, timestamp
from sumbi.measure.calls import START_CALLS, _records
from sumbi.measure.delegation import _context, _percentile
from sumbi.measure.pricing import add as add_value, empty, finish as finish_value


DIMENSIONS = ("lane", "account", "start_kind", "host")
MIN_PREFIX = 8
MAX_VALUE = 64


def _value(raw):
    if raw is None:
        return None
    if (not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_VALUE
        or any(ord(c) < 32 or ord(c) == 127 for c in raw)):
        raise ValueError
    return raw


def read(path):
    """Parse a label file; invalid lines are counted, never echoed."""
    entries, invalid, lines = [], 0, 0
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            lines += 1
            try:
                data = json.loads(line)
                session = data["session"]
                if not isinstance(session, str) or len(session) < MIN_PREFIX:
                    raise ValueError
                at = None
                if data.get("at") is not None:
                    at = timestamp(data["at"])
                    if at is None:
                        raise ValueError
                agent = _value(data.get("agent"))
                values = {d: _value(data.get(d)) for d in DIMENSIONS}
                if not any(values.values()):
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                invalid += 1
                continue
            entries.append({"session": session, "agent": agent, "at": at,
                "values": values, "order": len(entries)})
    return {"entries": entries, "lines": lines, "invalid": invalid}


class Labels:
    def __init__(self, parsed):
        self.parsed = parsed
        self.index = {}
        for entry in parsed["entries"]:
            self.index.setdefault(entry["session"][:MIN_PREFIX], []).append(entry)
        self.matched = set()

    def segments(self, session):
        """Lines for this session, by `at` (an untimed line applies from the start)."""
        found = [e for e in self.index.get(session.raw_id[:MIN_PREFIX], ())
            if session.raw_id.startswith(e["session"])
            and (e["agent"] is None or e["agent"] == session.agent)]
        self.matched.update(e["order"] for e in found)
        return sorted(found, key=lambda e: (e["at"] is not None, e["at"] or 0, e["order"]))

    def coverage(self):
        entries = self.parsed["entries"]
        return {"lines": self.parsed["lines"], "invalid_lines": self.parsed["invalid"],
            "entries": len(entries), "matched_entries": len(self.matched),
            "unmatched_entries": len(entries) - len(self.matched)}


def in_force(segments, when):
    """The latest line whose `at` is not after `when`; None before the first timed line."""
    chosen = None
    for segment in segments:
        if segment["at"] is None or segment["at"] <= when:
            chosen = segment
    return chosen


def model_at(session, when):
    """The latest observed model at or before `when`, else the session's only model."""
    chosen = None
    for at, model in sorted((e for e in session.model_history if e[0] is not None),
        key=lambda e: e[0]):
        if at <= when:
            chosen = model
    if chosen is None and len(session.models) == 1:
        chosen = next(iter(session.models))
    return chosen


def usage_events(session, window):
    """In-window billable usage records, the ones session token totals sum."""
    return sorted(((when, order, data[0]) for when, order, kind, data
        in session.attribution_events if kind == "usage" and window.contains(when)),
        key=lambda e: (e[0], e[1]))


def _starts(session, segments, window):
    """Start cost per segment: its first calls after its `at`, when they begin in-window."""
    records = _records(session)
    starts = []
    for index, segment in enumerate(segments or [None]):
        begin = segment["at"] if segment and segment["at"] is not None else None
        following = [r for r in records if begin is None or r[0] >= begin]
        if index + 1 < len(segments) and segments[index + 1]["at"] is not None:
            following = [r for r in following if r[0] < segments[index + 1]["at"]]
        if not following or not window.contains(following[0][0]):
            continue
        first = [_context(r[2]) for r in following[:START_CALLS] if window.contains(r[0])]
        starts.append((segment, first[0], None if None in first else sum(first)))
    return starts


def _row(value):
    return {"value": value, "sessions": set(), "tokens": dict.fromkeys(TOKEN_KINDS),
        "contexts": [], "starts": [], "amount": empty()}


def _finish(row, priced):
    known = [c for c in row["contexts"] if c is not None]
    first = [s for s, _ in row["starts"] if s is not None]
    five = [s for _, s in row["starts"] if s is not None]
    tokens = row["tokens"]
    total = sum(tokens[k] or 0 for k in TOKEN_KINDS if k != "reasoning_output")
    return {"value": row["value"], "sessions": len(row["sessions"]),
        "tokens": {**tokens, "total": total},
        "calls": {"count": len(row["contexts"]), "context_unobserved": len(row["contexts"])
            - len(known), "context_median": median(known) if known else None,
            "context_p90": _percentile(known, .9), "context_max": max(known, default=None),
            "starts": len(row["starts"]),
            "first_context_median": median(first) if first else None,
            "first5_context_sum_median": median(five) if five else None,
            "first5_context_sum_total": sum(five)},
        **({"spend_value": finish_value(row["amount"])} if priced else {})}


def measure(found, rows, window, labels, pricer=None):
    tables = {d: {} for d in DIMENSIONS}
    labeled = partly = 0
    for session in found:
        row = rows.get(session.id())
        if row is None:
            continue
        segments = labels.segments(session)
        row["labels"] = [{"from": s["at"].isoformat().replace("+00:00", "Z")
            if s["at"] else None, **s["values"]} for s in segments]
        events = usage_events(session, window)
        calls = [(r[0], _context(r[2])) for r in _records(session) if window.contains(r[0])]
        forced = [in_force(segments, when) for when, _, _ in events]
        prices = [pricer.value(model_at(session, when), values) if pricer else (0.0, 0)
            for when, _, values in events]
        if segments and all(forced):
            labeled += 1
        elif segments:
            partly += 1
        starts = _starts(session, segments, window)
        for dimension, table in tables.items():
            def bucket(segment):
                value = segment["values"][dimension] if segment else None
                return table.setdefault(value, _row(value))
            for (when, _, values), segment, price in zip(events, forced, prices):
                target = bucket(segment)
                add_value(target["amount"], *price)
                target["sessions"].add(session.id())
                for kind in TOKEN_KINDS:
                    if values.get(kind) is not None:
                        target["tokens"][kind] = (target["tokens"][kind] or 0) + values[kind]
            for when, context in calls:
                target = bucket(in_force(segments, when))
                target["sessions"].add(session.id())
                target["contexts"].append(context)
            for segment, first, five in starts:
                bucket(segment)["starts"].append((first, five))
    return {"measurement": "observed", "coverage": {**labels.coverage(),
        "sessions_labeled": labeled, "sessions_partly_labeled": partly},
        "by": {d: sorted((_finish(r, pricer is not None) for r in table.values()),
            key=lambda r: (-r["tokens"]["total"], r["value"] is None, r["value"] or ""))
            for d, table in tables.items()}}


def _show(value):
    if isinstance(value, int):
        return str(value)
    return "unobserved" if value is None else format(value, ".6g")


TEXT_ROWS = 12


def text_lines(report):
    cov = report["coverage"]
    lines = [f"Labels: lines {cov['lines']}; invalid {cov['invalid_lines']}; entries "
        f"{cov['entries']}; matched {cov['matched_entries']}; unmatched "
        f"{cov['unmatched_entries']}; sessions labeled {cov['sessions_labeled']}; partly "
        f"{cov['sessions_partly_labeled']}"]
    for dimension, rows in report["by"].items():
        for row in rows[:TEXT_ROWS]:
            calls = row["calls"]
            name = row["value"] if row["value"] is not None else "(none)"
            lines.append(f"  Label {dimension} {name}"
                f": sessions {row['sessions']}; tokens {row['tokens']['total']}; calls "
                f"{calls['count']}; context per call median/p90 "
                f"{_show(calls['context_median'])}/{_show(calls['context_p90'])}; starts "
                f"{calls['starts']} first {START_CALLS} sum median "
                f"{_show(calls['first5_context_sum_median'])}"
                + (f"; value {row['spend_value']['amount']:.2f}" if "spend_value" in row else ""))
        if len(rows) > TEXT_ROWS:
            lines.append(f"  Label {dimension}: {len(rows) - TEXT_ROWS} more values in JSON")
    return lines
