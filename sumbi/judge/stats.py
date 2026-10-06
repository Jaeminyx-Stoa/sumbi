"""Independent-arm score intervals and deliverable percentile bootstraps."""

import math
import random
from statistics import NormalDist

from sumbi.outcomes.github.deliver import wilson


def newcombe(before_successes, before_n, after_successes, after_n):
    """Newcombe (1998), method 10, after minus before; no correction."""
    before, after = wilson(before_successes, before_n), wilson(after_successes, after_n)
    result = {"before": before, "after": after, "difference": None,
              "interval_95": None, "interval_method": "newcombe_method_10"}
    if not before_n or not after_n:
        return result
    p, q = after["rate"], before["rate"]
    pl, pu = after["wilson_95"]
    ql, qu = before["wilson_95"]
    result.update(difference=p - q,
                  interval_95=[p - q - math.hypot(p - pl, qu - q),
                               p - q + math.hypot(pu - p, q - ql)])
    return result


def sample_size(baseline, margin_pp):
    """Equal-arm unpooled normal approximation, true difference zero."""
    if not math.isfinite(margin_pp) or not 0 < margin_pp < 100:
        raise ValueError("Margin must be between zero and 100 percentage points")
    if baseline is None:
        return None
    if not math.isfinite(baseline) or not 0 <= baseline <= 1:
        raise ValueError("Baseline must be a probability")
    z = NormalDist().inv_cdf(0.975) + NormalDist().inv_cdf(0.8)
    return math.ceil(2 * baseline * (1 - baseline) * z ** 2 / (margin_pp / 100) ** 2)


def percentile(values, probability):
    """Linear interpolation at (n-1)*p, including degenerate distributions."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def estimate(rows, metric):
    successes = sum(row["state"] == "success" for row in rows)
    values = [row["elapsed_seconds"] if metric == "time" else row["tokens"][metric] for row in rows]
    numerator = sum(values) if values and all(v is not None for v in values) else None
    return {"numerator": numerator, "denominator": successes,
            "value": numerator / successes if numerator is not None and successes else None,
            "interval_95": None, "interval_method": "percentile_bootstrap"}


def classify_ratio(value, interval):
    if value is None or interval is None:
        return "uncertain"
    if value <= 0.90 and interval[1] < 1.00:
        return "improved"
    if interval[0] > 1.00:
        return "worse"
    return "uncertain"


def bootstrap(before, after, metrics, *, seed=1729, resamples=5000):
    """Resample whole deliverables independently, reusing draws across metrics.

    Undefined draws are counted, never silently conditioned away. Any undefined
    draw leaves that interval unreported rather than inventing finite bounds.
    """
    if type(seed) is not int or type(resamples) is not int or resamples < 100:
        raise ValueError("Bootstrap requires an integer seed and at least 100 resamples")
    arms = {"before": before, "after": after}
    estimates = {arm: {metric: estimate(rows, metric) for metric in metrics} for arm, rows in arms.items()}
    samples = {arm: {metric: [] for metric in metrics} for arm in arms}
    ratios = {metric: [] for metric in metrics}
    rng = random.Random(seed)
    for _ in range(resamples):
        draw = {arm: [rows[rng.randrange(len(rows))] for _ in rows] for arm, rows in arms.items()}
        results = {arm: {metric: estimate(rows, metric)["value"] for metric in metrics} for arm, rows in draw.items()}
        for metric in metrics:
            for arm in arms:
                if results[arm][metric] is not None:
                    samples[arm][metric].append(results[arm][metric])
            b, a = results["before"][metric], results["after"][metric]
            if b is not None and b > 0 and a is not None:
                ratios[metric].append(a / b)
    def bounds(values):
        return [percentile(values, 0.025), percentile(values, 0.975)] if len(values) == resamples else None
    comparison = {}
    for metric in metrics:
        for arm in arms:
            row = estimates[arm][metric]
            row["interval_95"] = bounds(samples[arm][metric])
            row["defined_resamples"] = len(samples[arm][metric])
        b, a = estimates["before"][metric]["value"], estimates["after"][metric]["value"]
        value = a / b if b is not None and b > 0 and a is not None else None
        interval = bounds(ratios[metric])
        comparison[metric] = {"numerator": a, "denominator": b, "value": value,
                              "interval_95": interval, "interval_method": "percentile_bootstrap",
                              "defined_resamples": len(ratios[metric]),
                              "classification": classify_ratio(value, interval)}
    return estimates, comparison
