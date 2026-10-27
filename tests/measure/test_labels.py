"""Synthetic session labels and the forbidden-model check."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from sumbi.core.time import Window
from sumbi.measure.labels import Labels, measure, read, text_lines
from sumbi.measure.policy import measure as policy, text_lines as policy_text
from sumbi.sessions.session import Session

T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
WINDOW = Window(T0, T0 + timedelta(days=1))
RAW = "0123abcd-1111-2222-3333-444455556666"


def usage(new_input=1, cache_read=0, output=1):
    return {"new_input": new_input, "cache_write": 0, "cache_read": cache_read,
        "output": output, "reasoning_output": None}


def session(raw, minutes, *, models=(), agent="claude-code"):
    s = Session(agent, raw)
    for i, (minute, values) in enumerate(minutes):
        at = T0 + timedelta(minutes=minute)
        s.attribution_events.append((at, (i, 0), "usage", (values, None, ())))
        s.request_usage.append((at, (i, 0), values))
    s.model_history.extend((T0 + timedelta(minutes=m), name) for m, name in models)
    s.models.update(name for _, name in models)
    return s


def label_file(lines):
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "labels.jsonl"
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n"
        for line in lines), encoding="utf-8")
    return directory, path


def rows(*sessions):
    return {s.id(): {"id": s.id()} for s in sessions}


class LabelTests(unittest.TestCase):
    def test_invalid_lines_are_counted_and_short_ids_rejected(self):
        directory, path = label_file([{"session": RAW[:8], "lane": "a"},
            {"session": "short", "lane": "a"}, "not json", {"session": RAW, "lane": "x" * 65},
            {"session": RAW, "at": "2030-01-01T00:00:00", "lane": "a"},
            {"session": RAW}, {"session": RAW, "lane": "bad\nvalue"}])
        with directory:
            parsed = read(path)
        self.assertEqual((parsed["lines"], parsed["invalid"], len(parsed["entries"])), (7, 6, 1))

    def test_prefix_labels_parent_and_subagent_and_timed_lines_split_spend(self):
        directory, path = label_file([
            {"session": RAW[:8], "lane": "lane-a", "account": "b", "start_kind": "task_spawn"},
            {"session": RAW[:8], "at": "2030-01-01T00:10:00Z", "lane": "lane-a",
                "account": "c", "start_kind": "resume_move"},
            {"session": "ffffffff", "lane": "never"}])
        with directory:
            labels = Labels(read(path))
        parent = session(RAW, [(1, usage(100)), (2, usage(10, 100)), (11, usage(5, 1000)),
            (12, usage(1, 1005))])
        child = session(RAW + ":subagent:x", [(3, usage(7))])
        other = session("99999999-0000", [(4, usage(50))])
        found = [parent, child, other]
        table = rows(*found)
        report = measure(found, table, WINDOW, labels)
        cov = report["coverage"]
        self.assertEqual((cov["entries"], cov["matched_entries"], cov["unmatched_entries"],
            cov["sessions_labeled"]), (3, 2, 1, 2))
        account = {r["value"]: r for r in report["by"]["account"]}
        self.assertEqual(account["b"]["tokens"]["total"], 101 + 111 + 8)
        self.assertEqual(account["b"]["sessions"], 2)
        self.assertEqual(account["c"]["tokens"]["total"], 1006 + 1007)
        self.assertEqual(account[None]["tokens"]["total"], 51)
        kinds = {r["value"]: r["calls"] for r in report["by"]["start_kind"]}
        self.assertEqual(kinds["resume_move"]["starts"], 1)
        self.assertEqual(kinds["resume_move"]["first5_context_sum_median"], 1005 + 1006)
        self.assertEqual(kinds["task_spawn"]["starts"], 2)
        self.assertEqual(kinds["task_spawn"]["first5_context_sum_median"], (210 + 7) / 2)
        self.assertEqual(table[parent.id()]["labels"][1]["from"], "2030-01-01T00:10:00Z")
        self.assertNotIn(RAW[:8], json.dumps(report) + "\n".join(text_lines(report)))

    def test_duplicate_lines_and_leading_unlabeled_start(self):
        directory, path = label_file([
            {"session": RAW[:8], "at": "2030-01-01T00:10:00Z", "lane": "old"},
            {"session": RAW[:8], "at": "2030-01-01T00:10:00Z", "lane": "new"}])
        with directory:
            labels = Labels(read(path))
        s = session(RAW, [(1, usage(3)), (11, usage(5))])
        rows_ = rows(s)
        report = measure([s], rows_, WINDOW, labels)
        lane = {r["value"]: r for r in report["by"]["lane"]}
        self.assertEqual(set(lane), {"new", None})
        self.assertEqual((lane[None]["calls"]["starts"], lane["new"]["calls"]["starts"]), (1, 1))
        self.assertEqual(lane[None]["calls"]["first_context_median"], 3)
        self.assertEqual(report["coverage"]["sessions_partly_labeled"], 1)
        total = sum(r["tokens"]["total"] for r in report["by"]["lane"])
        self.assertEqual(total, 4 + 6)

    def test_forbidden_model_counts_only_events_on_that_model(self):
        directory, path = label_file([{"session": RAW[:8], "lane": "lane-a"}])
        with directory:
            labels = Labels(read(path))
        mixed = session(RAW, [(1, usage(10)), (5, usage(20))],
            models=[(0, "model-allowed"), (4, "Model-Banned")])
        only = session("77777777-x", [(1, usage(3))], models=[(0, "model-banned")])
        found = [mixed, only]
        report = policy(found, rows(*found), WINDOW, ("model-banned",), labels)
        row = report["forbidden_models"][0]
        self.assertEqual((row["sessions"], row["tokens"]["total"]), (2, 21 + 4))
        self.assertEqual({m["model"] for m in row["models"]}, {"Model-Banned", "model-banned"})
        lanes = {v["value"]: v["tokens"]["total"] for v in row["by_label"]["lane"]}
        self.assertEqual(lanes, {"lane-a": 21, None: 4})
        self.assertIn("Forbidden model model-banned: sessions 2; tokens 25", policy_text(report)[0])
        none = policy(found, rows(*found), WINDOW, ("gpt-*",))["forbidden_models"][0]
        self.assertEqual((none["sessions"], none["tokens"]["total"]), (0, 0))
        self.assertNotIn("by_label", none)
        bare = session("55555555-y", [(1, usage(2))])
        row = policy([bare], rows(bare), WINDOW, ("*",))["forbidden_models"][0]
        self.assertEqual((row["sessions"], row["model_unobserved_events"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
