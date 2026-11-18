from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ParsedPythonSource:
    path: Path
    relative: str
    tree: ast.Module
    string_constants: frozenset[str]
    imports: frozenset[str]


@lru_cache(maxsize=None)
def parse_python_source_index(root: Path, relative_to: Path) -> tuple[ParsedPythonSource, ...]:
    root = root.resolve()
    relative_to = relative_to.resolve()
    parsed: list[ParsedPythonSource] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        string_constants = frozenset(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        parsed.append(
            ParsedPythonSource(
                path=path,
                relative=path.relative_to(relative_to).as_posix(),
                tree=tree,
                string_constants=string_constants,
                imports=frozenset(imports),
            )
        )
    return tuple(parsed)
