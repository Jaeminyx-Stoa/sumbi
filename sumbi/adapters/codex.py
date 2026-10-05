"""Codex rollout JSONL: cumulative counters, resumes and paired event times."""

from pathlib import Path
import re

from sumbi.model import (Coverage, Session, Window, epoch, integer, label, mapping,
                         records, timestamp)

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
TOOL_ITEMS = {"CommandExecution", "McpToolCall", "FileChange", "ImageView",
              "CollabAgentToolCall", "WebSearch"}
FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def exit_code(payload: dict) -> int | None:
    """Read machine fields or the CLI's anchored completion envelope, never prose."""
    value = payload.get("exit_code")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    output = payload.get("output")
    if isinstance(output, dict):
        return exit_code(output)
    if isinstance(output, str):
        match = re.search(r"(?m)^Process exited with code (-?\d+)\s*$", output)
        if match:
            return int(match.group(1))
    return None


def collect(home: Path, window: Window, coverage: Coverage, *, local_review: bool = False) -> list[Session]:
    root = home / ".codex" / "sessions"
    sessions: dict[str, Session] = {}
    snapshots: dict[str, list] = {}
    contexts: dict[str, list] = {}
    for path in sorted(root.rglob("rollout-*.jsonl")):
        session = None
        meta_seen = False
        for event in records(path, coverage):
            kind = event.get("type")
            payload = mapping(event.get("payload"))
            inherited_meta = False
            if kind == "session_meta" and not meta_seen:
                raw_id = str(payload.get("id") or payload.get("session_id") or path.stem)
                session = sessions.setdefault(raw_id, Session("codex", raw_id))
                meta_seen = True
                if payload.get("cli_version"):
                    session.versions.add(label(payload["cli_version"], "version"))
            elif kind == "session_meta" and str(payload.get("id") or payload.get("session_id")) != session.raw_id:
                # Child rollouts can carry the parent's metadata after their own header.
                # The first header identifies this stream; session_id can name its root.
                inherited_meta = True
                coverage.inherited_session_meta += 1
            elif kind == "session_meta" and payload.get("cli_version"):
                session.versions.add(label(payload["cli_version"], "version"))
            if session is None:
                # Without meta, keep coverage and a pseudonymous, unlinked session.
                session = sessions.setdefault(path.stem, Session("codex", path.stem))
            if not session.accept(event, coverage):
                continue
            when = timestamp(event.get("timestamp"))
            if kind not in KNOWN:
                coverage.unknown(kind)
            if kind in ("session_meta", "turn_context") and not inherited_meta:
                session.cwd(payload.get("cwd"), when)
            if kind == "turn_context":
                contexts.setdefault(session.raw_id, []).append((when, payload.get("model"), payload.get("effort")))
            if kind == "compacted":
                session.count("compactions", event.get("ordinal", len(session.seen)), when, window)
            if kind == "event_msg":
                subtype = payload.get("type")
                identity = payload.get("call_id") or payload.get("turn_id") or event.get("ordinal", len(session.seen))
                if subtype not in EVENTS:
                    coverage.unknown("event_msg:" + str(subtype))
                if subtype == "token_count":
                    total = mapping(mapping(payload.get("info")).get("total_token_usage"))
                    if total:
                        values = {key: integer(total.get(key)) for key in FIELDS}
                        if when is None or any(values[k] is None for k in FIELDS[:3]) \
                                or values["cached_input_tokens"] > values["input_tokens"]:
                            coverage.invalid_token_records += 1
                        else:
                            order = integer(event.get("ordinal"))
                            snapshots.setdefault(session.raw_id, []).append(
                                (when, order if order is not None else len(session.seen), values))
                elif subtype in ("task_started", "task_complete", "task_completed"):
                    start = epoch(payload.get("started_at"))
                    end = epoch(payload.get("completed_at"))
                    session.interval("request", identity, start or (when if subtype == "task_started" else None),
                                     end or (when if subtype != "task_started" else None))
                elif subtype in ("exec_command_begin", "mcp_tool_call_begin"):
                    session.count("tool_calls", identity, when, window)
                    session.interval("tool", identity, when, None)
                elif subtype in ("exec_command_end", "mcp_tool_call_end"):
                    session.count("tool_results", identity, when, window)
                    if exit_code(payload) not in (None, 0):
                        session.count("tool_errors", identity, when, window)
                    session.interval("tool", identity, None, when)
                elif subtype in ("item_completed", "item_started"):
                    item = mapping(payload.get("item"))
                    item_type = item.get("type")
                    identity = item.get("id") or identity
                    start = epoch(payload.get("started_at_ms"), milliseconds=True)
                    end = epoch(payload.get("completed_at_ms"), milliseconds=True)
                    if item_type not in ITEMS:
                        coverage.unknown("item:" + str(item_type))
                    if item_type in TOOL_ITEMS:
                        session.count("tool_calls", identity, start, window)
                        if subtype == "item_completed":
                            session.count("tool_results", identity, when, window)
                            if exit_code(item) not in (None, 0):
                                session.count("tool_errors", identity, when, window)
                        session.interval("tool", identity, start, end)
                    if item_type == "CommandExecution":
                        session.cwd(item.get("cwd"), when)
                    if item_type == "AgentMessage":
                        session.local_text(item.get("content"), when, window, local_review)
                elif subtype == "error":
                    session.count("api_errors", identity, when, window)
                elif subtype == "request_user_input":
                    session.count("user_input_requests", identity, when, window)
            elif kind == "response_item":
                subtype = payload.get("type")
                identity = payload.get("call_id") or payload.get("id") or event.get("ordinal", len(session.seen))
                if subtype not in RESPONSES:
                    coverage.unknown("response_item:" + str(subtype))
                if subtype in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
                    session.count("tool_calls", identity, when, window)
                    session.interval("tool", identity, when, None)
                    if str(payload.get("name", "")).split(".")[-1] in ("request_user_input", "request_user_input_async"):
                        session.count("user_input_requests", identity, when, window)
                elif subtype in ("function_call_output", "custom_tool_call_output"):
                    session.count("tool_results", identity, when, window)
                    if exit_code(payload) not in (None, 0):
                        session.count("tool_errors", identity, when, window)
                    session.interval("tool", identity, None, when)
                if subtype in ("message", "agent_message"):
                    session.local_text(payload.get("content"), when, window, local_review)
    for raw_id, entries in contexts.items():
        eligible = [(t, model, effort) for t, model, effort in entries if t and t < window.until]
        prior = [entry for entry in eligible if entry[0] < window.since]
        relevant = [entry for entry in eligible if window.contains(entry[0])]
        if prior:
            relevant.append(max(prior, key=lambda entry: entry[0]))
        for _, model, effort in relevant:
            if model:
                sessions[raw_id].models.add(label(model, "model"))
            if effort:
                sessions[raw_id].efforts.add(label(effort, "effort"))
    for raw_id, entries in snapshots.items():
        session = sessions[raw_id]
        previous = dict.fromkeys(FIELDS, 0)
        for when, _, total in sorted(entries, key=lambda e: (e[0], e[1])):
            reset = any(total[k] is not None and previous[k] is not None and total[k] < previous[k]
                        for k in FIELDS)
            baseline = dict.fromkeys(FIELDS, 0) if reset else previous
            delta = {k: total[k] - baseline[k] if total[k] is not None and baseline[k] is not None else None
                     for k in FIELDS}
            if reset and window.contains(when):
                session.counts["counter_resets"] += 1
            if window.contains(when):
                fresh = delta["input_tokens"] - delta["cached_input_tokens"]
                if fresh < 0:
                    # A correction contradicts the nested cached-input contract. Expose the gap.
                    coverage.invalid_token_records += 1
                else:
                    session.add_tokens({"new_input": fresh, "cache_read": delta["cached_input_tokens"],
                                        "output": delta["output_tokens"], "reasoning_output": delta["reasoning_output_tokens"]})
            previous = total
    return list(sessions.values())
