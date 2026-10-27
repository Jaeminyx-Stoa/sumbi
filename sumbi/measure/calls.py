"""Model calls per session: how many, how large each call's context was, and start cost.

A call is one native request record (a Claude message snapshot, a Codex turn's
last usage, an open-event request). Its context is new input plus cache write
plus cache read, the delegation rule; a call without new input is unobserved.
"""

from statistics import median

from sumbi.measure.delegation import _context, _percentile


START_CALLS = 5


def _records(session):
    return sorted(session.request_usage, key=lambda r: (r[0], r[1]))


def contexts(session, window):
    """In-window call contexts in time order; None where a call's context is unobserved."""
    return [_context(r[2]) for r in _records(session) if window.contains(r[0])]


def _start(session, window):
    """The session's own first calls, only when its first call falls in the window."""
    records = _records(session)
    if not records or not window.contains(records[0][0]):
        return {"observed": False, "first_context": None, "first5_context_sum": None}
    first = [_context(r[2]) for r in records[:START_CALLS] if window.contains(r[0])]
    return {"observed": True, "first_context": first[0],
        "first5_context_sum": None if None in first else sum(first)}


def measure(session, window):
    values = contexts(session, window)
    known = [v for v in values if v is not None]
    return {"measurement": "observed", "count": len(values),
        "context_unobserved": len(values) - len(known),
        "context": {"total": sum(known), "median": median(known) if known else None,
            "p90": _percentile(known, .9), "max": max(known, default=None)},
        "start": _start(session, window)}


def summarize(entries):
    """Pool per-call contexts across sessions; start costs are per-session medians."""
    pooled = [v for e in entries for v in e["contexts"] if v is not None]
    counts = [len(e["contexts"]) for e in entries if e["contexts"]]
    starts = [e["calls"]["start"] for e in entries]
    observed = [s for s in starts if s["observed"]]
    first = [s["first_context"] for s in observed if s["first_context"] is not None]
    five = [s["first5_context_sum"] for s in observed if s["first5_context_sum"] is not None]
    return {"count": sum(counts), "sessions_with_calls": len(counts),
        "per_session_median": median(counts) if counts else None,
        "context_unobserved": sum(len(e["contexts"]) for e in entries) - len(pooled),
        "context_median": median(pooled) if pooled else None,
        "context_p90": _percentile(pooled, .9), "context_max": max(pooled, default=None),
        "start_sessions": len(observed),
        "first_context_median": median(first) if first else None,
        "first5_context_sum_median": median(five) if five else None,
        "first5_context_sum_total": sum(five)}
