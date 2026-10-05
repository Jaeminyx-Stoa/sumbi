"""Claude Code JSONL: one maximal usage snapshot per assistant message."""

from pathlib import Path

from sumbi.evidence import tool_evidence
from sumbi.deliver_evidence import branch, branch_query, tool_refs
from sumbi.model import Coverage, Session, Window, integer, label, mapping, records, timestamp

KNOWN = {"assistant", "user", "system", "progress", "attachment", "summary",
         "file-history-snapshot", "file-history-delta", "queue-operation", "pr-link",
         "frame-link", "last-prompt", "custom-title", "agent-name", "agent-color",
         "saved_hook_context", "worktree", "bridge_status", "legacy_bridge_status"}
SYSTEM = {"compact_boundary", "api_error", "stop_hook_summary", "local_command",
          "informational", "turn_duration", "request_start", "request_end"}
FIELDS = {"new_input": "input_tokens", "cache_write": "cache_creation_input_tokens",
          "cache_read": "cache_read_input_tokens", "output": "output_tokens"}


def collect(home: Path, window: Window, coverage: Coverage, *, local_review: bool = False,
            collect_links: bool = False) -> list[Session]:
    root = home / ".claude" / "projects"
    sessions: dict[str, Session] = {}
    messages: dict[str, dict] = {}
    tool_names = {}
    files = sorted(root.glob("*/*.jsonl")) + sorted(root.glob("*/**/subagents/**/*.jsonl"))
    for path in dict.fromkeys(files):
        parts = path.relative_to(root).parts
        child = "subagents" in parts
        index = parts.index("subagents") if child else None
        parent_hint = parts[index - 1] if child else path.stem
        agent_hint = (parts[index + 1] if child else path.stem).removesuffix(".jsonl").removeprefix("agent-")
        session = None
        for event in records(path, coverage):
            if session is None:
                parent = str(event.get("sessionId") or parent_hint)
                raw_id = parent + ":subagent:" + str(event.get("agentId") or agent_hint) if child else parent
                session = sessions.setdefault(raw_id, Session("claude-code", raw_id, parent if child else None))
            raw_id = session.raw_id
            if not session.accept(event, coverage):
                continue
            when = timestamp(event.get("timestamp"))
            order = (len(session.seen), len(session.seen))
            session.cwd(event.get("cwd"), when)
            if event.get("version"):
                session.versions.add(label(event["version"], "version"))
            kind = event.get("type")
            content = mapping(event.get("message")).get("content")
            if kind == "user" and not (isinstance(content, list) and any(
                    mapping(b).get("type") == "tool_result" for b in content)):
                session.context(when, order, event.get("cwd"))
                if collect_links and when:
                    session.deliverable_events.append((when, order, "context", (branch(event.get("gitBranch")), event.get("cwd"))))
            if kind not in KNOWN:
                coverage.unknown(kind)
            message = mapping(event.get("message"))
            paths = []
            command_cwd = None
            refs = set()
            for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    workdir, operands = tool_evidence(block.get("name"), block.get("input"))
                    command_cwd = workdir or command_cwd
                    paths.extend(operands)
                    if collect_links:
                        tool_names[raw_id, block.get("id")] = (block.get("name"), branch_query(block.get("name"), block.get("input")))
                        refs.update(tool_refs(block.get("name"), block.get("input")))
                elif collect_links and isinstance(block, dict) and block.get("type") == "tool_result":
                    result = block.get("content")
                    if isinstance(result, list):
                        result = "\n".join(b.get("text", "") for b in result
                                           if isinstance(b, dict) and b.get("type") == "text")
                    name, query = tool_names.get((raw_id, block.get("tool_use_id")), (None, False))
                    refs.update(tool_refs(name, result, output=True, query=query))
            if kind != "assistant" or not mapping(message.get("usage")):
                session.tool_paths(when, order, command_cwd or (event.get("cwd") if paths else None), paths)
                if collect_links and when and refs:
                    session.deliverable_events.append((when, order, "refs", refs))
            session.local_text(message.get("content"), when, window, local_review)
            session.local_text(event.get("content"), when, window, local_review)
            key = event.get("uuid") or len(session.seen)
            if kind == "system":
                subtype = event.get("subtype")
                if subtype not in SYSTEM:
                    coverage.unknown("system:" + str(subtype))
                if subtype == "compact_boundary":
                    session.count("compactions", key, when, window)
                elif subtype == "api_error":
                    session.count("api_errors", key, when, window)
                elif subtype in ("request_start", "request_end"):
                    session.interval("request", event.get("requestId") or key,
                                     when if subtype == "request_start" else None,
                                     when if subtype == "request_end" else None)
            if kind == "assistant":
                if message.get("model") and when and when < window.until:
                    session.models.add(label(message["model"], "model"))
                usage = mapping(message.get("usage"))
                if usage:
                    values = {k: integer(usage.get(v)) for k, v in FIELDS.items()}
                    if when is None or values["output"] is None:
                        coverage.invalid_token_records += 1
                    else:
                        # Streaming repeats input/cache and grows output. Choose largest output;
                        # break ties by latest timestamp. Attribute the whole message to that event.
                        identity = str(message.get("id") or event.get("uuid") or key)
                        candidates = messages.setdefault(raw_id, {})
                        previous = candidates.get(identity)
                        rank = values["output"], when
                        if previous is None or rank > previous[0]:
                            candidates[identity] = (rank, values, order, event.get("cwd") or command_cwd,
                                                    paths, command_cwd, branch(event.get("gitBranch")), refs)
            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                if kind == "assistant" and block.get("type") == "tool_use":
                    identity = block.get("id") or str(key) + ":" + str(index)
                    session.count("tool_calls", identity, when, window)
                    session.interval("tool", identity, when, None)
                    if block.get("name") == "AskUserQuestion":
                        session.count("user_input_requests", identity, when, window)
                elif kind == "user" and block.get("type") == "tool_result":
                    identity = block.get("tool_use_id") or str(key) + ":" + str(index)
                    session.count("tool_results", identity, when, window)
                    if block.get("is_error") is True:
                        session.count("tool_errors", identity, when, window)
                    session.interval("tool", identity, None, when)
    for raw_id, candidates in messages.items():
        for (_, when), values, order, cwd, paths, command_cwd, own_branch, refs in candidates.values():
            sessions[raw_id].tool_paths(when, order, command_cwd or (cwd if paths else None), paths)
            sessions[raw_id].usage(when, order, values, cwd=cwd, paths=paths)
            if collect_links:
                sessions[raw_id].deliverable_events.append((when, order, "own", (own_branch, refs)))
            if window.contains(when):
                sessions[raw_id].add_tokens(values)
    return list(sessions.values())
