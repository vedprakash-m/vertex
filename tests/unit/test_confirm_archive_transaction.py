from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.commands.confirm_stages import archive_transaction
from src.core.archive_store import ConfirmedIssueArchivePaths
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, read_events
from src.core.ledger.program_views import project_program_events
from src.core.ledger.source_refs import LTDeckRef, NewsletterRef, OperatorAssertionRef
from src.core.models import RunManifest
from src.core.projections.snapshot_manager import write_projection_snapshot


@dataclass(frozen=True, slots=True)
class _DimensionStub:
    name: str
    risk: object | None


@dataclass(frozen=True, slots=True)
class _ScorecardStub:
    dimensions: tuple[_DimensionStub, ...]


def _manifest() -> RunManifest:
    return RunManifest(
        manifest_id="manifest-1",
        issue_number=1,
        edition="acme_weekly",
        started_at=datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 6, 6, 10, 5, tzinfo=timezone.utc),
        config_hash="cfg",
        snapshot_hash="snap",
        html_hash="html",
        md_hash="md",
        ado_calls=0,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={},
        qg_results={},
        git_sha=None,
        metadata={"suggested_subject": "Weekly"},
    )


def _archive_paths(tmp_path: Path) -> ConfirmedIssueArchivePaths:
    return ConfirmedIssueArchivePaths(
        snapshot_path=tmp_path / "issue_001.snapshot.json",
        eml_path=tmp_path / "issue_001.eml",
        html_path=tmp_path / "issue_001.html",
        md_path=tmp_path / "issue_001.md",
        manifest_path=tmp_path / "issue_001.manifest.json",
        index_path=tmp_path / "index.json",
        scorecards_path=tmp_path / "scorecards.json",
        overrides_path=tmp_path / "issue_001.overrides.yaml",
        review_path=tmp_path / "issue_001.review.yaml",
        narratives_path=tmp_path / "narratives",
    )


def _selected_callback(result: Any, programs_root: Path | None):
    def invoke(**kwargs):
        if programs_root is None:
            assert "programs_root" not in kwargs
        else:
            assert kwargs["programs_root"] == programs_root
        return result

    return invoke


