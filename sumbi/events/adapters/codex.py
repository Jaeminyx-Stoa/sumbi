"""Codex rollout records translated into normalized observations."""

from pathlib import Path
import re

from sumbi.core.records import Coverage, records
from sumbi.core.values import epoch, integer, mapping, timestamp
from sumbi.events.schema import (
    Record, SessionStart, Context, Metadata, Tokens, TokenUsage, ToolEvidence,
    ToolInput, ToolOutput, ToolStart, ToolEnd, CommandExecution, FileEdit,
    Counter, Request, SessionEnd, Resume, LocalText, Diagnostic, SourceIdentity, freeze,
)
from .common import key, record_identity

KNOWN = {"session_meta", "turn_context", "event_msg", "response_item", "compacted",
         "token_usage_record", "inter_agent_communication_metadata", "world_state"}
EVENTS = {"token_count", "item_completed", "item_started", "exec_command_begin", "exec_command_end",
          "task_started", "task_complete", "task_completed", "thread_settings_applied",
          "user_message", "agent_message", "agent_reasoning", "agent_reasoning_raw_content",
          "agent_reasoning_section_break", "turn_aborted", "error", "warning", "shutdown_complete",
          "context_compacted", "mcp_tool_call_begin", "mcp_tool_call_end", "request_user_input"}
RESPONSES = {"message", "agent_message", "reasoning", "function_call", "function_call_output",
             "custom_tool_call", "custom_tool_call_output", "web_search_call",
             "local_shell_call", "image_generation_call", "compaction"}
ITEMS = {"UserMessage", "AgentMessage", "Reasoning", "CommandExecution", "McpToolCall",
         "Extension", "FileChange", "ContextCompaction", "SubAgentActivity", "ImageView",
         "CollabAgentToolCall", "WebSearch"}
TOOL_ITEMS = {"CommandExecution", "McpToolCall", "FileChange", "ImageView", "CollabAgentToolCall", "WebSearch"}
FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def exit_code(payload: dict) -> int | None:
    """Read machine fields or the CLI's anchored completion envelope, never prose."""
    value = payload.get("exit_code")
    if type(value) is int:
        return value
    output = payload.get("output")
    if isinstance(output, dict):
        return exit_code(output)
    if isinstance(output, str):
        match = re.search(r"(?m)^Process exited with code (-?\d+)\s*$", output)
        if match:
            return int(match.group(1))
    return None


def verification_exit_code(payload: dict) -> int | None:
    value = payload.get("exit_code")
    return value if type(value) is int else None


def _input(identity, name, arguments, *, at=None, own_time=False):
    source = SourceIdentity(freeze(identity)) if identity is not None else None
    return ToolEvidence(inputs=(ToolInput(key(identity), name, freeze(arguments), source),), at=at,
                        own_time=own_time, include_empty_refs=True, fallback_identity=True)


def _output(identity, output):
    source = SourceIdentity(freeze(identity)) if identity is not None else None
    return ToolEvidence(outputs=(ToolOutput(key(identity), freeze(output), source_identity=source),),
                        include_empty_refs=True, fallback_identity=True)


def _item(payload, subtype, identity, when):
    item = mapping(payload.get("item"))
    kind = item.get("type")
    identity = item.get("id") or identity
    source = SourceIdentity(freeze(identity)) if identity is not None else None
    start = epoch(payload.get("started_at_ms"), milliseconds=True)
    end = epoch(payload.get("completed_at_ms"), milliseconds=True)
    if kind not in ITEMS:
        yield Diagnostic("item:" + str(kind))
    if kind in TOOL_ITEMS:
        yield ToolStart(key(identity), at=start, own_time=True)
        if subtype == "item_completed":
            yield ToolEnd(key(identity), exit_code(item) not in (None, 0), interval=False)
        # The interval's completion time is independently machine-reported.
        if end is not None:
            yield ToolEnd(key(identity), at=end, own_time=True, count=False)
    if kind == "CommandExecution":
        yield Context(item.get("cwd") if isinstance(item.get("cwd"), str) else None, attribution=False)
        yield CommandExecution(key(identity), freeze(item.get("command")), verification_exit_code(item),
            started_at=(start or when) if subtype == "item_started" else start,
            cwd=freeze(item.get("cwd")), cwd_supplied="cwd" in item,
            phase="start" if subtype == "item_started" else "complete", pairing="interval", infer_cwd=True,
            source_identity=source)
    if kind == "FileChange" and subtype == "item_completed":
        yield FileEdit(key(identity))
    if kind in ("CommandExecution", "FileChange"):
        yield _input(identity, kind, item, at=start or when, own_time=True)
    if kind == "AgentMessage":
        yield LocalText(freeze(item.get("content")))


