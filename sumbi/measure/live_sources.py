"""Read-only native source discovery and account adapters; all locations injected."""

import json
import glob
import math
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sumbi.core.values import mapping
from sumbi.live import _accounts, _iso, _time
from sumbi.core.local_cache import atomic_json

ENGINES = ("claude", "codex", "opencode")


def _seconds(value):
    if type(value) in (int, float) and math.isfinite(value):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return _time(value)


def account_sample(account, engine, rate, now, *, stale_after=3600):
    at = _time(rate.get("observed_at"))
    reset = _time(rate.get("resets_at"))
    used, minutes = rate.get("used_percent"), rate.get("window_minutes")
    used = used if type(used) in (int, float) and math.isfinite(used) and 0 <= used <= 100 else None
    minutes = minutes if type(minutes) is int and minutes > 0 else None
    status = "ok"
    if at is None or at > now or used is None or minutes is None or reset is None:
        status = "unknown"
    elif (now - at).total_seconds() > stale_after or reset <= now:
        status = "stale"
    return {
        "account": account,
        "engine": engine,
        "observed_at": _iso(at or now),
        "used_percent": used,
        "resets_at": _iso(reset) if reset else None,
        "window_minutes": minutes,
        "status": status,
    }


def claude_accounts(usage, now):
    """Decode existing usage cache; never contact an authentication endpoint."""
    result = []
    for account, data in mapping(usage.get("accounts")).items():
        if not isinstance(account, str) or not account:
            continue
        data = mapping(data)
        at = _seconds(data.get("response_received_at", usage.get("at")))
        for key, minutes in (("five_hour", 300), ("seven_day", 10080)):
            result.append(
                account_sample(
                    account,
                    "claude",
                    {
                        "observed_at": _iso(at) if at else None,
                        "used_percent": data.get(key),
                        "window_minutes": minutes,
                        "resets_at": data.get(key + "_reset"),
                    },
                    now,
                )
            )
    return result


def cached_claude_accounts(path, cache, now, *, max_bytes=2_000_000):
    """Cache normalized quota fields, so an unchanged usage source costs 0 bytes.

    The caller must hold its collection lock. Privacy fields are discarded
    before saving; freshness is recalculated even when no source bytes are read.
    """
    cache = Path(cache)
    count = recovered = 0
    try:
        stat = os.stat(path)
        identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
        try:
            stored = json.loads(cache.read_text(encoding="utf-8"))
            if (
                set(stored) != {"version", "identity", "accounts", "status"}
                or stored["version"] != 1
                or stored["status"] not in ("read", "unknown")
            ):
                raise ValueError("quota_cache")
            accounts = _accounts(stored["accounts"])
            if any(
                row["engine"] != "claude" or row["window_minutes"] not in (300, 10080)
                for row in accounts
            ):
                raise ValueError("quota_cache_engine")
            if stored["identity"] == identity:
                refreshed = []
                for row in accounts:
                    sample = account_sample(row["account"], row["engine"], row, now)
                    if row["status"] == "unknown":
                        sample["status"] = "unknown"
                    refreshed.append(sample)
                return refreshed, 0, stored["status"], 0
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            recovered = 1
        with open(path, "rb") as stream:
            raw = stream.read(max_bytes + 1)
            count = len(raw)
        if count > max_bytes:
            return [], count, "unknown", recovered
        accounts = claude_accounts(json.loads(raw), now)
        atomic_json(
            cache, {"version": 1, "identity": identity, "accounts": accounts, "status": "read"}
        )
        return accounts, count, "read", recovered
    except (OSError, ValueError, TypeError, AttributeError):
        return [], count, "unknown", recovered


def codex_limits(record):
    payload = mapping(record.get("payload"))
    limits = mapping(payload.get("rate_limits"))
    at = _time(record.get("timestamp"))
    if not limits:
        return []
    result = []
    for key in ("primary", "secondary"):
        value = mapping(limits.get(key))
        if not value:
            continue
        reset = _seconds(value.get("resets_at"))
        used, minutes = value.get("used_percent"), value.get("window_minutes")
        result.append(
            {
                "observed_at": _iso(at) if at else None,
                "used_percent": used
                if type(used) in (int, float) and math.isfinite(used) and 0 <= used <= 100
                else None,
                "window_minutes": minutes if type(minutes) is int and minutes > 0 else None,
                "resets_at": _iso(reset) if reset else None,
            }
        )
    return result


