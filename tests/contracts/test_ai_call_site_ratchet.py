"""Function-level syntactic inventory ratchet, not runtime authorization.

The existing inventory is the only baseline. Recognized attribute-call syntax
does not resolve provider types, aliases, factories or dynamic dispatch.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
_INVENTORY_PATH = _REPO_ROOT / "governance" / "ai-call-inventory.md"
_BASELINE_HEADING = "## Function-level syntactic baseline"


def _provider_calls(source: str, path: str) -> list[tuple[str, str, str, int]]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    counts: Counter[tuple[str, str]] = Counter()
    found: list[tuple[str, str, str, int]] = []
    calls = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for node in calls:
        if not isinstance(node.func, ast.Attribute):
            continue
        callee = ast.unparse(node.func)
        if node.func.attr not in {"structured", "chat"} and not callee.endswith(".chat.completions.create"):
            continue
        scope: list[str] = []
        ancestor = node
        while ancestor in parents:
            ancestor = parents[ancestor]
            if isinstance(ancestor, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                scope.append(ancestor.name)
        function = ".".join(reversed(scope)) or "<module>"
        counts[function, callee] += 1
        found.append((path, function, callee, counts[function, callee]))
    return found


def _inventory_rows(text: str) -> list[tuple[str, str, str, int, str, str]]:
    assert text.count(_BASELINE_HEADING) == 1, "Expected one function-level inventory table."
    section = text.split(_BASELINE_HEADING, 1)[1].split("\n## ", 1)[0]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    assert len(lines) > 2, "Missing function-level inventory rows."
    assert lines[0] == "| Path | Qualified function | Callee | Ordinal | Feature | Disposition |"
    assert lines[1] == "|---|---|---|---|---|---|"
    rows: list[tuple[str, str, str, int, str, str]] = []
    for line in lines[2:]:
        fields = [field.strip() for field in line.strip("|").split("|")]
        assert len(fields) == 6 and all(fields), f"Invalid inventory row: {line}"
        path, function, callee, ordinal, feature, disposition = fields
        assert ordinal.isdecimal() and int(ordinal) > 0, f"Invalid ordinal: {line}"
        assert disposition in {"legacy", "test-only-pilot", "infrastructure"}, f"Invalid disposition: {line}"
        rows.append((path, function, callee, int(ordinal), feature, disposition))
    identities = [row[:4] for row in rows]
    assert len(set(identities)) == len(identities), "Duplicate invocation identity in inventory."
    return rows


def _assert_inventory(
    actual: list[tuple[str, str, str, int]],
    rows: list[tuple[str, str, str, int, str, str]],
) -> None:
    expected = Counter(row[:4] for row in rows)
    observed = Counter(actual)
    assert observed == expected, (
        f"Uninventoried syntax: {list((observed - expected).elements())}; "
        f"stale inventory: {list((expected - observed).elements())}"
    )


def test_provider_invocations_match_function_inventory() -> None:
    actual = [
        call
        for path in sorted(_SRC_ROOT.rglob("*.py"))
        for call in _provider_calls(path.read_text(encoding="utf-8-sig"), path.relative_to(_REPO_ROOT).as_posix())
    ]
    _assert_inventory(actual, _inventory_rows(_INVENTORY_PATH.read_text(encoding="utf-8")))


def _baseline_text(rows: str) -> str:
    return (
        "## Function-level syntactic baseline\n\n"
        "| Path | Qualified function | Callee | Ordinal | Feature | Disposition |\n"
        "|---|---|---|---|---|---|\n"
        f"{rows}\n\n## Static limitations\n"
    )


_SYNTHETIC_ROW = "| src/example.py | generate | client.structured | 1 | example | legacy |"


@pytest.mark.parametrize(
    "source",
    [
        "def generate():\n client.structured()\n client.structured()\n",
        "def generate():\n client.structured()\ndef another():\n client.chat()\n",
        "def generate():\n pass\n",
    ],
)
def test_function_baseline_rejects_added_or_missing_calls(source: str) -> None:
    with pytest.raises(AssertionError):
        _assert_inventory(
            _provider_calls(source, "src/example.py"),
            _inventory_rows(_baseline_text(_SYNTHETIC_ROW)),
        )


@pytest.mark.parametrize(
    "rows",
    [
        _SYNTHETIC_ROW + "\n" + _SYNTHETIC_ROW,
        _SYNTHETIC_ROW.replace("legacy", "approved"),
        _SYNTHETIC_ROW.replace("| 1 |", "| 0 |"),
        _SYNTHETIC_ROW.replace("| example |", "|  |"),
        "| src/example.py | generate | client.structured |",
    ],
)
def test_function_baseline_rejects_invalid_rows(rows: str) -> None:
    with pytest.raises(AssertionError):
        _inventory_rows(_baseline_text(rows))


def test_function_baseline_requires_table() -> None:
    with pytest.raises(AssertionError):
        _inventory_rows("# Inventory without a function baseline")


def test_function_baseline_rejects_stale_row() -> None:
    rows = _SYNTHETIC_ROW + "\n" + _SYNTHETIC_ROW.replace("generate", "removed")
    with pytest.raises(AssertionError):
        _assert_inventory(
            _provider_calls("def generate():\n client.structured()\n", "src/example.py"),
            _inventory_rows(_baseline_text(rows)),
        )


def test_function_baseline_ignores_comments_and_line_changes() -> None:
    source = (
        '"""client.chat()"""\n\n# client.structured()\n'
        "def generate():\n\n client.structured()\n"
    )
    _assert_inventory(
        _provider_calls(source, "src/example.py"),
        _inventory_rows(_baseline_text(_SYNTHETIC_ROW)),
    )


def test_function_baseline_uses_lexical_scope_and_source_order() -> None:
    source = (
        "class Extractor:\n"
        " def generate(self):\n"
        "  if enabled:\n"
        "   client.structured()\n"
        "  other.chat()\n"
        "  client.structured()\n"
        "  def provider():\n"
        "   client.structured()\n"
    )
    assert _provider_calls(source, "src/example.py") == [
        ("src/example.py", "Extractor.generate", "client.structured", 1),
        ("src/example.py", "Extractor.generate", "other.chat", 1),
        ("src/example.py", "Extractor.generate", "client.structured", 2),
        ("src/example.py", "Extractor.generate.provider", "client.structured", 1),
    ]


def test_function_baseline_recognizes_sdk_and_forwarding_syntax() -> None:
    source = (
        "def forward():\n client.chat()\ndef transport():\n"
        " self._client.chat.completions.create()\n other.create()\n"
    )
    assert _provider_calls(source, "src/example.py") == [
        ("src/example.py", "forward", "client.chat", 1),
        ("src/example.py", "transport", "self._client.chat.completions.create", 1),
    ]


def test_function_baseline_does_not_claim_dynamic_resolution() -> None:
    source = (
        "def generate():\n"
        " getattr(client, 'structured')()\n"
        " invoke = client.structured\n invoke()\n unrelated.chat()\n"
    )
    assert _provider_calls(source, "src/example.py") == [
        ("src/example.py", "generate", "unrelated.chat", 1),
    ]
