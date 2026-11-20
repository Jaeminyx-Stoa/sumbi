"""Bounded, read-only request snapshots from explicitly supplied sources."""

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sumbi.measure.customer_value import COMPANY, _VALUE_METRICS, load_customer_value
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


def _value_table(data, cell, value_dir=None):
    now = _time(data.get("observed_at"))
    if now is None:
        return "\n\n고객 가치당 토큰: 기간 미확인\n"
    day, values, reason = load_customer_value(value_dir, now, data.get("customer_value"))
    lines = [f"\n\n고객 가치당 토큰: {day} KST 하루 (끝 제외)",
             "| 고객 가치당 토큰 | 값 · 기간 · 근거 |", "| --- | --- |"]
    def describe(item):
        if "value" not in item:
            return "모름: " + cell(item.get("reason") or reason or "이 칸 기록 없음")
        value = item["value"]
        accounts = item.get("accounts")
        shown = f"{value} {item['unit']}"
        if item['unit'] == 'count' and type(accounts) is int and accounts >= 0:
            shown = f"{value}행/{accounts}계좌"
        text = (f"{shown}; {cell(item['start'])} ~ {cell(item['end'])} (끝 제외); "
                f"관측 {cell(item['observed_at'])}; 근거 {cell(item['source_ref'])}")
        if item.get('currency_code'):
            text += f"; 통화 {cell(item['currency_code'])}"
        if item.get('production_promotion_ref') and item.get('customer_walk_ref'):
            text += (f"; 승격 {cell(item['production_promotion_ref'])}; "
                     f"고객 걸음 {cell(item['customer_walk_ref'])}")
        if isinstance(item.get('caveat'), str) and item['caveat'].strip():
            text += f"; 주의 {cell(item['caveat'])}"
        return text
    for key, label, _, _ in COMPANY:
        lines.append(f"| 회사 전체 · {label} | {describe(values.get((None, key), {}))} |")
    lanes = dict.fromkeys(row['lane'] for row in data.get('windows', [])
                          if isinstance(row.get('lane'), str))
    absent = 0
    for lane in lanes:
        matching = [(key, label, unit) for key, label, unit, _ in _VALUE_METRICS
                    if (lane, key) in values]
        if not matching:
            absent += 1
            continue
        for key, label, unit in matching:
            item = values[(lane, key)]
            lines.append(f"| {cell(lane)} · {label} | {describe(item)} |")
            numerator = values.get((lane, 'window_tokens'), {})
            if key != 'window_tokens' and 'value' in item and 'value' in numerator:
                n, d = numerator['value'], item['value']
                try:
                    ratio = "계산 불가: 분모 0" if d == 0 else f"{n / d:.2f} tokens/{unit}"
                except OverflowError:
                    ratio = "계산 불가: 값 범위 초과"
                lines.append(
                    f"| {cell(lane)} · {label}당 토큰 | {ratio}; "
                    f"분자 근거 {cell(numerator['source_ref'])}; "
                    f"분모 근거 {cell(item['source_ref'])}; "
                    f"{cell(item['start'])} ~ {cell(item['end'])} (끝 제외) |")
    if absent or not lanes:
        lines.append(f"창별 귀속: 모름 — 귀속 기록 없음({absent}창)" if lanes else "창별 귀속: 모름 — 창 미관측")
    return "\n".join(lines) + "\n"


def _daily_table(data, cell):
    try:
        daily = data.get("daily_results")
        daily_table = ""
        if daily and daily.get("error"):
            daily_table = ("\n\n| 오늘 결과 | 값 · 범위 |\n| --- | --- |\n" +
                           "".join(f"| {label} | 미확인: {cell(daily['error'])} |\n" for label in
                                   ("하루 토큰", "머지", "검수", "OpenCode 구현 접수 몫")))
        elif daily:
            share = daily["impl_share"]
            selected = daily["tokens"]["selected_review_outcomes"]
            observed = [item["tokens"] for item in selected["outcomes"]
                        if item["tokens"] is not None]
            ratio = (f"{share['ratio'] * 100:.1f}%" if share['ratio'] is not None
                     else "미확인" if share['denominator'] is None else "미관측 (빈 분모)")
            merges = daily['merges']
            reviews = daily['reviews']
            merge_text = (f"미확인: {cell(merges['reason'])}"
                          if merges['observed'] is None and merges['unreadable_or_unparsed'] is None
                          else f"관측 {cell(merges['observed'])}; 전체 미확인, 읽기/파싱 미확인 "
                               f"{merges['unreadable_or_unparsed']}")
            review_text = (f"미확인: {cell(reviews['reason'])}" if reviews['observed'] is None
                           else f"관측 {reviews['observed']}; "
                                f"미확인 항목 {reviews['unknown']}, 전체 미확인")
            daily_table = (
                "\n\n| 오늘 결과 | 값 · 범위 |\n| --- | --- |\n"
                f"| 하루 토큰 | 미확인: {cell(daily['tokens']['reason'])}; 선택 검수 부분합 "
                f"{sum(observed) if observed else '미관측'} "
                f"({len(observed)}/{len(selected['outcomes'])} 결과), {cell(selected['reason'])} |\n"
                f"| 머지 | {merge_text} |\n"
                f"| 검수 | {review_text} |\n"
                f"| OpenCode 구현 접수 몫 | {cell(share['numerator'])}/"
                f"{cell(share['denominator'])} ({ratio}); 엔진별 {cell(share['engines'])}; "
                f"미확인 {cell(share['unknown'])}; {cell(share['basis'])} |\n"
                f"기간 {cell(daily['window_start'])} ~ {cell(daily['window_end_exclusive'])} (끝 제외)\n"
            )
    except (AttributeError, KeyError, TypeError, ValueError):
        daily_table = "\n\n오늘 결과: 미확인 — 이전 입력 형식\n"
    return daily_table


def _table(data, value_dir=None):
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
    daily_table = _daily_table(data, cell)
    return (
        "관측시각: "
        + cell(data["observed_at"])
        + "\n\ncoverage: "
        + coverage
        + "\n\n"
        + engines
        + _value_table(data, cell, value_dir)
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


def write_snapshot(data, directory, value_dir=None):
    """Publish a complete generation through one atomic current symlink switch.

    Readers needing a multi-file transaction should resolve current once (or use
    read_snapshot_bundle). The three convenience links follow that pointer.
    Interrupted writes leave the previous complete generation available.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "snapshot.json": json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        "LATEST.md": _table(data, value_dir),
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