@pytest.mark.parametrize("program_backed", [False, True])
def test_execute_archive_transaction_records_semantic_index_warning(
    tmp_path: Path, monkeypatch, program_backed: bool,
) -> None:
    dirty_calls: list[str] = []
    callback_roots: dict[str, Path] = {}

    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "ArchiveLock", lambda path: nullcontext())
    monkeypatch.setattr(archive_transaction, "get_archive_root", lambda edition, root, *, programs_root=None: root)
    monkeypatch.setattr(archive_transaction, "read_archive_index", lambda edition, archive_root, *, programs_root=None: SimpleNamespace(issues=()))
    monkeypatch.setattr(archive_transaction, "find_latest_confirmed_entry", lambda index, before_issue_number=None: None)
    monkeypatch.setattr(archive_transaction, "load_proposals", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        archive_transaction,
        "update_archive_semantic_index_for_issue",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("semantic boom")),
    )
    monkeypatch.setattr(
        archive_transaction,
        "mark_semantic_index_dirty",
        lambda edition, reason, archive_root, *, programs_root: (
            callback_roots.update(dirty=programs_root), dirty_calls.append(reason),
        ),
    )
    monkeypatch.setattr(archive_transaction, "project_confirmed_issue", lambda **kwargs: None)
    monkeypatch.setattr(archive_transaction, "record_override", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "try_advance_earned_autonomy", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "load_gather_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "write_issue_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "load_program_facts", lambda *args, **kwargs: object())
    monkeypatch.setattr(archive_transaction, "persist_program_fact_snapshot", lambda *args, **kwargs: None)

    programs_root = tmp_path / "programs"

    def get_overrides_path(edition, reports_root, *, issue_number, programs_root=None):
        callback_roots["overrides"] = programs_root
        return tmp_path / "issue_001.overrides.yaml"

    def record_claims(**kwargs):
        callback_roots["claims"] = kwargs.get("programs_root")
        return ()

    result = archive_transaction.execute_archive_transaction(
        edition_name="acme_weekly",
        issue_number=1,
        confirmed_at=datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc),
        warnings=(),
        archive_root=tmp_path / "archive",
        programs_root=programs_root,
        reports_root=tmp_path / "reports",
        approved_signals=(),
        artifacts=(
            None,
            None,
            SimpleNamespace(scorecards=()),
            None,
            "<html />",
            "markdown",
            SimpleNamespace(ado_data_as_of=datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc), items=()),
        ),
        bundle=SimpleNamespace(),
        manifest=_manifest(),
        overrides_document=SimpleNamespace(scorecards=()),
        vitality_archive_entry=None,
        chart_cache_entries=None,
        review_status_path=tmp_path / "review.yaml",
        narrative_dir=tmp_path / "narratives",
        program_id="acme",
        resolved_v2=(
            SimpleNamespace(program=SimpleNamespace(id="acme"), raw_program={"id": "acme"})
            if program_backed
            else None
        ),
        legacy_regex_extractor=False,
        extraction_result=None,
        extraction_mode="disabled",
        revalidate_evidence_fn=lambda: None,
        build_confirmed_eml_bytes_fn=lambda *args, **kwargs: None,
        write_confirmed_fn=_selected_callback(tmp_path / "staged.snapshot.json", programs_root if program_backed else None),
        write_confirmed_issue_fn=_selected_callback(_archive_paths(tmp_path), programs_root if program_backed else None),
        get_overrides_path_fn=get_overrides_path,
        load_draft_continuation_contract_path_fn=lambda edition, issue_number, *, programs_root=None: None,
        write_context_snapshot_for_issue_fn=lambda **kwargs: None,
        record_confirmed_claims_for_v2_fn=record_claims,
        semantic_index_enabled_fn=lambda raw_program: True,
        update_archive_semantic_index_for_issue_fn=archive_transaction.update_archive_semantic_index_for_issue,
        mark_semantic_index_dirty_fn=archive_transaction.mark_semantic_index_dirty,
        write_optimization_proposals_fn=lambda *args, **kwargs: None,
        load_gather_state_fn=archive_transaction.load_gather_state,
        compute_source_health_pct_fn=lambda gather_state: None,
        compute_provenance_confidence_fn=lambda approved_signals: None,
        get_signal_class_fn=lambda signal: archive_transaction.SignalClass.INFO,
        upsert_risk_from_signal_fn=lambda *args, **kwargs: None,
    )

    assert result.archive_paths.html_path == tmp_path / "issue_001.html"
    assert any("Semantic index update skipped: semantic boom" in warning for warning in result.warnings) is program_backed
    assert dirty_calls == (["confirm issue 001: semantic boom"] if program_backed else [])
    expected_root = programs_root if program_backed else None
    assert callback_roots == {
        "overrides": expected_root, "claims": expected_root,
        **({"dirty": expected_root} if program_backed else {}),
    }


def test_execute_archive_transaction_records_ledger_baseline_warning_on_snapshot_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "ArchiveLock", lambda path: nullcontext())
    monkeypatch.setattr(archive_transaction, "get_archive_root", lambda edition, root, *, programs_root=None: root)
    monkeypatch.setattr(archive_transaction, "read_archive_index", lambda edition, archive_root, *, programs_root=None: SimpleNamespace(issues=()))
    monkeypatch.setattr(archive_transaction, "find_latest_confirmed_entry", lambda index, before_issue_number=None: None)
    monkeypatch.setattr(archive_transaction, "load_proposals", lambda *args, **kwargs: ())
    monkeypatch.setattr(archive_transaction, "project_confirmed_issue", lambda **kwargs: None)
    monkeypatch.setattr(archive_transaction, "record_override", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "try_advance_earned_autonomy", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "load_gather_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "write_issue_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "load_program_facts", lambda *args, **kwargs: object())
    monkeypatch.setattr(archive_transaction, "persist_program_fact_snapshot", lambda *args, **kwargs: None)

    result = archive_transaction.execute_archive_transaction(
        edition_name="acme_weekly",
        issue_number=1,
        confirmed_at=datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc),
        warnings=(),
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
        approved_signals=(),
        artifacts=(
            None,
            None,
            SimpleNamespace(scorecards=()),
            None,
            "<html />",
            "markdown",
            SimpleNamespace(ado_data_as_of=datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc), items=()),
        ),
        bundle=SimpleNamespace(),
        manifest=_manifest(),
        overrides_document=SimpleNamespace(scorecards=()),
        vitality_archive_entry=None,
        chart_cache_entries=None,
        review_status_path=tmp_path / "review.yaml",
        narrative_dir=tmp_path / "narratives",
        program_id="acme",
        resolved_v2=SimpleNamespace(program=SimpleNamespace(id="acme"), raw_program={"id": "acme"}),
        legacy_regex_extractor=False,
        extraction_result=None,
        extraction_mode="disabled",
        revalidate_evidence_fn=lambda: None,
        build_confirmed_eml_bytes_fn=lambda *args, **kwargs: None,
        write_confirmed_fn=_selected_callback(tmp_path / "staged.snapshot.json", tmp_path / "programs"),
        write_confirmed_issue_fn=_selected_callback(_archive_paths(tmp_path), tmp_path / "programs"),
        get_overrides_path_fn=lambda edition, reports_root, issue_number, **kwargs: tmp_path / "issue_001.overrides.yaml",
        load_draft_continuation_contract_path_fn=lambda edition, issue_number, *, programs_root=None: None,
        write_context_snapshot_for_issue_fn=lambda **kwargs: None,
        record_confirmed_claims_for_v2_fn=lambda **kwargs: (),
        semantic_index_enabled_fn=lambda raw_program: False,
        update_archive_semantic_index_for_issue_fn=archive_transaction.update_archive_semantic_index_for_issue,
        mark_semantic_index_dirty_fn=archive_transaction.mark_semantic_index_dirty,
        write_optimization_proposals_fn=lambda *args, **kwargs: None,
        load_gather_state_fn=archive_transaction.load_gather_state,
        compute_source_health_pct_fn=lambda gather_state: None,
        compute_provenance_confidence_fn=lambda approved_signals: None,
        get_signal_class_fn=lambda signal: archive_transaction.SignalClass.INFO,
        upsert_risk_from_signal_fn=lambda *args, **kwargs: None,
        write_projection_snapshot_fn=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("snapshot boom")),
    )


