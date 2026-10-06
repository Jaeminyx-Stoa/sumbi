"""Conservative, data-driven convention detection and owner declarations."""

from collections import Counter
import json
from pathlib import Path
import re
import tomllib

from .errors import InstallError
from .text import scripts


def load_lexicon() -> dict:
    return json.loads((Path(__file__).parent.parent / "catalog" / "conventions.v1.json").read_text(encoding="utf-8"))


def detect(root: Path, paths: list[str], content, warnings: list[dict]) -> tuple[dict, int]:
    from .inventory import _without_code, read_bytes, safe_path

    lexicon = load_lexicon()
    conventions = {key: {"status": "absent", "evidence": []} for key in lexicon["conventions"]}
    counts = Counter()
    languages = lexicon["languages"].values()
    covered = {category for language in languages for category in language["scripts"]}
    exclusions = [re.compile(pattern, re.I) for language in languages for pattern in language["exclude_lines"]]
    directives = {key: [re.compile(pattern, re.I) for language in languages
                        for pattern in language["directives"].get(key, [])] for key in conventions}
    topics = {key: [re.compile(pattern, re.I) for language in languages
                    for pattern in language.get("topics", {}).get(key, [])] for key in conventions}
    denials = {key: [re.compile(pattern, re.I) for language in languages
                     for pattern in language.get("denials", {}).get(key, [])] for key in conventions}
    for path in paths:
        guidance = re.sub(r"\A---\s*\n.*?\n---(?:\s*\n|$)", "", content(path), flags=re.S)
        guidance = _without_code(guidance, inline=False)
        counts.update(scripts(guidance))
        text = "\n".join(line for line in guidance.splitlines()
                         if not any(pattern.search(line) for pattern in exclusions))
        for key, patterns in directives.items():
            directive_text = "\n".join(line for line in text.splitlines()
                                       if not any(pattern.search(line) for pattern in denials[key]))
            if any(pattern.search(directive_text) for pattern in patterns):
                conventions[key]["evidence"].append({"path": path, "kind": "directive"})
            elif any(pattern.search(guidance) for pattern in topics[key]):
                conventions[key]["evidence"].append({"path": path, "kind": "mentioned"})

    # Only letter counts vote: punctuation, digits and code cannot disguise the script.
    unsupported = {category: count for category, count in counts.items() if category not in covered}
    if sum(unsupported.values()) > sum(counts.values()) / 2:
        dominant, cumulative = [], 0
        for category, count in sorted(unsupported.items(), key=lambda item: (-item[1], item[0])):
            dominant.append(category)
            cumulative += count
            if cumulative > sum(counts.values()) / 2:
                break
        warnings.append({"kind": "convention-language-unsupported", "scripts": dominant})
        for convention in conventions.values():
            convention["status"] = "unknown"

    raw = read_bytes(root, ".sumbi/config.toml")
    if raw is not None:
        try:
            declarations = tomllib.loads(raw.decode("utf-8-sig")).get("conventions", {})
        except (ValueError, UnicodeError):
            raise InstallError("Invalid install convention configuration.") from None
        if not isinstance(declarations, dict):
            raise InstallError("Conventions must be a table of convention IDs and path lists.")
        for key, declared in declarations.items():
            if key not in conventions or not isinstance(declared, list) or any(not isinstance(p, str) for p in declared):
                raise InstallError("Conventions must use known IDs and repository-relative path lists.")
            for path in dict.fromkeys(declared):
                target = safe_path(root, path)
                if target.exists():
                    conventions[key]["evidence"].append({"path": path, "kind": "declared"})
                else:
                    warnings.append({"kind": "convention-declaration-missing", "convention": key, "path": path})
    for convention in conventions.values():
        if any(entry["kind"] in {"directive", "declared"} for entry in convention["evidence"]):
            convention["status"] = "present"
        elif convention["evidence"]:
            convention["status"] = "unknown"
    return conventions, lexicon["version"]
