"""Locked, resumable JSONL collection. Cache contains no native record bodies."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sumbi.live import RequestAccumulator, _iso, _time, snapshot
from sumbi.core.local_cache import CollectorBusy, atomic_json, exclusive_lock
from sumbi.measure.live_sources import account_sample, codex_limits


def _encode(acc):
    observations = []
    for at, order, identity, rank, value in acc.observations:
        if acc.engine == "claude":
            rank = [rank[0], _iso(rank[1]), rank[2]]
        elif acc.engine == "opencode":
            rank = [_iso(rank[0]), rank[1]]
        observations.append([_iso(at), order, identity, rank, value])
    return {
        "engine": acc.engine,
        "session": acc.session,
        "model": acc.model,
        "order": acc.order,
        "invalid": acc.invalid,
        "observations": observations,
    }


def _natural(n):
    return type(n) is int and n >= 0


def _decode(state):
    acc = RequestAccumulator(state["engine"])
    if set(state) != {"engine", "session", "model", "order", "invalid", "observations"}:
        raise ValueError("cache_schema")
    if not all(_natural(state[k]) for k in ("order", "invalid")):
        raise ValueError("cache_count")
    if any(state[k] is not None and not isinstance(state[k], str) for k in ("session", "model")):
        raise ValueError("cache_identity")
    acc.session, acc.model = state["session"], state["model"]
    acc.order, acc.invalid = state["order"], state["invalid"]
    for at, order, identity, rank, value in state["observations"]:
        at = _time(at)
        if at is None or not _natural(order) or order >= acc.order or not _natural(value):
            raise ValueError("cache_observation")
        if acc.engine == "codex":
            if (
                not isinstance(identity, list)
                or len(identity) != 4
                or not all(_natural(x) for x in identity)
                or rank is not None
            ):
                raise ValueError("cache_counter")
            identity = tuple(identity)
        else:
            if not isinstance(identity, str) or not identity:
                raise ValueError("cache_request")
            if acc.engine == "claude":
                if (
                    len(rank) != 3
                    or not _natural(rank[0])
                    or _time(rank[1]) is None
                    or not _natural(rank[2])
                ):
                    raise ValueError("cache_rank")
                rank = (rank[0], _time(rank[1]), rank[2])
            else:
                if len(rank) != 2 or _time(rank[0]) is None or not _natural(rank[1]):
                    raise ValueError("cache_rank")
                rank = (_time(rank[0]), rank[1])
        acc.observations.append((at, order, identity, rank, value))
    return acc


class IncrementalReader:
    """One transaction per context. Lock covers load, source reads and atomic save.

    Limits apply to each source scan. Bootstrap streams up to 512 MB / two
    million lines; a capped source remains incomplete and resumes next time.
    An unfinished line is deferred at its starting offset, never persisted raw.
    Metadata-identical sources (including partial lines) incur zero source bytes.
    """

    def __init__(
        self, cache, *, max_bytes=512_000_000, max_rows=2_000_000, max_line_bytes=4_000_000
    ):
        if min(max_bytes, max_rows, max_line_bytes) <= 0:
            raise ValueError("limits must be positive")
        self.path = Path(cache)
        self.limits = max_bytes, max_rows, max_line_bytes
        self.entries = {}
        self.bytes = self.rows = self.recovered = self.bootstraps = 0

    def __enter__(self):
        self.lock = exclusive_lock(str(self.path) + ".lock")
        self.lock.__enter__()
        try:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    set(data) != {"version", "entries"}
                    or data["version"] != 1
                    or not isinstance(data["entries"], dict)
                ):
                    raise ValueError("cache_version")
                for entry in data["entries"].values():
                    _decode(entry["state"])
                    if set(entry) != {
                        "state",
                        "device",
                        "inode",
                        "size",
                        "mtime",
                        "ctime",
                        "offset",
                        "rows",
                        "error",
                        "partial",
                        "rate_limits",
                    }:
                        raise ValueError("cache_entry")
                    if (
                        not all(
                            _natural(entry[k])
                            for k in ("device", "inode", "size", "mtime", "ctime", "offset", "rows")
                        )
                        or entry["offset"] > entry["size"]
                        or type(entry["partial"]) is not bool
                    ):
                        raise ValueError("cache_offset")
                    if entry["error"] not in (
                        None,
                        "invalid_json",
                        "line_limit",
                        "byte_limit",
                        "row_limit",
                    ):
                        raise ValueError("cache_error")
                    if not isinstance(entry["rate_limits"], list):
                        raise ValueError("cache_limits")
                    for rate in entry["rate_limits"]:
                        if not isinstance(rate, dict) or set(rate) != {
                            "observed_at",
                            "used_percent",
                            "window_minutes",
                            "resets_at",
                        }:
                            raise ValueError("cache_limit_shape")
                        for field in ("observed_at", "resets_at"):
                            if rate[field] is not None and _time(rate[field]) is None:
                                raise ValueError("cache_limit_time")
                        percent, minutes = rate["used_percent"], rate["window_minutes"]
                        if percent is not None and (
                            type(percent) not in (int, float) or not 0 <= percent <= 100
                        ):
                            raise ValueError("cache_limit_percent")
                        if minutes is not None and (not _natural(minutes) or minutes == 0):
                            raise ValueError("cache_limit_window")
                self.entries = data["entries"]
            except FileNotFoundError:
                pass
            except (ValueError, KeyError, TypeError, OverflowError):
                self.entries = {}
                self.recovered = 1
            return self
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise

    def __exit__(self, kind, value, trace):
        try:
            if kind is None:
                atomic_json(self.path, {"version": 1, "entries": self.entries})
        finally:
            self.lock.__exit__(kind, value, trace)

    def read(self, path, engine, session_id=None):
        """Return normalized state and coverage, never native JSON records."""
        key = hashlib.sha256(json.dumps([str(path), engine, session_id]).encode()).hexdigest()
        old = self.entries.get(key)
        if old and old["state"]["engine"] != engine:
            old = None
            self.recovered = 1
        try:
            with open(path, "rb") as stream:
                stat = os.fstat(stream.fileno())
                same = old and (old["device"], old["inode"]) == (stat.st_dev, stat.st_ino)
                resume = (
                    same
                    and stat.st_size >= old["size"]
                    and (
                        stat.st_size != old["size"]
                        or (stat.st_mtime_ns, stat.st_ctime_ns) == (old["mtime"], old["ctime"])
                    )
                )
                if resume:
                    entry, acc = dict(old), _decode(old["state"])
                    # Capped sources continue even without metadata changes.
                    if stat.st_size == old["size"] and old["error"] not in (
                        "byte_limit",
                        "row_limit",
                    ):
                        return acc, entry
                else:
                    self.bootstraps += 1
                    acc = RequestAccumulator(engine)
                    entry = {
                        "offset": 0,
                        "rows": 0,
                        "error": None,
                        "partial": False,
                        "rate_limits": [],
                    }
                self._scan(stream, acc, entry, stat)
                self.entries[key] = entry
                return acc, entry
        except OSError:
            return RequestAccumulator(engine), {
                "error": "source_unreadable",
                "partial": False,
                "rows": 0,
                "rate_limits": [],
            }

    def _scan(self, stream, acc, entry, stat):
        max_bytes, max_rows, max_line = self.limits
        engine = acc.engine
        stream.seek(entry["offset"])
        used = rows = 0
        if entry["error"] in ("byte_limit", "row_limit"):
            entry["error"] = None
        entry["partial"] = False
        # Freeze this scan at fstat size; concurrent appends are next pass.
        while stream.tell() < stat.st_size:
            if used >= max_bytes or rows >= max_rows:
                entry["error"] = "byte_limit" if used >= max_bytes else "row_limit"
                break
            begin = stream.tell()
            line = stream.readline(min(max_line + 1, max_bytes - used, stat.st_size - begin))
            used += len(line)
            self.bytes += len(line)
            if not line.endswith(b"\n"):
                if len(line) > max_line:
                    entry["error"] = "line_limit"
                elif stream.tell() < stat.st_size:
                    entry["error"] = "byte_limit"
                else:
                    entry["partial"] = True
                stream.seek(begin)
                break
            rows += 1
            self.rows += 1
            entry["rows"] += 1
            entry["offset"] = stream.tell()
            try:
                record = json.loads(line)
                acc.add(record)
                if engine == "codex" and isinstance(record, dict):
                    rates = codex_limits(record)
                    if rates:
                        entry["rate_limits"] = rates
            except (ValueError, UnicodeError):
                acc.invalid += 1
                entry["error"] = "invalid_json"
        after = os.fstat(stream.fileno())
        if after.st_size < stat.st_size:
            raise OSError("source_changed_during_read")
        entry.update(
            device=stat.st_dev,
            inode=stat.st_ino,
            size=stat.st_size,
            mtime=stat.st_mtime_ns,
            ctime=stat.st_ctime_ns,
            state=_encode(acc),
        )


def collect_incremental(inventory, reader, now=None, *, stale_after=3600):
    """Inventory equivalent of collect_inventory, with measured source bytes."""
    now = now or datetime.now(timezone.utc)
    windows, accounts = [], list(inventory.get("accounts", ()))
    partial = 0
    for original in inventory["windows"]:
        window = dict(original)
        engine = window.get("engine")
        if engine not in ("claude", "codex", "opencode"):
            windows.append(window)
            continue
        if "records" in window or "messages" in window:
            # SQLite/native adapters can supply records; they use the same fold.
            windows.append(window)
            continue
        acc = RequestAccumulator(engine)
        paths = window.get("source_paths", [])
        before = reader.bytes
        count = 0
        for path in paths:
            source, info = reader.read(path, engine, window.get("session_id"))
            # Normally one native session source. Preserve full fold ordering
            # for callers with multiple explicitly supplied source fragments.
            if len(paths) == 1:
                acc = source
            else:
                if source.session and acc.session and source.session != acc.session:
                    acc = RequestAccumulator(engine)
                if source.model and acc.model and source.model != acc.model:
                    acc = RequestAccumulator(engine)
                acc.observations.extend(
                    (at, order + acc.order, identity, rank, value)
                    for at, order, identity, rank, value in source.observations
                )
                acc.order += source.order
                acc.invalid += source.invalid
                acc.session, acc.model = source.session or acc.session, source.model or acc.model
            count += info["rows"]
            partial += int(info["partial"])
            if info["error"]:
                window["_error"] = info["error"]
            if engine == "codex" and window.get("account"):
                accounts.extend(
                    account_sample(window["account"], engine, rate, now, stale_after=stale_after)
                    for rate in info["rate_limits"]
                )
        if not paths:
            window["_error"] = window.get("_error", "source_missing")
        window.update(_summary=acc.summary(now), _bytes=reader.bytes - before, _rows=count)
        windows.append(window)
    result = snapshot(windows, accounts, now, stale_after=stale_after)
    result["coverage"].update(
        bytes=reader.bytes,
        new_rows=reader.rows,
        partial_lines=partial,
        cache_recovered=reader.recovered,
        bootstraps=reader.bootstraps,
        bootstrap_byte_limit=reader.limits[0],
        bootstrap_row_limit=reader.limits[1],
        line_byte_limit=reader.limits[2],
    )
    return result
