"""Recognize anchored native PreToolUse failures; never search arbitrary output prose."""

import json
import re


CLASSES = ("command_form", "path_scope", "size_limit", "tool_not_allowed", "wait_timeout",
    "other")
# First matching row wins. This small English keyword table is a heuristic.
KEYWORDS = (
    ("wait_timeout", ("timeout", "timed out", "slot wait", "did not respond")),
    ("size_limit", ("size limit", "too large", "too big", "maximum size", "file size")),
    ("path_scope", ("path scope", "outside", "worktree", "allowed path", "allowed directory")),
    ("tool_not_allowed", ("tool not allowed", "tool is not allowed", "disallowed tool",
        "forbidden tool", "unsupported tool")),
    ("command_form", ("accepted:", "command form", "command format", "use the form")),
)
CODEX_PREFIX = "Command blocked by PreToolUse hook:"
TOOL_PREFIX = "Tool call blocked by PreToolUse hook:"
CLAUDE_TIMEOUT = "PreToolUse hook did not respond before its timeout"


def content_text(value, agent):
    """Only the leading contiguous text content can establish an anchored failure."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    kind = "input_text" if agent == "codex" else "text"
    texts = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != kind:
            break
        if not isinstance(item.get("text"), str):
            break
        texts.append(item["text"])
    return "\n".join(texts) if texts else None


def recognize(value, agent, tool):
    """Return (reason class, normalized first clause, timeout) for a paired result."""
    text = content_text(value, agent)
    if text is None or not isinstance(tool, str):
        return None
    if agent == "codex":
        if text.startswith(TOOL_PREFIX):
            return _reason(text[len(TOOL_PREFIX):].strip(), False)
        # Code-mode wrappers are accepted only for that tool's own result.
        if tool.split(".")[-1] == "exec" and text.startswith("Script error:"):
            text = text[len("Script error:"):].lstrip()
            if text.startswith("Error: "):
                text = text[len("Error: "):]
        if text.startswith("{"):
            try:
                rejected = json.loads(text)
            except (ValueError, TypeError):
                return None
            if not isinstance(rejected, dict) or rejected.get("status") != "rejected":
                rejected = (rejected.get("result") if isinstance(rejected, dict)
                    and tool.split(".")[-1] == "exec" else None)
                if not isinstance(rejected, dict) or rejected.get("status") != "rejected":
                    return None
            text = rejected.get("reason")
        if not isinstance(text, str) or not text.startswith(CODEX_PREFIX):
            return None
        reason, timeout = text[len(CODEX_PREFIX):].strip(), False
    elif agent == "claude-code":
        prefix = "PreToolUse:" + tool + " hook error:"
        if text.startswith(CLAUDE_TIMEOUT):
            reason, timeout = text, True
        elif text.startswith(prefix):
            reason, timeout = text[len(prefix):].strip(), False
        else:
            return None
    else:
        return None
    return _reason(reason, timeout)


def _reason(reason, timeout):
    folded = reason.casefold()
    category = next((name for name, words in KEYWORDS
        if any(word in folded for word in words)), "other")
    return category, normalize_reason(reason), timeout


def recognize_all(value, agent, tool):
    """Return every denial with an inner-exec flag, preserving item boundaries."""
    if agent == "codex" and isinstance(tool, str) and tool.split(".")[-1] == "exec" \
            and isinstance(value, list):
        denials = []
        for item in value:
            if not isinstance(item, dict) or item.get("type") != "input_text":
                continue
            text = item.get("text")
            # The tool-level prefix applies to a whole output string only.
            if not isinstance(text, str) or text.startswith(TOOL_PREFIX):
                continue
            denial = recognize(text, agent, tool)
            if denial is not None:
                denials.append((*denial, True))
        return denials
    denial = recognize(value, agent, tool)
    return [(*denial, False)] if denial is not None else []


def candidate_output(value, agent):
    """Keep only anchored candidates and failure headers until call pairing."""
    prefixes = (CODEX_PREFIX, TOOL_PREFIX, "PreToolUse", "Script error:", "Script failed", "{")
    if agent == "codex" and isinstance(value, list):
        return [{"type": item.get("type"), "text": item["text"]
            if isinstance(item.get("text"), str) and item["text"].startswith(prefixes)
            else ""} if isinstance(item, dict) else {} for item in value]
    text = content_text(value, agent)
    return text if text and text.startswith(prefixes) else None


def normalize_reason(reason):
    """Remove operands before clause splitting so punctuation inside them cannot split."""
    text = re.sub(r'''"[^"\n]*"|'[^'\n]*'|`[^`\n]*`''', " ", reason)
    # Absolute, relative, home and drive paths, including unquoted operands.
    text = re.sub(r"(?:[A-Za-z]:[\\/]|[~.]*[/\\])[^\s;,]+", " ", text)
    text = re.sub(r"\b[^\s;,]*[/\\][^\s;,]*", " ", text)
    text = re.sub(r"\b[\w.-]+\.[A-Za-z][A-Za-z0-9_-]*\b", " ", text)
    text = re.split(r"[;\n]|(?<=[.!?])\s", text, maxsplit=1)[0]
    text = re.sub(r"\d+(?:[.,]\d+)*", " ", text)
    return " ".join(text.casefold().split()).rstrip(".!?").strip()


def failed_envelope(text, agent, tool):
    """A rejected or failed exec result cannot establish successful recovery."""
    text = content_text(text, agent)
    if agent != "codex" or not isinstance(text, str) or not isinstance(tool, str):
        return False
    if tool.split(".")[-1] == "exec" and text.startswith(("Script error:", "Script failed")):
        return True
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return False
        return isinstance(value, dict) and value.get("status") == "rejected"
    return False