def latest_accounts(accounts):
    """Keep independent engines and quota windows; choose latest window sample."""
    unique = {}
    for row in accounts:
        key = row["engine"], row["account"], row["window_minutes"]
        old = unique.get(key)
        if old is None or row["observed_at"] > old["observed_at"]:
            unique[key] = row
    return list(unique.values())


def sqlite_session(path, session_id, *, max_rows=100_000):
    """Read the exact session with WAL visibility. No immutable fallback.

    Errors and absent IDs remain unknown; never substitute a recent session.
    The connection is read-only, including for databases supplied by callers.
    """
    result = {
        "engine": "opencode",
        "session_id": session_id,
        "messages": [],
        "_sqlite_rows": 0,
        "_sqlite_payload_bytes": 0,
    }
    if not isinstance(session_id, str) or not session_id:
        result["_error"] = "sqlite_session_missing"
        return result
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("SELECT id FROM session WHERE id = ?", (session_id,)).fetchone() is None:
                result["_error"] = "sqlite_session_unmatched"
                return result
            cursor = db.execute(
                "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id",
                (session_id,),
            )
            for identity, raw in cursor:
                result["_sqlite_rows"] += 1
                result["_sqlite_payload_bytes"] += (
                    len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
                )
                if result["_sqlite_rows"] > max_rows:
                    result["_error"] = "sqlite_row_limit"
                    break
                data = json.loads(raw)
                if mapping(data).get("role") == "assistant":
                    result["messages"].append(
                        {"id": identity, "session_id": session_id, "data": data}
                    )
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result["_error"] = "sqlite_unreadable"
    return result


def command(arguments):
    return subprocess.run(arguments, capture_output=True, text=True, timeout=10, check=True).stdout


def open_rollouts(pid, proc_root):
    """Inspect only descendants of this pane, using their actual open FDs."""
    todo, seen, paths = [str(pid)], set(), {}
    while todo:
        current = todo.pop()
        if current in seen or not current.isdigit():
            continue
        seen.add(current)
        base = Path(proc_root) / current
        try:
            todo.extend((base / "task" / current / "children").read_text().split())
        except OSError:
            pass
        try:
            for fd in (base / "fd").iterdir():
                target = os.readlink(fd)
                name = Path(target.removesuffix(" (deleted)")).name
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    stat = fd.stat()
                    paths.setdefault((stat.st_dev, stat.st_ino), (str(fd), target))
        except OSError:
            continue
    return sorted(paths.values())


def live_inventory(*, target, proc_root, sqlite_path, run=command, tmux_socket=None):
    """tmux labels + process FDs/cfg+sid. No session recency heuristics."""
    fields = "#{window_index}\t#{@lane}\t#{@sid}\t#{@cfg}\t#{@engine}\t#{pane_pid}"
    prefix = ["tmux", "-S", str(tmux_socket)] if tmux_socket else ["tmux"]
    text = run(prefix + ["list-windows", "-t", target, "-F", fields])
    windows = []
    for line in text.splitlines():
        values = line.split("\t")
        if len(values) != 6:
            raise ValueError("tmux_inventory_shape")
        index, lane, sid, cfg, engine, pid = values
        window = {
            "lane": lane,
            "engine": engine or "unknown",
            "session_id": sid or None,
            "config_root": cfg or None,
            "window_index": index,
            "source_paths": [],
        }
        if engine == "opencode":
            window.update(sqlite_session(sqlite_path, sid))
        elif engine == "codex" or engine not in ENGINES:
            paths = open_rollouts(pid, proc_root)
            if len(paths) == 1:
                window.update(
                    engine="codex", source_paths=[paths[0][0]], source_targets=[paths[0][1]]
                )
            elif paths:
                window["_error"] = "ambiguous_open_rollout"
        if window["engine"] == "claude" or (
            window["engine"] not in ENGINES and not window["source_paths"]
        ):
            # Only exact sid in its own cfg. An empty/stale sid stays unknown.
            if cfg and sid and Path(sid).name == sid:
                paths = sorted(
                    str(p)
                    for p in (Path(cfg) / "projects").glob("*/" + glob.escape(sid) + ".jsonl")
                )
                if len(paths) == 1:
                    window["source_paths"] = paths
                elif paths:
                    window["_error"] = "ambiguous_session_source"
        windows.append(window)
    return {"windows": windows}


