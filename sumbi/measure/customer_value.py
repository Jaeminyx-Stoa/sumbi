"""Validate dated, sanitized customer-value aggregates without collecting data."""
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sumbi.core.values import timestamp

KST = timezone(timedelta(hours=9))
ENV = 'SUMBI_CUSTOMER_VALUE_DIR'
_VALUE_METRICS = (
    ("window_tokens", "창별 하루 토큰", "tokens", "window_usage"),
    ("q_customer_fill", "본인계좌 고객 체결", "count", "customer_fill"),
    ("real_orders", "실전계좌 주문", "count", "customer_order"),
    ("real_fills", "실전계좌 체결", "count", "customer_fill"),
    ("backtest_views", "자기규칙 결과 실제 열람", "count", "actual_view"),
    ("backtest_view_seconds", "자기규칙 결과 열람 시간", "seconds", "actual_view_duration"),
    ("inquiry_replies", "문의 해결 답 발송", "count", "resolved_reply_sent"),
    ("customer_screen_errors", "고객 화면 오류", "count", "customer_error"),
    ("ceo_first_defects", "대표 최초 발견 결함", "count", "first_found_defect"),
    ("mcp_new_domestic", "MCP 국내 새 연결 계정", "count", "mcp_new_domestic"),
    ("mcp_new_global", "MCP 글로벌 새 연결 계정", "count", "mcp_new_global"),
    ("mcp_active_domestic", "MCP 국내 활성 연결", "count", "mcp_active_domestic"),
    ("mcp_active_global", "MCP 글로벌 활성 연결", "count", "mcp_active_global"),
    ("mcp_calls_domestic", "MCP 국내 도구 호출", "count", "mcp_calls_domestic"),
    ("mcp_calls_global", "MCP 글로벌 도구 호출", "count", "mcp_calls_global"),
    ("api_issued", "개발자 API 발급 키", "count", "api_issued"),
    ("api_active", "개발자 API 활성 키", "count", "api_active"),
    ("api_calls", "개발자 API 호출", "count", "api_calls"),
    ("api_payments", "개발자 API 이용권 결제", "count", "api_payments"),
    ("bot_connections", "클라우드 봇 연결", "count", "bot_connections"),
    ("bot_running", "클라우드 실행 중 봇 수 (시점 gauge)", "count", "bot_running"),
    ("enterprise_contracts", "기업 계약", "count", "enterprise_contracts"),
    ("contract_revenue", "계약 매출", "currency", "contract_revenue"),
    ("signups", "가입", "count", "signup"),
    ("account_connections", "계좌 연결", "count", "account_connection"),
    ("first_backtests", "첫 백테스트", "count", "first_backtest"),
    ("payments", "결제", "count", "payment"),
    ("production_features", "운영 반영 기능·수정", "count", "production_feature_customer_walk"),
)


COMPANY = tuple(next(m for m in _VALUE_METRICS if m[0] == key) for key in
                ('signups', 'real_fills', 'account_connections', 'payments')) + (
    ('mcp_first_calls', 'MCP 첫 호출', 'count', 'mcp_first_call'),
    next(m for m in _VALUE_METRICS if m[0] == 'api_calls'))
METRICS = {m[0]: m for m in _VALUE_METRICS + COMPANY}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _time(value):
    return timestamp(value) if isinstance(value, str) else None


def _expand_cells(doc, start, end):
    records = []
    for key, value in doc.get('cells', {}).items():
        if key not in METRICS or not isinstance(value, dict):
            continue
        unit, kind = METRICS[key][2:]
        records.append(dict(
            value, metric_id=key, lane=None,
            unit=value.get('unit', unit), source_kind=value.get('source_kind', kind),
            period_start=value.get('period_start', doc.get('period_start', start.isoformat())),
            period_end=value.get('period_end', doc.get('period_end', end.isoformat())),
            status=value.get('status', 'complete'),
            coverage=value.get('coverage', doc.get('coverage')),
            observed_at=value.get('observed_at', doc.get('observed_at')),
            source_ref=value.get('source_ref', doc.get('source_ref')),
        ))
    return records


def _validate(record, lane, metric, start, end, now):
    unit, kind = METRICS[metric][2:]
    value = record.get('value')
    flags = ('complete', 'customer_scope', 'period', 'deduplicated')
    if lane is not None:
        flags += ('lane_attribution',)
    coverage = record.get('coverage')
    observed = _time(record.get('observed_at'))
    valid_number = (type(value) is int and value >= 0) or (
        unit in ('seconds', 'currency') and type(value) is float
        and math.isfinite(value) and value >= 0
    )
    valid = (valid_number and record.get('status') == 'complete'
             and isinstance(coverage, dict) and all(coverage.get(f) is True for f in flags)
             and _time(record.get('period_start')) == start
             and _time(record.get('period_end')) == end
             and observed is not None and end <= observed <= now
             and record.get('unit') == unit and record.get('source_kind') == kind
             and isinstance(record.get('source_ref'), str) and bool(record['source_ref'].strip()))
    if metric == 'contract_revenue':
        valid = (valid and isinstance(record.get('currency_code'), str)
                 and bool(record['currency_code'].strip()))
    if metric == 'production_features':
        valid = valid and all(isinstance(record.get(f), str) and record[f].strip()
                              for f in ('production_promotion_ref', 'customer_walk_ref'))
    if valid:
        return {'value': value, 'unit': unit, 'start': start.isoformat(), 'end': end.isoformat(),
                'observed_at': record['observed_at'], 'source_ref': record['source_ref'],
                'accounts': record.get('accounts'), 'currency_code': record.get('currency_code'),
                'caveat': record.get('caveat'),
                'production_promotion_ref': record.get('production_promotion_ref'),
                'customer_walk_ref': record.get('customer_walk_ref')}
    reason = record.get('reason')
    return {'reason': reason if isinstance(reason, str) and reason.strip()
            else '상태·범위·기간·값 또는 근거 미확인'}


def load_customer_value(directory, now, payload=None):
    """Return target date, validated cells and an explanatory file status."""
    day = now.astimezone(KST).date() - timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    end = start + timedelta(days=1)
    if payload is None:
        default = Path.home() / 'runs' / 'sumbi-live' / 'customer-value'
        root = Path(directory) if directory is not None else Path(os.environ.get(ENV) or default)
        path = root / f'{day}.json'
        try:
            doc = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique)
        except FileNotFoundError:
            return day, {}, '수집 파일 없음'
        except (OSError, UnicodeError, ValueError, RecursionError):
            return day, {}, '파일 읽기 또는 JSON 오류'
    else:
        doc = payload
    if not isinstance(doc, dict) or doc.get('period_date') != str(day):
        return day, {}, '날짜가 파일 이름 또는 어제와 다름'
    records = doc.get('records', [])
    if not isinstance(records, list):
        return day, {}, '기록 형식 오류'
    if not isinstance(doc.get('cells', {}), dict):
        return day, {}, '셀 형식 오류'
    records = list(records) + _expand_cells(doc, start, end)
    grouped = {}
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get('metric_id'), str)
                or record['metric_id'] not in METRICS):
            continue
        lane = record.get('lane')
        if lane is not None and (not isinstance(lane, str) or not lane.strip()):
            continue
        grouped.setdefault((lane, record['metric_id']), []).append(record)
    result = {}
    for (lane, metric), entries in grouped.items():
        result[(lane, metric)] = (
            {'reason': '중복 또는 상충 기록'} if len(entries) != 1
            else _validate(entries[0], lane, metric, start, end, now)
        )
    return day, result, None
