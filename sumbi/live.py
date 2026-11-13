"""Bounded, read-only request snapshots from explicitly supplied sources."""
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sumbi.core.values import mapping, timestamp
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
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def summarize_requests(engine, records, now):
    """Fold one complete session; duplicate updates retain the first request time."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must have a timezone')
    if engine not in ('claude', 'codex', 'opencode'):
        raise ValueError('unsupported engine')
    observations = []
    invalid = future = duplicates = 0
    session = model = None
    for order, record in enumerate(records):
        if not isinstance(record, dict):
            invalid += 1
            continue
        identity = (record.get('sessionId') if engine == 'claude' else
                    mapping(record.get('payload')).get('id') if engine == 'codex' and record.get('type') == 'session_meta' else
                    record.get('session_id') if engine == 'opencode' else None)
        if identity:
            if session is not None and identity != session:
                observations, invalid = [], 0
            session = identity
        current_model = (mapping(record.get('message')).get('model') if engine == 'claude' else
                         mapping(record.get('payload')).get('model') if engine == 'codex' and record.get('type') == 'turn_context' else
                         mapping(record.get('data', record)).get('modelID') if engine == 'opencode' else None)
        if current_model:
            if model is not None and current_model != model:
                observations, invalid = [], 0
            model = current_model
        if engine == 'claude':
            if record.get('type') != 'assistant':
                continue
            message = mapping(record.get('message'))
            at = _time(record.get('timestamp'))
            if not mapping(message.get('usage')) or at is None:
                invalid += 1
                continue
            usage = next((e for e in claude_code._translate(record, at, None, False, None)
                          if isinstance(e, TokenUsage)), None)
            if usage is None or not message.get('id'):
                invalid += 1
                continue
            tokens = usage.tokens
            values = (tokens.new_input, tokens.cache_write, tokens.cache_read, tokens.output)
            if any(type(x) is not int or x < 0 for x in values):
                invalid += 1
                continue
            observations.append((at, order, message['id'], (tokens.output, at, order), sum(values[:3])))
        elif engine == 'codex':
            payload = mapping(record.get('payload'))
            if record.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                continue
            at = _time(record.get('timestamp'))
            info = mapping(payload.get('info'))
            usage = next((e for e in codex._request_usage(payload, mapping(info.get('total_token_usage')), at)
                          if isinstance(e, RequestUsage)), None)
            if usage is None or usage.cumulative is None or any(type(x) is not int or x < 0 for x in usage.cumulative):
                invalid += 1
                continue
            observations.append((at, order, usage.cumulative, None, usage.tokens.new_input + usage.tokens.cache_read))
        else:
            # Extractors may provide a row with decoded data or the message itself.
            data = record.get('data', record)
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    invalid += 1
                    continue
            data = mapping(data)
            if data.get('role') != 'assistant':
                continue
            times = mapping(data.get('time'))
            if times.get('completed') is None:
                continue
            at = _time(times.get('created'))
            identity = record.get('id') or data.get('id')
            tokens = mapping(data.get('tokens'))
            cache = mapping(tokens.get('cache'))
            values = (tokens.get('input'), cache.get('read'), cache.get('write'), tokens.get('output'))
            if at is None or not isinstance(identity, str) or not identity or any(type(x) is not int or x < 0 for x in values):
                invalid += 1
                continue
            observations.append((at, order, identity, (_time(times.get('completed')) or at, order), sum(values[:3])))
    observations.sort(key=lambda x: (x[0], x[1]))
    future = sum(at >= now for at, *_ in observations)
    observations = [item for item in observations if item[0] < now]
    if engine in ('claude', 'opencode'):
        unique = {}
        for item in observations:
            old = unique.get(item[2])
            if old:
                duplicates += 1
                chosen = item if item[3] > old[3] else old
                unique[item[2]] = (min(item[0], old[0]), chosen[1], chosen[2], chosen[3], chosen[4])
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
    previous = [x for x in observations if now - timedelta(hours=2) <= x[0] < now - timedelta(hours=1)]
    return {'context_tokens': observations[-1][4] if observations else None,
            'baseline_tokens': observations[0][4] if observations else None,
            'calls_1h': len(recent) if observations and not invalid else None,
            'input_tokens_1h': sum(x[4] for x in recent) if observations and not invalid else None,
            'avg_context_1h': sum(x[4] for x in recent) / len(recent) if recent and not invalid else None,
            'previous_calls_1h': len(previous) if observations and not invalid else None,
            'invalid': invalid, 'future': future, 'duplicates': duplicates,
            'last_at': _iso(observations[-1][0]) if observations else None, 'session_id': session}


def _accounts(accounts):
    result = []
    for a in accounts:
        if not isinstance(a, dict) or not {'account', 'engine', 'observed_at', 'used_percent', 'resets_at', 'window_minutes', 'status'} <= a.keys() or not isinstance(a.get('account'), str) or not a['account'] or a.get('engine') not in ('claude', 'codex', 'opencode') or a.get('status') not in ('ok', 'unknown', 'stale'):
            raise ValueError('invalid account')
        at = _time(a.get('observed_at'))
        reset = _time(a.get('resets_at')) if a.get('resets_at') is not None else None
        percent, minutes = a.get('used_percent'), a.get('window_minutes')
        if at is None or (a.get('resets_at') is not None and reset is None) or (percent is not None and (type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100)) or (minutes is not None and (type(minutes) is not int or minutes <= 0)):
            raise ValueError('invalid account values')
        result.append({'account': a['account'], 'engine': a['engine'], 'observed_at': _iso(at), 'used_percent': percent, 'resets_at': _iso(reset) if reset else None, 'window_minutes': minutes, 'status': a['status']})
    return result


def snapshot(windows, accounts=(), now=None, *, stale_after=3600):
    """Build a snapshot from windows containing records/messages and completeness."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must have a timezone')
    rows = []
    coverage = {'windows': len(windows), 'invalid': 0, 'future': 0, 'duplicates': 0, 'incomplete': 0, 'stale': 0, 'bytes': 0, 'rows': 0}
    for window in windows:
        engine = window.get('engine')
        row = {'lane': window.get('lane', ''), 'engine': engine, 'account': window.get('account'), 'session_id': window.get('session_id'), 'context_tokens': None, 'baseline_tokens': None, 'calls_1h': None, 'avg_context_1h': None, 'input_tokens_1h': None, 'previous_calls_1h': None, 'observed_at': None, 'status': 'unknown', 'reason': None}
        coverage['bytes'] += window.get('_bytes', 0)
        records = window.get('records', window.get('messages', []))
        coverage['rows'] += len(records)
        if engine not in ('claude', 'codex', 'opencode'):
            row['reason'] = 'unknown_engine'
        else:
            result = summarize_requests(engine, records, now)
            for key in ('invalid', 'future', 'duplicates'):
                coverage[key] += result[key]
            for key in ('context_tokens', 'baseline_tokens', 'calls_1h', 'avg_context_1h', 'input_tokens_1h', 'previous_calls_1h'):
                row[key] = result[key]
            row['observed_at'] = result['last_at']
            if result['session_id']:
                row['session_id'] = result['session_id']
            if not window.get('complete', True) or window.get('_error') or result['invalid']:
                row['reason'] = window.get('_error') or 'incomplete_source'
                for key in ('baseline_tokens', 'calls_1h', 'avg_context_1h', 'input_tokens_1h', 'previous_calls_1h'):
                    row[key] = None
            elif not result['last_at']:
                row['reason'] = 'no_usage'
            elif (now - _time(result['last_at'])).total_seconds() > stale_after:
                row['status'], row['reason'] = 'stale', 'last_usage_stale'
                coverage['stale'] += 1
            else:
                row['status'] = 'ok'
        if row['status'] == 'unknown':
            coverage['incomplete'] += 1
        rows.append(row)
    return {'schema_version': 1, 'observed_at': _iso(now), 'windows': rows, 'accounts': _accounts(accounts), 'coverage': coverage}


