"""Claude Code records translated into immutable normalized observations."""

from pathlib import Path
import json
import re

from sumbi.core.records import Coverage, records
from sumbi.core.values import integer, mapping, timestamp
from sumbi.events.schema import (
    Record, SessionStart, Context, Metadata, Tokens, TokenUsage, ToolEvidence,
    ToolInput, ToolOutput, ToolStart, ToolEnd, CommandExecution, FileEdit,
    Counter, Request, LocalText, Diagnostic, SourceIdentity, freeze, ModelRequest,
)
from .common import key, record_identity

KNOWN = {"assistant", "user", "system", "progress", "attachment", "summary",
    "file-history-snapshot", "file-history-delta", "queue-operation", "pr-link",
    "frame-link", "last-prompt", "custom-title", "agent-name", "agent-color",
    "saved_hook_context", "worktree", "bridge_status", "legacy_bridge_status"}
SYSTEM = {"compact_boundary", "api_error", "stop_hook_summary", "local_command",
    "informational", "turn_duration", "request_start", "request_end"}
FIELDS = {"new_input": "input_tokens", "cache_write": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens", "output": "output_tokens"}


def bash_exit_code(event, block, *, run_in_background=False):
    """Use Bash's machine result and anchored error envelope, never stdout."""
    result = mapping(event.get("toolUseResult"))
    if run_in_background or result.get("interrupted") or result.get(
        "backgroundTaskId") or result.get("taskId"):
        return None
    for field in ("exitCode", "exit_code"):
        value = result.get(field)
        if type(value) is int:
            return value
    content = block.get("content")
    if block.get("is_error") is True and isinstance(content, str):
        match = re.match(r"\AExit code(?::)? (-?\d+)\n", content)
        return int(match.group(1)) if match else None
    if (block.get("is_error") is not True and result.get("interrupted") is False
        and isinstance(result.get("stdout"), str) and isinstance(result.get("stderr"), str)):
        return 0
    return None


def _blocks(event, kind, blocks, when):
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        suffix = ":" + str(index)
        if kind == "assistant" and block.get("type") == "tool_use":
            identity = key(block.get("id") or None)
            source = SourceIdentity(freeze(block["id"])) if block.get("id") else None
            name, args = block.get("name"), mapping(block.get("input"))
            if name == "Bash":
                yield CommandExecution(identity, freeze(args.get("command")), started_at=when,
                    cwd=freeze(args.get("cwd", event.get("cwd"))), phase="start",
                    pairing="launch", suffix=suffix,
                    deferred=args.get("run_in_background", False) is not False,
                    source_identity=source)
            if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                target = args.get("file_path") or args.get("notebook_path")
                yield FileEdit(identity, suffix,
                    (target if isinstance(target, str) else None,), event.get("cwd"))
            yield ToolStart(identity, suffix)
            if name == "AskUserQuestion":
                yield Counter("user_input_requests", identity, suffix)
        elif kind == "user" and block.get("type") == "tool_result":
            identity = key(block.get("tool_use_id") or None)
            source = SourceIdentity(freeze(block["tool_use_id"])) if block.get(
                "tool_use_id") else None
            # Pairing and deferred-launch status are resolved by the builder.
            result = mapping(event.get("toolUseResult"))
            error = block.get("is_error")
            yield CommandExecution(identity, exit_code=bash_exit_code(event, block),
                phase="end", pairing="launch", suffix=suffix, require_pair=True,
                source_identity=source, error=error if type(error) is bool else None,
                deferred=bool(result.get("interrupted") or result.get("backgroundTaskId")
                    or result.get("taskId")))
            yield ToolEnd(identity, block.get("is_error") is True, suffix)


def _translate(event, when, parent, child):
    kind = event.get("type")
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    yield SessionStart(cwd, parent if child else None, "worker" if child else "orchestrator",
        "claude-code", provenance="first-observed-cwd",
        dispatch_kind="subagent" if child else "interactive")
    if event.get("version"):
        yield Metadata(cli_version=freeze(event["version"]), supplied=("cli_version",))
    message = mapping(event.get("message"))
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    if kind == "user" and not any(mapping(b).get("type") == "tool_result" for b in blocks):
        yield Context(cwd, branch=freeze(event.get("gitBranch")))
    if kind not in KNOWN:
        yield Diagnostic(freeze(kind))
    inputs = tuple(ToolInput(key(b.get("id")), b.get("name"), freeze(b.get("input")),
        SourceIdentity(freeze(b.get("id"))))
        for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use")
    outputs = tuple(ToolOutput(key(b.get("tool_use_id")), freeze(b.get("content")),
        text_blocks=True,
        source_identity=SourceIdentity(freeze(b.get("tool_use_id"))))
        for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result")
    evidence = ToolEvidence(inputs, outputs, cwd, freeze(event.get("gitBranch")))
    usage = mapping(message.get("usage"))
    if kind != "assistant" or not usage:
        yield evidence
    else:
        yield ToolEvidence(inputs, outputs, cwd, freeze(event.get("gitBranch")), apply=False)
    yield LocalText(freeze(content))
    yield LocalText(freeze(event.get("content")))
    if kind == "system":
        subtype = event.get("subtype")
        if subtype not in SYSTEM:
            yield Diagnostic("system:" + str(subtype))
        if subtype in ("compact_boundary", "api_error"):
            yield Counter("compactions" if subtype == "compact_boundary" else "api_errors")
        elif subtype in ("request_start", "request_end"):
            yield Request(key(event.get("requestId") or None), endpoint="start"
                if subtype == "request_start" else "end")
    if kind == "assistant":
        if message.get("model"):
            yield Metadata(model=freeze(message["model"]), supplied=("model",))
        if usage:
            values = {k: integer(usage.get(v)) for k, v in FIELDS.items()}
            if when is None or values["output"] is None:
                yield Diagnostic("", "invalid_token_records")
            else:
                yield TokenUsage(Tokens(**values), cwd, "maximal_output",
                    key(message.get("id") or event.get("uuid") or None), evidence)
    yield from _blocks(event, kind, blocks, when)


def collect(home: Path, coverage: Coverage):
    """Yield records in file order, retaining native stream identity hints."""
    root = home / ".claude/projects"
    files = sorted(root.glob("*/*.jsonl")) + sorted(root.glob("*/**/subagents/**/*.jsonl"))
    for path in dict.fromkeys(files):
        parts = path.relative_to(root).parts
        child = "subagents" in parts
        indices = [i for i, part in enumerate(parts) if part == "subagents"]
        index = indices[-1] if child else None
        parent_hint = parts[index - 1] if child else path.stem
        agent_hint = (parts[index + 1] if child
            else path.stem).removesuffix(".jsonl").removeprefix("agent-")
        raw_id = None
        parent = None
        status = _model_request(root, parts, index, path) if child else "unknown"
        for event in records(path, coverage):
            if raw_id is None:
                child = child or (path.stem.startswith("agent-")
                    and event.get("isSidechain") is True)
                parent = str(event.get("sessionId") or parent_hint)
                if len(indices) > 1 and ":subagent:" not in parent:
                    for ancestor in indices[:-1]:
                        parent += ":subagent:" + parts[ancestor + 1].removeprefix("agent-")
                raw_id = parent + ":subagent:" + str(event.get("agentId")
                    or agent_hint) if child else parent
                if child and index is None:
                    status = _model_request(root, parts, index, path)
            when = timestamp(event.get("timestamp"))
            yield Record("claude-code", raw_id, when, record_identity(event),
                (ModelRequest(status), *tuple(_translate(event, when, parent, child))),
                timestamp_supplied="timestamp" in event, fallback_id=key(event.get("uuid") or None),
                parent_session_id=parent if child else None, worker=child)


def _model_request(root, parts, index, path):
    """Inspect only model presence; do not retain any sidecar values or text."""
    if index is not None:
        stem = parts[index + 1].removesuffix(".jsonl")
        path = root.joinpath(*parts[:index + 1], stem + ".meta.json")
    else:
        path = path.with_suffix(".meta.json")
    try:
        with path.open(encoding="utf-8") as stream:
            metadata = json.load(stream)
        if not isinstance(metadata, dict):
            return "unknown"
        return "requested" if "model" in metadata else "unrequested"
    except (OSError, ValueError, UnicodeError):
        return "unknown"