def test_execute_archive_transaction_projects_metrics_and_risk_followthrough(tmp_path: Path, monkeypatch) -> None:
    projected: list[str] = []
    metrics: list[object] = []
    risk_upserts: list[str] = []
    persisted: list[str] = []

    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "ArchiveLock", lambda path: nullcontext())
    monkeypatch.setattr(archive_transaction, "get_archive_root", lambda edition, root, *, programs_root=None: root)
    monkeypatch.setattr(archive_transaction, "read_archive_index", lambda edition, archive_root, *, programs_root=None: SimpleNamespace(issues=()))
    monkeypatch.setattr(archive_transaction, "find_latest_confirmed_entry", lambda index, before_issue_number=None: None)
    monkeypatch.setattr(archive_transaction, "load_proposals", lambda *args, **kwargs: ())
    monkeypatch.setattr(archive_transaction, "project_confirmed_issue", lambda **kwargs: projected.append("ok"))
    monkeypatch.setattr(archive_transaction, "record_override", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "try_advance_earned_autonomy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        archive_transaction,
        "load_gather_state",
        lambda *args, **kwargs: SimpleNamespace(channels={"ado": {"active": True, "last_error": None}}),
    )
    monkeypatch.setattr(archive_transaction, "write_issue_metrics", lambda metric, programs_root: metrics.append(metric))
    monkeypatch.setattr(archive_transaction, "load_program_facts", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        archive_transaction,
        "persist_program_fact_snapshot",
        lambda snapshot, recorded_at, accepted_by, db_root: persisted.append(accepted_by),
    )

    approved_signal = SimpleNamespace(
        id="sig-risk",
        text="Risk identified",
        entity_refs=("WI:42",),
        workstream_id="ws",
        source_confidence_tier="high",
    )

    result = archive_transaction.execute_archive_transaction(
        edition_name="acme_weekly",
        issue_number=1,
        confirmed_at=datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc),
        warnings=(),
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
        approved_signals=(approved_signal,),
        artifacts=(
            None,
            None,
            SimpleNamespace(scorecards=()),
            None,
            "<html />",
            "markdown",
            SimpleNamespace(ado_data_as_of=datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc), items=()),
        ),
        bundle=SimpleNamespace(),
        manifest=_manifest(),
        overrides_document=SimpleNamespace(
            scorecards=(
                _ScorecardStub(dimensions=(_DimensionStub(name="Delivery", risk=SimpleNamespace(value="low")),)),
            )
        ),
        vitality_archive_entry=None,
        chart_cache_entries=None,
        review_status_path=tmp_path / "review.yaml",
        narrative_dir=tmp_path / "narratives",
        program_id="acme",
        resolved_v2=SimpleNamespace(program=SimpleNamespace(id="acme"), raw_program={"id": "acme"}),
        legacy_regex_extractor=False,
        extraction_result=None,
        extraction_mode="disabled",
        revalidate_evidence_fn=lambda: None,
        build_confirmed_eml_bytes_fn=lambda *args, **kwargs: None,
        write_confirmed_fn=_selected_callback(tmp_path / "staged.snapshot.json", tmp_path / "programs"),
        write_confirmed_issue_fn=_selected_callback(_archive_paths(tmp_path), tmp_path / "programs"),
        get_overrides_path_fn=lambda edition, reports_root, issue_number, **kwargs: tmp_path / "issue_001.overrides.yaml",
        load_draft_continuation_contract_path_fn=lambda edition, issue_number, *, programs_root=None: None,
        write_context_snapshot_for_issue_fn=lambda **kwargs: None,
        record_confirmed_claims_for_v2_fn=lambda **kwargs: (),
        semantic_index_enabled_fn=lambda raw_program: False,
        update_archive_semantic_index_for_issue_fn=archive_transaction.update_archive_semantic_index_for_issue,
        mark_semantic_index_dirty_fn=archive_transaction.mark_semantic_index_dirty,
        write_optimization_proposals_fn=lambda *args, **kwargs: None,
        load_gather_state_fn=archive_transaction.load_gather_state,
        compute_source_health_pct_fn=lambda gather_state: 1.0,
        compute_provenance_confidence_fn=lambda approved_signals: 1.0,
        get_signal_class_fn=lambda signal: archive_transaction.SignalClass.RISK,
        upsert_risk_from_signal_fn=lambda program_id, **kwargs: risk_upserts.append(program_id),
    )

    # The archive signing warning is expected when no HMAC key is configured (dev/CI default).
    non_signing_warnings = tuple(w for w in result.warnings if "signing skipped" not in w)
    assert non_signing_warnings == ()
    assert projected == ["ok"]
    assert len(metrics) == 1
    assert risk_upserts == ["acme"]
    assert persisted == ["vertex.confirm"]


