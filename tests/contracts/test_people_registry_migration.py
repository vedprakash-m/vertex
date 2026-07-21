"""specs/people.md Phase 2a: migration/namespace-bridge contract tests.

This file is named directly by two work items' §9.1 verification text:
PPL-W2A.4 ("alias-rename and ledger refs resolve to the same canonical
entity") and PPL-W2A.7 (shadow-mode parity, not yet implemented --
that section will extend this file once it lands). This initial version
covers PPL-W2A.4's slice only: `src/core/people_namespace_bridge.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.exceptions import ConfigError
from src.core.people_entity_schema import (
    AliasStatus,
    CanonicalEntity,
    EntityAlias,
    EntityRedirect,
)
from src.core.people_namespace_bridge import (
    normalize_alias_for_lookup,
    normalize_email_for_lookup,
    reject_header_injection_risk,
    resolve_entity_redirect,
    resolve_ref_to_canonical_entity_id,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _alias(value: str, *, status: AliasStatus = AliasStatus.ACTIVE) -> EntityAlias:
    return EntityAlias(
        value=value,
        kind="vertex::alias",
        status=status,
        valid_from=_NOW,
        valid_until=None,
        source="operator_assertion",
        source_ref=None,
        recorded_at=_NOW,
        verified_at=_NOW,
        verified_by_principal="ACME\\steward",
    )


def _entity(entity_id: str, *, aliases: tuple[EntityAlias, ...]) -> CanonicalEntity:
    return CanonicalEntity(
        workspace_id="workspace:acme",
        entity_id=entity_id,
        entity_type="person",
        canonical_name=entity_id,
        aliases=aliases,
        scope="org",
        created_at=_NOW,
    )


def test_p_prefix_and_person_prefix_ledger_refs_resolve_to_the_same_canonical_entity() -> None:
    # specs/people.md §9.1's exact PPL-W2A.4 verification: "ledger refs
    # resolve to the same canonical entity."
    entities = (_entity("person:01ABC", aliases=(_alias("jdoe"),)),)

    from_signal_layer = resolve_ref_to_canonical_entity_id("P:jdoe", entities=entities)
    from_ledger_form = resolve_ref_to_canonical_entity_id("person:jdoe", entities=entities)
    from_bare_alias = resolve_ref_to_canonical_entity_id("jdoe", entities=entities)

    assert from_signal_layer.canonical_entity_id == "person:01ABC"
    assert from_ledger_form.canonical_entity_id == "person:01ABC"
    assert from_bare_alias.canonical_entity_id == "person:01ABC"


def test_already_canonical_ref_resolves_to_itself() -> None:
    entities = (_entity("person:01ABC", aliases=(_alias("jdoe"),)),)

    result = resolve_ref_to_canonical_entity_id("person:01ABC", entities=entities)

    assert result.canonical_entity_id == "person:01ABC"
    assert result.resolved_via == "already_canonical"


def test_alias_rename_still_resolves_to_the_same_canonical_entity_via_historical_alias() -> None:
    # specs/people.md §9.1's exact PPL-W2A.4 verification: "alias-rename...
    # resolve to the same canonical entity." §7.2a: "Alias history remains
    # resolvable after rename."
    entities = (
        _entity(
            "person:01ABC",
            aliases=(
                _alias("jdoe_old", status=AliasStatus.HISTORICAL),
                _alias("jdoe_new", status=AliasStatus.ACTIVE),
            ),
        ),
    )

    old_alias_resolution = resolve_ref_to_canonical_entity_id("P:jdoe_old", entities=entities)
    new_alias_resolution = resolve_ref_to_canonical_entity_id("P:jdoe_new", entities=entities)

    assert old_alias_resolution.canonical_entity_id == new_alias_resolution.canonical_entity_id == "person:01ABC"


def test_alias_lookup_is_case_and_unicode_normalization_insensitive() -> None:
    entities = (_entity("person:01ABC", aliases=(_alias("jdoe"),)),)

    result = resolve_ref_to_canonical_entity_id("P:JDoe", entities=entities)

    assert result.canonical_entity_id == "person:01ABC"


def test_unresolvable_ref_returns_none_not_an_exception() -> None:
    result = resolve_ref_to_canonical_entity_id("P:nobody", entities=())

    assert result.canonical_entity_id is None
    assert result.resolved_via == "unresolved"


def test_tombstoned_entity_resolves_through_its_redirect() -> None:
    entities = (
        _entity("person:old", aliases=(_alias("jdoe"),)),
        _entity("person:new", aliases=()),
    )
    redirects = (EntityRedirect(from_entity_id="person:old", to_entity_id="person:new", recorded_at=_NOW, principal_id="ACME\\steward", reason="merge"),)

    result = resolve_ref_to_canonical_entity_id("person:old", entities=entities, redirects=redirects)

    assert result.canonical_entity_id == "person:new"
    assert result.resolved_via == "redirect"


def test_alias_lookup_also_follows_redirect_to_the_final_target() -> None:
    entities = (
        _entity("person:old", aliases=(_alias("jdoe"),)),
        _entity("person:new", aliases=()),
    )
    redirects = (EntityRedirect(from_entity_id="person:old", to_entity_id="person:new", recorded_at=_NOW, principal_id="ACME\\steward", reason="merge"),)

    result = resolve_ref_to_canonical_entity_id("P:jdoe", entities=entities, redirects=redirects)

    assert result.canonical_entity_id == "person:new"


def test_resolve_entity_redirect_detects_cycles() -> None:
    redirects = (
        EntityRedirect(from_entity_id="person:a", to_entity_id="person:b", recorded_at=_NOW, principal_id="x", reason="r"),
        EntityRedirect(from_entity_id="person:b", to_entity_id="person:a", recorded_at=_NOW, principal_id="x", reason="r"),
    )

    with pytest.raises(ConfigError, match="cycle"):
        resolve_entity_redirect("person:a", redirects)


def test_resolve_entity_redirect_no_op_when_no_redirect_exists() -> None:
    assert resolve_entity_redirect("person:standalone", ()) == "person:standalone"


def test_normalize_alias_for_lookup_applies_nfc_and_casefold() -> None:
    assert normalize_alias_for_lookup("  JDoe  ") == "jdoe"


def test_normalize_email_preserves_local_part_case_but_normalizes_domain() -> None:
    normalized = normalize_email_for_lookup("JDoe@EXAMPLE.com")

    assert normalized == "JDoe@example.com"  # Local part case preserved; domain lowercased.


def test_reject_header_injection_risk_blocks_crlf() -> None:
    with pytest.raises(ConfigError, match="header-injection risk"):
        reject_header_injection_risk("jdoe\r\nBcc: attacker@evil.example", field_name="alias")


def test_reject_header_injection_risk_allows_clean_values() -> None:
    reject_header_injection_risk("jdoe", field_name="alias")  # Must not raise.


def test_normalize_alias_for_lookup_rejects_control_characters() -> None:
    with pytest.raises(ConfigError, match="header-injection risk"):
        normalize_alias_for_lookup("jdoe\n")


# ---------------------------------------------------------------------------
# PPL-W2A.7: shadow-mode parity proof.
# specs/people.md §9.1's own verification bar: "zero divergence on a
# representative existing program's data before any program can request
# `primary`." Uses only synthetic fixtures (no real customer program data,
# which is gitignored local-only and wouldn't exist in a fresh CI clone
# anyway -- confirmed via `git check-ignore` before writing this section).
# ---------------------------------------------------------------------------

import yaml as _yaml

from src.core.people_shadow_parity import (
    compute_and_record_shadow_parity_if_in_shadow_mode,
    compute_shadow_parity,
    record_shadow_parity,
    shadow_parity_status_path,
)


def _write_synthetic_program(programs_root, program_id: str = "acme") -> None:
    knowledge_dir = programs_root / program_id / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "people_directory.yaml").write_text(
        _yaml.safe_dump({"schema_version": "1.0", "people": [{"alias": "alice"}, {"alias": "bob"}]}),
        encoding="utf-8",
    )
    (knowledge_dir / "teams.yaml").write_text(
        _yaml.safe_dump({"schema_version": "1.0", "teams": [{"id": "team-a", "name": "Team A"}]}),
        encoding="utf-8",
    )
    (knowledge_dir / "people_profiles.yaml").write_text(_yaml.safe_dump({"schema_version": "1.0", "profiles": []}), encoding="utf-8")
    (knowledge_dir / "products.yaml").write_text(_yaml.safe_dump({"schema_version": "1.0", "products": []}), encoding="utf-8")
    (knowledge_dir / "golden_queries.yaml").write_text(_yaml.safe_dump({"schema_version": "1.0", "queries": []}), encoding="utf-8")


def test_compute_shadow_parity_is_zero_divergence_on_synthetic_program_data(tmp_path: Path) -> None:
    # specs/people.md §9.1's exact PPL-W2A.7 verification: "zero divergence
    # on a representative existing program's data." Both loaders read the
    # SAME on-disk files -- this proves the v2 dual-read loader recognizes
    # the identical identity surface the legacy loader does.
    programs_root = tmp_path / "programs"
    _write_synthetic_program(programs_root)

    record = compute_shadow_parity("acme", programs_root=programs_root)

    assert record.is_zero_divergence is True
    assert record.divergences == ()
    assert record.legacy_person_count == record.canonical_person_count == 2
    assert record.legacy_team_count == record.canonical_team_count == 1


def test_compute_shadow_parity_detects_a_deliberate_alias_divergence(tmp_path: Path, monkeypatch) -> None:
    # Proves the DETECTOR itself is correct, not just that same-file reads
    # trivially agree -- monkeypatches the canonical loader to simulate a
    # genuine parsing discrepancy.
    import src.core.people_shadow_parity as shadow_parity_module
    from src.core.people_directory_schema import PeopleProfilesLoadResult  # noqa: F401 (type reference only)
    from src.core.people_directory_schema import PersonDirectory, PeopleDirectoryLoadResult

    programs_root = tmp_path / "programs"
    _write_synthetic_program(programs_root)

    def _fake_load_people_directory(path):
        return PeopleDirectoryLoadResult(people=(PersonDirectory(entity_id="", alias="alice"),), diagnostics=())

    monkeypatch.setattr(shadow_parity_module, "load_people_directory", _fake_load_people_directory)

    record = compute_shadow_parity("acme", programs_root=programs_root)

    assert record.is_zero_divergence is False
    missing = [d for d in record.divergences if d.kind == "person_alias_missing_in_canonical"]
    assert len(missing) == 1
    assert missing[0].key == "bob"


def test_record_shadow_parity_writes_state_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_synthetic_program(programs_root)
    record = compute_shadow_parity("acme", programs_root=programs_root)

    written_path = record_shadow_parity(record, programs_root=programs_root)

    assert written_path == shadow_parity_status_path(programs_root, "acme")
    assert written_path.exists()
    import json as _json

    payload = _json.loads(written_path.read_text(encoding="utf-8"))
    assert payload["is_zero_divergence"] is True
    assert payload["program_id"] == "acme"


def test_wiring_skips_computation_for_a_legacy_mode_program(tmp_path: Path) -> None:
    # §6.6 PPL-W1.9 wiring: "legacy" mode never pays the double-compile cost.
    from src.core.people_registry_identity import bootstrap_registry_identity

    programs_root = tmp_path / "programs"
    _write_synthetic_program(programs_root)
    bootstrap_registry_identity(knowledge_root=programs_root.parent / "knowledge", customer_boundary_id="acme-corp", apply=True)

    result = compute_and_record_shadow_parity_if_in_shadow_mode("acme", programs_root=programs_root)

    assert result is None
    assert not shadow_parity_status_path(programs_root, "acme").exists()


def test_wiring_computes_and_records_for_a_shadow_mode_program(tmp_path: Path) -> None:
    from src.core.people_registry_identity import bootstrap_registry_identity
    from src.core.people_registry_modes import set_program_mode

    programs_root = tmp_path / "programs"
    _write_synthetic_program(programs_root)
    knowledge_root = programs_root.parent / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    set_program_mode(knowledge_root, "acme", "shadow", actor="operator")

    result = compute_and_record_shadow_parity_if_in_shadow_mode("acme", programs_root=programs_root)

    assert result is not None
    assert result.is_zero_divergence is True
    assert shadow_parity_status_path(programs_root, "acme").exists()


def test_wiring_returns_none_before_registry_bootstrap(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_synthetic_program(programs_root)

    assert compute_and_record_shadow_parity_if_in_shadow_mode("acme", programs_root=programs_root) is None
