"""Parse local-only edit operands without changing edit identity or timestamps."""

import json
import re


def file_change_targets(changes):
    """Retain native edit paths and move destinations; absent maps stay unknown."""
    if not isinstance(changes, dict) or not changes:
        return (None,)
    targets = []
    for target, entry in changes.items():
        targets.append(target)
        if isinstance(entry, dict) and entry.get("move_path") is not None:
            targets.append(entry["move_path"])
    return tuple(targets)


def patch_targets(arguments):
    """Extract every file header; malformed or unsupported patches stay unknown."""
    cwd = None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    if isinstance(arguments, dict):
        cwd = arguments.get("workdir", arguments.get("cwd"))
        arguments = arguments.get("patch", arguments.get("input"))
    if not isinstance(arguments, str):
        return (None,), cwd
    lines = arguments.strip().splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return (None,), cwd
    targets, section, body, moved = [], None, False, False
    for line in lines[1:-1]:
        header = re.fullmatch(r"\*\*\* (Add File|Update File|Delete File|Move to): (.+)", line)
        if header:
            kind, target = header.groups()
            if kind == "Move to":
                if section != "Update File" or moved or body:
                    return (None,), cwd
                moved = True
            else:
                if section == "Update File" and not (body or moved):
                    return (None,), cwd
                section, body, moved = kind, False, False
            targets.append(target)
        elif (section is None or line.startswith("*** ") and line != "*** End of File"
            or section == "Delete File"
            or section == "Add File" and not line.startswith("+")
            or section == "Update File" and not (
                line.startswith(("+", "-", " ", "@@")) or line in ("", "*** End of File"))):
            return (None,), cwd
        elif line.startswith(("+", "-", " ")):
            body = True
    if section == "Update File" and not (body or moved):
        return (None,), cwd
    return tuple(dict.fromkeys(targets)) or (None,), cwd