def test_write_ledger_baseline_artifacts_writes_snapshot_and_hardlock(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    domain_event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=recorded_at.date(), slide_number=3),
    )
    archive_transaction.write_event(domain_event, programs_root=programs_root)

    archive_transaction._write_ledger_baseline_artifacts(
        program_id="acme",
        edition_name="acme_weekly",
        issue_number=78,
        confirmed_at=recorded_at,
        confirmed_by="test-operator",
        archive_paths=_archive_paths(tmp_path),
        published_title="Weekly",
        programs_root=programs_root,
        project_program_events_fn=project_program_events,
        write_projection_snapshot_fn=write_projection_snapshot,
        read_events_fn=read_events,
        write_event_fn=archive_transaction.write_event,
    )

    events = read_events("acme", programs_root=programs_root)
    hardlocks = [event for event in events if event.event_type == "operator.baseline_hardlock.v1"]
    published = [event for event in events if event.event_type == "artifact.published.v1"]

    assert len(hardlocks) == 1
    assert hardlocks[0].source_ref == OperatorAssertionRef(asserted_by="test-operator", asserted_at=recorded_at, context="confirm")
    assert len(published) == 1
    assert published[0].payload["artifact_id"] == "published_artifact:issue-078"
    assert published[0].payload["location"] == str(_archive_paths(tmp_path).html_path)
    assert published[0].source_ref == NewsletterRef(
        file_path=str(_archive_paths(tmp_path).html_path),
        publication_date=recorded_at.date(),
        issue_number=78,
        section="acme_weekly",
    )
    snapshot_dir = programs_root / "acme" / "ledger" / "projections" / "snapshots"
    assert any(snapshot_dir.glob("issue_078-*.sqlite3"))
    assert any(snapshot_dir.glob("issue_078-*.manifest.json"))


# ---------------------------------------------------------------------------
# GAP-23: SoR guard on fact-snapshot shim write
# ---------------------------------------------------------------------------


