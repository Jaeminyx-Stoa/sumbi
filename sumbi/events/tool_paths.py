"""Extract bounded in-memory path evidence from known tool inputs without executing commands.
Prose and unsupported shell syntax do not establish path evidence.
"""

import json
import ntpath
import posixpath
import re
import shlex

from sumbi.core.values import mapping
from sumbi.core.paths import normalize_path


def resolve_path(value, cwd=None):
    if not isinstance(value, str) or not value or any(c in value for c in ("\n", "\r", "\x00")):
        return None
    if re.search(r"[$`*?{}]", value) or value.startswith("~"):
        return None
    if ntpath.isabs(value) and ntpath.splitdrive(value)[0]:
        return normalize_path(value)
    if value.startswith("/"):
        return normalize_path(value)
    if cwd:
        module = ntpath if ntpath.splitdrive(cwd)[0] else posixpath
        return normalize_path(module.join(cwd, value))
    return None


# Quotes may contain spaces, but expansions and chained/non-leading cd are not inferred.
PATH_WORD = r'''("[^"\r\n]+"|'[^'\r\n]+'|[^\s;&|<>]+)'''


def shell_paths(command):
    if isinstance(command, list) and all(isinstance(x, str) for x in command):
        # A logged argv vector is already separated; preserve spaces in operands.
        if len(command) >= 3 and command[0].rsplit("/", 1)[-1] in ("bash", "sh",
            "zsh") and command[1] in ("-c", "-lc"):
            command = command[2]
        elif len(command) >= 3 and command[0] == "git" and command[1] == "-C":
            return [command[2]]
        else:
            return []
    if not isinstance(command, str):
        return []
    # These forms need shell-specific evaluation, beyond literal operand extraction.
    if "$" in command or "`" in command or re.search(r"[\\^][;&|]", command):
        return []
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return []
    paths = []
    leading = re.match(r"^\s*cd\s+(?:/d\s+|--\s+)?" + PATH_WORD + r"(?=\s*(?:&&|;|$))", command,
        re.I)
    if leading:
        paths.append(leading[1].strip("\"'"))
    segments = [[]]
    for word in words:
        if word in (";", "&&", "||", "|"):
            segments.append([])
        else:
            segments[-1].append(word)
    for segment in segments:
        if len(segment) >= 3 and segment[:2] == ["git", "-C"]:
            operand = segment[2].strip("\"'")
            if leading and not ntpath.isabs(operand) and not operand.startswith("/"):
                base = leading[1].strip("\"'")
                module = ntpath if ntpath.splitdrive(base)[0] else posixpath
                operand = module.join(base, operand)
            paths.append(operand)
    return paths


def tool_evidence(name, arguments):
    """Return (explicit working directory, path operands) for a known tool."""
    short = str(name).rsplit(".", 1)[-1].rsplit("__", 1)[-1].casefold()
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (ValueError, TypeError):
            if short != "apply_patch":
                return None, []
        else:
            arguments = decoded
    args = mapping(arguments)
    paths = []
    cwd = None
    if short in ("bash", "shell", "exec_command", "run_command", "local_shell_call",
        "commandexecution"):
        cwd = args.get("workdir") or args.get("cwd")
        paths.extend(shell_paths(args.get("command", args.get("cmd"))))
    elif short in ("read", "read_file", "write", "write_file", "edit", "multiedit", "view_image"):
        value = args.get("file_path", args.get("path"))
        if isinstance(value, str):
            paths.append(value)
    elif short in ("apply_patch", "filechange"):
        patch = arguments if isinstance(arguments, str) else args.get("patch", args.get("input"))
        if isinstance(patch, str):
            paths.extend(
                re.findall(r"(?m)^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$",
                    patch))
        changes = args.get("changes")
        if isinstance(changes, dict):
            paths.extend(changes)
        elif isinstance(changes, list):
            paths.extend(x.get("path") for x in changes if isinstance(x, dict)
                and isinstance(x.get("path"), str))
    return cwd if isinstance(cwd, str) and cwd else None, paths
