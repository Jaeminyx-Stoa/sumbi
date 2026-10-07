"""Bounded machine references from tool records, never message prose."""

import json
import re
import shlex

from sumbi.core.values import mapping

BRANCH = r"[A-Za-z0-9][A-Za-z0-9_./-]{0,199}"
PR_URL = re.compile(r"(?<![A-Za-z0-9./-])https://github\.com/"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,9})(?![A-Za-z0-9])")


def branch(value):
    return value if isinstance(value, str) and re.fullmatch(BRANCH, value) else None


def branch_query(name, arguments):
    """Identify literal Git queries so their one-line output can be evidence."""
    short = str(name).rsplit(".", 1)[-1].rsplit("__", 1)[-1].lower()
    if short not in {"bash", "shell", "exec_command", "run_command", "local_shell_call",
        "commandexecution"}:
        return False
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return False
    args = mapping(arguments)
    value = args.get("command", args.get("cmd"))
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        words = value
    elif isinstance(value, str) and not any(c in value for c in "$`\\\n\r;&|<>"):
        try:
            words = shlex.split(value)
        except ValueError:
            return False
    else:
        return False
    if words[:2] == ["git", "-C"]:
        words = ["git", *words[3:]]
    return words in (["git", "branch", "--show-current"],
        ["git", "symbolic-ref", "--short", "HEAD"],
        ["git", "rev-parse", "--abbrev-ref", "HEAD"])


def tool_refs(name, arguments=None, *, output=False, query=False, worker=False, operation=None):
    """Only recognized shell/PR tools; no recursive mining of arbitrary objects.

    Output matching recognizes URLs and explicit JSON branch fields. Arbitrary
    tool prose is not retained or interpreted. Shell parsing never evaluates.
    """
    short = str(name).rsplit(".", 1)[-1].rsplit("__", 1)[-1].lower()
    if short not in {"bash", "shell", "exec_command", "run_command", "local_shell_call",
        "commandexecution", "github", "create_pull_request", "get_pull_request"}:
        return set()
    raw = arguments
    if isinstance(raw, str):
        try:
            arguments = json.loads(raw)
        except ValueError:
            arguments = raw
    args = mapping(arguments)
    refs = set()
    for key in ("branch", "head", "head_ref", "gitBranch"):
        if (value := branch(args.get(key))):
            refs.add(("branch", value))
            if worker and output and operation in ("push", "create"):
                refs.add(("branch_pushed" if operation == "push" else "branch_created", value))
    value = (arguments if isinstance(arguments, str) else
        args.get("output",
            args.get("url", args.get("html_url")))) if output else args.get("cmd",
                args.get("command"))
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        value = shlex.join(value)
    if not isinstance(value, str):
        return refs
    if output and query:
        # Plain Git output or the collector's anchored completion envelope.
        match = re.fullmatch(
            r"(?:Chunk ID: [^\n]+\nWall time: [^\n]+\nProcess exited with code 0\nFinal output:\n)?"
            + "(" + BRANCH + r")\s*", value)
        if match and match[1] != "HEAD":
            refs.add(("branch", match[1]))
            refs.add(("branch_observed", match[1]))
    refs.update(("pr", repo.lower() + "#" + number) for repo, number in PR_URL.findall(value))
    if worker:
        refs.update(worker_branches(value, output=output))
    if output or any(c in value for c in "$`\\\n\r;&|<>"):
        return refs
    try:
        words = shlex.split(value)
    except ValueError:
        return refs
    if words[:1] == ["git"]:
        if words[1:2] == ["-C"]:
            words = ["git", *words[3:]]
        if len(words) >= 3 and words[1] in ("switch", "checkout", "branch"):
            operands = [w for w in words[2:] if not w.startswith("-")]
            if len(operands) == 1 and (value := branch(operands[0])):
                refs.add(("branch", value))
                if words[1] in ("switch", "checkout"):
                    refs.add(("branch_change", value))
    return refs


def worker_branches(value, *, output=False):
    """Literal worker branch ownership; successful push envelopes are strongest.

    Command inputs show intent, rather than claiming that a push succeeded.
    No shell expansion, wrapper code or arbitrary prose is interpreted.
    """
    refs = set()
    if output:
        for repo in re.findall(r"(?m)^To (?:https://github\.com/|git@github\.com:)"
            r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?\s*$", value):
            refs.add(("repository", repo.lower()))
        for match in re.finditer(r"(?m)^\s*(?:\*\s+\[new branch\]|[0-9a-f]+\.\.[0-9a-f]+)"
            r"\s+(" + BRANCH + r")\s+->\s+(" + BRANCH + r")\s*$", value):
            refs.add(("branch_pushed", match[2]))
        return refs
    if any(c in value for c in "$`\\\n\r;&|<>"):
        return refs
    try:
        words = shlex.split(value)
    except ValueError:
        return refs
    if words[:2] == ["git", "-C"]:
        words = ["git", *words[3:]]
    if words[:2] in (["git", "switch"], ["git", "checkout"], ["git", "branch"]):
        args = words[2:]
        create = (words[1] == "branch" and len(args) == 1 or len(args) == 2
            and args[0] in (("-c", "-C") if words[1] == "switch" else ("-b", "-B")))
        operands = [v for v in args if not v.startswith("-")]
        if create and len(operands) == 1 and branch(operands[0]):
            refs.add(("branch_created", operands[0]))
    elif words[:2] == ["git", "push"]:
        args = words[2:]
        if any(v.startswith("-") and v not in ("-u", "--set-upstream", "-f", "--force",
            "--force-with-lease") for v in args):
            return refs
        operands = [v for v in args if not v.startswith("-")]
        if len(operands) == 2 and operands[1].startswith(":"):
            return refs
        refs.add(("branch_operation", "push"))
        if operands:
            match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)"
                r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", operands[0])
            if match:
                refs.add(("repository", match[1].lower()))
        if len(operands) == 2 and not operands[0].startswith("-"):
            value = operands[1].split(":")[-1].removeprefix("refs/heads/")
            if branch(value) and value != "HEAD":
                refs.add(("branch_push_intent", value))
    elif words[:3] == ["gh", "pr", "create"]:
        refs.add(("branch_operation", "create"))
        for index, word in enumerate(words[:-1]):
            if word in ("--head", "-H") and branch(words[index + 1]):
                refs.add(("branch_created", words[index + 1]))
            if word in ("--repo", "-R") and re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", words[index + 1]):
                refs.add(("repository", words[index + 1].lower()))
    return refs