class SourceProbe:
    """Bounded format checks, cached by inode/metadata without native contents."""

    def __init__(self, entries=None):
        self.entries = {}
        self.recovered = 0
        for path, entry in (entries or {}).items():
            if (
                isinstance(path, str)
                and isinstance(entry, dict)
                and set(entry) == {"version", "device", "inode", "size", "mtime", "ctime", "engine"}
                and entry["version"] == 2
                and entry["engine"] in (*ENGINES, "unknown")
                and all(
                    type(entry[k]) is int and entry[k] >= 0
                    for k in ("device", "inode", "size", "mtime", "ctime")
                )
            ):
                self.entries[path] = entry
            else:
                self.recovered = 1
        self.bytes = 0

    def engine(self, path):
        try:
            stat = os.stat(path)
            # Append does not change an established native file format.
            old = self.entries.get(str(path))
            if (
                old
                and old["device"] == stat.st_dev
                and old["inode"] == stat.st_ino
                and stat.st_size >= old["size"]
                and (
                    (stat.st_size > old["size"] and old["engine"] != "unknown")
                    or (
                        stat.st_size == old["size"]
                        and (old["mtime"], old["ctime"]) == (stat.st_mtime_ns, stat.st_ctime_ns)
                    )
                )
            ):
                return old["engine"]
            found = set()
            with open(path, "rb") as stream:
                for _ in range(32):
                    raw = stream.readline(65536)
                    self.bytes += len(raw)
                    if not raw:
                        break
                    try:
                        row = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    kind = row.get("type")
                    if kind == "session_meta" and isinstance(
                        mapping(row.get("payload")).get("id"), str
                    ):
                        found.add("codex")
                    elif (
                        kind == "event_msg"
                        and mapping(row.get("payload")).get("type") == "token_count"
                    ):
                        found.add("codex")
                    elif (
                        kind == "assistant"
                        and isinstance(row.get("sessionId"), str)
                        and isinstance(mapping(row.get("message")).get("usage"), dict)
                    ):
                        found.add("claude")
                    elif isinstance(row.get("sessionId"), str) and (
                        (kind == "custom-title" and isinstance(row.get("customTitle"), str))
                        or (kind == "agent-name" and isinstance(row.get("agentName"), str))
                    ):
                        found.add("claude")
            engine = next(iter(found)) if len(found) == 1 else "unknown"
            self.entries[str(path)] = {
                "version": 2,
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "size": stat.st_size,
                "mtime": stat.st_mtime_ns,
                "ctime": stat.st_ctime_ns,
                "engine": engine,
            }
            return engine
        except OSError:
            return "unknown"

    def inventory(self, inventory):
        windows = []
        for original in inventory["windows"]:
            window = dict(original)
            if "messages" in window or "records" in window:
                windows.append(window)
                continue
            by_engine = {}
            for path in window.get("source_paths", []):
                detected = self.engine(path)
                by_engine.setdefault(detected, []).append(path)
            engine = window.get("engine")
            if engine not in ENGINES:
                supported = set(by_engine) & set(ENGINES)
                if len(supported) == 1:
                    engine = next(iter(supported))
            window["engine"] = engine
            window["source_paths"] = by_engine.get(engine, []) if engine in ENGINES else []
            if not window["source_paths"]:
                window["_error"] = window.get("_error", "source_format_unconfirmed")
            windows.append(window)
        return {**inventory, "windows": windows}
