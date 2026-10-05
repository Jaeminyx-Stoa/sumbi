"""Read repository-local evidence without executing project code or commands."""

from __future__ import annotations

import ast
import configparser
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import textwrap
import tomllib

from .errors import InstallError
from .exclusions import GitIgnore, excluded_by, load_excludes

MAX_BYTES = 1_048_576
ESTIMATE_RULE = (
    "ceil(characters / 4), normalized line endings; unique instructions and local imports "
    "at root and largest inherited scope; all rule files included as an upper bound"
)


def safe_path(root: Path, relative: str) -> Path:
    """Reject ambiguous paths, links and traversal, including dangling links."""
    part = PurePosixPath(relative)
    components = relative.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {prefix + str(n) for prefix in ("COM", "LPT") for n in range(1, 10)}
    if (not relative or part.is_absolute() or "\\" in relative or ":" in relative
            or any(p in {"", ".", ".."} or p.endswith((".", " "))
                   or p.split(".", 1)[0].upper() in reserved
                   or any(ord(c) < 32 or c in '<>"|?*' for c in p)
                   for p in components)):
        raise InstallError("Unsafe repository-relative path.")
    current = root
    for component in part.parts:
        current = current / component
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise InstallError("Repository links are not followed.")
    if not current.resolve().is_relative_to(root.resolve()):
        raise InstallError("Path escapes the repository.")
    return current


def read_bytes(root: Path, relative: str) -> bytes | None:
    path = safe_path(root, relative)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise InstallError("Expected a bounded regular file.")
    try:
        return path.read_bytes()
    except OSError:
        raise InstallError("Cannot read a repository file.") from None


def _paths(root: Path, patterns: tuple[str, ...]) -> tuple[list[str], list[dict], dict, dict, set[str]]:
    found, warnings = [], []
    counts = dict.fromkeys(patterns, 0)
    git = GitIgnore(root)
    contexts = {root: git}
    nested, ignored = [], set()
    ignored_count = 0
    if git.versioned is False:
        warnings.append({"kind": "root-not-versioned"})
    def failed(_error: OSError) -> None:
        warnings.append({"kind": "unreadable-directory"})
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        base = Path(directory)
        context_root = next(p for p in (base, *base.parents) if p in contexts)
        context = contexts[context_root]
        marker = base / ".git"
        if base != root and (".git" in dirs or ".git" in files) and not marker.is_symlink() and not getattr(marker, "is_junction", lambda: False)():
            candidate = GitIgnore(base)
            contexts[base] = candidate
            context_root, context = base, candidate
            if candidate.versioned:
                nested.append(base.relative_to(root).as_posix())
        ignored.update((context_root.relative_to(root) / p).as_posix()
                       for p in context.ignored if context.available)
        kept = []
        for name in sorted(dirs):
            path = base / name
            pattern = excluded_by(path.relative_to(root).as_posix(), patterns)
            if pattern:
                counts[pattern] += 1
                continue
            if context.excludes(path.relative_to(context_root).as_posix()):
                ignored_count += 1
                continue
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                warnings.append({"kind": "skipped-link", "path": path.relative_to(root).as_posix()})
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            pattern = excluded_by(relative, patterns)
            if pattern:
                counts[pattern] += 1
                continue
            if context.excludes(path.relative_to(context_root).as_posix()):
                ignored_count += 1
                continue
            if path.is_symlink():
                warnings.append({"kind": "skipped-link", "path": relative})
            else:
                found.append(relative)
    if any(not context.available for context in contexts.values()):
        warnings.append({"kind": "gitignore-unavailable"})
    return sorted(found), warnings, {
        "count": sum(counts.values()) + ignored_count,
        "gitignore_count": ignored_count,
        "patterns": [{"pattern": p, "count": count} for p, count in counts.items()],
    }, {"root_versioned": git.versioned, "nested_repositories": _entry(nested)}, ignored


def _without_code(text: str) -> str:
    """Remove fenced blocks and matching backtick spans before import parsing."""
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        if fence is not None:
            if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*(?:\n)?$", line):
                fence = None
            lines.append("\n")
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)", line)
        if opening:
            fence = (opening[1][0], len(opening[1]))
            lines.append("\n")
        else:
            lines.append(line)
    text = "".join(lines)
    runs = list(re.finditer(r"`+", text))
    output, start, index = [], 0, 0
    while index < len(runs):
        opening = runs[index]
        closing = next((j for j in range(index + 1, len(runs))
                        if len(runs[j][0]) == len(opening[0])), None)
        if closing is None:
            index += 1
            continue
        output.append(text[start:opening.start()])
        output.append(" ")
        start = runs[closing].end()
        index = closing + 1
    output.append(text[start:])
    return "".join(output)


