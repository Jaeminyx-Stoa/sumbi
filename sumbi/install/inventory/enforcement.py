"""Static workflow jobs, ownership, hooks, checks and test-command sources."""

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re

from .context import InventoryContext, entry


# Recognize the top-level block jobs mapping.
WORKFLOW_JOBS = re.compile(r"^jobs:\s*(?:#.*)?$")

# Stop jobs parsing at the next top-level key.
TOP_LEVEL_KEY = re.compile(r"^\S")

# Capture a plain or quoted job ID at its indentation.
WORKFLOW_JOB = re.compile(r"^( +)(?:['\"])?([A-Za-z0-9_-]+)(?:['\"])?:\s*(.*)$")

# Identify the first job property indentation.
WORKFLOW_PROPERTY = re.compile(r"^( +)[A-Za-z][A-Za-z0-9_-]*:")

# Capture a scalar job display name.
WORKFLOW_NAME = re.compile(r"^( +)name:\s*(.*?)\s*$")

# Recognize test and check:test script prefixes.
PACKAGE_TEST_SCRIPT = re.compile(r"^(test|check:test)(:|$)")

# Recognize declared test targets without assignments.
MAKE_TEST_TARGET = re.compile(r"(?m)^(?:test|check|tests)(?:[ \t]+[^:]+)?:[^=]")

# Detect documented test runners without exporting commands.
DOCUMENTED_TEST_COMMAND = re.compile(
    r"\b(?:python(?:3)? -m (?:unittest|pytest)|pytest(?:\s|`|$)"
    r"|npm (?:run )?test|pnpm test|yarn test|cargo test|go test|make test"
    r"|tox(?:\s|`|$)|nox(?:\s|`|$))"
)

LINT_FILES = {
    ".flake8", ".pylintrc", "ruff.toml", ".ruff.toml", "clippy.toml",
    ".clippy.toml", ".golangci.yml", ".golangci.yaml", "biome.json",
}
LINT_PREFIXES = (".eslintrc", "eslint.config.", ".stylelintrc", "stylelint.config.")
FORMAT_FILES = {".clang-format", ".yapf", ".editorconfig", "rustfmt.toml", ".rustfmt.toml",
    "biome.json"}
FORMAT_PREFIXES = (".prettierrc", "prettier.config.")
TYPE_FILES = {"mypy.ini", ".mypy.ini", "pyrightconfig.json", "tsconfig.json"}
PR_TEMPLATES = {"pull_request_template.md", ".github/pull_request_template.md",
    "docs/pull_request_template.md"}
PROJECT_SCRIPT_GROUPS = (
    ("project", "scripts"), ("tool", "pdm", "scripts"), ("tool", "poe", "tasks"),
    ("tool", "hatch", "envs", "default", "scripts"),
)


@dataclass
class _Sources:
    lint: list[str] = field(default_factory=list)
    formatting: list[str] = field(default_factory=list)
    typing: list[str] = field(default_factory=list)
    test_sources: list[dict] = field(default_factory=list)


def _workflows(context: InventoryContext) -> list[dict]:
    paths, content, warnings = context.paths, context.content, context.warnings
    workflows = []
    for path in paths:
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            # A deliberately bounded YAML subset; unsupported syntax is visible.
            jobs, in_jobs, job_indent, property_indent = [], False, None, None
            for line in content(path).splitlines():
                if WORKFLOW_JOBS.match(line):
                    in_jobs = True
                elif in_jobs and TOP_LEVEL_KEY.match(line):
                    in_jobs = False
                elif in_jobs:
                    match = WORKFLOW_JOB.match(line)
                    if match and job_indent is None:
                        job_indent = len(match.group(1))
                    if match and len(match.group(1)) == job_indent:
                        jobs.append({"id": match.group(2)})
                        property_indent = None
                        if match.group(3) and not match.group(3).startswith("#"):
                            warnings.append({"kind": "workflow-job-details-unknown", "path": path})
                    property_line = WORKFLOW_PROPERTY.match(line)
                    if property_line and jobs and len(
                        property_line.group(1)) > job_indent and property_indent is None:
                        property_indent = len(property_line.group(1))
                    name = WORKFLOW_NAME.match(line)
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

    return workflows


