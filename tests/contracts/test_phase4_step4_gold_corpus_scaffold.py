"""Contract: Phase 4 Step 4 — gold corpus is a frozen regression
fixture for deterministic-first claim/action extraction.

Step 4 of Phase 4 says: deterministic-first claim and action
extractors must be exhaustively covered by a gold corpus, so that
future changes to the deterministic path (or the AI wrapper) cannot
silently regress on evidence classes that were previously captured.

The gold corpus already exists at ``programs/acme/gold_corpus/``
(``claims/`` and ``actions/`` subdirectories) and the
``tests/unit/test_ai_extractor_gold_corpus.py`` test harness
already exercises every case against a failing AI client
(``_FailingClient``) to prove the deterministic path takes over.

This contract freezes three things so the corpus doesn't drift:
  (a) **Layout invariant**: the corpus is at the canonical path
      with the canonical subdirectory structure (claims/, actions/).
  (b) **Schema invariant**: every case has a valid shape that
      matches the per-case test harness. Claim cases use the
      ``narratives`` shape; action cases use the ``signals`` shape.
      Both require ``input`` / ``expected_output`` /
      ``expected_deterministic_confidence`` at the top level.
  (c) **Coverage invariant**: every deterministic evidence class
      in ``_CLAIM_HINTS`` and ``_ASK_HINTS`` (in
      ``src/core/claim_tracker.py``) is covered by at least one
      corpus case that exercises the matching phrase. If a new
      hint is added to the regex evidence set without a corpus
      case, this contract will fail.

Why:** the gold corpus is the regression fixture that proves the
deterministic path actually fires. If the corpus is missing a
case for a hint, a refactor could break that hint's behavior
silently (the test would still pass against the AI fallback).
The coverage invariant makes that drift auditable.
**How to apply:** when adding a new claim hint to
``_CLAIM_HINTS`` or ``_ASK_HINTS``, also add a corresponding
``programs/acme/gold_corpus/claims/<hint>_claim.yaml`` case that
exercises the new phrase. The contract test will fail until
the case is added.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_CORPUS = REPO_ROOT / "programs" / "acme" / "gold_corpus"
CLAIMS_DIR = GOLD_CORPUS / "claims"
ACTIONS_DIR = GOLD_CORPUS / "actions"
CLAIM_TRACKER = REPO_ROOT / "src" / "core" / "claim_tracker.py"


# --- (a) Layout invariant ---------------------------------------------------

def test_gold_corpus_directories_exist() -> None:
    """The corpus must live at the canonical path with the canonical
    subdirectory structure. This is the layout that the per-case
    test harness (``tests/unit/test_ai_extractor_gold_corpus.py``)
    expects."""
    assert GOLD_CORPUS.is_dir(), (
        f"Gold corpus directory not found at {GOLD_CORPUS}. "
        f"Step 4 requires programs/acme/gold_corpus/ with claims/ and actions/ subdirs."
    )
    assert CLAIMS_DIR.is_dir(), f"Missing claims/ subdirectory at {CLAIMS_DIR}"
    assert ACTIONS_DIR.is_dir(), f"Missing actions/ subdirectory at {ACTIONS_DIR}"


def test_gold_corpus_has_minimum_case_counts() -> None:
    """The corpus must have a meaningful number of cases. The
    lower bound is the count of deterministic evidence classes
    we want to lock down -- one case per evidence class, with
    some headroom for boundary conditions."""
    claim_cases = list(CLAIMS_DIR.glob("*.yaml")) if CLAIMS_DIR.exists() else []
    action_cases = list(ACTIONS_DIR.glob("*.yaml")) if ACTIONS_DIR.exists() else []
    assert len(claim_cases) >= 4, (
        f"Gold corpus has only {len(claim_cases)} claim cases; "
        f"expected at least 4 to cover the deterministic evidence classes."
    )
    assert len(action_cases) >= 2, (
        f"Gold corpus has only {len(action_cases)} action cases; "
        f"expected at least 2 to cover the explicit-marker and heuristic paths."
    )


# --- (b) Schema invariant ---------------------------------------------------

_CLAIM_INPUT_KEYS = frozenset({"program_id", "edition_id", "issue_number", "claim_date", "narratives", "items"})
_ACTION_INPUT_KEYS = frozenset({"program_id", "signals"})
_OUTPUT_KEYS = frozenset({"claims", "decision_asks", "actions"})


def _validate_claim_case(case_path: Path) -> list[str]:
    errors: list[str] = []
    doc = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return [f"{case_path.name}: top-level must be a YAML mapping"]
    if "input" not in doc or not isinstance(doc["input"], dict):
        errors.append(f"{case_path.name}: missing 'input' mapping")
        return errors
    if "expected_output" not in doc or not isinstance(doc["expected_output"], dict):
        errors.append(f"{case_path.name}: missing 'expected_output' mapping")
        return errors
    input_keys = set(doc["input"].keys())
    missing_input = _CLAIM_INPUT_KEYS - input_keys
    if missing_input:
        errors.append(
            f"{case_path.name}: claim input missing required keys: {sorted(missing_input)}. "
            f"Claim cases must declare program_id, edition_id, issue_number, claim_date, narratives."
        )
    output_keys = set(doc["expected_output"].keys())
    if not (output_keys & _OUTPUT_KEYS):
        errors.append(
            f"{case_path.name}: claim expected_output must declare at least one of: claims, decision_asks."
        )
    if "expected_deterministic_confidence" not in doc:
        errors.append(
            f"{case_path.name}: missing 'expected_deterministic_confidence' "
            f"at the top level (required so per-case test asserts the deterministic "
            f"path actually fired)."
        )
    return errors


def _validate_action_case(case_path: Path) -> list[str]:
    errors: list[str] = []
    doc = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return [f"{case_path.name}: top-level must be a YAML mapping"]
    if "input" not in doc or not isinstance(doc["input"], dict):
        errors.append(f"{case_path.name}: missing 'input' mapping")
        return errors
    if "expected_output" not in doc or not isinstance(doc["expected_output"], dict):
        errors.append(f"{case_path.name}: missing 'expected_output' mapping")
        return errors
    input_keys = set(doc["input"].keys())
    missing_input = _ACTION_INPUT_KEYS - input_keys
    if missing_input:
        errors.append(
            f"{case_path.name}: action input missing required keys: {sorted(missing_input)}. "
            f"Action cases must declare program_id and signals (list of signal dicts)."
        )
    if "actions" not in doc["expected_output"]:
        errors.append(
            f"{case_path.name}: action expected_output must declare 'actions' list."
        )
    if "expected_deterministic_confidence" not in doc:
        errors.append(
            f"{case_path.name}: missing 'expected_deterministic_confidence' at the top level."
        )
    return errors


@pytest.mark.parametrize("path", sorted(CLAIMS_DIR.glob("*.yaml")) if CLAIMS_DIR.exists() else [])
def test_claim_case_schema_is_canonical(path: Path) -> None:
    errors = _validate_claim_case(path)
    assert not errors, "Gold corpus claim case schema drift:\n" + "\n".join(errors)


@pytest.mark.parametrize("path", sorted(ACTIONS_DIR.glob("*.yaml")) if ACTIONS_DIR.exists() else [])
def test_action_case_schema_is_canonical(path: Path) -> None:
    errors = _validate_action_case(path)
    assert not errors, "Gold corpus action case schema drift:\n" + "\n".join(errors)


# --- (c) Coverage invariant -------------------------------------------------

def _extract_claim_tracker_hints() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Parse ``_CLAIM_HINTS`` and ``_ASK_HINTS`` from
    ``src/core/claim_tracker.py`` as raw string tuples."""
    source = CLAIM_TRACKER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CLAIM_TRACKER))
    claim_hints: list[str] = []
    ask_hints: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        is_claim = any(isinstance(t, ast.Name) and t.id == "_CLAIM_HINTS" for t in node.targets)
        is_ask = any(isinstance(t, ast.Name) and t.id == "_ASK_HINTS" for t in node.targets)
        if not (is_claim or is_ask):
            continue
        if not isinstance(node.value, ast.Tuple):
            continue
        for elt in node.value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                if is_claim:
                    claim_hints.append(elt.value)
                else:
                    ask_hints.append(elt.value)
    return tuple(claim_hints), tuple(ask_hints)


