"""Agent entry files and rules, including conditional Claude front matter."""

from pathlib import PurePosixPath
import re

from .context import InventoryContext, entry


# Bound the YAML rule front matter without parsing objects.
RULE_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", re.S)

# Recognize a plain or quoted top-level paths key.
RULE_PATHS = re.compile(r"^(?:paths|'paths'|\"paths\"):[ \t]*(.*)$")

# Whitespace starts comments while quoted glob hashes survive.
YAML_COMMENT = re.compile(r"\s+#")


def conditional_paths(text: str) -> bool:
    """Recognize non-empty paths in a bounded YAML front-matter subset.

    Scalar, flow-list and indented block forms are supported. No YAML objects
    are constructed and no path expressions or tags are executed.
    """
    frontmatter = RULE_FRONT_MATTER.match(text)
    if not frontmatter:
        return False
    lines = frontmatter[1].splitlines()
    empty = {"", "[]", "{}", "null", "Null", "NULL", "~", "''", '""'}
    for index, line in enumerate(lines):
        match = RULE_PATHS.match(line)
        if not match:
            continue
        # Comments start at whitespace; a '#' inside a quoted glob is content.
        value = YAML_COMMENT.split(match[1], maxsplit=1)[0].strip()
        if value.startswith("#"):
            value = ""
        if value.startswith("[") and value.endswith("]") and not value[1:-1].strip(" \t,\"'"):
            continue
        if value in empty and value:
            continue
        if value not in empty and value not in {"|", ">", "|-", ">-", "|+", ">+"}:
            return True
        for following in lines[index + 1:]:
            if not following.strip() or following.lstrip().startswith("#"):
                continue
            if not following.startswith((" ", "\t", "-")):
                break
            item = YAML_COMMENT.split(following.strip().removeprefix("-").strip(), maxsplit=1)[0].strip()
            if item not in empty:
                return True
    return False


def scan(context: InventoryContext) -> dict:
    paths = context.paths
    instructions = {
        "agents": [p for p in paths if PurePosixPath(p).name == "AGENTS.md"],
        "claude": [p for p in paths if PurePosixPath(p).name == "CLAUDE.md"],
        "claude_rules": [p for p in paths if p.startswith(".claude/rules/") and p.endswith(".md")],
        "gemini": [p for p in paths if PurePosixPath(p).name == "GEMINI.md"],
        "copilot": [p for p in paths if p == ".github/copilot-instructions.md"],
        "cursor_rules": [p for p in paths if p.startswith(".cursor/rules/")],
    }
    context.instructions = instructions
    return {key: entry(value) for key, value in instructions.items()}
