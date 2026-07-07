"""Unit tests for FR-SG-32: entity_resolution.py"""

from __future__ import annotations

import textwrap
from pathlib import Path

from src.core.entity_resolution import (
    AliasEntry,
    apply_entity_resolution,
    build_combined_directory,
    extract_mention_candidates,
    extract_work_item_refs,
    load_alias_map,
    resolve_alias,
)
from src.core.models_v2 import PersonDirectory, Signal
from src.core.models import Confidence


# ---------------------------------------------------------------------------
# AliasEntry construction
# ---------------------------------------------------------------------------

def test_alias_entry_is_frozen_and_hashable() -> None:
    entry = AliasEntry(canonical="jsmith", aliases=("john.smith", "john_smith"), email="jsmith@example.com")
    assert hash(entry) is not None


# ---------------------------------------------------------------------------
# resolve_alias
# ---------------------------------------------------------------------------

def test_resolve_alias_by_canonical() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=()),)
    assert resolve_alias("jsmith", entries) == "jsmith"


def test_resolve_alias_case_insensitive() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=()),)
    assert resolve_alias("JSMITH", entries) == "jsmith"


def test_resolve_alias_by_alias_member() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=("john.smith", "johnny")),)
    assert resolve_alias("john.smith", entries) == "jsmith"


def test_resolve_alias_by_email() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=(), email="jsmith@example.com"),)
    assert resolve_alias("jsmith@example.com", entries) == "jsmith"


def test_resolve_alias_returns_none_for_unknown() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=()),)
    assert resolve_alias("nobody", entries) is None


def test_resolve_alias_returns_none_for_empty_string() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=()),)
    assert resolve_alias("", entries) is None


def test_resolve_alias_returns_none_for_whitespace() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=()),)
    assert resolve_alias("   ", entries) is None


# ---------------------------------------------------------------------------
# extract_mention_candidates
# ---------------------------------------------------------------------------

def test_extract_mention_candidates_at_mention() -> None:
    text = "Please sync with @jsmith on this."
    candidates = extract_mention_candidates(text)
    assert "jsmith" in candidates


def test_extract_mention_candidates_email() -> None:
    text = "Send to jsmith@example.com for review."
    candidates = extract_mention_candidates(text)
    assert "jsmith@example.com" in candidates


def test_extract_mention_candidates_dedupes() -> None:
    text = "@jsmith and @jsmith again."
    candidates = extract_mention_candidates(text)
    assert candidates.count("jsmith") == 1


def test_extract_mention_candidates_empty_text() -> None:
    assert extract_mention_candidates("") == ()


# ---------------------------------------------------------------------------
# apply_entity_resolution
# ---------------------------------------------------------------------------

def _signal(**kwargs) -> Signal:
    defaults = dict(
        id="sig-1",
        timestamp=__import__("datetime").datetime(2026, 5, 17, 12, 0, tzinfo=__import__("datetime").timezone.utc),
        source="ado/revision",
        program_id="acme",
        workstream_id="acme",
        entity_refs=(),
        text="",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )
    defaults.update(kwargs)
    return Signal(**defaults)


def test_apply_entity_resolution_resolves_entity_refs() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=("john.smith",)),)
    sig = _signal(entity_refs=("john.smith",))
    resolved = apply_entity_resolution(sig, entries)
    assert resolved.entity_refs == ("jsmith",)


def test_apply_entity_resolution_resolves_sender_alias_in_metadata() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=("john.smith",)),)
    sig = _signal(metadata={"sender_alias": "john.smith"})
    resolved = apply_entity_resolution(sig, entries)
    assert resolved.metadata["sender_alias"] == "jsmith"


def test_apply_entity_resolution_keeps_unresolved_refs_as_is() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=()),)
    sig = _signal(entity_refs=("unknown_person",))
    resolved = apply_entity_resolution(sig, entries)
    assert "unknown_person" in resolved.entity_refs


def test_apply_entity_resolution_adds_resolved_mention_from_text() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=("jsmith",)),)
    sig = _signal(text="Please ask @jsmith to review.")
    resolved = apply_entity_resolution(sig, entries)
    assert "jsmith" in resolved.entity_refs


def test_apply_entity_resolution_no_duplicate_refs_after_mention_resolution() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=()),)
    sig = _signal(entity_refs=("jsmith",), text="CC @jsmith on this.")
    resolved = apply_entity_resolution(sig, entries)
    assert resolved.entity_refs.count("jsmith") == 1


def test_apply_entity_resolution_preserves_other_metadata() -> None:
    entries = (AliasEntry(canonical="jsmith", aliases=()),)
    sig = _signal(metadata={"sender_alias": "jsmith", "other_key": "value"})
    resolved = apply_entity_resolution(sig, entries)
    assert resolved.metadata.get("other_key") == "value"


# ---------------------------------------------------------------------------
# extract_work_item_refs
# ---------------------------------------------------------------------------

def test_extract_work_item_refs_ab_hash_format() -> None:
    refs = extract_work_item_refs("Blocked by AB#12345 and AB#67890.")
    assert 12345 in refs
    assert 67890 in refs


def test_extract_work_item_refs_hash_format() -> None:
    refs = extract_work_item_refs("See #900001 for context.")
    assert 900001 in refs


def test_extract_work_item_refs_dedupes() -> None:
    refs = extract_work_item_refs("AB#12345 and AB#12345 again.")
    assert refs.count(12345) == 1


def test_extract_work_item_refs_no_match() -> None:
    assert extract_work_item_refs("No work items here.") == ()


# ---------------------------------------------------------------------------
# load_alias_map
# ---------------------------------------------------------------------------

def test_load_alias_map_returns_empty_tuple_when_absent(tmp_path: Path) -> None:
    result = load_alias_map("acme", programs_root=tmp_path / "programs")
    assert result == ()


def test_load_alias_map_parses_yaml(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    alias_file = programs_root / "acme" / "alias_map.yaml"
    alias_file.parent.mkdir(parents=True, exist_ok=True)
    alias_file.write_text(
        textwrap.dedent("""\
            aliases:
              - canonical: jsmith
                aliases: [john.smith, johnny]
                email: jsmith@example.com
                display_name: "John Smith"
        """),
        encoding="utf-8",
    )
    entries = load_alias_map("acme", programs_root=programs_root)
    assert len(entries) == 1
    assert entries[0].canonical == "jsmith"
    assert "john.smith" in entries[0].aliases
    assert entries[0].email == "jsmith@example.com"


def test_load_alias_map_skips_entries_without_canonical(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    alias_file = programs_root / "acme" / "alias_map.yaml"
    alias_file.parent.mkdir(parents=True, exist_ok=True)
    alias_file.write_text(
        textwrap.dedent("""\
            aliases:
              - aliases: [anon]
        """),
        encoding="utf-8",
    )
    entries = load_alias_map("acme", programs_root=programs_root)
    assert entries == ()


# ---------------------------------------------------------------------------
# build_combined_directory
# ---------------------------------------------------------------------------

def test_build_combined_directory_merges_person_and_alias_entries() -> None:
    person = PersonDirectory(alias="pmehta", email="pmehta@example.com", display_name="Priya Mehta")
    alias_entry = AliasEntry(canonical="jsmith", aliases=("john.smith",))
    combined = build_combined_directory((person,), (alias_entry,))
    canonicals = {e.canonical for e in combined}
    assert "jsmith" in canonicals
    assert "pmehta" in canonicals
