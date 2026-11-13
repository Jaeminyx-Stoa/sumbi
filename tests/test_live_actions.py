import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

from sumbi.live_actions import plan, quota

ROOT = Path(__file__).resolve().parents[1]
NOW = '2026-01-01T12:00:00Z'

def window(**updates):
    row = dict(lane='lane1', engine='claude', account='acct1', session_id='s1', context_tokens=200000, previous_calls_1h=10, calls_1h=30, observed_at=NOW, status='ok')
    row.update(updates)
    return row

def sample(**updates):
    row = dict(account='acct1', engine='claude', observed_at=NOW, used_percent=90, resets_at='2026-01-01T12:40:00Z', window_minutes=300, status='ok')
    row.update(updates)
    return row

def snapshot(windows=None, accounts=None):
    return dict(schema_version=1, observed_at=NOW, windows=windows or [], accounts=accounts or [], coverage={})

class Sink:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
    def send(self, key, lane, message):
        self.calls.append((key, lane, message))
        if self.fail == message:
            self.fail = None
            raise OSError('injected')

class Tests(unittest.TestCase):
    def test_threshold_and_missing_comparison(self):
        rows = plan(snapshot([window(), window(lane="lane2", context_tokens=200001, previous_calls_1h=None)]), now=datetime.fromisoformat(NOW.replace("Z", "+00:00")))
        self.assertEqual([r['status'] for r in rows], ['clear', 'signal', 'signal', 'unknown'])

    def test_quota(self):
        now = datetime.fromisoformat(NOW.replace('Z', '+00:00'))
        previous = sample(observed_at='2026-01-01T11:55:00Z', used_percent=88)
        self.assertEqual(quota(previous, sample(), now)['status'], 'signal')
        for changed in (dict(resets_at='2026-01-01T13:00:00Z'), dict(engine='codex'), dict(account='acct2'), dict(observed_at='2026-01-01T11:00:00Z')):
            self.assertEqual(quota(previous, sample(**changed), now)['status'], 'unknown')
        self.assertEqual(quota(sample(), sample(), now)['status'], 'unknown')

    def test_quota_targets_only_same_account_engine(self):
        current = snapshot([window(), window(lane='lane2', engine='codex'), window(lane='lane3', account='acct2')], [sample()])
        previous = snapshot(accounts=[sample(observed_at='2026-01-01T11:55:00Z', used_percent=88)])
        decisions = plan(current, previous, now=datetime.fromisoformat(NOW.replace('Z', '+00:00')))
        matches = [d for d in decisions if d['kind'] == 'quota']
        self.assertEqual([(d['lane'], d['status']) for d in matches], [('lane1', 'signal')])
        stale = plan(current, previous, now=datetime.fromisoformat('2026-01-01T12:11:00+00:00'))
        self.assertTrue(all(d['status'] == 'unknown' for d in stale))

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat(NOW.replace('Z', '+00:00'))

    def test_unknown_account_keeps_window_signals(self):
        for account in (None, ''):
            rows = plan(snapshot([window(account=account, context_tokens=200001)]), now=self.now)
            self.assertEqual([r['status'] for r in rows], ['signal', 'signal'])

    def test_duplicate_windows_are_unknown(self):
        rows = plan(snapshot([window(), window()]), now=self.now)
        self.assertTrue(all(r['status'] == 'unknown' for r in rows))

    def test_future_and_stale_samples(self):
        for stamp in ('2026-01-01T12:00:01Z', '2026-01-01T11:49:59Z'):
            rows = plan(snapshot([window(observed_at=stamp)]), now=self.now)
            self.assertTrue(all(r['status'] == 'unknown' for r in rows))
        stale = snapshot([window()])
        stale['observed_at'] = '2026-01-01T11:59:00Z'
        rows = plan(stale, now=self.now)
        self.assertEqual(rows[1]['status'], 'signal')

    def test_invalid_numeric_samples(self):
        for value in (True, -1, float('nan'), float('inf'), '200001'):
            rows = plan(snapshot([window(context_tokens=value)]), now=self.now)
            self.assertEqual(rows[0]['status'], 'unknown')

    def test_quota_windows_are_independent(self):
        current = snapshot([window()], [sample(), sample(window_minutes=10080)])
        old_rows = [sample(observed_at='2026-01-01T11:55:00Z', used_percent=88),
                    sample(window_minutes=10080, observed_at='2026-01-01T11:55:00Z', used_percent=88)]
        rows = [r for r in plan(current, snapshot(accounts=old_rows), self.now) if r['kind'] == 'quota']
        self.assertEqual([(r['window_minutes'], r['status']) for r in rows],
                         [(300, 'signal'), (10080, 'signal')])
        current['accounts'].append(sample())
        rows = [r for r in plan(current, snapshot(accounts=old_rows), self.now) if r['kind'] == 'quota']
        self.assertEqual([r['status'] for r in rows], ['unknown', 'signal', 'unknown'])

    def test_duplicate_previous_and_nonlist_previous(self):
        a = sample(observed_at='2026-01-01T11:55:00Z', used_percent=88)
        for previous in (snapshot(accounts=[a, a]), dict(schema_version=1, accounts=None)):
            rows = plan(snapshot([window()], [sample()]), previous, self.now)
            self.assertEqual(rows[-1]['status'], 'unknown')

    def test_invalid_snapshot(self):
        for data in (None, {}, dict(schema_version=1, observed_at=NOW, windows={}, accounts=[])):
            with self.assertRaises(ValueError):
                plan(data, now=self.now)

if __name__ == '__main__': unittest.main()
