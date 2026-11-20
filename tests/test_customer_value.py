import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sumbi.measure.customer_value import load_customer_value
from sumbi.live import snapshot, write_snapshot, read_snapshot_bundle

FIXTURE = Path(__file__).parent / 'fixtures' / 'customer_value_2026-11-13.json'
NOW = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)


class CustomerValueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / '2026-11-13.json'
        self.doc = json.loads(FIXTURE.read_text())

    def save(self):
        self.path.write_text(json.dumps(self.doc), encoding='utf-8')

    def render(self):
        data = snapshot([{'lane': 'alpha', 'engine': 'claude'}], now=NOW)
        write_snapshot(data, self.root / 'out', value_dir=self.root)
        return read_snapshot_bundle(self.root / 'out')['LATEST.md']

    def test_missing_then_arriving_same_publication_and_zero(self):
        self.assertIn('수집 파일 없음', self.render())
        self.save()
        text = self.render()
        self.assertIn('회사 전체 · 가입 | 5 count', text)
        self.assertIn('회사 전체 · 계좌 연결 | 0 count', text)
        self.assertIn('30행/9계좌', text)
        self.assertIn('근거 synthetic-fixture', text)
        self.assertIn('주의 synthetic caveat', text)
        self.assertNotIn('머지당 토큰', text)

    def test_uncollected_cells_are_unknown(self):
        self.doc['cells'].pop('mcp_first_calls')
        self.doc['cells'].pop('api_calls')
        self.save()
        text = self.render()
        self.assertIn('MCP 첫 호출 | 모름:', text)
        self.assertIn('개발자 API 호출 | 모름:', text)

    def test_existing_positional_directory_argument(self):
        self.save()
        write_snapshot(snapshot([], now=NOW), self.root / 'out', self.root)
        self.assertIn('회사 전체 · 가입 | 5 count',
                      read_snapshot_bundle(self.root / 'out')['LATEST.md'])

    def test_only_yesterday_including_midnight(self):
        now = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
        (self.root / '2026-11-14.json').write_text(json.dumps(self.doc))
        (self.root / '2026-11-12.json').write_text(json.dumps(self.doc))
        day, values, reason = load_customer_value(self.root, now)
        self.assertEqual(str(day), '2026-11-13')
        self.assertEqual(values, {})
        self.assertEqual(reason, '수집 파일 없음')

    def test_corruption_name_mismatch_and_duplicate_json_key(self):
        for text in ('{broken', '{"period_date":"2026-11-13","period_date":"2026-11-13"}'):
            self.path.write_text(text)
            self.assertIn('JSON 오류', self.render())
        self.doc['period_date'] = '2026-11-12'
        self.save()
        self.assertIn('날짜가 파일 이름', self.render())

    def test_read_failure_still_publishes_unknown(self):
        self.save()
        original = Path.read_text

        def fail_file(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError('injected at file read')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', fail_file):
            self.assertIn('파일 읽기 또는 JSON 오류', self.render())

    def test_bad_cells_leave_other_values(self):
        for value in (-1, True, 1.5, None):
            self.doc['cells']['signups']['value'] = value
            self.save()
            text = self.render()
            self.assertIn('회사 전체 · 가입 | 모름:', text)
            self.assertIn('회사 전체 · 결제 | 2 count', text)
        self.doc['cells']['signups'] = {'value': 1, 'status': 'partial'}
        self.save()
        self.assertIn('회사 전체 · 가입 | 모름:', self.render())

    def test_malformed_record_does_not_stop_other_cells(self):
        self.doc['records'] = [{'metric_id': []}, None]
        self.save()
        self.assertIn('회사 전체 · 결제 | 2 count', self.render())

    def test_record_scope_period_duplicates_and_ratios(self):
        self.doc['cells'] = {}
        base = {'lane': 'alpha', 'status': 'complete', 'coverage': dict(self.doc['coverage'], lane_attribution=True),
                'period_start': '2026-11-13T00:00:00+09:00', 'period_end': '2026-11-14T00:00:00+09:00',
                'observed_at': self.doc['observed_at'], 'source_ref': 'synthetic-fixture'}
        tokens = dict(base, metric_id='window_tokens', value=100, unit='tokens', source_kind='window_usage')
        metric = dict(base, metric_id='real_fills', value=5, unit='count', source_kind='customer_fill')
        self.doc['records'] = [tokens, metric]
        self.save()
        self.assertIn('20.00 tokens/count', self.render())
        metric['value'] = 0
        self.save()
        self.assertIn('계산 불가: 분모 0', self.render())
        metric['value'] = 5
        metric['period_end'] = '2026-11-13T12:00:00+09:00'
        self.save()
        text = self.render()
        self.assertNotIn('20.00 tokens/count', text)
        self.assertIn('기간·값 또는 근거 미확인', text)
        metric['period_end'] = base['period_end']
        self.doc['records'].append(copy.deepcopy(metric))
        self.save()
        self.assertIn('중복 또는 상충 기록', self.render())

    def test_missing_coverage_and_observation_are_unknown(self):
        for change in ({'coverage': {}}, {'observed_at': '2026-11-15T00:00:00Z'},
                       {'source_ref': ''}, {'period_end': '2026-11-13T12:00:00+09:00'}):
            self.doc.update(change)
            self.save()
            self.assertIn('회사 전체 · 가입 | 모름:', self.render())
            self.doc = json.loads(FIXTURE.read_text())

    def test_old_daily_summary_does_not_stop_publication(self):
        data = snapshot([], now=NOW)
        data['daily_results'] = {'tokens': {}}
        write_snapshot(data, self.root / 'out', value_dir=self.root)
        self.assertIn('이전 입력 형식', read_snapshot_bundle(self.root / 'out')['LATEST.md'])

    def test_existing_idle_status_keeps_its_label(self):
        data = snapshot([{'lane': 'alpha', 'engine': 'unknown', '_error': 'no_active_native'}], now=NOW)
        write_snapshot(data, self.root / 'out', value_dir=self.root)
        self.assertIn('멈춤/셸만', read_snapshot_bundle(self.root / 'out')['LATEST.md'])

if __name__ == '__main__':
    unittest.main()
