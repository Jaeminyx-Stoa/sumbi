"""Guard the install decomposition and shared reader's observable contracts."""

import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from sumbi.install.errors import InstallError
from sumbi.install.inventory.context import InventoryContext

INSTALL = Path(__file__).resolve().parents[2] / "sumbi" / "install"
SCANNERS = {"walk", "instructions", "imports", "capabilities", "enforcement",
            "conventions", "cost", "versioning"}


class InstallStructureTests(unittest.TestCase):
    def test_install_functions_are_at_most_eighty_lines(self):
        for path in INSTALL.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    with self.subTest(module=path.relative_to(INSTALL).as_posix(), function=node.name):
                        self.assertLessEqual(node.end_lineno - node.lineno + 1, 80)

    def test_scanners_share_context_without_importing_sibling_scanners(self):
        for name in SCANNERS:
            tree = ast.parse((INSTALL / "inventory" / (name + ".py")).read_text(encoding="utf-8"))
            scan = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "scan")
            self.assertEqual([arg.arg for arg in scan.args.args], ["context"])
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    self.assertNotIn(node.module, SCANNERS)
                elif isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name.startswith("sumbi.install.inventory.")
                                         and alias.name.rsplit(".", 1)[-1] in SCANNERS
                                         for alias in node.names))

    def test_content_is_read_once_and_normalized_for_all_consumers(self):
        context = InventoryContext(Path("."), (), 2000)
        with patch("sumbi.install.inventory.context.read_bytes", return_value=b"\xef\xbb\xbfone\r\ntwo\r") as read:
            self.assertEqual(context.content("AGENTS.md"), "one\ntwo\n")
            self.assertEqual(context.content("AGENTS.md"), "one\ntwo\n")
        read.assert_called_once()
        self.assertEqual(context.warnings, [])

    def test_unreadable_content_emits_one_diagnostic_across_consumers(self):
        context = InventoryContext(Path("."), (), 2000)
        with patch("sumbi.install.inventory.context.read_bytes", side_effect=InstallError("Synthetic failure")) as read:
            self.assertEqual(context.content("AGENTS.md"), "")
            self.assertEqual(context.content("AGENTS.md"), "")
        read.assert_called_once()
        self.assertEqual(context.warnings, [{"kind": "unreadable-or-oversized", "path": "AGENTS.md"}])