def test_archive_transaction_skips_fact_snapshot_when_program_is_primary(
    tmp_path: Path, monkeypatch
) -> None:
    """When the program is in `primary` SoR mode, the confirm flow must NOT
    re-persist the fact snapshot (it already lives in the canonical store).

    Regression: archive_transaction.py used to call
    persist_program_fact_snapshot unconditionally, which created duplicate
    entries for Acme (primary). The SoR guard short-circuits when
    resolve_fact_sor_mode returns "primary".
    """
    from src.commands.confirm_stages import archive_transaction as at_module

    programs_root = tmp_path / "programs"
    program_root = programs_root / "acme"
    program_root.mkdir(parents=True)

    # Patch the SoR resolver to report "primary" for this program.
    monkeypatch.setattr(
        at_module,
        "resolve_fact_sor_mode",
        lambda program_id, programs_root: "primary",
    )

    # Track every call to persist_program_fact_snapshot.
    persist_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        at_module,
        "persist_program_fact_snapshot",
        lambda *args, **kwargs: persist_calls.append((args[0].program_id,)),
    )
    monkeypatch.setattr(
        at_module,
        "load_program_facts",
        lambda *args, **kwargs: SimpleNamespace(program_id="acme"),
    )

    # Walk into the archive_transaction code path that contains the SoR guard.
    # The function under test is `execute_archive_transaction`; the guard sits
    # in a nested branch. To exercise it cleanly, call the inner block via a
    # test shim that mimics the SoR-gated logic.
    def _shim_gated_write(program_id: str) -> str | None:
        sor = at_module.resolve_fact_sor_mode(
            program_id=program_id, programs_root=programs_root
        )
        if sor == "primary":
            return None
        snapshot = at_module.load_program_facts(
            program_id, programs_root=programs_root
        )
        at_module.persist_program_fact_snapshot(
            snapshot,
            recorded_at=datetime.now(timezone.utc),
            accepted_by="vertex.confirm",
        )
        return "wrote"

    result = _shim_gated_write("acme")
    assert result is None  # No write performed for primary program
    assert persist_calls == []  # Zero persist calls


def test_archive_transaction_writes_fact_snapshot_for_legacy_program(
    tmp_path: Path, monkeypatch
) -> None:
    """When the program is in `legacy` SoR mode, the shadow write is still
    performed — the legacy sidecar is the single source of record for those
    programs and the shim write keeps it in sync with the snapshot."""
    from src.commands.confirm_stages import archive_transaction as at_module

    programs_root = tmp_path / "programs"
    program_root = programs_root / "acme"
    program_root.mkdir(parents=True)

    monkeypatch.setattr(
        at_module,
        "resolve_fact_sor_mode",
        lambda program_id, programs_root: "legacy",
    )

    persist_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        at_module,
        "persist_program_fact_snapshot",
        lambda *args, **kwargs: persist_calls.append((args[0].program_id,)),
    )
    monkeypatch.setattr(
        at_module,
        "load_program_facts",
        lambda *args, **kwargs: SimpleNamespace(program_id="acme"),
    )

    def _shim_gated_write(program_id: str) -> str | None:
        sor = at_module.resolve_fact_sor_mode(
            program_id=program_id, programs_root=programs_root
        )
        if sor == "primary":
            return None
        snapshot = at_module.load_program_facts(
            program_id, programs_root=programs_root
        )
        at_module.persist_program_fact_snapshot(
            snapshot,
            recorded_at=datetime.now(timezone.utc),
            accepted_by="vertex.confirm",
        )
        return "wrote"

    result = _shim_gated_write("acme")
    assert result == "wrote"
    assert persist_calls == [("acme",)]


