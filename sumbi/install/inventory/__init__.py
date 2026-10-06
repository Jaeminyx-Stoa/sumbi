"""Assemble offline install evidence in the stable report and scan order."""

from pathlib import Path

from ..errors import InstallError
from ..exclusions import load_excludes
from .context import InventoryContext
from .instructions import conditional_paths
from . import walk, versioning, instructions, imports, capabilities, enforcement, conventions, cost


def inventory(repository: Path | str = ".", budget: int = 2000, *,
              exclude: list[str] | tuple[str, ...] = ()) -> dict:
    """Return only relative paths, counts, public labels and static diagnostics."""
    root = Path(repository).resolve()
    if not root.is_dir() or budget < 1:
        raise InstallError("Expected a repository directory and a positive token budget.")
    context = InventoryContext(root, load_excludes(root, exclude), budget)
    exclusions = walk.scan(context)
    versioning_section = versioning.scan(context)
    instruction_section = instructions.scan(context)
    import_section = imports.scan(context)
    capability_sections = capabilities.scan(context)
    enforcement_section = enforcement.scan(context)
    convention_sections = conventions.scan(context)
    # Keep conditional-rule reads after convention diagnostics, as before.
    context.conditional_rules = [p for p in context.instructions["claude_rules"]
                                 if conditional_paths(context.content(p))]
    cost_section = cost.scan(context)
    return {
        "exclusions": exclusions,
        "versioning": versioning_section,
        "instructions": instruction_section,
        "claude_imports": import_section, "capabilities": capability_sections["capabilities"],
        "configurations": capability_sections["configurations"],
        "enforcement": enforcement_section,
        "cost": cost_section,
        **convention_sections, "warnings": context.warnings,
    }
