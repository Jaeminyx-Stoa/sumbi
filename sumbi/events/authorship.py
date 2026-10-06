"""Bounded operands and results for successful worker authorship executions."""

import json
import re
import shlex

from sumbi.core.paths import execution_cwd, normalize_origin
from sumbi.events.references import BRANCH, PR_URL, worker_branches
from sumbi.events.tool_paths import resolve_path


def commands(value):
    """Parse literal commands and success-dependent chains without evaluation."""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        if len(value) == 3 and value[0].rsplit("/", 1)[-1] in ("bash", "sh"):
            return commands(value[2]) if value[1] in ("-c", "-lc") else []
        return [value]
    if not isinstance(value, str) or any(c in value for c in "$`\n\r"):
        return []
    try:
        lexer = shlex.shlex(value, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return []
    groups = [[]]
    for word in words:
        if word == "&&":
            if not groups[-1]:
                return []
            groups.append([])
        elif word and all(c in ";&|<>()" for c in word):
            return []
        else:
            groups[-1].append(word)
    return groups if groups[-1] else []


def actions(command):
    """Actions are intentions until a normalized execution exits zero."""
    found = set()
    text = command if isinstance(command, str) else "\n".join(command) if (
        isinstance(command, list) and all(isinstance(v, str) for v in command)) else ""
    # Creation results identify the repository even inside unevaluated wrappers.
    if re.search(r"(?<![\w./-])gh\s+pr\s+create\b", text):
        found.add(("create", None))
    for words in commands(command):
        if words[:2] == ["git", "-C"]:
            words = ["git", *words[3:]]
        if words[:2] == ["git", "push"]:
            refs = worker_branches(shlex.join(words))
            if ("branch_operation", "push") in refs:
                found.add(("push", None))
                operands = [v for v in words[2:] if not v.startswith("-")]
                if operands and (operands[0] == "origin"
                    or any(k == "repository" for k, _ in refs)):
                    found.update(("push", v) for k, v in refs if k == "branch_push_intent")
        elif words[:2] == ["git", "commit"]:
            if not any(v in ("--dry-run", "--help", "-h") for v in words[2:]):
                found.add(("commit", None))
        elif words[:3] == ["gh", "pr", "create"]:
            if not any(v in ("--dry-run", "--help", "-h") for v in words[3:]):
                found.add(("create", None))
        elif words[:2] == ["gh", "api"]:
            endpoint = any(re.fullmatch(r"/?repos/[^/]+/[^/]+/pulls", v) for v in words[2:])
            post = any(words[i:i + 2] in (["--method", "POST"], ["-X", "POST"])
                for i in range(2, len(words) - 1))
            implicit_post = any(v in ("-f", "-F", "--field", "--raw-field", "--input")
                for v in words[2:]) and not any(v in ("--method", "-X") for v in words[2:])
            if endpoint and (post or implicit_post):
                found.add(("create", None))
    return found


def command_scope(command, cwd):
    """Honor literal git -C; refuse shell cwd changes and mixed repositories."""
    normalized = execution_cwd(cwd)
    if cwd is not None and normalized is None:
        return None, set(), False
    cwd = normalized
    directories, repositories = set(), set()
    for words in commands(command):
        if words[:1] in (["cd"], ["pushd"], ["popd"]):
            return None, set(), False
        directory = cwd
        if words[:2] == ["git", "-C"] and len(words) > 3:
            directory = resolve_path(words[2], cwd)
            words = ["git", *words[3:]]
        if words[:2] in (["git", "push"], ["git", "commit"]):
            directories.add(directory)
        refs = worker_branches(shlex.join(words))
        repositories.update(v for k, v in refs if k == "repository")
        if words[:2] == ["gh", "api"]:
            for word in words[2:]:
                match = re.fullmatch(r"/?repos/([^/]+/[^/]+)/pulls", word)
                if match:
                    repositories.add(match[1].lower())
    return (next(iter(directories)) if len(directories) == 1 else cwd,
        repositories, len(directories) <= 1 and len(repositories) <= 1)


def result_refs(value):
    """Read creation identities and anchored git results, never quoted prose."""
    if isinstance(value, str):
        value = re.sub(r"\AChunk ID: [^\n]+\nWall time: [^\n]+\n"
            r"Process exited with code 0\nFinal output:\n", "", value)
        try:
            value = json.loads(value)
        except ValueError:
            pass
    refs = set()
    if isinstance(value, dict):
        url = value.get("html_url", value.get("url"))
    elif isinstance(value, str):
        url = None
        refs.update(push_results(value))
        for line in value.splitlines():
            if match := PR_URL.fullmatch(line.strip()):
                refs.add(("pr_created", match[1].lower() + "#" + match[2]))
        match = re.search(r"(?m)^\[(" + BRANCH + r") (?:\(root-commit\) )?[0-9a-f]{7,40}\] ",
            value)
        if match:
            refs.add(("branch_committed", match[1]))
    else:
        return refs
    if isinstance(url, str) and (match := PR_URL.fullmatch(url)):
        refs.add(("pr_created", match[1].lower() + "#" + match[2]))
    return refs


def push_results(value):
    """Pair each changed branch with its own remote block, independent of shell."""
    refs, repository = set(), None
    for line in value.splitlines():
        if line.startswith("To "):
            origin = normalize_origin(line[3:])
            match = re.fullmatch(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
                origin or "", re.I)
            repository = match[1].lower() if match else None
            continue
        if not repository:
            continue
        match = re.fullmatch(r"\s*(?:\*\s+\[new branch\]|\+?\s*[0-9a-f]{4,40}"
            r"\.{2,3}[0-9a-f]{4,40})\s+(" + BRANCH + r")\s+->\s+(" + BRANCH
            + r")(?:\s+\([^\r\n]*\))?\s*", line)
        if match:
            destination = match[2]
            if destination.startswith("refs/") and not destination.startswith("refs/heads/"):
                continue
            refs.add(("push_result", (repository, destination.removeprefix("refs/heads/"))))
    return refs
