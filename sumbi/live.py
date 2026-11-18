"""Bounded, read-only request snapshots from explicitly supplied sources."""

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sumbi.core.values import mapping, timestamp
from sumbi.core.local_cache import exclusive_lock
from sumbi.events.adapters import claude_code, codex
from sumbi.events.schema import RequestUsage, TokenUsage


def _time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return timestamp(value)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RequestAccumulator:
    """Normalized numeric observations shared by full and incremental collection.

    Retain request IDs and counter boundaries even outside the recent windows:
    delayed duplicate updates must keep their original request timestamp.
    Raw records, tool arguments and message content are never retained.
    """

    def __init__(self, engine):
        if engine not in ("claude", "codex", "opencode"):
            raise ValueError("unsupported engine")
        self.engine = engine
        self.observations = []
        self.invalid = 0
        self.error_observations = 0
        self.session = self.model = None
        self.order = 0

    def add(self, record):
        engine = self.engine
        order = self.order
        self.order += 1
        if not isinstance(record, dict):
            self.invalid += 1
            return
        if engine == "claude" and record.get("type") == "assistant" and (
            record.get("isApiErrorMessage") is True or record.get("error")
        ):
            self.error_observations += 1
            return
        identity = (
            record.get("sessionId")
            if engine == "claude"
            else mapping(record.get("payload")).get("id")
            if engine == "codex" and record.get("type") == "session_meta"
            else record.get("session_id")
            if engine == "opencode"
            else None
        )
        current_model = (
            mapping(record.get("message")).get("model")
            if engine == "claude"
            else mapping(record.get("payload")).get("model")
            if engine == "codex" and record.get("type") == "turn_context"
            else mapping(record.get("data", record)).get("modelID")
            if engine == "opencode"
            else None
        )
        if isinstance(identity, str) and identity:
            if identity != self.session and (self.session is not None or self.observations):
                self.observations, self.invalid = [], 0
                self.error_observations = 0
                self.model = None
            self.session = identity
        if isinstance(current_model, str) and current_model:
            if self.model is not None and current_model != self.model:
                self.observations, self.invalid = [], 0
                self.error_observations = 0
            self.model = current_model
        normalizers = {"claude": self._claude, "codex": self._codex, "opencode": self._opencode}
        normalizers[engine](record, order)

    def _claude(self, record, order):
        if record.get("type") != "assistant":
            return
        message = mapping(record.get("message"))
        at = _time(record.get("timestamp"))
        if not mapping(message.get("usage")) or at is None:
            self.invalid += 1
            return
        usage = next(
            (
                e
                for e in claude_code._translate(record, at, None, False, None)
                if isinstance(e, TokenUsage)
            ),
            None,
        )
        if usage is None or not isinstance(message.get("id"), str) or not message["id"]:
            self.invalid += 1
            return
        tokens = usage.tokens
        values = (tokens.new_input, tokens.cache_write, tokens.cache_read, tokens.output)
        if any(type(x) is not int or x < 0 for x in values):
            self.invalid += 1
            return
        self.observations.append(
            (at, order, message["id"], (tokens.output, at, order), sum(values[:3]))
        )

    def _codex(self, record, order):
        payload = mapping(record.get("payload"))
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            return
        at = _time(record.get("timestamp"))
        info = mapping(payload.get("info"))
        usage = next(
            (
                e
                for e in codex._request_usage(payload, mapping(info.get("total_token_usage")), at)
                if isinstance(e, RequestUsage)
            ),
            None,
        )
        if (
            usage is None
            or usage.cumulative is None
            or any(type(x) is not int or x < 0 for x in usage.cumulative)
        ):
            self.invalid += 1
            return
        self.observations.append(
            (at, order, usage.cumulative, None, usage.tokens.new_input + usage.tokens.cache_read)
        )

    def _opencode(self, record, order):
        # Extractors may provide a row with decoded data or the message itself.
        data = record.get("data", record)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                self.invalid += 1
                return
        data = mapping(data)
        if data.get("role") != "assistant":
            return
        times = mapping(data.get("time"))
        if times.get("completed") is None:
            return
        at = _time(times.get("created"))
        identity = record.get("id") or data.get("id")
        tokens = mapping(data.get("tokens"))
        cache = mapping(tokens.get("cache"))
        values = (tokens.get("input"), cache.get("read"), cache.get("write"), tokens.get("output"))
        if (
            at is None
            or not isinstance(identity, str)
            or not identity
            or any(type(x) is not int or x < 0 for x in values)
        ):
            self.invalid += 1
            return
        self.observations.append(
            (at, order, identity, (_time(times.get("completed")) or at, order), sum(values[:3]))
        )

    def summary(self, now):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must have a timezone")
        observations = sorted(self.observations, key=lambda x: (x[0], x[1]))
        engine, invalid, duplicates = self.engine, self.invalid, 0
        future = sum(at >= now for at, *_ in observations)
        observations = [item for item in observations if item[0] < now]
        if engine in ("claude", "opencode"):
            unique = {}
            for item in observations:
                old = unique.get(item[2])
                if old:
                    duplicates += 1
                    chosen = item if item[3] > old[3] else old
                    unique[item[2]] = (
                        min(item[0], old[0]),
                        chosen[1],
                        chosen[2],
                        chosen[3],
                        chosen[4],
                    )
                else:
                    unique[item[2]] = item
            observations = sorted(unique.values(), key=lambda x: (x[0], x[1]))
        else:
            unique = []
            previous = None
            for item in observations:
                if item[2] == previous:
                    duplicates += 1
                    continue
                unique.append(item)
                previous = item[2]
            observations = unique
        recent = [x for x in observations if x[0] >= now - timedelta(hours=1)]
        previous = [
            x for x in observations if now - timedelta(hours=2) <= x[0] < now - timedelta(hours=1)
        ]
        return {
            "context_tokens": observations[-1][4] if observations else None,
            "baseline_tokens": observations[0][4] if observations else None,
            "calls_1h": len(recent) if observations and not invalid else None,
            "input_tokens_1h": sum(x[4] for x in recent) if observations and not invalid else None,
            "avg_context_1h": sum(x[4] for x in recent) / len(recent)
            if recent and not invalid
            else None,
            "previous_calls_1h": len(previous) if observations and not invalid else None,
            "invalid": invalid,
            "error_observations": self.error_observations,
            "future": future,
            "duplicates": duplicates,
            "last_at": _iso(observations[-1][0]) if observations else None,
            "session_id": self.session,
        }


