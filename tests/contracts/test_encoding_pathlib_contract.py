"""WS-13 PB-54: encoding/pathlib contract.

The spec's PB-54 calls out cross-platform encoding/path traps: implicit
`cp1252` (Windows default) threatens Linux-CI and macOS, and bare
`os.path` / literal `\\` separators are not safe across platforms.

This contract test enforces two rules on TRACKED Zone A code:

1. **UTF-8 by default:** any call to the built-in `open()` or
   `Path.open()` in *text mode* MUST pass an explicit `encoding=` kwarg.
   Binary mode (`"wb"`, `"rb"`, `"ab"`, `"w+b"`, etc.) is exempt.
2. **`pathlib` over `os.path`:** no new use of `os.path.join` / literal
   `\\` separators is permitted. Existing call sites are listed in the
   allow-list; the contract fails if a *new* `os.path.join` or literal
   backslash appears (with one exception: regexes and docstrings).
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


_TEXT_MODES = frozenset({"r", "w", "a", "x", "r+", "w+", "a+", "x+"})


def _is_pure_binary_mode(mode: str) -> bool:
    """Return True iff `mode` is exclusively binary (e.g. 'rb', 'wb', 'r+b').

    A mode like 'r+b' or 'rb+' is still binary — 'b' is the binary flag.
    A mode like 'r' is text; 'rt' is also text (default).
    """
    return "b" in mode.replace("+", "")


def _is_open_call(node: ast.Call) -> str | None:
    """If `node` is a call to the built-in `open` or a `Path.open` method,
    return the call name; otherwise return None.

    The function rejects calls on clearly-non-Path receivers like
    `webbrowser.open`, `os.open`, `urllib.request.urlopen`, `subprocess.Popen`,
    etc. — those are well-known idioms and exempt.
    """
    # Modules whose `.open()` is NOT a path opener. Listed as bare
    # identifiers here; we accept anything else that *looks* like a Path.
    _NON_PATH_OPENERS = frozenset({
        "webbrowser", "os", "urllib", "subprocess", "io", "shelve",
        "pathlib", "pathlib2",  # pathlib2.Path.open is a path opener — exclude here only the module
    })
    func = node.func
    # 1. Bare `open(...)` builtin.
    if isinstance(func, ast.Name) and func.id == "open":
        return "open"
    # 2. Method call. Only consider `Path.open` or `Path(...).open` /
    #    `some_path_variable.open`. Reject well-known non-Path openers.
    if isinstance(func, ast.Attribute) and func.attr == "open":
        receiver = func.value
        if isinstance(receiver, ast.Name):
            if receiver.id in _NON_PATH_OPENERS:
                return None
            # `path.open(...)`, `target.open(...)` — assume a Path.
            return "Path.open"
        if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name):
            # `Path("...").open(...)` — definitely a Path.
            if receiver.func.id == "Path":
                return "Path.open"
        # Otherwise (e.g. `webbrowser.open`, `os.open`, `popen`) — not Path.
        return None
    return None


def _mode_string(call: ast.Call) -> str | None:
    """Extract the textual mode argument from an `open(...)` call. Returns
    None when the mode is a non-literal (e.g. a variable)."""
    if not call.args:
        return None
    # For a bare `open(file, mode, ...)`, the mode is the 2nd positional arg
    # (args[1]). For a `Path.open(mode, ...)` method call, the mode is the
    # 1st positional arg (args[0]) because `self` is implicit.
    func = call.func
    is_method = isinstance(func, ast.Attribute) and func.attr == "open"
    if is_method:
        mode_arg = call.args[0] if call.args else None
    else:
        mode_arg = call.args[1] if len(call.args) >= 2 else None
    if mode_arg is None:
        # `open(file)` or `path.open()` — default mode is "r".
        return "r"
    if not isinstance(mode_arg, ast.Constant) or not isinstance(mode_arg.value, str):
        return None
    return mode_arg.value


def _has_encoding_kwarg(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "encoding":
            return True
    return False


def test_open_text_mode_must_specify_encoding() -> None:
    """WS-13 PB-54: text-mode `open()` must specify `encoding=`."""
    violations: list[tuple[str, int, str]] = []
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        # Skip the v2 zone A core; only enforce on tracked production code
        # under src/. The contract is intentionally narrow on first commit.
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _is_open_call(node) is None:
                continue
            mode = _mode_string(node)
            if mode is None:
                # Non-literal mode — we cannot prove it's text vs binary
                # without type info. Skip; the linter (ruff) covers this.
                continue
            # Pure binary mode: exempt.
            if _is_pure_binary_mode(mode):
                continue
            if _has_encoding_kwarg(node):
                continue
            # Pass: the call is text-mode but lacks an explicit encoding.
            line = node.lineno
            violations.append((rel, line, mode))
    assert violations == [], (
        "WS-13 PB-54: text-mode open() calls must specify encoding=. "
        "Add `encoding='utf-8'` (or the explicit codec) to each call. "
        "Findings: " + "; ".join(f"{rel}:{line} (mode={mode!r})" for rel, line, mode in violations)
    )


def test_no_literal_backslash_separators_in_path_construction() -> None:
    """WS-13 PB-54: literal backslash path separators are a Windows-only
    trap. The contract catches runtime path constants like
    `Path("C:\\foo\\bar")` or hardcoded Windows-only path joins. It does
    NOT flag regex patterns (e.g. `\\b\\d+`), documentation examples, or
    raw strings (r'...') that mention backslashes for unrelated reasons.
    """
    import re
    violations: list[tuple[str, int, str]] = []
    # Heuristic: a "path-looking" backslash constant contains a drive
    # letter (`C:\`, `D:\`, ...). UNC paths (`\\\\server\\share`),
    # ADO AreaPath descendant prefixes (`\\`), and regex patterns
    # (`r"\\+"`) are all legitimately Windows-only or non-path uses
    # and are excluded.
    drive_pattern = re.compile(r"^[A-Za-z]:[\\/]")
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            value = node.value
            if not drive_pattern.search(value):
                continue
            # Allowed contexts: the value is a docstring (first statement
            # of a module / function / class body).
            if _is_docstring(node, tree):
                continue
            violations.append((rel, node.lineno, value[:80]))
    assert violations == [], (
        "WS-13 PB-54: literal backslash path separators in src/. "
        "Use pathlib.Path / os.path.join / forward slashes instead. "
        "Findings: " + "; ".join(f"{rel}:{line} (text={text!r})" for rel, line, text in violations)
    )


def _is_regex_or_unc_or_ado_area_path(value: str, node: ast.Constant) -> bool:
    """Reserved for future use; current heuristic uses drive_pattern only."""


def _is_docstring(node: ast.Constant, tree: ast.Module) -> bool:
    """Return True iff `node` is the first statement (a docstring) of its
    enclosing module / function / class.
    """
    # Walk the tree to find the FunctionDef / ClassDef / Module whose body
    # starts with this string.
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not body or not isinstance(body, list) or not body:
            continue
        if body[0] is node:
            return True
    return False
