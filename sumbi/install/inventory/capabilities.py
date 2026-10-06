"""Skills and their metadata, commands, agents, hook declarations and MCP counts."""

from pathlib import PurePosixPath
import re
import textwrap

from ..text import estimate_tokens
from .context import InventoryContext, entry


# Read only the bounded skill front-matter block.
SKILL_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", re.S)

# Capture a scalar or indented skill description.
SKILL_DESCRIPTION = re.compile(r"(?m)^description:\s*(.*?)(?=\n[^ \t]|\Z)", re.S)


def scan(context: InventoryContext) -> dict:
    paths = context.paths
    areas = {
        "claude_skills": ".claude/skills/", "agent_skills": ".agents/skills/",
        "commands": ".claude/commands/", "agents": ".claude/agents/",
    }
    capabilities = {}
    for key, prefix in areas.items():
        files = [p for p in paths if p.startswith(prefix)]
        entries = [p for p in files
            if PurePosixPath(p).name == "SKILL.md"] if key.endswith("skills") else files
        capabilities[key] = {**entry(entries), "file_count": len(files)}
    context.capabilities = capabilities
    context.skill_descriptions = skill_descriptions(context, areas)
    configs = configurations(context)
    return {"capabilities": capabilities, "configurations": configs}


def skill_descriptions(context: InventoryContext, areas: dict) -> list[dict]:
    paths, content = context.paths, context.content
    descriptions = []
    for path in paths:
        if PurePosixPath(path).name != "SKILL.md" or not path.startswith(tuple(areas[k]
            for k in ("claude_skills", "agent_skills"))):
            continue
        text = content(path)
        metadata = SKILL_FRONT_MATTER.match(text)
        description = ""
        if metadata:
            match = SKILL_DESCRIPTION.search(metadata.group(1))
            if match:
                description = match.group(1).strip()
                if description.startswith(("|", ">")):
                    indicator, _, body = description.partition("\n")
                    description = textwrap.dedent(body).strip()
                    if indicator.startswith(">"):
                        description = " ".join(description.splitlines())
                elif (len(description) >= 2 and description[0] == description[-1]
                    and description[0] in "\"'"):
                    description = description[1:-1]
        descriptions.append({"path": path, "characters": len(description),
            "estimated_tokens": estimate_tokens(description)})

    return descriptions


def configurations(context: InventoryContext) -> list[dict]:
    paths, structured = context.paths, context.structured
    config_paths = [p
        for p in (".claude/settings.json", ".codex/config.toml", ".codex/hooks.json",
            ".mcp.json") if p in paths]
    configs, prompt_hooks = [], []
    for path in config_paths:
        data = structured(path, path.endswith(".toml"))
        hooks = data.get("hooks", {})
        hook_count = 0
        if isinstance(hooks, dict):
            for event, groups in hooks.items():
                groups = groups if isinstance(groups, list) else [groups]
                count = 0
                for group in groups:
                    if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                        count += len(group["hooks"])
                    else:
                        count += 1
                hook_count += count
                if event.lower().replace("_", "").replace("-", "") == "userpromptsubmit":
                    prompt_hooks.append({"path": path, "event": "UserPromptSubmit", "count": count})
        permissions = data.get("permissions", {})
        permission_count = sum(len(v) for v in permissions.values()
            if isinstance(v, list)) if isinstance(permissions, dict) else 0
        mcp = data.get("mcpServers", data.get("mcp_servers", {}))
        configs.append({"path": path, "hooks": hook_count,
            "permission_entries": permission_count,
            "mcp_servers": len(mcp) if isinstance(mcp, dict) else 0,
            "approval_policy_present": "approval_policy" in data,
            "sandbox_mode_present": "sandbox_mode" in data})

    context.prompt_hooks = prompt_hooks
    return configs
