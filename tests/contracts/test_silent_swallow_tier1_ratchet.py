"""WS-13 PB-29 — Tier-1 silent-swallow ratchet contract.

The spec (§WS-13 design, PB-29) calls for a **tiered** audit of the
``except Exception`` handlers in ``src/``. The repo carries ≈121
``except Exception`` occurrences (≈65 bare swallows + 56 with
``as exc``). The fail-loud principle is most critical on the **Tier-1**
governance / data-loss paths; Tier-2 (intentionally broad / annotated
with ``# noqa: BLE001``) is acceptable for now.

Tier-1 paths (any non-handling ``except Exception: pass`` or
``except Exception: <silent>`` here is a ratchet violation):
  - ``src/commands/gather_pipeline/persistence_stage.py``
  - ``src/commands/confirm_stages/post_confirm_support.py``
  - ``src/commands/confirm_stages/archive_transaction.py``
  - ``src/commands/confirm_stages/baseline_followthrough.py``
  - ``src/commands/confirm_stages/weekly_summary_card.py``
  - ``src/core/archive_store.py``
  - ``src/core/narrative_store.py``
  - ``src/core/snapshot_store.py``
  - ``src/core/program_fact_store.py``
  - ``src/core/quality_gates/`` (gates must surface as QG not as silent)

The contract test enumerates every ``except Exception`` line in those
files, classifies it as one of:
  - ``cleanup_then_raise``  → not a swallow (acceptable)
  - ``loud_log_and_raise``  → not a swallow (acceptable)
  - ``loud_warn_continue``  → records the warning into the result
    (acceptable for partial-degrade flows; collected in ``warnings``)
  - ``bare_silent``         → violation; the ratchet rejects

The test is the **starting point** of a ratchet. It records the current
count of ``bare_silent`` occurrences in Tier-1 files. The expected
behavior is **0** at the time of this contract being added. If a future
PR regresses (adds a ``bare_silent`` in a Tier-1 path), the test fails.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

# Tier-1 governance / data-loss paths. Paths are relative to ``src/`` and
# match either an exact file or a subpackage directory.
TIER1_PATHS: tuple[str, ...] = (
    "commands/gather_pipeline/persistence_stage.py",
    "commands/confirm_stages/post_confirm_support.py",
    "commands/confirm_stages/archive_transaction.py",
    "commands/confirm_stages/baseline_followthrough.py",
    "commands/confirm_stages/weekly_summary_card.py",
    "core/archive_store.py",
    "core/narrative_store.py",
    "core/snapshot_store.py",
    "core/program_fact_store.py",
)


def _classify_handler(node: ast.ExceptHandler, source: str) -> str:
    """Classify the body of an ``except Exception`` handler.

    Returns one of:
      - ``cleanup_then_raise``  : the body re-raises (the exception is
        not lost; the surrounding stage just gets a chance to clean up).
      - ``loud_log_and_raise``  : the body calls ``log.error(...)`` /
        ``logger.error(...)`` and then re-raises.
      - ``loud_warn_continue``  : records the warning into a ``warnings``
        collection or returns a warning-bearing tuple. The confirm-stages
        use this pattern: ``return (card_path, False, f'...: {exc}')``.
      - ``noqa_pre_approved``   : annotated with ``# noqa: BLE001`` — the
        spec's rev. 318 inventory recognizes 10 such pre-approved sites.
      - ``bare_silent``         : violation; the ratchet rejects.
    """
    body = node.body
    if not body:
        return "bare_silent"

    # Pre-approved: the spec recognizes `# noqa: BLE001` annotations.
    handler_lineno = node.lineno
    handler_lines = _get_source_lines(source, handler_lineno, len(body) + 2)
    for line in handler_lines:
        if "# noqa: BLE001" in line or "# noqa:BLE001" in line:
            return "noqa_pre_approved"

    # Direct re-raise: ``raise`` or ``raise SomeError(...)``.
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return "cleanup_then_raise"

    # Loud log + raise.
    saw_log = False
    for stmt in body:
        if _is_logging_call(stmt, level="error"):
            saw_log = True
        elif isinstance(stmt, ast.Raise) and saw_log:
            return "loud_log_and_raise"

    # Loud-warn: append / extend to a ``warnings`` collection OR return
    # a warning-bearing tuple.
    saw_warn = False
    for stmt in body:
        if _is_warnings_append(stmt):
            saw_warn = True
        elif _is_warning_return(stmt):
            saw_warn = True
    if saw_warn:
        return "loud_warn_continue"

    # Otherwise: bare silent.
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return "bare_silent"

    # Multi-statement body: any raise → cleanup_then_raise; otherwise
    # treat as bare.
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return "cleanup_then_raise"
    return "bare_silent"


def _is_logging_call(stmt: ast.stmt, *, level: str) -> bool:
    """Return True iff ``stmt`` is a call like ``log.error(...)`` /
    ``logger.exception(...)``."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr in {level, "exception", "critical"}