def summarize_requests(engine, records, now):
    """Fold one complete session using the incremental collector's normalizer."""
    accumulator = RequestAccumulator(engine)
    for record in records:
        accumulator.add(record)
    return accumulator.summary(now)


def _accounts(accounts):
    result = []
    for a in accounts:
        if (
            not isinstance(a, dict)
            or not {
                "account",
                "engine",
                "observed_at",
                "used_percent",
                "resets_at",
                "window_minutes",
                "status",
            }
            <= a.keys()
            or not isinstance(a.get("account"), str)
            or not a["account"]
            or a.get("engine") not in ("claude", "codex", "opencode")
            or a.get("status") not in ("ok", "unknown", "stale")
        ):
            raise ValueError("invalid account")
        at = _time(a.get("observed_at"))
        reset = _time(a.get("resets_at")) if a.get("resets_at") is not None else None
        percent, minutes = a.get("used_percent"), a.get("window_minutes")
        if (
            at is None
            or (a.get("resets_at") is not None and reset is None)
            or (
                percent is not None
                and (
                    type(percent) not in (int, float)
                    or not math.isfinite(percent)
                    or not 0 <= percent <= 100
                )
            )
            or (minutes is not None and (type(minutes) is not int or minutes <= 0))
        ):
            raise ValueError("invalid account values")
        result.append(
            {
                "account": a["account"],
                "engine": a["engine"],
                "observed_at": _iso(at),
                "used_percent": percent,
                "resets_at": _iso(reset) if reset else None,
                "window_minutes": minutes,
                "status": a["status"],
            }
        )
    return result


