"""Opt-in agent-neutral JSONL v1 with delta costs and explicit local evidence."""

from pathlib import Path
import json
import re

from sumbi.model import (Coverage, Session, TOKEN_KINDS, Window, execution_cwd,
                         integer, records, timestamp)
from sumbi.privacy import pseudonym

KINDS = {"session_start", "session_end", "context",
         "token_usage", "tool_start", "tool_end", "command_execution", "file_edit"}


def identity(value):
    return isinstance(value, str) and bool(value) and "\x00" not in value


def local_cwd(value):
    normalized = execution_cwd(value)
    return normalized if normalized and not normalized.startswith("//") and (
        normalized.startswith("/") or re.match(r"^[a-z]:/", normalized, re.I)) else None


def collect(home: Path, window: Window, coverage: Coverage, *, local_review=False,
            collect_links=False) -> list[Session]:
    """Read all streams before applying evidence; conflicts cannot win by order."""
    sessions, unique, conflicting = {}, {}, set()
    invalid, token_invalid = set(), set()
    for path in sorted((home / ".sumbi/events").rglob("*.jsonl")):
        for event in records(path, coverage, ignore_blank=True):
            raw_id = event.get("session_id")
            if identity(raw_id):
                sessions.setdefault(raw_id, Session("sumbi-events", raw_id))
            event_id = event.get("event_id")
            if not identity(event_id):
                coverage.unknown("invalid_event_record")
                if identity(raw_id):
                    invalid.add(raw_id)
                    if event.get("type") == "token_usage":
                        token_invalid.add(raw_id)
                continue
            canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
            if event_id in unique:
                previous, digest = unique[event_id]
                if canonical == digest and event_id not in conflicting:
                    coverage.duplicate_events += 1
                    continue
                coverage.unknown("conflicting_event_id")
                conflicting.add(event_id)
                for sid in (previous.get("session_id"), raw_id):
                    if identity(sid):
                        invalid.add(sid)
                for record in (previous, event):
                    if record.get("type") == "token_usage" and identity(record.get("session_id")):
                        token_invalid.add(record["session_id"])
                continue
            unique[event_id] = event, canonical

    starts, metadata, contexts = {}, {}, {}
    for order, (event_id, (event, _)) in enumerate(unique.items()):
        if event_id in conflicting:
            continue
        raw_id = event.get("session_id")
        if not identity(raw_id):
            coverage.unknown("invalid_event_record")
            continue
        session = sessions[raw_id]
        kind = event.get("type")
        if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
            coverage.unknown("unsupported_schema_version")
            invalid.add(raw_id)
            if kind == "token_usage":
                token_invalid.add(raw_id)
            continue
        if not isinstance(kind, str) or kind not in KINDS:
            coverage.unknown("unknown_event_type")
            invalid.add(raw_id)
            continue
        when = timestamp(event.get("timestamp"))
        if when is not None:
            session.times.add(when)
        else:
            coverage.invalid_timestamps += 1
            invalid.add(raw_id)
        if kind == "session_start":
            agent, cwd = event.get("agent"), event.get("cwd")
            parent = event.get("parent_session_id")
            role = event.get("role")
            if (not identity(agent) or local_cwd(cwd) is None
                    or role not in ("worker", "orchestrator")
                    or parent is not None and (not identity(parent) or parent == raw_id)
                    or when is None):
                coverage.unknown("invalid_session_start")
                invalid.add(raw_id)
                starts.setdefault(raw_id, []).append(None)
                continue
            signature = (when, agent, cwd, parent, role, *(event.get(k) for k in ("model", "effort", "cli_version")))
            starts.setdefault(raw_id, []).append(signature)
            session.cwd(cwd, when)
            session.context(when, order, cwd)
            contexts.setdefault(raw_id, []).append((when, local_cwd(cwd)))
        elif kind == "session_end":
            if when is not None and when < window.until:
                session.completed_at = max(when, session.completed_at or when)
        elif kind == "context":
            cwd = event.get("cwd")
            if "cwd" in event:
                if cwd is not None and local_cwd(cwd) is None:
                    coverage.unknown("invalid_context_cwd")
                    invalid.add(raw_id)
                session.cwd(cwd, when)
                session.context(when, order, cwd)
                contexts.setdefault(raw_id, []).append((when, local_cwd(cwd)))
        elif kind == "token_usage":
            tokens = event.get("tokens")
            if event.get("cwd") is not None and local_cwd(event["cwd"]) is None:
                coverage.unknown("invalid_token_cwd")
                invalid.add(raw_id)
                token_invalid.add(raw_id)
                continue
            values = {k: integer(tokens.get(k)) for k in TOKEN_KINDS} if isinstance(tokens, dict) else {}
            if (not isinstance(tokens, dict) or when is None
                    or any(tokens.get(k) is not None and values[k] is None for k in TOKEN_KINDS)
                    or values["reasoning_output"] is not None and values["output"] is not None
                    and values["reasoning_output"] > values["output"]):
                coverage.invalid_token_records += 1
                invalid.add(raw_id)
                token_invalid.add(raw_id)
                continue
            session.usage(when, order, values, cwd=local_cwd(event.get("cwd")))
            if window.contains(when):
                session.add_tokens(values)
        elif kind in ("tool_start", "tool_end"):
            tool_id = event.get("tool_call_id")
            if not identity(tool_id) or kind == "tool_end" and "error" in event and type(event["error"]) is not bool:
                coverage.unknown("invalid_tool_record")
                invalid.add(raw_id)
                continue
            start = kind == "tool_start"
            session.count("tool_calls" if start else "tool_results", tool_id, when, window)
            if not start and event.get("error") is True:
                session.count("tool_errors", tool_id, when, window)
            session.interval("tool", tool_id, when if start else None, None if start else when)
        elif kind == "command_execution":
            command, code = event.get("command"), event.get("exit_code")
            start = timestamp(event.get("started_at"))
            cwd = local_cwd(event.get("cwd"))
            valid_command = command is None or isinstance(command, str) or (
                isinstance(command, list) and all(isinstance(p, str) for p in command))
            malformed = (not valid_command or code is not None and type(code) is not int
                         or any(key not in event for key in ("exit_code", "command", "cwd", "started_at"))
                         or event.get("cwd") is not None and cwd is None
                         or event.get("started_at") is not None and start is None)
            if malformed:
                coverage.unknown("invalid_command_record")
                invalid.add(raw_id)
                command, code, start, cwd = None, None, None, None
            if command is None or command == "" or command == []:
                session.local_evidence_gaps["command_content_unknown"] += 1
            # Missing completion is an enduring gap, never an inferred result.
            session.execution(event_id, when, command, code, started_at=start, cwd=cwd)
        elif kind == "file_edit":
            session.edit(event_id, when)

        for field, target, category in (("model", session.models, "model"),
                ("effort", session.efforts, "effort"), ("cli_version", session.versions, "version")):
            if when is not None and (kind == "session_start" or kind == "context" and field in event):
                value = event.get(field)
                if value is not None and not isinstance(value, str):
                    coverage.unknown("invalid_context_metadata")
                    invalid.add(raw_id)
                    metadata.setdefault((raw_id, field), []).append((when, None))
                else:
                    metadata.setdefault((raw_id, field), []).append((when, value))

    for raw_id, session in sessions.items():
        session.token_evidence_incomplete = raw_id in token_invalid
        cwds = {}
        for at, value in contexts.get(raw_id, []):
            cwds.setdefault(at, set()).add(value)
        if any(len(values) > 1 for values in cwds.values()):
            coverage.unknown("conflicting_context_metadata")
            invalid.add(raw_id)
            session.attribution_events = [(at, order, kind,
                (None, True) if kind == "context" and len(cwds.get(at, ())) > 1 else data)
                for at, order, kind, data in session.attribution_events]
        signatures = starts.get(raw_id, [])
        if signatures and all(s is not None and s == signatures[0] for s in signatures):
            start, emitter, cwd, parent, role, *_ = signatures[0]
            session.start_at, session.start_cwd = start, cwd
            session.start_evidence = "sumbi-events-v1"
            session.emitter_agent, session.parent_raw_id = emitter, parent
            session.is_worker = role == "worker"
        elif signatures:
            coverage.unknown("conflicting_session_start")
            invalid.add(raw_id)
        for field, target, category in (("model", session.models, "model"),
                ("effort", session.efforts, "effort"), ("cli_version", session.versions, "version")):
            entries = [(at, value) for at, value in metadata.get((raw_id, field), []) if at < window.until]
            prior = [(at, value) for at, value in entries if at < window.since]
            relevant = [(at, value) for at, value in entries if window.contains(at)]
            if prior:
                last = max(at for at, _ in prior)
                relevant.extend((at, value) for at, value in prior if at == last)
            grouped = {}
            for at, value in entries:
                grouped.setdefault(at, set()).add(value)
            if any(len(values) > 1 for values in grouped.values()):
                coverage.unknown("conflicting_context_metadata")
                invalid.add(raw_id)
            session.metadata_incomplete[field] = any(value is None for _, value in relevant) and any(
                value is not None for _, value in relevant)
            for _, value in relevant:
                if value is not None:
                    target.add(pseudonym(category, value))
        if raw_id in invalid:
            session.local_evidence_gaps["open_event_evidence_incomplete"] += 1
    return list(sessions.values())