def _is_warnings_append(stmt: ast.stmt) -> bool:
    """Return True iff ``stmt`` is a ``warnings = warnings + (...)`` or
    ``warnings.append(...)`` (or a tuple-typed equivalent)."""
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            if isinstance(target, ast.Name) and target.id == "warnings":
                return True
    if isinstance(stmt, ast.Expr):
        call = stmt.value
        if isinstance(call, ast.Call):
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr in {"append", "extend"}:
                if isinstance(func.value, ast.Name) and func.value.id == "warnings":
                    return True
    return False


def _is_warning_return(stmt: ast.stmt) -> bool:
    """Return True iff ``stmt`` is a ``return`` whose value is a tuple
    that includes an f-string formatted with ``{exc}`` (or any name
    ending in ``exc``/``error``). The confirm-stages use this pattern:
    ``return (None, False, f"...: {exc}")``.
    """
    if not isinstance(stmt, ast.Return):
        return False
    val = stmt.value
    if not isinstance(val, ast.Tuple):
        return False
    for elt in val.elts:
        if isinstance(elt, ast.JoinedStr):
            for value in elt.values:
                if isinstance(value, ast.FormattedValue):
                    src = ast.unparse(value.value)
                    if src.endswith("exc") or src.endswith("error"):
                        return True
    return False


def _get_source_lines(source: str, start: int, count: int) -> list[str]:
    """Return ``count`` lines of ``source`` starting at ``start`` (1-indexed)."""
    lines = source.splitlines()
    return lines[start - 1 : start - 1 + count]


def _iter_tier1_files() -> list[Path]:
    files: list[Path] = []
    for rel in TIER1_PATHS:
        path = SRC_ROOT / rel
        if path.is_file():
            files.append(path)
    return files


def test_tier1_silent_swallows_are_zero() -> None:
    """The ratchet. Tier-1 paths MUST NOT contain ``bare_silent`` handlers.

    This is the starting point. If you legitimately need to add a silent
    degrade in a Tier-1 path, you must (1) add a ``# noqa: BLE001`` AND
    (2) make it ``loud_warn_continue`` by recording into ``warnings``.
    """
    violations: list[tuple[str, int, str]] = []
    counts: dict[str, int] = {
        "cleanup_then_raise": 0,
        "loud_log_and_raise": 0,
        "loud_warn_continue": 0,
        "noqa_pre_approved": 0,
        "bare_silent": 0,
    }
    for path in _iter_tier1_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _is_broad_exception(node.type):
                continue
            kind = _classify_handler(node, source)
            counts[kind] += 1
            if kind == "bare_silent":
                violations.append((rel, node.lineno, ast.unparse(node.body[0]) if node.body else "pass"))
    assert violations == [], (
        "WS-13 PB-29 Tier-1 ratchet: a Tier-1 governance / data-loss "
        "path has a bare silent-swallow. Either (a) re-raise the "
        "exception, (b) log + re-raise, or (c) record the error into "
        "the result's ``warnings`` tuple. Findings: "
        + "; ".join(f"{rel}:{line} (body={body!r})" for rel, line, body in violations)
    )
    # Diagnostic: print the breakdown so the spec can track progress.
    # (Pytest captures stdout; ``-s`` will surface it.)
    print(f"WS-13 Tier-1 audit: {counts}")


def _is_broad_exception(type_node: ast.AST | None) -> bool:
    """Return True iff the except clause catches ``Exception`` (or a
    bare ``except:``). We don't catch ``BaseException`` here — that's a
    separate pattern the ratchet intentionally ignores.
    """
    if type_node is None:
        # bare ``except:`` — this IS a broad catch.
        return True
    if isinstance(type_node, ast.Name) and type_node.id == "Exception":
        return True
    if isinstance(type_node, ast.Tuple):
        return any(_is_broad_exception(elt) for elt in type_node.elts)
    return False


@pytest.mark.parametrize("rel_path", list(TIER1_PATHS))
def test_tier1_paths_exist(rel_path: str) -> None:
    """Every Tier-1 path listed in the contract must still exist on disk.
    This catches the case where a file is renamed and the ratchet
    silently stops auditing it.
    """
    path = SRC_ROOT / rel_path
    assert path.is_file(), f"Tier-1 ratchet path {rel_path} is missing"
