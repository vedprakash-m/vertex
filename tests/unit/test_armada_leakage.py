"""specs/backlog.md BL-F1 (armada.md D-9): tag-hygiene leakage_rate metric
and candidate lifecycle tests. No live network -- ADO fetches go through an
injected client_factory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core.armada_leakage import (
    LEAKAGE_QUERY_ID,
    RawAdoCandidate,
    compute_leakage_rate,
    dispose_leakage_candidate,
    fetch_leakage_candidates_from_ado,
    find_leakage_query,
    fold_leakage_candidates,
    leakage_query_version,
    leakage_sla_violations,
    read_leakage_events,
    sync_leakage_candidates,
)
from src.core.gather_run_manifest import (
    GatherRunManifest,
    GatherRunStatus,
    QueryResultEntry,
    RequiredScopeStatus,
    commit_staging_run,
    create_staging_manifest,
)
from src.core.knowledge_store import KnowledgeStore
from src.core.models_v2 import ADOConfig, KustoQuery, Program

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _candidate(work_item_id: int, **overrides: Any) -> RawAdoCandidate:
    defaults: dict[str, Any] = dict(
        work_item_id=work_item_id, work_item_type="Bug", title=f"Item {work_item_id}", state="Active", assigned_to="alice",
    )
    defaults.update(overrides)
    return RawAdoCandidate(**defaults)


def test_leakage_query_version_is_stable_and_sensitive_to_text_change() -> None:
    v1 = leakage_query_version("Tags/any(t: t/TagName eq 'Armada')", "WorkItemId,Title")
    v1_again = leakage_query_version("Tags/any(t: t/TagName eq 'Armada')", "WorkItemId,Title")
    v2 = leakage_query_version("Tags/any(t: t/TagName eq 'ArmadaChanged')", "WorkItemId,Title")
    assert v1 == v1_again
    assert v1 != v2


def test_sync_discovers_new_candidates(tmp_path: Path) -> None:
    result = sync_leakage_candidates(
        "armada", org="msazure", project="One",
        raw_candidates=(_candidate(1), _candidate(2)),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    assert result.discovered == (1, 2)
    assert result.reseen == ()
    states = fold_leakage_candidates(read_leakage_events("armada", programs_root=tmp_path))
    assert set(states) == {1, 2}
    assert all(s.disposition == "unresolved" for s in states.values())


def test_sync_reseens_unresolved_candidates_without_changing_disposition(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    dispose_leakage_candidate(
        "armada", 1, org="msazure", project="One", disposition="owner_assigned", programs_root=tmp_path, now=NOW,
    )
    result = sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-2", query_version="v1", programs_root=tmp_path, now=NOW + timedelta(days=1),
    )
    assert result.reseen == (1,)
    states = fold_leakage_candidates(read_leakage_events("armada", programs_root=tmp_path))
    assert states[1].disposition == "owner_assigned"  # unchanged by reseen


def test_sync_auto_resolves_candidates_that_drop_out_of_the_query(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1), _candidate(2)),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    result = sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),  # 2 no longer matches
        discovery_run_id="run-2", query_version="v1", programs_root=tmp_path, now=NOW + timedelta(days=1),
    )
    assert result.auto_resolved == (2,)
    states = fold_leakage_candidates(read_leakage_events("armada", programs_root=tmp_path))
    assert states[2].disposition == "resolved"
    assert states[1].disposition == "unresolved"


def test_sync_reopens_a_previously_resolved_candidate_that_reappears(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    dispose_leakage_candidate(
        "armada", 1, org="msazure", project="One", disposition="correctly_untagged", programs_root=tmp_path, now=NOW,
    )
    result = sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-2", query_version="v1", programs_root=tmp_path, now=NOW + timedelta(days=2),
    )
    assert result.reopened == (1,)
    states = fold_leakage_candidates(read_leakage_events("armada", programs_root=tmp_path))
    assert states[1].disposition == "unresolved"


def test_dispose_unknown_candidate_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a known leakage candidate"):
        dispose_leakage_candidate(
            "armada", 999, org="msazure", project="One", disposition="resolved", programs_root=tmp_path,
        )


def test_compute_leakage_rate_unavailable_without_committed_gather_run(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    result = compute_leakage_rate("armada", programs_root=tmp_path)
    assert result.confidence.value == "unavailable"
    assert result.value is None
    assert result.likely_missing_tag_items == 1


def _commit_manifest(programs_root: Path, *, raw_count: int) -> None:
    manifest = GatherRunManifest(
        run_id="gather-01LEAK",
        status=GatherRunStatus.RUNNING,
        program_id="armada",
        actor_identity_type="interactive",
        lease_owner="host-a",
        lease_fencing_token=1,
        started_at=NOW,
        scope_as_of=NOW,
        required_scope_status=RequiredScopeStatus.FULL,
        query_results=(
            QueryResultEntry(
                query_id="q-armada", scope_id="scope-a", wiql_hash="h1", captured_at=NOW,
                raw_count=raw_count, membership_ids=(), membership_hash="mh1",
                cap_reached=False, completeness_state="FULL",
            ),
        ),
    )
    create_staging_manifest(manifest, programs_root=programs_root)
    commit_staging_run(manifest, finished_at=NOW, programs_root=programs_root)


def test_compute_leakage_rate_measured_with_committed_gather_run(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1), _candidate(2)),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    _commit_manifest(tmp_path, raw_count=18)

    result = compute_leakage_rate("armada", programs_root=tmp_path)

    assert result.confidence.value == "measured"
    assert result.likely_missing_tag_items == 2
    assert result.authoritative_scope_items == 18
    assert result.value == pytest.approx(2 / 20)


def test_compute_leakage_rate_excludes_settled_dispositions(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1), _candidate(2)),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    dispose_leakage_candidate(
        "armada", 1, org="msazure", project="One", disposition="correctly_untagged", programs_root=tmp_path, now=NOW,
    )
    _commit_manifest(tmp_path, raw_count=8)

    result = compute_leakage_rate("armada", programs_root=tmp_path)

    assert result.likely_missing_tag_items == 1  # only candidate 2 -- 1 is settled
    assert result.value == pytest.approx(1 / 9, abs=1e-4)


def test_sla_violations_flag_owner_disposition_overdue_after_7_days(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    violations = leakage_sla_violations("armada", programs_root=tmp_path, now=NOW + timedelta(days=8))
    kinds = {v.kind for v in violations}
    assert "owner_disposition_overdue" in kinds
    assert "no_unresolved_candidate" not in kinds  # not yet 14 days


def test_sla_violations_flag_no_unresolved_candidate_after_14_days(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    violations = leakage_sla_violations("armada", programs_root=tmp_path, now=NOW + timedelta(days=15))
    kinds = {v.kind for v in violations}
    assert "owner_disposition_overdue" in kinds
    assert "no_unresolved_candidate" in kinds


def test_sla_violations_none_within_window(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    violations = leakage_sla_violations("armada", programs_root=tmp_path, now=NOW + timedelta(days=2))
    assert violations == ()


def test_sla_violations_skip_settled_candidates(tmp_path: Path) -> None:
    sync_leakage_candidates(
        "armada", org="msazure", project="One", raw_candidates=(_candidate(1),),
        discovery_run_id="run-1", query_version="v1", programs_root=tmp_path, now=NOW,
    )
    dispose_leakage_candidate(
        "armada", 1, org="msazure", project="One", disposition="resolved", programs_root=tmp_path, now=NOW,
    )
    violations = leakage_sla_violations("armada", programs_root=tmp_path, now=NOW + timedelta(days=30))
    assert violations == ()


def _golden_query(**overrides: Any) -> KustoQuery:
    defaults: dict[str, Any] = dict(
        id=LEAKAGE_QUERY_ID, cluster="https://analytics.dev.azure.com/msazure/One", database="WorkItems",
        kql="", section="Armada xHealth Backlog", render_as="table", confidence="high", engine="ado_odata",
        ado_filter="Tags/any(t: t/TagName eq 'Armada')", ado_select="WorkItemId,WorkItemType,Title,State,AssignedTo",
    )
    defaults.update(overrides)
    return KustoQuery(**defaults)


def test_find_leakage_query_returns_the_ado_odata_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.core.armada_leakage as leakage_mod

    monkeypatch.setattr(
        leakage_mod, "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(),
            golden_queries=(_golden_query(),), engms_pages=(),
        ),
    )
    query = find_leakage_query("armada", programs_root=tmp_path)
    assert query is not None
    assert query.engine == "ado_odata"


def test_find_leakage_query_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.core.armada_leakage as leakage_mod

    monkeypatch.setattr(
        leakage_mod, "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(), golden_queries=(), engms_pages=(),
        ),
    )
    assert find_leakage_query("armada", programs_root=tmp_path) is None


class _FakeAdoClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.last_filter: str | None = None
        self.last_select: tuple[str, ...] | None = None

    def query_work_items(self, filter_expression: str, select_fields: tuple[str, ...], top: int = 1000) -> list[dict[str, Any]]:
        self.last_filter = filter_expression
        self.last_select = select_fields
        return self._rows


def _write_program_with_ado(programs_root: Path, program_id: str = "armada") -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        f"""
schema_version: '3.0'
id: {program_id}
name: Armada
ado:
  organization: msazure
  project: One
  area_paths: [One\\Xstore\\Armada]
  work_item_types: [Feature]
  excluded_states: [Removed]
  date_window_days: 14
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_fetch_leakage_candidates_from_ado_uses_injected_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.core.armada_leakage as leakage_mod

    programs_root = tmp_path / "programs"
    _write_program_with_ado(programs_root)
    monkeypatch.setattr(
        leakage_mod, "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(),
            golden_queries=(_golden_query(),), engms_pages=(),
        ),
    )
    fake_client = _FakeAdoClient([
        {"WorkItemId": 111, "WorkItemType": "Bug", "Title": "T1", "State": "Active", "AssignedTo": "alice@example.com"},
        {"WorkItemId": 222, "WorkItemType": "Feature", "Title": "T2", "State": "New", "AssignedTo": {"displayName": "Bob"}},
        {"WorkItemId": 333, "WorkItemType": "Bug", "Title": "T3", "State": "Active", "AssignedTo": None},
    ])

    candidates = fetch_leakage_candidates_from_ado(
        "armada", programs_root=programs_root, client_factory=lambda **_kwargs: fake_client,
    )

    assert len(candidates) == 3
    assert candidates[0].work_item_id == 111
    assert candidates[0].assigned_to == "alice@example.com"
    assert candidates[1].assigned_to == "Bob"
    assert candidates[2].assigned_to is None
    assert fake_client.last_filter == "Tags/any(t: t/TagName eq 'Armada')"
    assert fake_client.last_select == ("WorkItemId", "WorkItemType", "Title", "State", "AssignedTo")


def test_fetch_leakage_candidates_raises_without_ado_config(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "noado").mkdir(parents=True)
    (programs_root / "noado" / "program.yaml").write_text(
        "schema_version: '3.0'\nid: noado\nname: No Ado\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no ado config"):
        fetch_leakage_candidates_from_ado("noado", programs_root=programs_root)


def test_fetch_leakage_candidates_raises_without_configured_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.core.armada_leakage as leakage_mod

    programs_root = tmp_path / "programs"
    _write_program_with_ado(programs_root)
    monkeypatch.setattr(
        leakage_mod, "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(), golden_queries=(), engms_pages=(),
        ),
    )
    with pytest.raises(ValueError, match="No 'armada-xhealth-catchall-ado'"):
        fetch_leakage_candidates_from_ado("armada", programs_root=programs_root)
