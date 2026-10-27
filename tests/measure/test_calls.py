"""Synthetic model calls: count, per-call context and the session's start cost."""

from datetime import datetime, timedelta, timezone
import unittest

from sumbi.core.time import Window
from sumbi.measure.calls import contexts, measure, summarize
from sumbi.measure.profile import measure as profile, tail_lines
from sumbi.sessions.session import Session

T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
WINDOW = Window(T0, T0 + timedelta(days=1))


def usage(new_input, cache_write=0, cache_read=0):
    return {"new_input": new_input, "cache_write": cache_write, "cache_read": cache_read,
        "output": 1, "reasoning_output": None}


def session(raw, calls, *, offset=0):
    s = Session("claude-code", raw)
    s.request_usage.extend((T0 + timedelta(minutes=offset + i), (i, 0), values)
        for i, values in enumerate(calls))
    return s


class CallTests(unittest.TestCase):
    def test_context_is_new_input_plus_cache_and_start_is_first_five(self):
        s = session("a", [usage(10, 100, 0), usage(1, 5, 110), usage(1, 0, 115),
            usage(2, 0, 116), usage(3, 0, 118), usage(1, 0, 1000)])
        row = measure(s, WINDOW)
        self.assertEqual(row["count"], 6)
        self.assertEqual(row["context"]["max"], 1001)
        self.assertEqual(row["context"]["median"], (116 + 118) / 2)
        self.assertEqual(row["start"], {"observed": True, "first_context": 110,
            "first5_context_sum": 110 + 116 + 116 + 118 + 121})

    def test_session_started_before_window_has_no_start_cost(self):
        s = session("b", [usage(5), usage(6)], offset=-1)
        row = measure(s, WINDOW)
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["start"]["observed"], False)
        self.assertIsNone(row["start"]["first5_context_sum"])

    def test_zero_usage_records_are_not_calls(self):
        zero = {"new_input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
            "reasoning_output": None}
        s = session("z", [zero, usage(10), zero])
        row = measure(s, WINDOW)
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["start"]["first_context"], 10)

    def test_unobserved_context_is_counted_not_guessed(self):
        s = session("c", [usage(None, 5, 5), usage(4)])
        row = measure(s, WINDOW)
        self.assertEqual(row["context_unobserved"], 1)
        self.assertEqual(row["start"]["first_context"], None)
        self.assertIsNone(row["start"]["first5_context_sum"])
        self.assertEqual(row["context"]["total"], 4)

    def test_summary_pools_calls_and_takes_start_medians(self):
        a = session("a", [usage(100), usage(200)])
        b = session("b", [usage(10), usage(20), usage(30)])
        entries = [{"contexts": contexts(s, WINDOW), "calls": measure(s, WINDOW)}
            for s in (a, b)]
        out = summarize(entries)
        self.assertEqual((out["count"], out["sessions_with_calls"]), (5, 2))
        self.assertEqual(out["per_session_median"], 2.5)
        self.assertEqual((out["context_median"], out["context_p90"], out["context_max"]),
            (30, 200, 200))
        self.assertEqual(out["first_context_median"], 55)
        self.assertEqual(out["first5_context_sum_median"], (300 + 60) / 2)
        self.assertEqual(out["first5_context_sum_total"], 360)

    def test_profile_role_and_key_summary_carry_calls(self):
        s = session("a", [usage(100), usage(200)])
        row = {"id": s.id(), "parent_id": None, "agent": "claude-code",
            "project": {"bucket": "other", "project_key": None, "rule": None},
            "models": [], "allocations": [], "tokens": {"new_input": 300, "total": 302},
            "counts": {"tool_calls": 0, "tool_results": 0, "tool_errors": 0, "compactions": 0},
            "time": {"active": {"seconds": 0}, "durations": {"tool": {"seconds": 0,
                "paired_intervals": 0}}}}
        result = profile([s], {s.id(): row}, WINDOW)
        self.assertEqual(result["summary"]["roles"]["orchestrator"]["calls"]["count"], 2)
        tail = tail_lines({"window": {"since": "a", "until": "b"},
            "summary": {"sessions": 1, "tokens": {"total": 302}}}, result)
        self.assertIn("  Calls orchestrator: 2 (per session median 2); context per call "
            "median/p90/max 150/200/200; starts 1: first call median 100, first 5 calls sum "
            "median 300", tail)


if __name__ == "__main__":
    unittest.main()