def collect_inventory(inventory, now=None, *, max_bytes=16_000_000, max_rows=100_000, stale_after=3600):
    """Read only supplied JSONL paths; opencode messages come from the caller."""
    if max_bytes <= 0 or max_rows <= 0:
        raise ValueError('limits must be positive')
    windows = []
    for original in inventory['windows']:
        w = dict(original)
        if w.get('engine') not in ('claude', 'codex', 'opencode'):
            windows.append(w)
            continue
        if 'records' in w or 'messages' in w:
            records = w.get('records', w.get('messages'))
            if len(records) > max_rows:
                w['records'], w['_error'] = records[:max_rows], 'row_limit'
            windows.append(w)
            continue
        records, count, error, matched = [], 0, None, False
        for path in w.get('source_paths', []):
            try:
                with open(path, 'rb') as stream:
                    while True:
                        line = stream.readline(max_bytes - count + 1)
                        if not line:
                            break
                        count += len(line)
                        if count > max_bytes:
                            error = 'byte_limit'
                            break
                        if len(records) >= max_rows:
                            error = 'row_limit'
                            break
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeError):
                            error = 'invalid_json'
                            continue
                        if isinstance(record, dict):
                            kind = record.get('type')
                            is_match = (w['engine'] == 'codex' and (kind == 'session_meta' or (kind == 'event_msg' and mapping(record.get('payload')).get('type') == 'token_count'))) or (w['engine'] == 'claude' and kind == 'assistant')
                            if is_match:
                                matched = True
                            records.append(record)
                        else:
                            error = 'invalid_json'
            except OSError:
                error = 'source_unreadable'
            if error in ('byte_limit', 'row_limit'):
                break
        w['records'], w['_bytes'] = records, count
        if not w.get('source_paths'):
            error = 'source_missing'
        elif not matched and not error:
            error = 'no_matching_source'
        if error:
            w['_error'] = error
        windows.append(w)
    return snapshot(windows, inventory.get('accounts', ()), now, stale_after=stale_after)


def _table(data):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ') if value is not None else 'unknown'
    keys = ('lane', 'engine', 'account', 'session_id', 'context_tokens', 'calls_1h', 'avg_context_1h', 'baseline_tokens', 'previous_calls_1h', 'input_tokens_1h', 'observed_at', 'status', 'reason')
    return '| ' + ' | '.join(keys) + ' |\n| ' + ' | '.join('---' for _ in keys) + ' |\n' + ''.join('| ' + ' | '.join(cell(row[k]) for k in keys) + ' |\n' for row in data['windows'])


def write_snapshot(data, directory):
    """Atomically replace each output file in the destination directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {'snapshot.json': json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', 'LATEST.md': _table(data), 'fortress-line.txt': _table(data)}
    for name, content in outputs.items():
        fd, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
