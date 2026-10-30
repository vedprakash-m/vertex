from dataclasses import FrozenInstanceError, asdict, fields
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.core.exceptions import ConfigError
from src.core.manifest_writer import (
    build_run_evidence_snapshot,
    build_run_manifest,
    hash_jsonable,
    load_run_evidence_snapshot,
)
from src.core.gather_run_manifest import GatherRunManifest, GatherRunStatus, QueryResultEntry, RequiredScopeStatus
from src.core.models import EditionType, EvidenceBinding, RiskLevel, RunEvidenceSnapshot, Snapshot, SnapshotItem, SourceScopeContract
from src.core.program_fact_store import FactSnapshotPin, ProgramFactRevision, ProgramFactSnapshot


def _capture(*, ids=(2, 1), queries=(), facts=()):
    clock = datetime(2026, 5, 5, tzinfo=timezone.utc)
    pin = FactSnapshotPin("pfs_original", "fixture", clock, None, len(facts), {}, 2, "a" * 64)
    snapshot = ProgramFactSnapshot("fixture", clock, cast(tuple[ProgramFactRevision, ...], facts), clock)
    report = Snapshot(7, clock, clock, EditionType.DETAILED, tuple(
        SnapshotItem(item_id, "Task", "Synthetic", "Active", None, "Area", None, RiskLevel.LOW, [])
        for item_id in ids
    ), ())
    gather = GatherRunManifest(
        run_id="gather", status=GatherRunStatus.COMMITTED, program_id="fixture",
        actor_identity_type="interactive", lease_owner="test", lease_fencing_token=1,
        started_at=clock, scope_as_of=clock, required_scope_status=RequiredScopeStatus.FULL,
        manifest_hash="gather-hash", query_results=cast(tuple[QueryResultEntry, ...], queries),
    )
    manifest = build_run_manifest(
        "manifest", 7, "fixture", clock, clock, {}, report, "", "", 0, 0, 0.0, {}, {}, None,
        gather_run_id="gather", gather_run_hash="gather-hash",
    )
    return build_run_evidence_snapshot(pin, snapshot, gather, manifest, report)


def _wire(capture):
    payload = {
        "config_hash": capture.config_hash, "snapshot_hash": capture.report_snapshot_hash,
        "gather_run_id": capture.gather_run_id, "gather_run_hash": capture.gather_run_hash,
        "metadata": {"run_evidence_snapshot": asdict(capture)},
    }
    draft = {
        "program_fact_snapshot": {"snapshot_id": capture.fact_snapshot_id, "program_id": capture.program_id},
        "gather_run_id": capture.gather_run_id, "gather_run_hash": capture.gather_run_hash,
        "run_evidence_snapshot_sha256": capture.sha256,
    }
    return payload, draft


def test_minimal_capture_round_trip_and_integrity():
    capture = _capture()
    assert sum(len(fields(model)) for model in (SourceScopeContract, EvidenceBinding, RunEvidenceSnapshot)) == 21
    assert capture.capture_state == "unqualified_capture"
    assert capture.fetched_item_ids == (1, 2)
    payload, draft = _wire(capture)
    assert load_run_evidence_snapshot(payload, draft) == capture
    with pytest.raises(FrozenInstanceError):
        capture.program_id = "other"


@pytest.mark.parametrize("field,value", [
    ("schema_version", "other"), ("capture_state", "qualified"), ("limitations", ()),
    ("sha256", "bad"), ("as_of", datetime(2026, 5, 5)), ("fetched_item_ids", (True,)),
    ("fetched_item_ids", (1, 1)), ("fetched_item_ids", (-1,)), ("program_id", ""),
    ("source_scopes", ({"query_id": "q", "scope_id": "s", "query_hash": "h"},
                       {"query_id": "q", "scope_id": "s", "query_hash": "other"})),
])
def test_invalid_declared_capture_fails(field, value):
    payload, draft = _wire(_capture())
    payload["metadata"]["run_evidence_snapshot"][field] = value
    with pytest.raises(ConfigError):
        load_run_evidence_snapshot(payload, draft)


@pytest.mark.parametrize("location,field,value", [
    ("draft", "run_evidence_snapshot_sha256", None),
    ("draft", "gather_run_id", "other"),
    ("payload", "config_hash", "other"),
    ("payload", "snapshot_hash", "other"),
    ("payload", "gather_run_hash", "other"),
    ("draft", "program_fact_snapshot", {"snapshot_id": "other", "program_id": "fixture"}),
    ("draft", "program_fact_snapshot", {"snapshot_id": "pfs_original", "program_id": "other"}),
])
def test_capture_reference_and_alias_mismatch_fails(location, field, value):
    payload, draft = _wire(_capture())
    (draft if location == "draft" else payload)[field] = value
    with pytest.raises(ConfigError):
        load_run_evidence_snapshot(payload, draft)


def test_declarations_are_not_observed_membership_or_evaluated_bindings():
    query = SimpleNamespace(query_id="q", scope_id="s", wiql_hash="query", membership_ids=("999",))
    fact = SimpleNamespace(revision_id="revision", entity_refs=("PROGRAM:fixture",))
    first = _capture(queries=(query,), facts=(fact,))
    second = _capture(ids=(3,), queries=(SimpleNamespace(
        query_id="q", scope_id="s", wiql_hash="query", membership_ids=(),
    ),), facts=(fact,))
    assert first.source_scopes == second.source_scopes == (SourceScopeContract("q", "s", "query"),)
    assert first.sha256 != second.sha256
    assert first.binding_declarations == (EvidenceBinding("revision", ("PROGRAM:fixture",)),)
    assert "binding_unevaluated" in first.limitations
    assert not hasattr(first.binding_declarations[0], "accepted")
    assert _capture(facts=(SimpleNamespace(revision_id="empty", entity_refs=()),)).binding_declarations[0].entity_refs == ()


def test_absent_capture_is_legacy_but_one_sided_reference_is_not():
    assert load_run_evidence_snapshot({}, {}) is None
    assert load_run_evidence_snapshot({}, {"run_evidence_snapshot_sha256": None}) is None
    with pytest.raises(ConfigError):
        load_run_evidence_snapshot({}, {"run_evidence_snapshot_sha256": "declared"})
    with pytest.raises(ConfigError):
        load_run_evidence_snapshot({"metadata": {"run_evidence_snapshot": None}}, {})
    with pytest.raises(ConfigError):
        load_run_evidence_snapshot({"metadata": ["invalid"]}, {})


def test_builder_rejects_missing_or_foreign_retained_facts():
    clock = datetime(2026, 5, 5, tzinfo=timezone.utc)
    pin = SimpleNamespace(program_id="fixture")
    for snapshot in (None, SimpleNamespace(program_id="other", as_of=clock)):
        with pytest.raises(ConfigError):
            build_run_evidence_snapshot(cast(Any, pin), cast(Any, snapshot), None, cast(Any, None), cast(Any, None))


def test_capture_requires_exact_wire_fields():
    payload, draft = _wire(_capture())
    del payload["metadata"]["run_evidence_snapshot"]["config_hash"]
    with pytest.raises(ConfigError):
        load_run_evidence_snapshot(payload, draft)
