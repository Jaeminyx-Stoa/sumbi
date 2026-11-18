import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sumbi.live import _table, load_customer_value, snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "customer_value_2026-11-13.json"
# 11-14 09:00 KST — "yesterday" is 11-13 KST.
NOW = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)


def _data():
    return snapshot([{"lane": "주인·숨비", "engine": "claude"}, {"lane": "주인·큐", "engine": "claude"}], now=NOW)


class CustomerValueTableTests(unittest.TestCase):
    def render(self, files=None, data=None):
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (files or {}).items():
                Path(tmp, name).write_text(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
            return _table(data or _data(), tmp)

    def test_fixture_values_shown_company_wide(self):
        text = self.render({"2026-11-13.json": FIXTURE.read_text()})
        self.assertIn("고객 가치당 토큰: 2026-11-13 KST", text)
        self.assertIn("| 회사 전체 · 가입 | 5 count;", text)
        self.assertIn("30행/9계좌", text)
        self.assertIn("| 회사 전체 · 결제 | 2 count;", text)
        self.assertIn("| 회사 전체 · 계좌 연결 | 0 count;", text)
        self.assertIn("근거 ", text)
        self.assertIn("창별 귀속: 모름 — 귀속 기록 없음(2창)", text)

    def test_no_file_says_unknown_not_zero(self):
        text = self.render({})
        self.assertIn("모름: 수집 파일 없음", text)
        self.assertNotIn("| 회사 전체 · 가입 | 0", text)

    def test_null_cell_is_unknown(self):
        doc = json.loads(FIXTURE.read_text())
        doc["cells"]["signups"] = {"value": None, "reason": "customer_filter_unverified"}
        text = self.render({"2026-11-13.json": doc})
        self.assertIn("| 회사 전체 · 가입 | 모름: customer_filter_unverified", text)
        self.assertIn("30행/9계좌", text)

    def test_stale_file_ignored(self):
        doc = json.loads(FIXTURE.read_text())
        doc["period_date"] = "2026-11-12"
        text = self.render({"2026-11-12.json": doc})
        self.assertNotIn("30행/9계좌", text)
        self.assertIn("모름: 수집 파일 없음", text)

    def test_name_date_mismatch_rejected(self):
        payload, reason = None, None
        with tempfile.TemporaryDirectory() as tmp:
            doc = json.loads(FIXTURE.read_text())
            doc["period_date"] = "2026-11-12"
            Path(tmp, "2026-11-13.json").write_text(json.dumps(doc))
            payload, reason = load_customer_value(tmp, NOW)
        self.assertIsNone(payload)
        self.assertIn("날짜가 파일 이름과 다름", reason)

    def test_merge_table_gone(self):
        text = self.render({"2026-11-13.json": FIXTURE.read_text()})
        self.assertNotIn("머지당 토큰", text)


if __name__ == "__main__":
    unittest.main()
