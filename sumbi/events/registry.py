"""Local log adapters and their roots."""

from sumbi.events.adapters import claude_code, codex, sumbi_events

ADAPTERS = {"claude-code": claude_code, "codex": codex, "sumbi-events": sumbi_events}


DEFAULT_AGENTS = ("claude-code", "codex")


def log_roots(home):
    return [home / ".claude/projects", home / ".codex/sessions", home / ".sumbi/events"]
