import unittest
from datetime import datetime, timedelta, timezone

from sumbi.live import summarize_requests


class RequestSummaryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def codex(self, seconds, total, value):
        return {"timestamp": (self.now + timedelta(seconds=seconds)).isoformat(),
                "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": total, "cached_input_tokens": 0,
                                          "output_tokens": total, "reasoning_output_tokens": 0},
                    "last_token_usage": {"input_tokens": value, "cached_input_tokens": 0,
                                         "output_tokens": 1, "reasoning_output_tokens": 0}}}}

    def claude(self, seconds, identity, value, output):
        return {"timestamp": (self.now + timedelta(seconds=seconds)).isoformat(),
                "type": "assistant", "message": {"id": identity, "usage": {
                    "input_tokens": value, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0, "output_tokens": output}}}

    def test_codex_boundaries_and_duplicate(self):
        events = [self.codex(-7200, 1, 10), self.codex(-3601, 2, 20),
                  self.codex(-3600, 3, 30), self.codex(-1, 4, 40),
                  self.codex(-1, 4, 40), self.codex(0, 5, 50)]
        result = summarize_requests('codex', events, self.now)
        self.assertEqual((result['previous_calls_1h'], result['calls_1h']), (2, 2))
        self.assertEqual((result['baseline_tokens'], result['context_tokens']), (10, 40))
        self.assertEqual((result['input_tokens_1h'], result['duplicates'], result['future']), (70, 1, 1))

    def test_claude_streamed_message(self):
        events = [self.claude(-3601, 'a', 10, 1), self.claude(-3600, 'b', 20, 1),
                  self.claude(-1, 'b', 20, 2)]
        result = summarize_requests('claude', events, self.now)
        self.assertEqual((result['previous_calls_1h'], result['calls_1h'], result['duplicates']), (1, 1, 1))

    def test_missing_timestamp_is_unknown(self):
        event = self.codex(-1, 1, 10)
        del event['timestamp']
        result = summarize_requests('codex', [event], self.now)
        self.assertIsNone(result['calls_1h'])
        self.assertEqual(result['invalid'], 1)


if __name__ == '__main__':
    unittest.main()
