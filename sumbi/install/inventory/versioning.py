"""Report root and nested versioning from the existing traversal queries."""

from .context import InventoryContext, entry


def scan(context: InventoryContext) -> dict:
    return {"root_versioned": context.git_contexts[context.root].versioned,
            "nested_repositories": entry(context.nested_repositories)}
