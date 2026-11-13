"""Request-level building blocks for live measurements.

This module does not yet provide the snapshot or source collection API.
"""

from datetime import timedelta

from sumbi.core.values import mapping, timestamp
from sumbi.events.adapters import claude_code, codex
from sumbi.events.schema import RequestUsage, TokenUsage


def summarize_requests(engine, records, now):
    """Summarize native records in two adjacent half-open UTC windows.

    The caller supplies records from one session, including its beginning. Invalid
    usage makes coverage incomplete; it is never silently counted as zero.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must have a timezone")
    if engine not in ("claude", "codex"):
        raise ValueError("unsupported engine")
    observations = []
    invalid = future = duplicates = 0
    for order, record in enumerate(records):
        if not isinstance(record, dict):
            invalid += 1
            continue
        at = timestamp(record.get("timestamp"))
        if engine == "claude":
            if record.get("type") != "assistant" or not mapping(mapping(record.get("message")).get("usage")):
                continue
            if at is None:
                invalid += 1
                continue
            translated = claude_code._translate(record, at, None, False, None)
            usage = next((e for e in translated if isinstance(e, TokenUsage)), None)
            if usage is None or usage.message_id is None:
                invalid += 1
                continue
            tokens = usage.tokens
            values = (tokens.new_input, tokens.cache_write, tokens.cache_read)
            if any(type(x) is not int or x < 0 for x in values):
                invalid += 1
                continue
            identity = usage.message_id
            rank = (tokens.output, at, order)
            observations.append((at, order, identity, rank, sum(values)))
        else:
            payload = mapping(record.get("payload"))
            if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            if at is None:
                invalid += 1
                continue
            info = mapping(payload.get("info"))
            usage = next((e for e in codex._request_usage(payload, mapping(info.get("total_token_usage")), at)
                          if isinstance(e, RequestUsage)), None)
            if usage is None or usage.cumulative is None or any(x is None for x in usage.cumulative):
                invalid += 1
                continue
            tokens = usage.tokens
            observations.append((at, order, usage.cumulative, None, tokens.new_input + tokens.cache_read))
    observations.sort(key=lambda x: (x[0], x[1]))
    future = sum(at >= now for at, *_ in observations)
    observations = [item for item in observations if item[0] < now]
    if engine == "claude":
        unique = {}
        for item in observations:
            if item[2] in unique:
                duplicates += 1
            if item[2] not in unique or item[3] > unique[item[2]][3]:
                unique[item[2]] = item
        observations = sorted(unique.values(), key=lambda x: (x[0], x[1]))
    else:
        unique = []
        previous = None
        for item in observations:
            if item[2] == previous:
                duplicates += 1
                continue
            unique.append(item)
            previous = item[2]
        observations = unique
    observed = observations
    recent = [x for x in observed if x[0] >= now - timedelta(hours=1)]
    previous = [x for x in observed if now - timedelta(hours=2) <= x[0] < now - timedelta(hours=1)]
    return {
        "context_tokens": observed[-1][4] if observed else None,
        "baseline_tokens": observed[0][4] if observed else None,
        "calls_1h": len(recent) if not invalid else None,
        "input_tokens_1h": sum(x[4] for x in recent) if not invalid else None,
        "avg_context_1h": (sum(x[4] for x in recent) / len(recent) if recent else None) if not invalid else None,
        "previous_calls_1h": len(previous) if not invalid else None,
        "invalid": invalid, "future": future, "duplicates": duplicates,
    }
