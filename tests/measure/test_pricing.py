"""Synthetic price table: per-event model, unpriced kinds and models, label and policy value."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from sumbi.core.time import Window
from sumbi.measure.labels import Labels, measure as labels, model_at, usage_events
from sumbi.measure.policy import measure as policy
from sumbi.measure.pricing import measure, read
from sumbi.measure.profile import role
from sumbi.sessions.session import Session

T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
WINDOW = Window(T0, T0 + timedelta(days=1))
TABLE = {"currency": "USD", "as_of": "2030-01-01", "models": {
    "model-a*": {"new_input": 2, "cache_write": 2.5, "cache_read": 0.2, "output": 10},
    "model-b": {"new_input": 1, "output": 4}}}


def usage(**values):
    return {"new_input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
        "reasoning_output": None, **values}


def session(raw, events, models):
    s = Session("claude-code", raw)
    for i, (minute, values) in enumerate(events):
        at = T0 + timedelta(minutes=minute)
        s.attribution_events.append((at, (i, 0), "usage", (values, None, ())))
        s.request_usage.append((at, (i, 0), values))
    s.model_history.extend((T0 + timedelta(minutes=m), name) for m, name in models)
    s.models.update(name for _, name in models)
    return s


def write(data):
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "prices.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return directory, path


class PricingTests(unittest.TestCase):
    def setUp(self):
        directory, path = write(TABLE)
        with directory:
            self.pricer = read(path)

    def test_invalid_tables_are_rejected(self):
        for bad in ({}, {"models": {}}, {"models": {"m": {"output": -1}}},
            {"models": {"m": {"thinking": 1}}}, {"models": {"m": {"output": True}}}):
            directory, path = write(bad)
            with directory, self.assertRaises(ValueError):
                read(path)

    def test_value_follows_model_in_force_and_reports_unpriced_tokens(self):
        s = session("0123abcd-1", [
            (1, usage(new_input=1_000_000, cache_read=1_000_000, output=100_000)),
            (5, usage(new_input=1_000_000, cache_write=1_000_000, output=1_000_000)),
            (9, usage(new_input=7))],
            [(0, "Model-A-1"), (4, "model-b"), (8, "model-z")])
        rows = {s.id(): {"id": s.id(), "agent": "claude-code"}}
        report = measure([s], rows, WINDOW, self.pricer, role, model_at, usage_events)
        self.assertAlmostEqual(report["summary"]["amount"], 2 + 0.2 + 1 + 1 + 4)
        self.assertEqual(report["summary"]["unpriced_tokens"], 1_000_000 + 7)
        self.assertEqual(rows[s.id()]["value"]["unpriced_tokens"], 1_000_007)
        by_model = {r["model"]: r for r in report["by_model"]}
        self.assertAlmostEqual(by_model["Model-A-1"]["amount"], 3.2)
        self.assertEqual(by_model["model-z"]["unpriced_tokens"], 7)
        self.assertEqual(set(report["by_role"]), {"orchestrator"})

    def test_labels_and_forbidden_rows_carry_value(self):
        directory, path = write({})
        lines = Path(directory.name) / "labels.jsonl"
        lines.write_text(json.dumps({"session": "0123abcd", "lane": "lane-a"}) + "\n",
            encoding="utf-8")
        with directory:
            from sumbi.measure.labels import read as read_labels
            parsed = Labels(read_labels(lines))
        s = session("0123abcd-1", [(1, usage(output=1_000_000))], [(0, "model-b")])
        rows = {s.id(): {"id": s.id()}}
        lane = labels([s], rows, WINDOW, parsed, self.pricer)["by"]["lane"][0]
        self.assertEqual((lane["value"], lane["spend_value"]["amount"]), ("lane-a", 4.0))
        row = policy([s], rows, WINDOW, ("model-b",), parsed, self.pricer)["forbidden_models"][0]
        self.assertEqual(row["spend_value"]["amount"], 4.0)
        self.assertEqual(row["by_label"]["lane"][0]["value"], "lane-a")
        self.assertNotIn("spend_value", labels([s], rows, WINDOW, parsed)["by"]["lane"][0])


if __name__ == "__main__":
    unittest.main()