def _event(payload, event, when):
    subtype = payload.get("type")
    identity = payload.get("call_id") or payload.get("turn_id") or event.get("ordinal")
    source = SourceIdentity(freeze(identity)) if identity is not None else None
    if subtype not in EVENTS:
        yield Diagnostic("event_msg:" + str(subtype))
    if subtype == "token_count":
        total = mapping(mapping(payload.get("info")).get("total_token_usage"))
        if total:
            values = {k: integer(total.get(k)) for k in FIELDS}
            if (when is None or any(values[k] is None for k in FIELDS[:3])
                    or values["cached_input_tokens"] > values["input_tokens"]):
                yield Diagnostic("", "invalid_token_records")
            else:
                yield TokenUsage(Tokens(new_input=values["input_tokens"] - values["cached_input_tokens"],
                    cache_read=values["cached_input_tokens"],
                    output=values["output_tokens"], reasoning_output=values["reasoning_output_tokens"]),
                    selection="cumulative_including_cache", input_total=values["input_tokens"])
    elif subtype in ("task_started", "task_complete", "task_completed"):
        yield Resume() if subtype == "task_started" else SessionEnd("observed")
        yield Request(key(identity), epoch(payload.get("started_at")), epoch(payload.get("completed_at")),
                      "start" if subtype == "task_started" else "end")
    elif subtype in ("exec_command_begin", "mcp_tool_call_begin"):
        if subtype == "exec_command_begin":
            yield CommandExecution(key(identity), freeze(payload.get("command", payload.get("cmd"))),
                started_at=when, cwd=freeze(payload.get("cwd", payload.get("workdir"))),
                cwd_supplied="cwd" in payload or "workdir" in payload, phase="start", pairing="launch", source_identity=source)
            yield _input(identity, "exec_command", payload)
        yield ToolStart(key(identity))
    elif subtype in ("exec_command_end", "mcp_tool_call_end"):
        if subtype == "exec_command_end":
            yield CommandExecution(key(identity), freeze(payload.get("command")), verification_exit_code(payload),
                phase="end", pairing="launch", command_supplied="command" in payload, infer_cwd=True,
                source_identity=source)
        yield _output(identity, payload.get("output"))
        yield ToolEnd(key(identity), exit_code(payload) not in (None, 0))
    elif subtype in ("item_completed", "item_started"):
        yield from _item(payload, subtype, identity, when)
    elif subtype == "shutdown_complete":
        yield SessionEnd("observed")
    elif subtype in ("error", "request_user_input"):
        yield Counter("api_errors" if subtype == "error" else "user_input_requests", key(identity))


def _response(payload, event):
    subtype = payload.get("type")
    identity = payload.get("call_id") or payload.get("id") or event.get("ordinal")
    if subtype not in RESPONSES:
        yield Diagnostic("response_item:" + str(subtype))
    if subtype in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
        name = payload.get("name", subtype)
        if str(payload.get("name", "")).split(".")[-1] == "apply_patch":
            yield FileEdit(key(identity))
        yield _input(identity, name, payload.get("arguments", payload.get("input", payload.get("action"))))
        yield ToolStart(key(identity))
        if str(payload.get("name", "")).split(".")[-1] in ("request_user_input", "request_user_input_async"):
            yield Counter("user_input_requests", key(identity))
    elif subtype in ("function_call_output", "custom_tool_call_output"):
        yield _output(identity, payload.get("output"))
        yield ToolEnd(key(identity), exit_code(payload) not in (None, 0))
    if subtype in ("message", "agent_message"):
        yield LocalText(freeze(payload.get("content")))


def _translate(event, payload, when, inherited):
    kind = event.get("type")
    if kind == "session_meta" and not inherited:
        subagent = mapping(mapping(payload.get("source")).get("subagent"))
        parent = mapping(subagent.get("thread_spawn")).get("parent_thread_id")
        yield SessionStart(payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
            parent if isinstance(parent, str) else None, "worker" if subagent else "orchestrator",
            agent="codex", provenance="session-header")
    if kind not in KNOWN:
        yield Diagnostic(freeze(kind))
    if kind in ("session_meta", "turn_context") and not inherited:
        yield Context(payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
            "cwd" in payload, freeze(payload.get("branch") or mapping(payload.get("git")).get("branch")),
            execution_context=True)
    if kind == "turn_context":
        yield Metadata(freeze(payload.get("model")), freeze(payload.get("effort")), selection="context")
    if kind == "compacted":
        yield Counter("compactions", key(event.get("ordinal")))
    if kind == "event_msg":
        yield from _event(payload, event, when)
    elif kind == "response_item":
        yield from _response(payload, event)


def collect(home: Path, coverage: Coverage):
    for path in sorted((home / ".codex/sessions").rglob("rollout-*.jsonl")):
        raw_id, meta_seen = None, False
        for event in records(path, coverage):
            kind, payload = event.get("type"), mapping(event.get("payload"))
            inherited = False
            before = ()
            if kind == "session_meta" and not meta_seen:
                raw_id = str(payload.get("id") or payload.get("session_id") or path.stem)
                meta_seen = True
            elif kind == "session_meta" and str(payload.get("id") or payload.get("session_id")) != raw_id:
                inherited = True
                coverage.inherited_session_meta += 1
            if kind == "session_meta" and not inherited and payload.get("cli_version"):
                before = (Metadata(cli_version=freeze(payload["cli_version"]), supplied=("cli_version",)),)
            if raw_id is None:
                raw_id = path.stem
            when = timestamp(event.get("timestamp"))
            yield Record("codex", raw_id, when, record_identity(event), tuple(_translate(event, payload, when, inherited)),
                timestamp_supplied="timestamp" in event, ordinal=integer(event.get("ordinal")), before_dedup=before,
                fallback_id=str(event["ordinal"]) if "ordinal" in event else None,
                fallback_source_identity=SourceIdentity(freeze(event["ordinal"])) if "ordinal" in event else None)