def _phrase_in_claim_corpus(phrase: str, case_paths: list[Path]) -> bool:
    """Return True if any claim corpus case contains the phrase in
    its narrative. Case-insensitive substring match is enough --
    the deterministic regex uses substring containment."""
    needle = phrase.lower()
    for case_path in case_paths:
        doc = yaml.safe_load(case_path.read_text(encoding="utf-8"))
        narratives = doc.get("input", {}).get("narratives", {})
        if not isinstance(narratives, dict):
            continue
        for text in narratives.values():
            if isinstance(text, str) and needle in text.lower():
                return True
    return False


def test_each_claim_hint_has_corpus_coverage() -> None:
    """For every entry in ``_CLAIM_HINTS``, at least one corpus
    case must include that phrase in its narrative. If you add a
    new hint, also add a gold corpus case that exercises it --
    otherwise this contract will fail."""
    claim_hints, _ = _extract_claim_tracker_hints()
    assert claim_hints, "No _CLAIM_HINTS found in claim_tracker.py"
    case_paths = list(CLAIMS_DIR.glob("*.yaml")) if CLAIMS_DIR.exists() else []
    if not case_paths:
        pytest.skip("No claim corpus cases present (programs/ is gitignored)")
    uncovered: list[str] = []
    for hint in claim_hints:
        if not _phrase_in_claim_corpus(hint, case_paths):
            uncovered.append(hint)
    assert not uncovered, (
        f"Gold corpus is missing coverage for these _CLAIM_HINTS entries: {uncovered}. "
        f"Add a programs/acme/gold_corpus/claims/<hint>_claim.yaml case that exercises "
        f"each uncovered phrase so future regressions are caught. "
        f"Available claim cases: {[p.name for p in case_paths]}."
    )


