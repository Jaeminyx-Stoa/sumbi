"""Translate the open sumbi-events v1 contract without session accounting."""

from pathlib import Path
import re

from sumbi.core.records import Coverage, records
from sumbi.core.values import TOKEN_KINDS, integer, timestamp
from sumbi.core.paths import execution_cwd
from sumbi.events.schema import (
    Record, Identity, SessionStart, Context, Metadata, Tokens, TokenUsage,
    ToolStart, ToolEnd, CommandExecution, FileEdit, SessionEnd, Diagnostic, freeze,
)
from .common import digest

KINDS = {"session_start", "session_end", "context", "token_usage",
    "tool_start", "tool_end", "command_execution", "file_edit"}


def identity(value):
    return isinstance(value, str) and bool(value) and "\x00" not in value


def local_cwd(value):
    normalized = execution_cwd(value)
    return normalized if normalized and not normalized.startswith("//") and (
        normalized.startswith("/") or re.match(r"^[a-z]:/", normalized, re.I)) else None


def _gap(category, *, token=False, start=False, counter="unknown"):
    return Diagnostic(category, counter, evidence_gap=True, token_gap=token, invalid_start=start)


def _metadata(event, *, start=False):
    supplied = tuple(k for k in ("model", "effort", "cli_version") if start or k in event)
    return Metadata(*(freeze(event.get(k)) for k in ("model", "effort", "cli_version")),
        supplied, "explicit")


def _usage(event, when):
    tokens = event.get("tokens")
    if event.get("cwd") is not None and local_cwd(event["cwd"]) is None:
        yield _gap("invalid_token_cwd", token=True)
        return
    values = {k: integer(tokens.get(k)) for k in TOKEN_KINDS} if isinstance(tokens, dict) else {}
    if (not isinstance(tokens, dict) or when is None
        or any(tokens.get(k) is not None and values[k] is None for k in TOKEN_KINDS)
        or values["reasoning_output"] is not None and values["output"] is not None
        and values["reasoning_output"] > values["output"]):
        yield _gap("", token=True, counter="invalid_token_records")
        return
    yield TokenUsage(Tokens(**values), local_cwd(event.get("cwd")))


def _command(event):
    command, code = event.get("command"), event.get("exit_code")
    start, cwd = timestamp(event.get("started_at")), local_cwd(event.get("cwd"))
    valid_command = command is None or isinstance(command, str) or (
        isinstance(command, list) and all(isinstance(p, str) for p in command))
    malformed = (not valid_command or code is not None and type(code) is not int
        or any(k not in event for k in ("exit_code", "command", "cwd", "started_at"))
        or event.get("cwd") is not None and cwd is None
        or event.get("started_at") is not None and start is None)
    if malformed:
        yield _gap("invalid_command_record")
        command, code, start, cwd = None, None, None, None
    yield CommandExecution(event["event_id"], freeze(command), code, start, cwd)


def _translate(event, when):
    raw_id, kind = event.get("session_id"), event.get("type")
    if not identity(raw_id):
        yield _gap("invalid_event_record")
        return
    if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        yield _gap("unsupported_schema_version", token=kind == "token_usage")
        return
    if not isinstance(kind, str) or kind not in KINDS:
        yield _gap("unknown_event_type")
        return
    if kind == "session_start":
        agent, cwd, parent, role = (event.get(k)
            for k in ("agent", "cwd", "parent_session_id", "role"))
        if (not identity(agent) or local_cwd(cwd) is None or role not in ("worker", "orchestrator")
            or parent is not None and (not identity(parent) or parent == raw_id)
            or when is None):
            yield _gap("invalid_session_start", start=True)
            return
        yield SessionStart(cwd, parent, role, agent, metadata=_metadata(event, start=True))
    elif kind == "session_end":
        yield SessionEnd()
    elif kind == "context":
        cwd = event.get("cwd")
        if "cwd" in event and cwd is not None and local_cwd(cwd) is None:
            yield _gap("invalid_context_cwd")
        yield Context(cwd if isinstance(cwd, str) else None, "cwd" in event,
            metadata=_metadata(event),
            cwd_valid=cwd is None or local_cwd(cwd) is not None)
    elif kind == "token_usage":
        yield from _usage(event, when)
    elif kind in ("tool_start", "tool_end"):
        tool_id = event.get("tool_call_id")
        if not identity(tool_id) or kind == "tool_end" and "error" in event and type(
            event["error"]) is not bool:
            yield _gap("invalid_tool_record")
            return
        yield ToolStart(tool_id) if kind == "tool_start" else ToolEnd(tool_id, event.get("error"))
    elif kind == "command_execution":
        yield from _command(event)
    elif kind == "file_edit":
        yield FileEdit(event["event_id"])


def collect(home: Path, coverage: Coverage):
    for path in sorted((home / ".sumbi/events").rglob("*.jsonl")):
        for event in records(path, coverage, ignore_blank=True):
            raw_id, event_id = event.get("session_id"), event.get("event_id")
            valid_id = identity(event_id)
            valid_kind = type(
                event.get("schema_version")) is int and event["schema_version"] == 1 and (
                    isinstance(event.get("type"), str) and event["type"] in KINDS)
            when = timestamp(event.get("timestamp"))
            observations = tuple(_translate(event, when)) if valid_id else (
                _gap("invalid_event_record", token=event.get("type") == "token_usage"),)
            yield Record("sumbi-events", raw_id if identity(raw_id) else None, when,
                Identity(digest(event), event_id if valid_id else None, "event_id"), observations,
                observe_time=valid_id and valid_kind and identity(raw_id),
                usage_record=event.get("type") == "token_usage")
