"""Conservative, data-driven convention detection and owner declarations."""

from collections import Counter
import json
from pathlib import Path
import re

from ..errors import InstallError
from ..configuration import load_configuration
from ..text import scripts
from ..files import _without_code, safe_path
from .context import InventoryContext


# Remove metadata before directive and script detection.
FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---(?:\s*\n|$)", re.S)


def load_lexicon() -> dict:
    return json.loads((Path(__file__).parents[2] / "catalog" / "conventions.v1.json").read_text(encoding="utf-8"))


def detect(root: Path, paths: list[str], content, warnings: list[dict]) -> tuple[dict, int, str]:

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
        guidance = FRONT_MATTER.sub("", content(path))
        guidance = _without_code(guidance, inline=False)
        counts.update(scripts(_without_code(guidance)))
        text = "\n".join(line for line in guidance.splitlines()
                         if not any(pattern.search(line) for pattern in exclusions))
        for key, patterns in directives.items():
            directive_text = "\n".join(line for line in text.splitlines()
                                       if not any(pattern.search(line) for pattern in denials[key]))
            if any(pattern.search(directive_text) for pattern in patterns):
                conventions[key]["evidence"].append({"path": path, "kind": "directive"})
            elif any(pattern.search(guidance) for pattern in topics[key]):
                conventions[key]["evidence"].append({"path": path, "kind": "mentioned"})

    _unsupported_language(counts, covered, conventions, warnings)
    config = load_configuration(root)
    _declarations(root, config.get("conventions", {}), conventions, warnings)
    for convention in conventions.values():
        kinds = {entry["kind"] for entry in convention["evidence"]}
        if kinds & {"directive", "declared"}:
            convention["status"] = "present"
        elif "declared-absent" in kinds:
            convention["status"] = "absent"
        elif convention["evidence"]:
            convention["status"] = "unknown"
    dominant = max(sorted(covered), key=lambda category: (counts[category], category == "Latin"), default=None)
    language = config.get("language", "ko" if dominant == "Hangul" and counts[dominant] else "en")
    return conventions, lexicon["version"], language


def _unsupported_language(counts, covered, conventions, warnings) -> None:
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



def _declarations(root: Path, declarations, conventions: dict, warnings: list) -> None:
    if not isinstance(declarations, dict):
        raise InstallError('Conventions must be a table of IDs and path lists or "absent".')
    for key, declared in declarations.items():
        if key not in conventions:
            raise InstallError("Conventions must use known IDs.")
        if declared == "absent":
            evidence = conventions[key]["evidence"]
            evidence.append({"path": ".sumbi/config.toml", "kind": "declared-absent"})
            if any(entry["kind"] == "directive" for entry in evidence):
                warnings.append({"kind": "convention-declaration-conflict", "convention": key})
            continue
        if not isinstance(declared, list) or any(not isinstance(p, str) for p in declared):
            raise InstallError('Conventions must use repository-relative path lists or "absent".')
        for path in dict.fromkeys(declared):
            target = safe_path(root, path)
            if target.exists():
                conventions[key]["evidence"].append({"path": path, "kind": "declared"})
            else:
                warnings.append({"kind": "convention-declaration-missing", "convention": key, "path": path})


def scan(context: InventoryContext) -> dict:
    instructions, imports = context.instructions, context.imports
    capabilities = context.capabilities
    convention_paths = sorted(
        {p for group in instructions.values() for p in group}
        | {item["path"] for item in imports if item["status"] == "resolved"}
        | {p for group in capabilities.values() for p in group["paths"]}
    )
    conventions, lexicon_version, language = detect(context.root, convention_paths, context.content, context.warnings)
    return {"conventions": conventions, "convention_lexicon_version": lexicon_version,
            "convention_search_paths": convention_paths, "practice_language": language}
