"""Value-weighted spend from an operator price table; prices live in the file, never in code.

The table maps case-insensitive model-label globs to prices per million tokens
for new input, cache write, cache read and output (reasoning output is a subset
of output and is not priced twice). Each usage event is valued at the model in
force at its time; tokens of a kind with no price, or on a model the table does
not cover, are reported as unpriced instead of guessed.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
import math



PRICED = ("new_input", "cache_write", "cache_read", "output")


def read(path):
    with open(path, encoding="utf-8") as stream:
        try:
            data = json.load(stream)
        except RecursionError:
            raise ValueError("price table nesting is too deep") from None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict) or not models:
        raise ValueError("price table needs a models object")
    table = []
    for pattern, prices in models.items():
        if not isinstance(pattern, str) or not pattern or not isinstance(prices, dict):
            raise ValueError("price rows map a model pattern to prices")
        row = {}
        for kind, price in prices.items():
            if (kind not in PRICED or isinstance(price, bool)
                or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0):
                raise ValueError("prices are finite non-negative numbers per token kind")
            row[kind] = float(price)
        table.append((pattern.casefold(), row))
    currency = data.get("currency", "USD")
    as_of = data.get("as_of")
    return Pricer(table, currency if isinstance(currency, str) else "USD",
        as_of if isinstance(as_of, str) else None)


class Pricer:
    def __init__(self, table, currency="USD", as_of=None):
        self.table, self.currency, self.as_of = table, currency, as_of
        self._cache = {}

    def prices(self, model):
        if model not in self._cache:
            folded = model.casefold() if model else None
            self._cache[model] = next((row for pattern, row in self.table
                if folded is not None and fnmatchcase(folded, pattern)), None)
        return self._cache[model]

    def value(self, model, values):
        """(amount, unpriced tokens) for one usage event."""
        prices = self.prices(model)
        amount, unpriced = 0.0, 0
        for kind in PRICED:
            tokens = values.get(kind)
            if not tokens:
                continue
            if prices is None or kind not in prices:
                unpriced += tokens
            else:
                amount += tokens * prices[kind] / 1e6
        return amount, unpriced


def empty():
    return {"amount": 0.0, "unpriced_tokens": 0}


def add(target, amount, unpriced):
    target["amount"] += amount
    target["unpriced_tokens"] += unpriced


def finish(target):
    return {"amount": round(target["amount"], 6), "unpriced_tokens": target["unpriced_tokens"]}


def measure(found, rows, window, pricer, role_of, model_at, usage_events):
    """Per-session `value` on rows, plus summary, model, agent and role totals."""
    total, models, agents, roles = empty(), {}, {}, {}
    for session in found:
        row = rows.get(session.id())
        if row is None:
            continue
        own = empty()
        for when, _, values in usage_events(session, window):
            model = model_at(session, when)
            amount, unpriced = pricer.value(model, values)
            for target in (own, total, models.setdefault(model, empty()),
                agents.setdefault(row["agent"], empty()),
                roles.setdefault(role_of(session), empty())):
                add(target, amount, unpriced)
        row["value"] = finish(own)
    return {"measurement": "estimated", "currency": pricer.currency, "as_of": pricer.as_of,
        "basis": "price_table_per_usage_event_model_in_force",
        "summary": finish(total),
        "by_model": sorted(({"model": m, **finish(v)} for m, v in models.items()),
            key=lambda r: (-r["amount"], r["model"] or "")),
        "by_agent": {a: finish(v) for a, v in sorted(agents.items())},
        "by_role": {r: finish(v) for r, v in sorted(roles.items())}}


def text_lines(report):
    s = report["summary"]
    lines = [f"Value ({report['currency']}, estimated from price table"
        + (f" as of {report['as_of']}" if report["as_of"] else "") + f"): {s['amount']:.2f}; "
        f"unpriced tokens {s['unpriced_tokens']}"]
    lines.append("  By role: " + "; ".join(f"{r} {v['amount']:.2f}"
        for r, v in report["by_role"].items()))
    lines.append("  By model: " + "; ".join(f"{m['model'] or 'unknown'} {m['amount']:.2f}"
        + (f" (unpriced tokens {m['unpriced_tokens']})" if m["unpriced_tokens"] else "")
        for m in report["by_model"][:8]))
    return lines
