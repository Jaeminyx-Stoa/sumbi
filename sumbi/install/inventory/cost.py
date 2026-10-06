"""Script-aware root, inherited-scope and conditional instruction estimates."""

from pathlib import PurePosixPath

from ..text import ESTIMATE_RULE, estimate_tokens
from .context import InventoryContext


def scan(context: InventoryContext) -> dict:
    instructions, imports, content = context.instructions, context.imports, context.content
    budget, descriptions, prompt_hooks = (context.budget, context.skill_descriptions,
        context.prompt_hooks)
    conditional_rules = context.conditional_rules
    agent_sources = {"codex": instructions["agents"], "claude": instructions["claude"],
        "gemini": instructions["gemini"], "copilot": [], "cursor": []}
    global_sources = {"codex": [],
        "claude": [p for p in instructions["claude_rules"] if p not in conditional_rules],
        "gemini": [],
        "copilot": instructions["copilot"], "cursor": instructions["cursor_rules"]}
    costs = {}
    for agent, main_sources in agent_sources.items():
        scopes = sorted({PurePosixPath(p).parent for p in main_sources} | {PurePosixPath(".")},
            key=str)
        largest_sources, largest_text, root_text, largest_tokens = [], "", "", 0
        for scope in scopes:
            ancestors = {scope, *scope.parents}
            sources = {p for p in main_sources
                if PurePosixPath(p).parent in ancestors} | set(global_sources[agent])
            if agent == "claude":
                pending = list(sources)
                while pending:
                    source = pending.pop()
                    for item in imports:
                        if (item["source"] == source and item["status"] == "resolved"
                            and item["path"] not in sources):
                            sources.add(item["path"])
                            pending.append(item["path"])
                sources.difference_update(conditional_rules)
            sources = sorted(sources)
            text = "".join(content(path) for path in sources)
            tokens = estimate_tokens(text)
            if scope == PurePosixPath("."):
                root_text = text
            if tokens > largest_tokens or not largest_sources:
                largest_text, largest_sources = text, sources
                largest_tokens = tokens
        costs[agent] = {"paths": largest_sources, "characters": len(largest_text),
            "estimated_tokens": largest_tokens,
            "root_estimated_tokens": estimate_tokens(root_text)}
    return {"estimate_rule": ESTIMATE_RULE, "budget": budget,
        "instructions": costs, "conditional_instructions": {"claude": {
            "paths": conditional_rules,
            "characters": sum(len(content(p)) for p in conditional_rules),
            "estimated_tokens": estimate_tokens("".join(content(p)
                for p in conditional_rules))}},
        "skill_descriptions": descriptions,
        "every_prompt_hooks": prompt_hooks}
