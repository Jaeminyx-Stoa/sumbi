"""Enforce package dependency direction using every import in the AST."""

import ast
from importlib.util import resolve_name
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent / "sumbi"
LAYERS = {"core": 0, "catalog": 0, "events": 1, "sessions": 2,
          "measure": 3, "outcomes": 4, "install": 4, "judge": 5, "cli": 6}
# Both existing command entry points are explicitly allowed to delegate to CLI.
ENTRIES = {"sumbi.__main__", "sumbi.install.__main__"}


def module_name(path):
    parts = path.relative_to(ROOT.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def layer(module):
    if module in ENTRIES:
        return LAYERS["cli"]
    return LAYERS[module.split(".")[1]] if "." in module else 0


def imports(module, source, modules, *, package=False):
    """Resolve absolute/relative imports, including imports in nested scopes."""
    tree = ast.parse(source)
    parent = module if package else module.rpartition(".")[0]
    edges = set()
    local = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = (resolve_name("." * node.level + (node.module or ""), parent)
                    if node.level else node.module)
            targets = [] if base == module and node.module is None else [base]
            targets.extend(base + "." + alias.name for alias in node.names
                           if base + "." + alias.name in modules)
        else:
            continue
        if node.col_offset:
            local.append(node.lineno)
        for target in targets:
            if target == "sumbi" or target.startswith("sumbi."):
                if target not in modules:
                    raise ValueError("Unknown internal import: " + target)
                edges.add(target)
    return edges, local


def forbidden_edges(graph):
    return sorted((source, target) for source, targets in graph.items()
                  for target in targets if layer(source) < layer(target)
                  or source.startswith("sumbi.install.") and source not in ENTRIES
                  and target.startswith("sumbi.judge"))


def cycles(graph):
    visited, active, found = set(), [], []
    def visit(module):
        if module in active:
            found.append(tuple(active[active.index(module):] + [module]))
            return
        if module in visited:
            return
        active.append(module)
        for target in sorted(graph.get(module, ())):
            visit(target)
        active.pop()
        visited.add(module)
    for module in sorted(graph):
        visit(module)
    return found


class LayerTests(unittest.TestCase):
    def test_every_package_import_follows_layers_without_cycles(self):
        paths = {module_name(path): path for path in ROOT.rglob("*.py")}
        graph = {}
        for module, path in paths.items():
            graph[module], local = imports(module, path.read_text(encoding="utf-8"), paths,
                                          package=path.name == "__init__.py")
            self.assertEqual(local, [], module + " has deferred imports")
        self.assertEqual(forbidden_edges(graph), [])
        self.assertEqual(cycles(graph), [])

    def test_nested_relative_and_absolute_imports_are_checked(self):
        modules = {"sumbi.install.files", "sumbi.install.inventory", "sumbi.judge.stats"}
        source = ("from .files import read_bytes\n"
                  "def deferred():\n    from sumbi.judge.stats import wilson\n")
        edges, local = imports("sumbi.install.inventory", source, modules)
        self.assertEqual(edges, {"sumbi.install.files", "sumbi.judge.stats"})
        self.assertEqual(local, [3])
        self.assertEqual(forbidden_edges({"sumbi.install.inventory": edges}),
                         [("sumbi.install.inventory", "sumbi.judge.stats")])

    def test_import_module_aliases_and_package_children(self):
        modules = {"sumbi.core", "sumbi.core.values", "sumbi.core.time"}
        edges, _ = imports("sumbi.core.time", "import sumbi.core.values as values\n", modules)
        self.assertEqual(edges, {"sumbi.core.values"})
        edges, _ = imports("sumbi.core", "from . import values\n", modules, package=True)
        self.assertEqual(edges, {"sumbi.core.values"})
        with self.assertRaisesRegex(ValueError, "Unknown internal import"):
            imports("sumbi.core.time", "from sumbi.model import Window\n", modules)

    def test_same_layer_cycles_and_later_edges_are_rejected(self):
        graph = {"sumbi.core.values": {"sumbi.events.references"},
                 "sumbi.events.references": {"sumbi.core.values"}}
        self.assertEqual(forbidden_edges(graph),
                         [("sumbi.core.values", "sumbi.events.references")])
        self.assertEqual(len(cycles(graph)), 1)
        self.assertEqual(cycles({"sumbi.core.values": {"sumbi.core.paths"},
                                 "sumbi.core.paths": {"sumbi.core.values"}}),
                         [("sumbi.core.paths", "sumbi.core.values", "sumbi.core.paths")])


if __name__ == "__main__":
    unittest.main()