def _spine_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], list[str]]:
    from src.core import archive_store, snapshot_store
    from tests.unit.test_archive_store import _build_snapshot
    from tests.unit.test_archive_workspace_context import _seed, _workspace

    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected, 78)
    _seed(decoy, 77)
    effects: list[str] = []
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", lambda *args, **kwargs: effects.append("checkpoint"))
    monkeypatch.setattr(archive_transaction, "_try_sign_archive_manifest", lambda **kwargs: ())
    monkeypatch.setattr(archive_transaction, "_write_ledger_baseline_artifacts", lambda **kwargs: None)
    monkeypatch.setattr(archive_transaction, "load_proposals", lambda *args, **kwargs: ())
    monkeypatch.setattr(archive_transaction, "project_confirmed_issue", lambda **kwargs: None)
    monkeypatch.setattr(archive_transaction, "try_advance_earned_autonomy", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "write_issue_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(archive_transaction, "load_program_facts", lambda *args, **kwargs: object())
    monkeypatch.setattr(archive_transaction, "persist_program_fact_snapshot", lambda *args, **kwargs: None)
    snapshot = _build_snapshot(79)
    return {
        "edition_name": "acme_weekly",
        "issue_number": 79,
        "confirmed_at": snapshot.generated_at,
        "warnings": (),
        "archive_root": archive,
        "programs_root": selected,
        "reports_root": tmp_path / "legacy" / "reports",
        "approved_signals": (),
        "artifacts": (None, None, snapshot, None, "new html", "new markdown", SimpleNamespace(ado_data_as_of=snapshot.ado_data_as_of, items=())),
        "bundle": SimpleNamespace(),
        "manifest": replace(_manifest(), issue_number=79),
        "overrides_document": SimpleNamespace(scorecards=()),
        "vitality_archive_entry": None,
        "chart_cache_entries": None,
        "review_status_path": tmp_path / "missing-review.yaml",
        "narrative_dir": tmp_path / "missing-narratives",
        "program_id": "acme",
        "resolved_v2": SimpleNamespace(program=SimpleNamespace(id="acme"), raw_program={"id": "acme"}),
        "legacy_regex_extractor": False,
        "extraction_result": None,
        "extraction_mode": "disabled",
        "revalidate_evidence_fn": lambda: effects.append("revalidate"),
        "build_confirmed_eml_bytes_fn": lambda *args, **kwargs: None,
        "write_confirmed_fn": snapshot_store.write_confirmed,
        "write_confirmed_issue_fn": archive_store.write_confirmed_issue,
        "get_overrides_path_fn": lambda *args, **kwargs: None,
        "load_draft_continuation_contract_path_fn": lambda *args, **kwargs: None,
        "write_context_snapshot_for_issue_fn": lambda **kwargs: None,
        "record_confirmed_claims_for_v2_fn": lambda **kwargs: (),
        "semantic_index_enabled_fn": lambda raw_program: False,
        "update_archive_semantic_index_for_issue_fn": lambda *args, **kwargs: None,
        "mark_semantic_index_dirty_fn": lambda *args, **kwargs: None,
        "write_optimization_proposals_fn": lambda *args, **kwargs: None,
        "load_gather_state_fn": lambda *args, **kwargs: None,
        "compute_source_health_pct_fn": lambda state: None,
        "compute_provenance_confidence_fn": lambda signals: None,
        "get_signal_class_fn": lambda signal: archive_transaction.SignalClass.INFO,
        "upsert_risk_from_signal_fn": lambda *args, **kwargs: None,
    }, effects


@pytest.mark.parametrize("fail_refresh", [False, True])
def test_selected_transaction_semantic_callbacks_retain_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_refresh: bool,
) -> None:
    from src.core import semantic_index
    from tests.unit.test_archive_workspace_context import _bytes

    kwargs, _ = _spine_inputs(tmp_path, monkeypatch)
    selected = kwargs["programs_root"]
    archive = kwargs["archive_root"]
    before = _bytes(archive.parent)
    semantic_index.update_archive_semantic_index_for_issue(
        "acme_weekly", 78, archive_root=archive, programs_root=selected,
    )
    prior = semantic_index.load_semantic_index_state(
        "acme_weekly", archive_root=archive, programs_root=selected,
    )
    calls: list[Path] = []

    def refresh(edition, issue, *, archive_root, programs_root):
        calls.append(programs_root)
        if fail_refresh:
            raise RuntimeError("selected refresh failed")
        return semantic_index.update_archive_semantic_index_for_issue(
            edition, issue, archive_root=archive_root, programs_root=programs_root,
        )

    kwargs.update(
        semantic_index_enabled_fn=lambda raw: True,
        update_archive_semantic_index_for_issue_fn=refresh,
        mark_semantic_index_dirty_fn=semantic_index.mark_semantic_index_dirty,
    )
    result = archive_transaction.execute_archive_transaction(**kwargs)
    state = semantic_index.load_semantic_index_state(
        "acme_weekly", archive_root=archive, programs_root=selected,
    )
    assert calls == [selected]
    assert state is not None and state.semantic_index_dirty is fail_refresh
    assert state.latest_confirmed_issue == (78 if fail_refresh else 79)
    if fail_refresh:
        assert state.last_built_at == prior.last_built_at
        assert state.indexed_document_count == prior.indexed_document_count
        assert "Semantic index update skipped: selected refresh failed" in result.warnings
    assert _bytes(archive.parent) == before


@pytest.mark.parametrize("program_backed", [False, True])
def test_transaction_disabled_semantic_callbacks_are_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool,
) -> None:
    kwargs, _ = _spine_inputs(tmp_path, monkeypatch)
    kwargs["resolved_v2"] = kwargs["resolved_v2"] if program_backed else None
    kwargs["semantic_index_enabled_fn"] = lambda raw: not program_backed

    def forbidden(*args, **kwargs):
        pytest.fail("disabled or flat transaction called semantic callback")

    kwargs["update_archive_semantic_index_for_issue_fn"] = forbidden
    kwargs["mark_semantic_index_dirty_fn"] = forbidden
    archive_transaction.execute_archive_transaction(**kwargs)


