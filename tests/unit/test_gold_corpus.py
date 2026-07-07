"""Tests for the gold corpus classification fixture (FR-SG-26)."""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

_CORPUS_PATH = Path(__file__).parents[2] / "programs" / "acme" / "gold_corpus" / "issue_078_sentences.yaml"

# The gold corpus is local-only program data (programs/ is gitignored). On a
# fresh clone / CI it is absent, so skip the module rather than fail.
pytestmark = pytest.mark.skipif(
    not _CORPUS_PATH.exists(),
    reason="Requires local gold corpus data (programs/ is gitignored)",
)

_VALID_SOURCE_CATEGORIES = {"ado", "email", "teams", "kusto", "manual"}
_VALID_SIGNAL_CLASSES = {"status", "rca", "mitigation", "decision", "risk", "dependency"}
_VALID_SECTIONS = {"top_3_now", "milestone", "dependency", "risk", "decision", "narrative", "rca", None}


def _load_corpus() -> list[dict]:
    doc = yaml.safe_load(_CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "corpus must be a YAML mapping"
    entries = doc.get("entries")
    assert isinstance(entries, list), "corpus must have an 'entries' list"
    return entries


def test_gold_corpus_file_exists():
    assert _CORPUS_PATH.exists(), f"gold corpus file not found at {_CORPUS_PATH}"


def test_gold_corpus_has_entries():
    entries = _load_corpus()
    assert len(entries) >= 10, "corpus must have at least 10 entries"


def test_gold_corpus_schema():
    entries = _load_corpus()
    for i, entry in enumerate(entries):
        assert isinstance(entry, dict), f"entry {i} must be a dict"
        assert "sentence" in entry, f"entry {i} missing 'sentence'"
        assert "source_category" in entry, f"entry {i} missing 'source_category'"
        assert "signal_class" in entry, f"entry {i} missing 'signal_class'"

        assert isinstance(entry["sentence"], str) and entry["sentence"].strip(), f"entry {i} 'sentence' must be non-empty string"
        assert entry["source_category"] in _VALID_SOURCE_CATEGORIES, f"entry {i} invalid source_category: {entry['source_category']!r}"
        assert entry["signal_class"] in _VALID_SIGNAL_CLASSES, f"entry {i} invalid signal_class: {entry['signal_class']!r}"
        section = entry.get("section")
        assert section in _VALID_SECTIONS, f"entry {i} invalid section: {section!r}"


def test_gold_corpus_covers_all_signal_classes():
    entries = _load_corpus()
    found_classes = {e["signal_class"] for e in entries}
    missing = _VALID_SIGNAL_CLASSES - found_classes
    assert not missing, f"corpus is missing signal_class entries for: {missing}"


def test_gold_corpus_covers_multiple_sources():
    entries = _load_corpus()
    found_sources = {e["source_category"] for e in entries}
    assert len(found_sources) >= 3, f"corpus should cover at least 3 source categories; found: {found_sources}"