def _empty_row(window):
    return {
        "lane": window.get("lane", ""),
        "engine": window.get("engine"),
        "account": window.get("account"),
        "session_id": window.get("session_id"),
        "context_tokens": None,
        "baseline_tokens": None,
        "calls_1h": None,
        "avg_context_1h": None,
        "input_tokens_1h": None,
        "previous_calls_1h": None,
        "observed_at": None,
        "status": "unknown",
        "reason": None,
    }


def snapshot(windows, accounts=(), now=None, *, stale_after=3600):
    """Build a snapshot from windows containing records/messages and completeness."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must have a timezone")
    rows = []
    coverage = {
        "windows": len(windows),
        "invalid": 0,
        "error_observations": 0,
        "future": 0,
        "duplicates": 0,
        "incomplete": 0,
        "stale": 0,
        "bytes": 0,
        "rows": 0,
    }
    for window in windows:
        engine = window.get("engine")
        row = _empty_row(window)
        if window.get("_excluded_source_count"):
            row["excluded_source_count"] = window["_excluded_source_count"]
            row["excluded_source_reason"] = window["_excluded_source_reason"]
        coverage["bytes"] += window.get("_bytes", 0)
        records = window.get("records", window.get("messages", []))
        coverage["rows"] += window.get("_rows", len(records))
        if engine not in ("claude", "codex", "opencode"):
            row["reason"] = window.get("_error") or "unknown_engine"
        else:
            result = window.get("_summary") or summarize_requests(engine, records, now)
            for key in ("invalid", "error_observations", "future", "duplicates"):
                coverage[key] += result[key]
            for key in (
                "context_tokens",
                "baseline_tokens",
                "calls_1h",
                "avg_context_1h",
                "input_tokens_1h",
                "previous_calls_1h",
            ):
                row[key] = result[key]
            row["observed_at"] = result["last_at"]
            if result["session_id"]:
                row["session_id"] = result["session_id"]
            if not window.get("complete", True) or window.get("_error") or result["invalid"]:
                row["reason"] = window.get("_error") or "incomplete_source"
                for key in (
                    "baseline_tokens",
                    "calls_1h",
                    "avg_context_1h",
                    "input_tokens_1h",
                    "previous_calls_1h",
                ):
                    row[key] = None
            elif not result["last_at"]:
                row["reason"] = "no_usage"
            elif (now - _time(result["last_at"])).total_seconds() > stale_after:
                row["status"], row["reason"] = "stale", "last_usage_stale"
                coverage["stale"] += 1
            else:
                row["status"] = "ok"
        if row["status"] == "unknown":
            coverage["incomplete"] += 1
        rows.append(row)
    return {
        "schema_version": 1,
        "observed_at": _iso(now),
        "windows": rows,
        "accounts": _accounts(accounts),
        "coverage": coverage,
    }


def collect_inventory(
    inventory, now=None, *, max_bytes=16_000_000, max_rows=100_000, stale_after=3600
):
    """Read only supplied JSONL paths; opencode messages come from the caller."""
    if max_bytes <= 0 or max_rows <= 0:
        raise ValueError("limits must be positive")
    windows = []
    for original in inventory["windows"]:
        w = dict(original)
        if w.get("engine") not in ("claude", "codex", "opencode"):
            windows.append(w)
            continue
        if "records" in w or "messages" in w:
            records = w.get("records", w.get("messages"))
            if len(records) > max_rows:
                w["records"], w["_error"] = records[:max_rows], "row_limit"
            windows.append(w)
            continue
        records, count, error, matched = [], 0, None, False
        for path in w.get("source_paths", []):
            try:
                with open(path, "rb") as stream:
                    while True:
                        line = stream.readline(max_bytes - count + 1)
                        if not line:
                            break
                        count += len(line)
                        if count > max_bytes:
                            error = "byte_limit"
                            break
                        if len(records) >= max_rows:
                            error = "row_limit"
                            break
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeError):
                            error = "invalid_json"
                            continue
                        if isinstance(record, dict):
                            kind = record.get("type")
                            is_match = (
                                w["engine"] == "codex"
                                and (
                                    kind == "session_meta"
                                    or (
                                        kind == "event_msg"
                                        and mapping(record.get("payload")).get("type")
                                        == "token_count"
                                    )
                                )
                            ) or (w["engine"] == "claude" and kind == "assistant")
                            if is_match:
                                matched = True
                            records.append(record)
                        else:
                            error = "invalid_json"
            except OSError:
                error = "source_unreadable"
            if error in ("byte_limit", "row_limit"):
                break
        w["records"], w["_bytes"] = records, count
        if not w.get("source_paths"):
            error = "source_missing"
        elif not matched and not error:
            error = "no_matching_source"
        if error:
            w["_error"] = error
        windows.append(w)
    return snapshot(windows, inventory.get("accounts", ()), now, stale_after=stale_after)


# The caller may supply already-sanitized aggregates in customer_value.records.
# No collector is run here; every accepted record carries its own evidence.
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


def _value_table(data, cell):
    now = _time(data.get("observed_at"))
    if now is None:
        return "\n\n고객 가치당 토큰: 기간 미확인\n"
    kst = timezone(timedelta(hours=9))
    local_now = now.astimezone(kst)
    payload = data.get("customer_value")
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if isinstance(payload, dict) and payload.get("period_date") is not None:
        try:
            requested = datetime.strptime(payload["period_date"], "%Y-%m-%d").date()
            if requested not in (local_now.date(), local_now.date() - timedelta(days=1)):
                raise ValueError()
            start = start.replace(year=requested.year, month=requested.month, day=requested.day)
        except (ValueError, TypeError):
            return "\n\n고객 가치당 토큰: 기간 미확인\n"
    day_end = start + timedelta(days=1)
    records = payload.get("records", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        records = []
    lines = [f"\n\n고객 가치당 토큰: {start.date()} KST 달력일 (끝 제외)", "| 고객 가치당 토큰 | 값 · 기간 · 근거 |", "| --- | --- |"]
    lanes = [r.get("lane") for r in data.get("windows", []) if isinstance(r.get("lane"), str)]
    valid = {}
    for lane in dict.fromkeys(lanes):
        missing = []
        for metric, label, unit, kind in _VALUE_METRICS:
            matches = []
            for record in records:
                if not isinstance(record, dict) or record.get("lane") != lane or record.get("metric_id") != metric:
                    continue
                begin, end = _time(record.get("period_start")), _time(record.get("period_end"))
                # Other days and intervals outside this day cannot contaminate this table.
                if begin == start and end is not None and start < end <= min(day_end, now):
                    matches.append(record)
            value = "미수집 또는 연결 미확인"
            if len(matches) > 1:
                value = "상충 또는 중복: 미확인"
            elif matches:
                record = matches[0]
                coverage = record.get("coverage")
                observed = _time(record.get("observed_at"))
                end = _time(record.get("period_end"))
                number = record.get("value")
                valid_number = ((type(number) is int and number >= 0) or
                                (unit in ("seconds", "currency") and type(number) is float
                                 and math.isfinite(number) and number >= 0))
                complete = (isinstance(coverage, dict) and all(coverage.get(k) is True for k in
                            ("complete", "customer_scope", "period", "deduplicated", "lane_attribution")))
                evidence = (isinstance(record.get("source_ref"), str) and bool(record["source_ref"].strip())
                            and record.get("source_kind") == kind and record.get("unit") == unit
                            and observed is not None and end <= observed <= now)
                if metric == "production_features":
                    evidence = (evidence and all(isinstance(record.get(k), str) and record[k].strip()
                                                for k in ("production_promotion_ref", "customer_walk_ref")))
                if metric == "contract_revenue":
                    evidence = (evidence and isinstance(record.get("currency_code"), str)
                                and bool(record["currency_code"].strip()))
                period = f"{cell(record.get('period_start'))} ~ {cell(record.get('period_end'))} (끝 제외; " + ("하루 전체" if end == day_end else "부분 기간") + ")"
                if record.get("status") == "complete" and complete and evidence and valid_number:
                    value = f"{number} {unit}; {period}; 관측 {cell(record['observed_at'])}; 근거 {cell(record['source_ref'])}"
                    if metric == "production_features":
                        value += f"; 승격 {cell(record['production_promotion_ref'])}; 고객 걸음 {cell(record['customer_walk_ref'])}"
                    if metric == "contract_revenue":
                        value += f"; 통화 {cell(record['currency_code'])}"
                    valid[(lane, metric)] = (number, _time(record.get("period_start")), end, record)
                else:
                    value = f"미확인: 입력의 완전성·범위·기간·귀속 또는 근거 미확인; {period}"
            if not matches:
                missing.append(f"{label} ({kind})")
            else:
                lines.append(f"| {cell(lane)} · {label} | {value} |")
        if missing:
            lines.append(f"| {cell(lane)} · 모름: 미수집 또는 연결 미확인 | {cell(', '.join(missing))}; 필요: 각 이벤트·기간·창 귀속 및 출처 |")
    daily = data.get("daily_results") or {}
    if daily.get("error"):
        merge = f"미확인: {cell(daily['error'])}"
    elif daily:
        merge = f"미확인: {cell(daily['tokens_per_merge']['reason'])}; 분자 하루 전체 토큰, 분모 머지; 기간 {cell(daily['window_start'])} ~ {cell(daily['window_end_exclusive'])} (끝 제외)"
    else:
        merge = "미확인: 하루 전체 토큰·머지와 동일 기간의 출처 필요"
    merge = f"전역 전체 범위 (창별 값 아님); {merge}"
    lines.extend(["", "| 창 | 머지당 토큰 | 피쳐당 토큰 | KPI당 토큰 |", "| --- | --- | --- | --- |"])
    def ratio(lane, metric):
        numerator = valid.get((lane, "window_tokens"))
        denominator = valid.get((lane, metric))
        if not numerator or not denominator or numerator[1:3] != denominator[1:3]:
            return "모름: 동일 기간·창의 토큰과 지표 및 출처 필요"
        n, begin, end, nr = numerator
        d, _, _, dr = denominator
        period = f"{cell(nr['period_start'])} ~ {cell(nr['period_end'])} (끝 제외)"
        detail = f"분자 {n} tokens ({cell(nr['source_ref'])}); 분모 {d} {cell(dr['unit'])} ({cell(dr['source_ref'])}); {period}"
        if metric == "production_features":
            detail += f"; 승격 {cell(dr['production_promotion_ref'])}; 고객 걸음 {cell(dr['customer_walk_ref'])}"
        if metric == "contract_revenue":
            detail += f"; 통화 {cell(dr['currency_code'])}"
        return (f"계산 불가: 분모 0; {detail}" if d == 0 else f"{n / d:.2f} tokens/{cell(dr['unit'])}; {detail}")
    for lane in dict.fromkeys(lanes):
        kpi_metrics = [(metric, label) for metric, label, _, _ in _VALUE_METRICS
                       if metric not in ("window_tokens", "production_features", "signups", "account_connections", "first_backtests", "payments")]
        kpis = [f"{label}: {ratio(lane, metric)}" for metric, label in kpi_metrics if (lane, metric) in valid]
        absent = [label for metric, label in kpi_metrics if (lane, metric) not in valid]
        if absent:
            kpis.append(f"{cell(', '.join(absent))}: 모름; 각 이벤트·동일 기간·창 귀속 및 출처 필요")
        lines.append(f"| {cell(lane)} | {merge} | {ratio(lane, 'production_features')} | {'<br>'.join(kpis)} |")
    lines.append("운영 반영 기능·수정 분모: 운영 배포·승격 기록과 고객 화면 걸음 증거 필요; dev·머지 제외.")
    if not lanes:
        lines.append("| 창별 고객 가치 | 창 미관측; 값 미확인 |")
    lines.append("과거 근거 (11-13 KST): 23:08~23:14 ①~③ 담당 미보유, 23:20 ④⑥ 미수신, 23:24 ⑤ 연결 NO-GO; 23:11 창별 하루 전체 귀속 미확인. 이 회신은 오늘 제품 입력이나 0이 아님.")
    return "\n".join(lines) + "\n"


def _table(data):
    def cell(value):
        return (
            str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
            if value is not None
            else "unknown"
        )

    columns = (
        ("lane", "창"),
        ("engine", "엔진"),
        ("context_tokens", "맥락"),
        ("calls_1h", "1시간 호출"),
        ("avg_context_1h", "평균"),
        ("baseline_tokens", "바닥"),
        ("status", "상태"),
        ("observed_at", "마지막 요청"),
        ("reason", "원인"),
    )
    coverage = json.dumps(data["coverage"], ensure_ascii=False, sort_keys=True)
    counts = {
        engine: sum(r["engine"] == engine for r in data["windows"])
        for engine in ("claude", "codex", "opencode")
    }
    engines = " · ".join(
        f"{engine} {count}창" + ("/미관측" if not count else "") for engine, count in counts.items()
    )
    daily = data.get("daily_results")
    daily_table = ""
    if daily and daily.get("error"):
        daily_table = ("\n\n| 오늘 결과 | 값 · 범위 |\n| --- | --- |\n" +
                       "".join(f"| {label} | 미확인: {cell(daily['error'])} |\n" for label in
                               ("머지당 토큰", "하루 토큰", "머지", "검수", "OpenCode 구현 접수 몫")))
    elif daily:
        share = daily["impl_share"]
        selected = daily["tokens"]["selected_review_outcomes"]
        observed = [item["tokens"] for item in selected["outcomes"] if item["tokens"] is not None]
        ratio = f"{share['ratio'] * 100:.1f}%" if share['ratio'] is not None else ("미확인" if share['denominator'] is None else "미관측 (빈 분모)")
        merges = daily['merges']
        reviews = daily['reviews']
        merge_text = (f"미확인: {cell(merges['reason'])}" if merges['observed'] is None and merges['unreadable_or_unparsed'] is None else f"관측 {cell(merges['observed'])}; 전체 미확인, 읽기/파싱 미확인 {merges['unreadable_or_unparsed']}")
        review_text = (f"미확인: {cell(reviews['reason'])}" if reviews['observed'] is None else f"관측 {reviews['observed']}; 미확인 항목 {reviews['unknown']}, 전체 미확인")
        daily_table = (
            "\n\n| 오늘 결과 | 값 · 범위 |\n| --- | --- |\n"
            f"| 머지당 토큰 | 미확인: {cell(daily['tokens_per_merge']['reason'])} |\n"
            f"| 하루 토큰 | 미확인: {cell(daily['tokens']['reason'])}; 선택 검수 부분합 {sum(observed) if observed else '미관측'} ({len(observed)}/{len(selected['outcomes'])} 결과), {cell(selected['reason'])} |\n"
            f"| 머지 | {merge_text} |\n"
            f"| 검수 | {review_text} |\n"
            f"| OpenCode 구현 접수 몫 | {cell(share['numerator'])}/{cell(share['denominator'])} ({ratio}); 엔진별 {cell(share['engines'])}; 미확인 {cell(share['unknown'])}; {cell(share['basis'])} |\n"
            f"기간 {cell(daily['window_start'])} ~ {cell(daily['window_end_exclusive'])} (끝 제외)\n"
        )
    return (
        "관측시각: "
        + cell(data["observed_at"])
        + "\n\ncoverage: "
        + coverage
        + "\n\n"
        + engines
        + _value_table(data, cell)
        + daily_table
        + "\n\n| "
        + " | ".join(title for _, title in columns)
        + " |\n| "
        + " | ".join("---" for _ in columns)
        + " |\n"
        + "".join(
            "| " + " | ".join(
                "멈춤/셸만" if key == "status" and row["reason"] == "no_active_native"
                else "-" if key == "reason" and row[key] is None
                else f"{row[key]:.2f}" if key == "avg_context_1h" and row[key] is not None
                else cell(row[key]) for key, _ in columns
            ) + " |\n"
            for row in data["windows"]
        )
    )


def fortress_line(data):
    rows = data["windows"]
    unknown = sum(row["status"] != "ok" for row in rows)
    over = sum(row["context_tokens"] > 200000 for row in rows if row["context_tokens"] is not None)
    # A missing summand must never silently become zero in a fleet total.
    calls = (
        sum(row["calls_1h"] for row in rows)
        if all(row["calls_1h"] is not None for row in rows)
        else None
    )
    inputs = (
        sum(row["input_tokens_1h"] for row in rows)
        if all(row["input_tokens_1h"] is not None for row in rows)
        else None
    )
    average = round(inputs / calls, 2) if calls and inputs is not None else None
    over_text = (
        str(over) if all(row["context_tokens"] is not None for row in rows) else f"{over}+unknown"
    )
    parts = (
        data["observed_at"],
        f"창 {len(rows)}",
        f"20만 초과 {over_text}",
        f"1h호출 {calls if calls is not None else 'unknown'}",
        f"평균 {average if average is not None else 'unknown'}",
        f"미확인 {unknown}",
    )
    text = " · ".join(parts)
    return text.replace("\n", " ").replace("\r", " ") + "\n"


def write_snapshot(data, directory):
    """Publish a complete generation through one atomic current symlink switch.

    Readers needing a multi-file transaction should resolve current once (or use
    read_snapshot_bundle). The three convenience links follow that pointer.
    Interrupted writes leave the previous complete generation available.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "snapshot.json": json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        "LATEST.md": _table(data),
        "fortress-line.txt": fortress_line(data),
    }
    with exclusive_lock(directory / ".publish.lock"):
        generation = Path(tempfile.mkdtemp(prefix=".generation-", dir=directory))
        for name, content in outputs.items():
            with (generation / name).open("w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        # Preserve complete legacy regular-file outputs while migrating the
        # convenience paths to symlinks. A failed first switch still has a
        # complete previous generation, just like later publications do.
        if not (directory / "current").exists() and all(
            (directory / name).is_file() for name in outputs
        ):
            legacy = Path(tempfile.mkdtemp(prefix=".generation-legacy-", dir=directory))
            for name in outputs:
                with (legacy / name).open("wb") as stream:
                    stream.write((directory / name).read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
            temporary = legacy / "current.link"
            temporary.symlink_to(legacy.name)
            os.replace(temporary, directory / "current")
        # All files exist before any publication pointer is changed.
        for name in outputs:
            link = directory / name
            if link.is_symlink() and os.readlink(link) == "current/" + name:
                continue
            temporary = generation / (name + ".link")
            temporary.symlink_to("current/" + name)
            os.replace(temporary, link)
        temporary = generation / "current.link"
        temporary.symlink_to(generation.name)
        os.replace(temporary, directory / "current")
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return generation.name


def read_snapshot_bundle(directory):
    """Pin a publication generation before opening any of its three files."""
    directory = Path(directory)
    with exclusive_lock(directory / ".publish.lock"):
        generation = (directory / "current").resolve(strict=True)
        return {
            name: (generation / name).read_text(encoding="utf-8")
            for name in ("snapshot.json", "LATEST.md", "fortress-line.txt")
        }