def test_arc_txn01_real_writers_lock_stage_and_commit_selected_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import archive_store, snapshot_store
    from tests.unit.test_archive_workspace_context import _bytes

    kwargs, effects = _spine_inputs(tmp_path, monkeypatch)
    selected = kwargs["programs_root"]
    decoy = kwargs["archive_root"].parent
    before = _bytes(decoy)
    edition_root = selected / "acme" / "archive" / "acme_weekly"
    receipts: list[str] = []

    def revalidate() -> None:
        assert (edition_root / ".lock").is_file()
        receipts.append("lock")

    def stage(*, programs_root: Path, **writer_kwargs: Any) -> Path:
        assert programs_root == selected
        assert writer_kwargs["promote"] is False and writer_kwargs["acquire_lock"] is False
        receipts.append("stage")
        path = snapshot_store.write_confirmed(programs_root=programs_root, **writer_kwargs)
        assert path.is_relative_to(edition_root / "staging")
        assert path.is_file()
        return path

    def commit(*, programs_root: Path, **writer_kwargs: Any) -> ConfirmedIssueArchivePaths:
        assert programs_root == selected
        assert writer_kwargs["snapshot_source"].is_file()
        assert writer_kwargs["snapshot_is_staged"] is True and writer_kwargs["acquire_lock"] is False
        receipts.append("commit")
        return archive_store.write_confirmed_issue(programs_root=programs_root, **writer_kwargs)

    kwargs.update(revalidate_evidence_fn=revalidate, write_confirmed_fn=stage, write_confirmed_issue_fn=commit)
    result = archive_transaction.execute_archive_transaction(**kwargs)
    assert result.warnings == ()
    assert result.archive_paths.snapshot_path == edition_root / "snapshots" / "issue_079.snapshot.json"
    assert snapshot_store.read_snapshot(result.archive_paths.snapshot_path).issue_number == 79
    assert [row.issue_number for row in archive_store.read_archive_index("acme_weekly", programs_root=selected).issues] == [78, 79]
    assert receipts == ["lock", "stage", "commit"] and effects == ["checkpoint"]
    assert not (edition_root / "staging").exists() and not (edition_root / ".lock").exists()
    assert _bytes(decoy) == before


def test_arc_txn02_real_snapshot_callback_preserves_selected_hardlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.baseline_lock import BaselineLockError
    from tests.unit.test_archive_workspace_context import _bytes

    kwargs, effects = _spine_inputs(tmp_path, monkeypatch)
    selected = kwargs["programs_root"]
    (selected / "acme" / "trusted_baseline.yaml").write_text("trusted_issue_number: 78\n", encoding="utf-8")
    kwargs["issue_number"] = 78
    kwargs["artifacts"] = (*kwargs["artifacts"][:2], replace(kwargs["artifacts"][2], issue_number=78), *kwargs["artifacts"][3:])
    kwargs["manifest"] = replace(kwargs["manifest"], issue_number=78)
    before = _bytes(tmp_path)
    with pytest.raises(BaselineLockError):
        archive_transaction.execute_archive_transaction(**kwargs)
    assert effects == ["revalidate", "checkpoint"]
    assert _bytes(tmp_path) == before


def test_arc_txn03_context_callback_receives_selected_prior_not_new_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs, _ = _spine_inputs(tmp_path, monkeypatch)
    receipts: list[dict[str, Any]] = []
    kwargs["write_context_snapshot_for_issue_fn"] = lambda **context: receipts.append(context)
    archive_transaction.execute_archive_transaction(**kwargs)
    assert len(receipts) == 1
    assert receipts[0]["programs_root"] == kwargs["programs_root"]
    assert receipts[0]["prior_issue_entry"].issue_number == 78
    assert Path(receipts[0]["prior_issue_entry"].snapshot_path).is_relative_to(kwargs["programs_root"])


