"""Conservative executed-program recognition. Raw commands are local evidence."""

from pathlib import Path
import posixpath
import re
import shlex
import tomllib

READS = {"get-content", "cat", "type", "rg", "grep", "select-string", "sed", "head", "tail"}
SHELLS = {"bash", "sh", "bash.exe", "sh.exe"}
POWERSHELLS = {"pwsh", "powershell", "pwsh.exe", "powershell.exe"}


def declared_commands(repository: Path, commands=None):
    """CLI declarations replace the repository-local TOML verify list."""
    if commands is None:
        path = repository / ".sumbi" / "config.toml"
        try:
            commands = tomllib.loads(path.read_text(encoding="utf-8")).get("verify", [])
        except FileNotFoundError:
            commands = []
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            raise ValueError("Verification config must be valid readable TOML") from None
    if (not isinstance(commands, (list, tuple)) or not commands
        or any(not isinstance(s, str) or not s or s.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:", s) or ".." in s.replace("\\", "/").split("/")
            or any(c in s for c in "\r\n\x00") for s in commands)):
        raise ValueError("Verification requires repository-relative script declarations")
    return tuple(sorted({normalize(s) for s in commands}))


def normalize(value):
    return posixpath.normpath(value.replace("\\", "/"))


def basename(value):
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


def tokens(value):
    lexer = shlex.shlex(value, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def recognize(command, declared):
    """matched, read, other, or unmatched_shape; never return command text.

    Only one simple invocation is accepted. Conditionals, pipelines, redirects,
    dynamic substitutions and unrecognized options cannot establish execution.
    Declaration paths match exactly after separator and dot normalization.
    """
    def mentions(value):
        text = str(value).replace("\\", "/")
        return any(s in text for s in declared)

    def inspect(value, depth=0):
        if depth > 5:
            return "unmatched_shape"
        if isinstance(value, str):
            if any(c in value for c in "\n\r"):
                return "unmatched_shape" if mentions(value) else "other"
            try:
                parts = tokens(value)
            except ValueError:
                return "unmatched_shape" if mentions(value) else "other"
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            parts = list(value)
        else:
            return "unmatched_shape"
        while parts and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
            parts.pop(0)
        if parts and parts[0] == "&":
            parts.pop(0)
        if not parts:
            return "other"
        program = basename(parts[0])
        # Unwrap before syntax checks: the script is a quoted single argument.
        if program in POWERSHELLS:
            args = parts[1:]
            while args and args[0].lower() in ("-noprofile", "-noninteractive", "-nologo"):
                args.pop(0)
            if len(args) == 2 and args[0].lower() in ("-command", "-c"):
                return inspect(args[1], depth + 1)
            return "unmatched_shape" if mentions(value) else "other"
        if program in SHELLS:
            args = parts[1:]
            while args and args[0] in ("-l", "--login"):
                args.pop(0)
            if args and args[0] in ("-n", "--noexec"):
                return "syntax_only"
            if args and args[0] in ("-c", "-lc", "-cl"):
                if len(args) == 2:
                    return inspect(args[1], depth + 1)
                return "unmatched_shape" if mentions(value) else "other"
            parts = args
            if not parts or parts[0].startswith("-"):
                return "unmatched_shape" if mentions(value) else "other"
            program = basename(parts[0])
        if any(p and all(c in ";&|<>()" for c in p)
            or any(c in p for c in "\n\r`$") for p in parts):
            return "unmatched_shape" if mentions(value) else "other"
        if program in READS:
            return "read"
        if parts and normalize(parts[0]) in declared:
            return "matched" if len(parts) == 1 else "unmatched_shape"
        return "unmatched_shape" if mentions(value) else "other"

    return inspect(command)
