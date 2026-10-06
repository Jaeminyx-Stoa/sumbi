"""Descriptive costs and Wilson score intervals shared across reports."""

import math


def wilson(successes, total):
    if type(total) is not int or type(successes) is not int or not 0 <= successes <= total:
        raise ValueError("Wilson counts must be integers with 0 <= successes <= total")
    if total == 0:
        return {"numerator": 0, "denominator": 0, "rate": None, "wilson_95": None}
    z = 1.959963984540054
    p, z2 = successes / total, z * z
    center = (p + z2 / (2 * total)) / (1 + z2 / total)
    radius = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    return {"numerator": successes, "denominator": total, "rate": p,
            "wilson_95": [max(0.0, center - radius), min(1.0, center + radius)]}


def estimate(rows, metric):
    successes = sum(row["state"] == "success" for row in rows)
    values = [row["elapsed_seconds"] if metric == "time" else row["tokens"][metric] for row in rows]
    numerator = sum(values) if values and all(v is not None for v in values) else None
    return {"numerator": numerator, "denominator": successes,
            "value": numerator / successes if numerator is not None and successes else None,
            "interval_95": None, "interval_method": "percentile_bootstrap"}
