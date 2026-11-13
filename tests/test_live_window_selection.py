"""Regression coverage for window discovery and failed assistant observations."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sumbi.live import snapshot
from sumbi.measure.live_sources import live_inventory


class LiveWindowSelectionTests(unittest.TestCase):
    def test_only_unidentified_windows_without_sources_are_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            inv = live_inventory(
                target="x", proc_root=Path(temp), sqlite_path=Path(temp) / "db",
                run=lambda _: "0\t\t\t\t\t1\n1\t\ts\t\t\t2\n2\t\t\t\tcodex\t3\n"
                "3\t\t\t/config\t\t4\n4\tlane\t\t\t\t5",
            )
        self.assertEqual(inv["excluded_no_agent_metadata_or_source"], 1)
        self.assertEqual(len(inv["windows"]), 4)

    def test_failure_is_observed_without_replacing_valid_zero_or_context(self):
        now = datetime.now(timezone.utc)
        timestamp = (now - timedelta(seconds=1)).isoformat()
        def record(value, failed=False):
            return {"type": "assistant", "timestamp": timestamp, "isApiErrorMessage": failed,
                    "message": {"id": "b" if failed else "a", "usage": {
                        "input_tokens": value, "output_tokens": 0,
                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
        for value in (0, 25):
            data = snapshot([{"engine": "claude", "records": [record(value), record(0, True)]}], now=now)
            self.assertEqual(data["windows"][0]["context_tokens"], value)
            self.assertEqual(data["coverage"]["error_observations"], 1)


if __name__ == "__main__":
    unittest.main()
