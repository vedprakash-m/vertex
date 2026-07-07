from __future__ import annotations

from pathlib import Path

from src.core.ncfl_models import TARGET_STORES
from src.core.ncfl_store_policy import (
    audit_plane1_store_policy,
    is_ncfl_apply_writable_target_store,
    ncfl_apply_writable_target_stores,
    plane1_policy_by_root_yaml,
    target_policy_by_store,
)


def test_nova_root_yaml_inventory_is_explicitly_classified() -> None:
    program_root = Path(__file__).resolve().parents[2] / "programs" / "nova"

    assert audit_plane1_store_policy(program_root) == ()


def test_ncfl_target_stores_are_all_explicitly_decisioned() -> None:
    policies = target_policy_by_store()

    assert TARGET_STORES <= set(policies)
    # Phase 5 (§24.6): knowledge_doc is now apply-writable (Zone B markdown target,
    # not a Plane 1 YAML record store — hence root_yaml stays None).
    assert policies["knowledge_doc"].apply_writable is True
    assert policies["knowledge_doc"].root_yaml is None


def test_ncfl_apply_writable_subset_is_conservative() -> None:
    # Phase 5 (§24.6) adds knowledge_doc (Zone B markdown synthesis target).
    assert ncfl_apply_writable_target_stores() == frozenset({
        "assumptions",
        "decisions",
        "milestones",
        "risk_register",
        "workstreams",
        "knowledge_doc",
    })
    assert is_ncfl_apply_writable_target_store("knowledge_doc") is True
    assert is_ncfl_apply_writable_target_store("dependencies") is False


def test_apply_writable_targets_require_ncfl_writable_root_yaml() -> None:
    root_policies = plane1_policy_by_root_yaml()
    for target_policy in target_policy_by_store().values():
        if not target_policy.apply_writable:
            continue
        # knowledge_doc is a Zone B markdown target (Phase 5, §24.6): it writes
        # knowledge/<doc>.md directly, so it has no Plane 1 YAML root_yaml.
        if target_policy.target_store == "knowledge_doc":
            assert target_policy.root_yaml is None
            continue
        assert target_policy.root_yaml is not None
        assert root_policies[target_policy.root_yaml].ncfl_writable is True
