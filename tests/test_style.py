"""Keep package contracts and mechanical readability limits visible."""

import ast
import io
from pathlib import Path
import re
import tokenize
import unittest

ROOT = Path(__file__).resolve().parent.parent / "sumbi"
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
REGEX_CALLS = {"compile", "match", "fullmatch", "search", "findall", "finditer", "sub", "split"}
EXCEPTION = re.compile(r"# noqa: long-line (\S.*)")


def long_line_exceptions(source):
    """Only URL strings or literal regex arguments with a reason can be exempt."""
    regex_spans = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"
                and node.func.attr in REGEX_CALLS and node.args):
            pattern = node.args[0]
            if isinstance(pattern, ast.Constant) and isinstance(pattern.value, (str, bytes)):
                regex_spans.append((pattern.lineno, pattern.end_lineno))
    literals, reasons = set(), set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and EXCEPTION.fullmatch(token.string):
            reasons.add(token.start[0])
        elif token.type == tokenize.STRING:
            if re.search(r"https?://[^\s'\"]+", token.string) or any(
                    begin <= token.start[0] <= end for begin, end in regex_spans):
                literals.update(range(token.start[0], token.end[0] + 1))
    return literals & reasons


class StyleTests(unittest.TestCase):
    def test_package_lines_are_at_most_one_hundred_characters(self):
        for path in sorted(ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            exceptions = long_line_exceptions(source)
            for number, line in enumerate(source.splitlines(), 1):
                with self.subTest(module=path.relative_to(ROOT).as_posix(), line=number):
                    if number not in exceptions:
                        self.assertLessEqual(len(line), 100)

    def test_package_functions_are_at_most_eighty_lines(self):
        for path in sorted(ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, FUNCTIONS):
                    with self.subTest(module=path.relative_to(ROOT).as_posix(), function=node.name):
                        self.assertLessEqual(node.end_lineno - node.lineno + 1, 80)

    def test_every_package_module_has_a_docstring(self):
        for path in sorted(ROOT.rglob("*.py")):
            with self.subTest(module=path.relative_to(ROOT).as_posix()):
                self.assertTrue(ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))))

    def test_long_line_exceptions_require_a_literal_and_a_reason(self):
        self.assertEqual(long_line_exceptions('url = "https://example.invalid/path"\n'), set())
        self.assertEqual(long_line_exceptions(
            'url = "https://example.invalid/path"  # noqa: long-line preserve URL\n'), {1})
        self.assertEqual(long_line_exceptions(
            'pattern = re.compile(r"^synthetic.*$")  # noqa: long-line preserve regex\n'), {1})
        for source in (
                'text = "ordinary text"  # noqa: long-line readability\n',
                '# https://example.invalid/path # noqa: long-line preserve URL\n',
                'url = "https://example.invalid/path"  # noqa: long-line\n',
                'pattern = re.compile(variable)  # noqa: long-line preserve regex\n'):
            with self.subTest(source=source):
                self.assertEqual(long_line_exceptions(source), set())


if __name__ == "__main__":
    unittest.main()