def _entry(paths: list[str]) -> dict:
    return {"count": len(paths), "paths": sorted(paths)}


def inventory(repository: Path | str = ".", budget: int = 2000, *,
              exclude: list[str] | tuple[str, ...] = ()) -> dict:
    """Return only relative paths, counts, public labels and static diagnostics."""
    root = Path(repository).resolve()
    if not root.is_dir() or budget < 1:
        raise InstallError("Expected a repository directory and a positive token budget.")
    patterns = load_excludes(root, exclude)
    paths, warnings, exclusions, versioning, ignored = _paths(root, patterns)
    texts: dict[str, str] = {}

    def content(path: str) -> str:
        if path not in texts:
            try:
                raw = read_bytes(root, path)
                texts[path] = (raw or b"").decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            except (InstallError, UnicodeError, OSError):
                warnings.append({"kind": "unreadable-or-oversized", "path": path})
                texts[path] = ""
        return texts[path]

    def structured(path: str, toml: bool = False) -> dict:
        try:
            value = tomllib.loads(content(path)) if toml else json.loads(content(path))
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
        warnings.append({"kind": "invalid-config", "path": path})
        return {}

    def ini(path: str) -> configparser.RawConfigParser:
        parser = configparser.RawConfigParser()
        try:
            parser.read_string(content(path))
        except configparser.Error:
            warnings.append({"kind": "invalid-config", "path": path})
        return parser

    instructions = {
        "agents": [p for p in paths if PurePosixPath(p).name == "AGENTS.md"],
        "claude": [p for p in paths if PurePosixPath(p).name == "CLAUDE.md"],
        "claude_rules": [p for p in paths if p.startswith(".claude/rules/")],
        "gemini": [p for p in paths if PurePosixPath(p).name == "GEMINI.md"],
        "copilot": [p for p in paths if p == ".github/copilot-instructions.md"],
        "cursor_rules": [p for p in paths if p.startswith(".cursor/rules/")],
    }
    imports, visited = [], set()
    def follow(source: str) -> None:
        if source in visited:
            return
        visited.add(source)
        # Imports must occupy a token boundary; email addresses are not imports.
        for match in re.finditer(r"(?<![\w@])@(?:\"([^\"\n]+)\"|([^\s`<>]+))", _without_code(content(source))):
            target = (match.group(1) or match.group(2)).rstrip(",;)")
            # Only local relative references are inspected; no home expansion.
            if target.startswith(("/", "~")) or "\\" in target or ":" in target:
                imports.append({"source": source, "status": "external-not-read"})
                continue
            candidate = PurePosixPath(source).parent / target
            parts: list[str] = []
            escaped = False
            for component in candidate.parts:
                if component == "..":
                    if not parts:
                        escaped = True
                        break
                    parts.pop()
                elif component != ".":
                    parts.append(component)
            relative = "/".join(parts)
            if escaped or not relative:
                imports.append({"source": source, "status": "external-not-read"})
                continue
            if excluded_by(relative, patterns) or any(p.as_posix() in ignored for p in (PurePosixPath(relative), *PurePosixPath(relative).parents)):
                imports.append({"source": source, "status": "excluded-not-read"})
                continue
            status = "resolved" if relative in paths else "missing-or-link"
            imports.append({"source": source, "path": relative, "status": status})
            if status == "resolved":
                follow(relative)
    for path in instructions["claude"]:
        follow(path)

    areas = {
        "claude_skills": ".claude/skills/", "agent_skills": ".agents/skills/",
        "commands": ".claude/commands/", "agents": ".claude/agents/",
    }
    capabilities = {}
    for key, prefix in areas.items():
        files = [p for p in paths if p.startswith(prefix)]
        entries = [p for p in files if PurePosixPath(p).name == "SKILL.md"] if key.endswith("skills") else files
        capabilities[key] = {**_entry(entries), "file_count": len(files)}
    descriptions = []
    for path in paths:
        if PurePosixPath(path).name != "SKILL.md" or not path.startswith(tuple(areas[k] for k in ("claude_skills", "agent_skills"))):
            continue
        text = content(path)
        metadata = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
        description = ""
        if metadata:
            match = re.search(r"(?m)^description:\s*(.*?)(?=\n[^ \t]|\Z)", metadata.group(1), re.S)
            if match:
                description = match.group(1).strip()
                if description.startswith(("|", ">")):
                    indicator, _, body = description.partition("\n")
                    description = textwrap.dedent(body).strip()
                    if indicator.startswith(">"):
                        description = " ".join(description.splitlines())
                elif len(description) >= 2 and description[0] == description[-1] and description[0] in "\"'":
                    description = description[1:-1]
        descriptions.append({"path": path, "characters": len(description),
                             "estimated_tokens": math.ceil(len(description) / 4)})

    config_paths = [p for p in (".claude/settings.json", ".codex/config.toml", ".codex/hooks.json", ".mcp.json") if p in paths]
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
        permission_count = sum(len(v) for v in permissions.values() if isinstance(v, list)) if isinstance(permissions, dict) else 0
        mcp = data.get("mcpServers", data.get("mcp_servers", {}))
        configs.append({"path": path, "hooks": hook_count,
                        "permission_entries": permission_count,
                        "mcp_servers": len(mcp) if isinstance(mcp, dict) else 0,
                        "approval_policy_present": "approval_policy" in data,
                        "sandbox_mode_present": "sandbox_mode" in data})

    workflows = []
    for path in paths:
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            # A deliberately bounded YAML subset; unsupported syntax is visible.
            jobs, in_jobs, job_indent, property_indent = [], False, None, None
            for line in content(path).splitlines():
                if re.match(r"^jobs:\s*(?:#.*)?$", line):
                    in_jobs = True
                elif in_jobs and re.match(r"^\S", line):
                    in_jobs = False
                elif in_jobs:
                    match = re.match(r"^( +)(?:['\"])?([A-Za-z0-9_-]+)(?:['\"])?:\s*(.*)$", line)
                    if match and job_indent is None:
                        job_indent = len(match.group(1))
                    if match and len(match.group(1)) == job_indent:
                        jobs.append({"id": match.group(2)})
                        property_indent = None
                        if match.group(3) and not match.group(3).startswith("#"):
                            warnings.append({"kind": "workflow-job-details-unknown", "path": path})
                    property_line = re.match(r"^( +)[A-Za-z][A-Za-z0-9_-]*:", line)
                    if property_line and jobs and len(property_line.group(1)) > job_indent and property_indent is None:
                        property_indent = len(property_line.group(1))
                    name = re.match(r"^( +)name:\s*(.*?)\s*$", line)
                    if name and jobs and len(name.group(1)) == property_indent:
                        value = name.group(2)
                        if value.startswith(("|", ">", "&", "!", "*")):
                            warnings.append({"kind": "workflow-name-unknown", "path": path})
                        else:
                            jobs[-1]["name"] = value.strip("\"'")
            workflows.append({"path": path, "jobs": jobs, "job_count": len(jobs),
                              "parser": "block YAML subset"})
            if not jobs:
                warnings.append({"kind": "workflow-jobs-unknown", "path": path})

    codeowners = [p for p in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS") if p in paths]
    templates = [p for p in paths if p.lower() in {"pull_request_template.md", ".github/pull_request_template.md", "docs/pull_request_template.md"}
                 or p.lower().startswith(".github/pull_request_template/")]
    git_hooks = [p for p in paths if p in {".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml"}
                 or p.startswith((".husky/", ".githooks/"))]
    lint, formatting, typing = [], [], []
    test_sources = []
    for path in paths:
        name = PurePosixPath(path).name
        if name in {".flake8", ".pylintrc", "ruff.toml", ".ruff.toml", "clippy.toml", ".clippy.toml", ".golangci.yml", ".golangci.yaml", "biome.json"} or name.startswith((".eslintrc", "eslint.config.", ".stylelintrc", "stylelint.config.")):
            lint.append(path)
        if name.startswith((".prettierrc", "prettier.config.")) or name in {".clang-format", ".yapf", ".editorconfig", "rustfmt.toml", ".rustfmt.toml", "biome.json"}:
            formatting.append(path)
        if name in {"mypy.ini", ".mypy.ini", "pyrightconfig.json", "tsconfig.json"} or name.startswith("tsconfig.") and name.endswith(".json"):
            typing.append(path)
        if name == "package.json":
            data = structured(path)
            scripts = data.get("scripts", {})
            if isinstance(scripts, dict):
                if any(re.match(r"^(test|check:test)(:|$)", key) and isinstance(v, str) and v.strip() for key, v in scripts.items()):
                    test_sources.append({"path": path, "kind": "package-script"})
                for prefix, group in (("lint", lint), ("format", formatting), ("typecheck", typing)):
                    if any(key.startswith(prefix) for key in scripts):
                        group.append(path)
            for key, group in (("eslintConfig", lint), ("prettier", formatting)):
                if key in data:
                    group.append(path)
        if name == "pyproject.toml":
            data = structured(path, True)
            tool = data.get("tool", {})
            if isinstance(tool, dict):
                for keys, group in (({"ruff", "pylint", "flake8"}, lint), ({"black", "isort", "ruff"}, formatting), ({"mypy", "pyright"}, typing)):
                    if keys.intersection(tool):
                        group.append(path)
                if "pytest" in tool:
                    test_sources.append({"path": path, "kind": "pytest-config"})
                scripts = tool.get("poetry", {}).get("scripts", {}) if isinstance(tool.get("poetry"), dict) else {}
                if isinstance(scripts, dict) and "test" in scripts:
                    test_sources.append({"path": path, "kind": "project-script"})
            script_groups = []
            for chain in (("project", "scripts"), ("tool", "pdm", "scripts"), ("tool", "poe", "tasks"), ("tool", "hatch", "envs", "default", "scripts")):
                group = data
                for key in chain:
                    group = group.get(key, {}) if isinstance(group, dict) else {}
                script_groups.append(group)
            if any(isinstance(group, dict) and "test" in group for group in script_groups):
                source = {"path": path, "kind": "project-script"}
                if source not in test_sources:
                    test_sources.append(source)
        if name in {"Makefile", "makefile", "GNUmakefile"} and re.search(r"(?m)^(?:test|check|tests)(?:[ \t]+[^:]+)?:[^=]", content(path)):
            test_sources.append({"path": path, "kind": "make-target"})
        if name in {"tox.ini", "setup.cfg"}:
            config = ini(path)
            if any(s.startswith("testenv") and config.get(s, "commands", fallback="").strip() for s in config.sections()):
                test_sources.append({"path": path, "kind": "tox-command"})
            for section, group in (("flake8", lint), ("isort", formatting), ("mypy", typing)):
                if config.has_section(section):
                    group.append(path)
            if config.has_section("tool:pytest"):
                test_sources.append({"path": path, "kind": "pytest-config"})
        if name in {"noxfile.py", "noxfile"}:
            try:
                tree = ast.parse(content(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "run":
                        continue
                    args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in node.args]
                    if args and (args[0] in {"pytest", "unittest"} or args[:3] in (["python", "-m", "pytest"], ["python", "-m", "unittest"], ["python3", "-m", "pytest"], ["python3", "-m", "unittest"])):
                        test_sources.append({"path": path, "kind": "nox-command"})
                        break
            except SyntaxError:
                warnings.append({"kind": "invalid-config", "path": path})
        if path.endswith(".md") and not path.startswith(("sumbi/", "tests/")):
            if re.search(r"\b(?:python(?:3)? -m (?:unittest|pytest)|pytest(?:\s|`|$)|npm (?:run )?test|pnpm test|yarn test|cargo test|go test|make test|tox(?:\s|`|$)|nox(?:\s|`|$))", content(path)):
                test_sources.append({"path": path, "kind": "documented-command"})

    convention_paths = sorted(
        {p for group in instructions.values() for p in group}
        | {item["path"] for item in imports if item["status"] == "resolved"}
        | {p for group in capabilities.values() for p in group["paths"]}
    )
    conventions = {key: [] for key in ("review_gate", "handoff", "friction_line", "parallel_worktree", "plan_approval")}
    patterns = {
        "review_gate": r"(?:\b(?:risk\w*|security|money|payment|auth\w*)\b[^.\n]{0,120}\b(?:require|must|need|before|gate)\b[^.\n]{0,120}\breview\b|\b(?:require|must|need|gate)\b[^.\n]{0,120}\breview\b[^.\n]{0,120}\b(?:risk\w*|security|money|payment|auth\w*)\b|reviewer from a different model family reviews)",
        "handoff": r"(?:\b(?:leave|write|create|keep|update|record|use|require|must)\b[^.\n]{0,100}\bhandoff\b|\bhandoff (?:document|record|template|convention)\b[^.\n]{0,100}\b(?:must|should|record|include|contain)\b)",
        "friction_line": r"\b(?:end|include|write|record|report|use|must)\b[^\n]{0,120}\b(?:harness )?friction:\s*",
        "parallel_worktree": r"(?:\bparallel\b[^.\n]{0,100}\b(?:use|require|must|need)\b[^.\n]{0,100}\bworktrees?\b|\b(?:use|require|must|assign)\b[^.\n]{0,100}\bparallel\b[^.\n]{0,100}\bworktrees?\b)",
        "plan_approval": r"(?:\b(?:write|record|create|critique|review|need|needs|must|approve|require)\b[^\n]{0,100}\bplan\b[^\n]{0,160}\b(?:approval|approve|before implementation)\b|\bplan\b[^\n]{0,80}\b(?:needs|must|requires)\b[^\n]{0,100}\b(?:approval|approve|before implementation)\b)",
    }
    for path in convention_paths:
        # Evidence must be a directive, not a roadmap, an example or a denial.
        guidance = re.sub(r"\A---\s*\n.*?\n---(?:\s*\n|$)", "", content(path), flags=re.S)
        guidance = re.sub(r"(?m)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", guidance, flags=re.S)
        text = "\n".join(line for line in guidance.splitlines() if not re.search(
            r"\b(?:roadmap|planned|ideas?|examples?|might|could|consider)\b|\b(?:not|never)\s+(?:require|use|write|need)",
            line, re.I))
        for key, pattern in patterns.items():
            if re.search(pattern, text, re.I):
                conventions[key].append(path)
    agent_sources = {"codex": instructions["agents"], "claude": instructions["claude"],
                     "gemini": instructions["gemini"], "copilot": [], "cursor": []}
    global_sources = {"codex": [], "claude": instructions["claude_rules"], "gemini": [],
                      "copilot": instructions["copilot"], "cursor": instructions["cursor_rules"]}
    costs = {}
    for agent, main_sources in agent_sources.items():
        scopes = sorted({PurePosixPath(p).parent for p in main_sources} | {PurePosixPath(".")}, key=str)
        largest_sources, largest_characters, root_characters = [], 0, 0
        for scope in scopes:
            ancestors = {scope, *scope.parents}
            sources = {p for p in main_sources if PurePosixPath(p).parent in ancestors} | set(global_sources[agent])
            if agent == "claude":
                pending = list(sources)
                while pending:
                    source = pending.pop()
                    for item in imports:
                        if item["source"] == source and item["status"] == "resolved" and item["path"] not in sources:
                            sources.add(item["path"])
                            pending.append(item["path"])
            sources = sorted(sources)
            characters = sum(len(content(path)) for path in sources)
            if scope == PurePosixPath("."):
                root_characters = characters
            if characters > largest_characters or not largest_sources:
                largest_characters, largest_sources = characters, sources
        costs[agent] = {"paths": largest_sources, "characters": largest_characters,
                        "estimated_tokens": math.ceil(largest_characters / 4),
                        "root_estimated_tokens": math.ceil(root_characters / 4)}
    return {
        "exclusions": exclusions,
        "versioning": versioning,
        "instructions": {key: _entry(value) for key, value in instructions.items()},
        "claude_imports": imports, "capabilities": capabilities,
        "configurations": configs,
        "enforcement": {"workflows": workflows, "codeowners": _entry(codeowners),
                        "pr_templates": _entry(templates), "git_hooks": _entry(git_hooks),
                        "lint": _entry(sorted(set(lint))), "format": _entry(sorted(set(formatting))),
                        "type_check": _entry(sorted(set(typing))), "test_sources": test_sources,
                        "required_checks": "unknown (offline)", "rulesets": "unknown (offline)"},
        "cost": {"estimate_rule": ESTIMATE_RULE, "budget": budget,
                 "instructions": costs, "skill_descriptions": descriptions,
                 "every_prompt_hooks": prompt_hooks},
        "conventions": conventions, "convention_search_paths": convention_paths, "warnings": warnings,
    }
