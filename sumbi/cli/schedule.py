"""Run the local jobs that are due now once, recording outcomes without output."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class ConfigError(ValueError):
    """The local job table is not a valid schedule configuration."""


def configure_parser(command):
    command.add_argument("--config", type=Path, required=True, metavar="TOML",
        help="Local job table, read with tomllib; keep it outside the repository")
    command.add_argument("--state", type=Path, required=True, metavar="DIR",
        help="Local directory for locks, last-start files and run records")
    command.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)


def _fail(message):
    print("sumbi schedule: " + message, file=sys.stderr)
    return 2


def _load(path):
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    jobs = data.get("job")
    if not isinstance(jobs, list) or not jobs:
        raise ConfigError("Configuration needs at least one [[job]] table.")
    seen, result = set(), []
    for job in jobs:
        result.append(_job(job, seen))
    return result


def _job(job, seen):
    if not isinstance(job, dict):
        raise ConfigError("Each job must be a table.")
    name = job.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ConfigError("Each job needs a bounded name.")
    if name in seen:
        raise ConfigError("Job names must be unique.")
    seen.add(name)
    command = job.get("command")
    if (not isinstance(command, list) or not command
            or not all(isinstance(part, str) and part for part in command)):
        raise ConfigError("Each job needs a non-empty command array of strings.")
    every, daily = job.get("every_sec"), job.get("at_utc")
    if (every is None) == (daily is None):
        raise ConfigError("Each job needs exactly one of every_sec or at_utc.")
    normalized = {"name": name, "command": tuple(command), "cwd": None,
        "timeout_sec": 3600}
    timeout = job.get("timeout_sec", 3600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ConfigError("timeout_sec must be a positive integer.")
    normalized["timeout_sec"] = timeout
    if every is not None:
        if isinstance(every, bool) or not isinstance(every, int) or every <= 0:
            raise ConfigError("every_sec must be a positive integer.")
        normalized["every_sec"] = every
    if daily is not None:
        if not isinstance(daily, str) or not CLOCK.fullmatch(daily):
            raise ConfigError("at_utc must be HH:MM in UTC.")
        normalized["at_utc"] = daily
    cwd = job.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd:
            raise ConfigError("cwd must be a non-empty string.")
        normalized["cwd"] = cwd
    return normalized


def _last_start(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        last = value["last_start"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return float(last) if isinstance(last, (int, float)) else None


def _day_target(value, now):
    moment = datetime.fromtimestamp(now, timezone.utc)
    hour, minute = (int(part) for part in value.split(":"))
    return datetime(moment.year, moment.month, moment.day, hour, minute,
        tzinfo=timezone.utc).timestamp()


def _due(job, state, now):
    last = _last_start(state / (job["name"] + ".json"))
    if "every_sec" in job:
        return last is None or now - last >= job["every_sec"] - 30
    target = _day_target(job["at_utc"], now)
    return now >= target and (last is None or last < target)


def _write_start(path, now):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".sumbi-schedule-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"last_start": now}, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _record(state, name, start, end, rc, status):
    line = json.dumps({"name": name, "start": start, "end": end, "rc": rc,
        "status": status}, sort_keys=True) + "\n"
    with (state / "runs.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)


def _execute(job):
    try:
        done = subprocess.run(list(job["command"]), cwd=job["cwd"],
            timeout=job["timeout_sec"])
    except subprocess.TimeoutExpired:
        return "timeout", None
    return ("ok" if done.returncode == 0 else "failed"), done.returncode


def _run_job(job, state, now):
    name = job["name"]
    if not _due(job, state, now):
        _record(state, name, now, now, None, "skipped")
        return "skipped", None
    try:
        descriptor = os.open(state / (name + ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        _record(state, name, now, now, None, "failed")
        return "failed", None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _record(state, name, now, now, None, "busy")
            return "busy", None
        _write_start(state / (name + ".json"), now)
        status, rc = _execute(job)
        _record(state, name, now, time.time(), rc, status)
        return status, rc
    finally:
        os.close(descriptor)


def run(args, now=None):
    timestamp = args.now if now is None else now
    if timestamp is None:
        timestamp = time.time()
    try:
        jobs = _load(Path(args.config))
    except (ConfigError, OSError, tomllib.TOMLDecodeError):
        return _fail("Configuration is invalid; no job was run.")
    state = Path(args.state)
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _fail("State directory could not be created; no job was run.")
    failed = False
    for job in jobs:
        try:
            status, _ = _run_job(job, state, timestamp)
        except OSError:
            failed = True
            continue
        if status in ("failed", "timeout"):
            failed = True
    return 1 if failed else 0
