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

class SnapshotTests(RequestSummaryTests):
    def test_three_engines_and_repeat(self):
        from sumbi.live import snapshot
        op = lambda ident, sec, n: {'id': ident, 'data': {'role': 'assistant', 'time': {'created': int((self.now + timedelta(seconds=sec)).timestamp()*1000), 'completed': int((self.now + timedelta(seconds=sec+1)).timestamp()*1000)}, 'tokens': {'input': n, 'output': 1, 'cache': {'read': 2, 'write': 3}}}}
        windows = [
            {'lane': 'a', 'engine': 'claude', 'records': [self.claude(-3601, 'x', 5, 1), self.claude(-1, 'y', 8, 1), self.claude(-1, 'y', 8, 2)]},
            {'lane': 'b', 'engine': 'codex', 'records': [self.codex(-3600, 10, 20), self.codex(-1, 10, 20), self.codex(-1, 1, 30)]},
            {'lane': 'c', 'engine': 'opencode', 'messages': [op('x', -7200, 10), op('y', -1, 20), op('y', -1, 21)]},
        ]
        result = snapshot(windows, now=self.now)
        self.assertEqual(result, snapshot(windows, now=self.now))
        self.assertEqual([r['calls_1h'] for r in result['windows']], [1, 2, 1])
        self.assertEqual([r['baseline_tokens'] for r in result['windows']], [5, 20, 15])
        self.assertEqual(result['windows'][2]['context_tokens'], 26)
        self.assertEqual(result['coverage']['duplicates'], 3)

    def test_incomplete_and_stale(self):
        from sumbi.live import snapshot
        data = snapshot([{'lane': 'a', 'engine': 'claude', 'records': [self.claude(-4000, 'a', 1, 1)]}, {'lane': 'b', 'engine': 'codex', 'records': [self.codex(-1, 1, 2)], 'complete': False}, {'lane': 'c', 'engine': 'unknown'}], now=self.now)
        self.assertEqual([r['status'] for r in data['windows']], ['stale', 'unknown', 'unknown'])
        self.assertIsNone(data['windows'][1]['baseline_tokens'])
        self.assertIsNone(data['windows'][2]['calls_1h'])

    def test_files_limits_and_atomic_output(self):
        import json
        import tempfile
        from pathlib import Path
        from sumbi.live import collect_inventory, write_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.jsonl'
            path.write_text(json.dumps(self.codex(-1, 1, 4)) + '\n')
            inventory = {'windows': [{'lane': 'a', 'engine': 'codex', 'source_paths': [str(path)]}]}
            data = collect_inventory(inventory, self.now)
            self.assertEqual(data['windows'][0]['calls_1h'], 1)
            self.assertEqual(collect_inventory(inventory, self.now, max_bytes=3)['windows'][0]['reason'], 'byte_limit')
            write_snapshot(data, tmp)
            self.assertEqual(json.loads((Path(tmp) / 'snapshot.json').read_text()), data)
            self.assertIn('| a | codex |', (Path(tmp) / 'LATEST.md').read_text())
            self.assertEqual((Path(tmp) / 'fortress-line.txt').read_text().count('\n'), 1)
            path.write_text(path.read_text() + '{broken\n')
            self.assertEqual(collect_inventory(inventory, self.now)['windows'][0]['reason'], 'invalid_json')

    def test_session_transition_and_missing_usage(self):
        from sumbi.live import snapshot
        records = [{'type': 'session_meta', 'payload': {'id': 'old'}}, self.codex(-2, 9, 90), {'type': 'session_meta', 'payload': {'id': 'new'}}, self.codex(-1, 1, 10)]
        row = snapshot([{'lane': 'a', 'engine': 'codex', 'records': records}], now=self.now)['windows'][0]
        self.assertEqual((row['session_id'], row['baseline_tokens'], row['calls_1h']), ('new', 10, 1))
        bad = self.claude(-1, 'a', 2, 1)
        del bad['message']['usage']
        row = snapshot([{'lane': 'a', 'engine': 'claude', 'records': [bad]}], now=self.now)['windows'][0]
        self.assertEqual((row['status'], row['reason']), ('unknown', 'incomplete_source'))

    def test_model_transition(self):
        from sumbi.live import snapshot
        a, b = self.claude(-2, 'a', 10, 1), self.claude(-1, 'b', 20, 1)
        a['message']['model'], b['message']['model'] = 'model-a', 'model-b'
        row = snapshot([{'lane': 'a', 'engine': 'claude', 'records': [a, b]}], now=self.now)['windows'][0]
        self.assertEqual((row['baseline_tokens'], row['calls_1h']), (20, 1))
