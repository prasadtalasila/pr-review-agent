"""The size limits AGENTS.md sets, measured rather than asserted in prose.

The limits are on **code**: blank lines, comments and docstrings do not
count. This repository explains itself at length on purpose -- a docstring
saying why a rule exists is the thing most worth keeping -- and a limit that
counted prose would be met by deleting the explanations, which is the
opposite of what it is for. Measured this way the limit means what it says:
this much behaviour in one place, and no more.

The check lives in the suite rather than in a lint configuration because no
linter measures a *file* this way, and one that nobody runs is prose again.
"""

import ast
import io
import tokenize
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
SOURCES = sorted(SRC.rglob("*.py"))

MAX_FILE_LINES = 250
MAX_FUNCTION_LINES = 25


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _uncounted(source: str, tree: ast.Module) -> set[int]:
    """Line numbers holding comment or docstring rather than code."""
    lines = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.NL):
            lines.update(range(token.start[0], token.end[0] + 1))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, *_FUNCTIONS)):
            continue
        if ast.get_docstring(node, clean=False) is not None:
            docstring = node.body[0]
            lines.update(range(docstring.lineno, _last(docstring) + 1))
    return lines


def _last(node: ast.stmt) -> int:
    """``end_lineno``, which the parser always sets but the type allows None."""
    return node.end_lineno or node.lineno


def _count(source: str, uncounted: set[int], first: int, last: int) -> int:
    lines = source.splitlines()
    return sum(
        1
        for number in range(first, last + 1)
        if number not in uncounted and lines[number - 1].strip()
    )


def _measure(path: Path) -> tuple[int, dict[str, int]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    uncounted = _uncounted(source, tree)
    functions = {
        f"{node.name} (line {node.lineno})": _count(
            source, uncounted, node.body[0].lineno, _last(node)
        )
        for node in ast.walk(tree)
        if isinstance(node, _FUNCTIONS)
    }
    return _count(source, uncounted, 1, len(source.splitlines())), functions


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_a_module_stays_under_the_file_limit(path):
    total, _ = _measure(path)
    assert total <= MAX_FILE_LINES, (
        f"{path.relative_to(SRC)} holds {total} lines of code; "
        f"the limit is {MAX_FILE_LINES}. Split it."
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_every_function_stays_under_the_function_limit(path):
    _, functions = _measure(path)
    too_long = {
        name: size for name, size in functions.items() if size > MAX_FUNCTION_LINES
    }
    assert not too_long, (
        f"{path.relative_to(SRC)}: {too_long}; the limit is {MAX_FUNCTION_LINES}."
    )


def test_the_measure_ignores_prose():
    """The property the limits depend on, pinned on a file with both."""
    source = '"""Module docstring.\n\nSecond line.\n"""\n\n# a comment\nx = 1\n'
    total = _count(source, _uncounted(source, ast.parse(source)), 1, 7)
    assert total == 1