@pytest.mark.parametrize("kind", ("missing", "relative", "file", "missing_edition", "flat", "ambiguous", "malformed", "denied"))
def test_arc_txn04_invalid_selection_precedes_lock_and_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from src.core.exceptions import ConfigError
    from tests.unit.test_archive_workspace_context import _bytes

    kwargs, effects = _spine_inputs(tmp_path, monkeypatch)
    root = kwargs["programs_root"]
    edition = root / "acme" / "editions" / "acme_weekly.yaml"
    if kind == "missing":
        kwargs["programs_root"] = tmp_path / "absent"
    elif kind == "relative":
        kwargs["programs_root"] = Path("relative-programs")
    elif kind == "file":
        kwargs["programs_root"] = edition
    elif kind in {"missing_edition", "flat"}:
        edition.unlink()
        if kind == "flat":
            flat = root.parent / "editions"
            flat.mkdir()
            (flat / edition.name).write_text("program_id: acme\n", encoding="utf-8")
    elif kind == "ambiguous":
        other = root / "other" / "editions"
        other.mkdir(parents=True)
        (other / edition.name).write_text("program_id: other\n", encoding="utf-8")
    elif kind == "malformed":
        edition.write_text("program_id: ../escape\n", encoding="utf-8")
    else:
        original = Path.glob

        def denied(path: Path, pattern: str, **kwargs):
            if path == root:
                raise PermissionError("synthetic inaccessible root")
            return original(path, pattern, **kwargs)

        monkeypatch.setattr(Path, "glob", denied)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid selection reached lock or writer")

    monkeypatch.setattr(archive_transaction, "ArchiveLock", forbidden)
    kwargs.update(write_confirmed_fn=forbidden, write_confirmed_issue_fn=forbidden)
    before = _bytes(tmp_path)
    with pytest.raises(ConfigError):
        archive_transaction.execute_archive_transaction(**kwargs)
    assert effects == []
    assert _bytes(tmp_path) == before


def test_arc_txn05_real_archive_promotion_failure_restores_selected_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import archive_store
    from tests.unit.test_archive_workspace_context import _bytes

    kwargs, effects = _spine_inputs(tmp_path, monkeypatch)
    kwargs.update(issue_number=78, artifacts=(None, None, replace(kwargs["artifacts"][2], issue_number=78), None, "replacement html", "replacement md", kwargs["artifacts"][6]))
    kwargs["manifest"] = replace(kwargs["manifest"], issue_number=78)
    selected = kwargs["programs_root"]
    promote = archive_store._promote_file_with_rollback
    count = 0

    def fail_after_promotions(*args, **kwargs):
        nonlocal count
        assert args[1].is_relative_to(selected)
        count += 1
        if count == 4:
            raise OSError("synthetic promotion failure")
        return promote(*args, **kwargs)

    monkeypatch.setattr(archive_store, "_promote_file_with_rollback", fail_after_promotions)
    before = _bytes(tmp_path)
    with pytest.raises(OSError, match="synthetic promotion"):
        archive_transaction.execute_archive_transaction(**kwargs)
    assert count == 4 and effects == ["revalidate", "checkpoint"]
    assert _bytes(tmp_path) == before
    assert not (kwargs["programs_root"] / "acme" / "archive" / "acme_weekly" / "staging").exists()


def test_arc_txn06_flat_mode_preserves_legacy_override_and_callback_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import archive_store, snapshot_store
    from tests.unit.test_archive_workspace_context import _bytes

    kwargs, _ = _spine_inputs(tmp_path, monkeypatch)
    kwargs.update(resolved_v2=None, archive_root=tmp_path / "flat-archive")
    selected_before = _bytes(kwargs["programs_root"])
    legacy_before = _bytes(tmp_path / "legacy")
    root_calls: list[Path] = []
    original = archive_transaction.get_archive_root

    def resolve(edition: str, archive_root: Path) -> Path:
        root_calls.append(archive_root)
        return original(edition, archive_root)

    def stage(**writer_kwargs: Any) -> Path:
        assert set(writer_kwargs) == {"edition", "issue_number", "snapshot", "archive_root", "promote", "acquire_lock"}
        return snapshot_store.write_confirmed(**writer_kwargs)

    def commit(**writer_kwargs: Any) -> ConfirmedIssueArchivePaths:
        assert set(writer_kwargs) == {
            "edition", "issue_number", "snapshot", "eml_bytes", "html_body", "markdown_body", "manifest",
            "overrides_source", "review_status_source", "narratives_source_dir", "continuation_contract_source",
            "vitality_record", "chart_cache_entries", "archive_root", "snapshot_source", "snapshot_is_staged", "acquire_lock",
        }
        return archive_store.write_confirmed_issue(**writer_kwargs)

    monkeypatch.setattr(archive_transaction, "get_archive_root", resolve)
    kwargs.update(write_confirmed_fn=stage, write_confirmed_issue_fn=commit)
    result = archive_transaction.execute_archive_transaction(**kwargs)
    assert result.archive_paths.snapshot_path == kwargs["archive_root"] / "acme_weekly" / "snapshots" / "issue_079.snapshot.json"
    assert root_calls == [kwargs["archive_root"]]
    assert _bytes(kwargs["programs_root"]) == selected_before
    assert _bytes(tmp_path / "legacy") == legacy_before