def scan(context: InventoryContext) -> dict:
    paths = context.paths
    workflow_entries = _workflows(context)
    codeowners = [p for p in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS") if p in paths]
    templates = [p for p in paths if p.lower() in PR_TEMPLATES
        or p.lower().startswith(".github/pull_request_template/")]
    git_hooks = [p for p in paths
        if p in {".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml"}
        or p.startswith((".husky/", ".githooks/"))]
    sources = _Sources()
    lint, formatting, typing = sources.lint, sources.formatting, sources.typing
    test_sources = sources.test_sources
    for path in paths:
        _file_checks(context, path, sources)
        _package_checks(context, path, sources)
        _project_checks(context, path, sources)
        _make_tests(context, path, sources)
        _ini_checks(context, path, sources)
        _nox_tests(context, path, sources)
        _documented_tests(context, path, sources)

    return {"workflows": workflow_entries, "codeowners": entry(codeowners),
        "pr_templates": entry(templates), "git_hooks": entry(git_hooks),
        "lint": entry(sorted(set(lint))), "format": entry(sorted(set(formatting))),
        "type_check": entry(sorted(set(typing))), "test_sources": test_sources,
        "required_checks": "unknown (offline)", "rulesets": "unknown (offline)"}


def _file_checks(context: InventoryContext, path: str, sources: _Sources) -> None:
    name = PurePosixPath(path).name
    lint, formatting, typing = sources.lint, sources.formatting, sources.typing
    if name in LINT_FILES or name.startswith(LINT_PREFIXES):
        lint.append(path)
    if name.startswith(FORMAT_PREFIXES) or name in FORMAT_FILES:
        formatting.append(path)
    if name in TYPE_FILES or name.startswith("tsconfig.") and name.endswith(".json"):
        typing.append(path)


def _package_checks(context: InventoryContext, path: str, sources: _Sources) -> None:
    name = PurePosixPath(path).name
    lint, formatting, typing = sources.lint, sources.formatting, sources.typing
    test_sources = sources.test_sources
    structured = context.structured
    if name == "package.json":
        data = structured(path)
        scripts = data.get("scripts", {})
        if isinstance(scripts, dict):
            if any(PACKAGE_TEST_SCRIPT.match(key) and isinstance(v, str) and v.strip() for key,
                v in scripts.items()):
                test_sources.append({"path": path, "kind": "package-script"})
            for prefix, group in (("lint", lint), ("format", formatting), ("typecheck", typing)):
                if any(key.startswith(prefix) for key in scripts):
                    group.append(path)
        for key, group in (("eslintConfig", lint), ("prettier", formatting)):
            if key in data:
                group.append(path)


def _project_checks(context: InventoryContext, path: str, sources: _Sources) -> None:
    name = PurePosixPath(path).name
    lint, formatting, typing = sources.lint, sources.formatting, sources.typing
    test_sources = sources.test_sources
    structured = context.structured
    if name == "pyproject.toml":
        data = structured(path, True)
        tool = data.get("tool", {})
        if isinstance(tool, dict):
            for keys, group in (({"ruff", "pylint", "flake8"}, lint),
                ({"black", "isort", "ruff"}, formatting), ({"mypy", "pyright"}, typing)):
                if keys.intersection(tool):
                    group.append(path)
            if "pytest" in tool:
                test_sources.append({"path": path, "kind": "pytest-config"})
            scripts = tool.get("poetry", {}).get("scripts",
                {}) if isinstance(tool.get("poetry"), dict) else {}
            if isinstance(scripts, dict) and "test" in scripts:
                test_sources.append({"path": path, "kind": "project-script"})
        script_groups = []
        for chain in PROJECT_SCRIPT_GROUPS:
            group = data
            for key in chain:
                group = group.get(key, {}) if isinstance(group, dict) else {}
            script_groups.append(group)
        if any(isinstance(group, dict) and "test" in group for group in script_groups):
            source = {"path": path, "kind": "project-script"}
            if source not in test_sources:
                test_sources.append(source)


def _make_tests(context: InventoryContext, path: str, sources: _Sources) -> None:
    name = PurePosixPath(path).name
    test_sources = sources.test_sources
    content = context.content
    if name in {"Makefile", "makefile", "GNUmakefile"} and MAKE_TEST_TARGET.search(content(path)):
        test_sources.append({"path": path, "kind": "make-target"})


def _ini_checks(context: InventoryContext, path: str, sources: _Sources) -> None:
    name = PurePosixPath(path).name
    lint, formatting, typing = sources.lint, sources.formatting, sources.typing
    test_sources = sources.test_sources
    ini = context.ini
    if name in {"tox.ini", "setup.cfg"}:
        config = ini(path)
        if any(s.startswith("testenv") and config.get(s, "commands", fallback="").strip()
            for s in config.sections()):
            test_sources.append({"path": path, "kind": "tox-command"})
        for section, group in (("flake8", lint), ("isort", formatting), ("mypy", typing)):
            if config.has_section(section):
                group.append(path)
        if config.has_section("tool:pytest"):
            test_sources.append({"path": path, "kind": "pytest-config"})


def _nox_tests(context: InventoryContext, path: str, sources: _Sources) -> None:
    name = PurePosixPath(path).name
    test_sources = sources.test_sources
    content = context.content
    warnings = context.warnings
    if name in {"noxfile.py", "noxfile"}:
        try:
            tree = ast.parse(content(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func,
                    ast.Attribute) or node.func.attr != "run":
                    continue
                args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    else None for a in node.args]
                if args and (args[0] in {"pytest", "unittest"}
                    or args[:3] in (["python", "-m", "pytest"], ["python", "-m", "unittest"],
                        ["python3", "-m", "pytest"], ["python3", "-m", "unittest"])):
                    test_sources.append({"path": path, "kind": "nox-command"})
                    break
        except SyntaxError:
            warnings.append({"kind": "invalid-config", "path": path})


def _documented_tests(context: InventoryContext, path: str, sources: _Sources) -> None:
    test_sources = sources.test_sources
    content = context.content
    if path.endswith(".md") and not path.startswith(("sumbi/", "tests/")):
        if DOCUMENTED_TEST_COMMAND.search(content(path)):
            test_sources.append({"path": path, "kind": "documented-command"})