def test_each_ask_hint_has_corpus_coverage() -> None:
    """Same coverage invariant for ``_ASK_HINTS``. Decision-ask
    detection is part of the deterministic claim surface, and a
    new ask phrase without a corpus case could regress
    silently."""
    _, ask_hints = _extract_claim_tracker_hints()
    assert ask_hints, "No _ASK_HINTS found in claim_tracker.py"
    case_paths = list(CLAIMS_DIR.glob("*.yaml")) if CLAIMS_DIR.exists() else []
    if not case_paths:
        pytest.skip("No claim corpus cases present (programs/ is gitignored)")
    uncovered: list[str] = []
    for hint in ask_hints:
        if not _phrase_in_claim_corpus(hint, case_paths):
            uncovered.append(hint)
    assert not uncovered, (
        f"Gold corpus is missing coverage for these _ASK_HINTS entries: {uncovered}. "
        f"Add a programs/acme/gold_corpus/claims/<hint>_claim.yaml case that exercises "
        f"each uncovered phrase so future regressions are caught. "
        f"Available claim cases: {[p.name for p in case_paths]}."
    )


def test_action_marker_is_in_at_least_one_action_case() -> None:
    """The deterministic action path is triggered by lines
    starting with ``Action:`` (see
    ``_starts_with_action_marker`` in
    ``src/ai/action_extractor.py``). At least one gold corpus
    action case must use that marker so the regression fixture
    covers the explicit-marker path."""
    case_paths = list(ACTIONS_DIR.glob("*.yaml")) if ACTIONS_DIR.exists() else []
    if not case_paths:
        pytest.skip("No action corpus cases present (programs/ is gitignored)")
    marker_re = re.compile(r"(?im)^\s*Action:")
    for case_path in case_paths:
        doc = yaml.safe_load(case_path.read_text(encoding="utf-8"))
        signals = doc.get("input", {}).get("signals", [])
        if not isinstance(signals, list):
            continue
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            text = signal.get("text", "")
            if isinstance(text, str) and marker_re.search(text):
                return
    pytest.fail(
        "No gold corpus action case uses the 'Action:' marker. The deterministic "
        "action path is triggered by lines starting with that marker -- the corpus "
        "must cover the explicit-marker code path."
    )

