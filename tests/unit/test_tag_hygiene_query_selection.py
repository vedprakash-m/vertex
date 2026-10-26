"""Synthetic query-selection and historical-ledger regressions."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core import tag_hygiene as hygiene
from src.core.exceptions import ConfigError
from src.core.knowledge_store import KnowledgeStore
from src.core.models_v2 import ADOConfig, KustoQuery, Program


@pytest.fixture(autouse=True)
def _clear_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERTEX_LEAKAGE_QUERY_ID", raising=False)


def _query(query_id: str, **changes: object) -> KustoQuery:
    values = dict(
        id=query_id, cluster="https://analytics.dev.azure.com/example/Synthetic",
        database="WorkItems", kql="", section="Synthetic hygiene",
        render_as="table", confidence="high", engine="ado_odata",
        ado_filter="State eq 'Active'", ado_select="WorkItemId,Title",
    )
    values.update(changes)
    return KustoQuery(**values)


def _knowledge(monkeypatch: pytest.MonkeyPatch, *queries: KustoQuery) -> None:
    monkeypatch.setattr(
        hygiene, "load_program_knowledge",
        lambda *_args, **_kwargs: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(),
            golden_queries=queries, engms_pages=(),
        ),
    )


def test_default_is_generic_and_does_not_discover_other_queries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert hygiene.LEAKAGE_QUERY_ID == "tag-hygiene-candidates"
    _knowledge(monkeypatch, _query("synthetic-operator-query"))
    assert hygiene.find_leakage_query("demo", programs_root=tmp_path) is None


def test_operator_selection_is_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selected = _query("synthetic-operator-query")
    _knowledge(monkeypatch, _query("tag-hygiene-candidates"), selected)
    monkeypatch.setenv("VERTEX_LEAKAGE_QUERY_ID", selected.id)
    assert hygiene.find_leakage_query("demo", programs_root=tmp_path) is selected


@pytest.mark.parametrize("query_id", ["", "   "])
def test_blank_override_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, query_id: str) -> None:
    _knowledge(monkeypatch, _query("tag-hygiene-candidates"))
    monkeypatch.setenv("VERTEX_LEAKAGE_QUERY_ID", query_id)
    with pytest.raises(ValueError, match="VERTEX_LEAKAGE_QUERY_ID"):
        hygiene.find_leakage_query("demo", programs_root=tmp_path)


def test_missing_override_fails_without_generic_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _knowledge(monkeypatch, _query("tag-hygiene-candidates"))
    monkeypatch.setenv("VERTEX_LEAKAGE_QUERY_ID", "synthetic-missing")
    with pytest.raises(ValueError, match="synthetic-missing"):
        hygiene.find_leakage_query("demo", programs_root=tmp_path)


@pytest.mark.parametrize(
    "queries",
    [
        (_query("tag-hygiene-candidates"), _query("tag-hygiene-candidates")),
        (_query("tag-hygiene-candidates", engine="kusto"),),
        (_query("tag-hygiene-candidates", ado_filter=""),),
        (_query("tag-hygiene-candidates", ado_select=""),),
    ],
)
def test_ambiguous_or_invalid_selection_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, queries: tuple[KustoQuery, ...],
) -> None:
    _knowledge(monkeypatch, *queries)
    with pytest.raises(ValueError, match="tag-hygiene-candidates"):
        hygiene.find_leakage_query("demo", programs_root=tmp_path)


def test_ledger_records_selected_identity_and_keeps_it_for_disposition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setenv("VERTEX_LEAKAGE_QUERY_ID", "synthetic-operator-query")
    candidate = hygiene.RawAdoCandidate(31, "Bug", "Synthetic candidate", "Active", None)
    hygiene.sync_leakage_candidates(
        "demo", org="example", project="Synthetic", raw_candidates=(candidate,),
        discovery_run_id="synthetic-run", query_version="version-1", programs_root=tmp_path, now=now,
    )
    monkeypatch.setenv("VERTEX_LEAKAGE_QUERY_ID", "synthetic-other-query")
    hygiene.dispose_leakage_candidate(
        "demo", 31, org="example", project="Synthetic", disposition="owner_assigned",
        programs_root=tmp_path, now=now,
    )
    events = hygiene.read_leakage_events("demo", programs_root=tmp_path)
    assert [event.query_id for event in events] == ["synthetic-operator-query"] * 2
    assert events[-1].query_version == "version-1"


def test_auto_resolve_records_explicit_discovery_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    candidate = hygiene.RawAdoCandidate(31, "Bug", "Synthetic candidate", "Active", None)
    for candidates in [(candidate,), ()]:
        hygiene.sync_leakage_candidates(
            "demo", org="example", project="Synthetic", raw_candidates=candidates,
            discovery_run_id="synthetic-run", query_id="synthetic-explicit-query",
            query_version="version-1", programs_root=tmp_path,
        )
    events = hygiene.read_leakage_events("demo", programs_root=tmp_path)
    assert [event.query_id for event in events] == ["synthetic-explicit-query"] * 2
    assert events[-1].event == "auto_resolved"


def test_disposition_retains_last_discovery_when_timestamps_tie(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = hygiene.RawAdoCandidate(31, "Bug", "Synthetic candidate", "Active", None)
    for query_id in ["synthetic-first-query", "synthetic-latest-query"]:
        hygiene.sync_leakage_candidates(
            "demo", org="example", project="Synthetic", raw_candidates=(candidate,),
            discovery_run_id="synthetic-run", query_id=query_id,
            query_version="version-1", programs_root=tmp_path, now=now,
        )
    hygiene.dispose_leakage_candidate(
        "demo", 31, org="example", project="Synthetic", disposition="resolved",
        programs_root=tmp_path, now=now,
    )
    assert hygiene.read_leakage_events("demo", programs_root=tmp_path)[-1].query_id == "synthetic-latest-query"


def test_historical_ledger_identity_survives_without_selection(tmp_path: Path) -> None:
    ledger = hygiene.leakage_ledger_path("demo", programs_root=tmp_path)
    ledger.parent.mkdir(parents=True)
    historical = (
        '{"recorded_at":"2025-01-01T00:00:00+00:00","program_id":"demo","work_item_id":31,'
        '"org":"example","project":"Synthetic","discovery_run_id":"historical-run",'
        '"query_id":"synthetic-retired-query","query_version":"historical-version",'
        '"event":"discovered","disposition":"unresolved","work_item_type":"Bug",'
        '"title":"Synthetic historical candidate","state":"Active","assigned_to":null}\n'
    )
    ledger.write_text(historical, encoding="utf-8")
    events = hygiene.read_leakage_events("demo", programs_root=tmp_path)
    assert events[0].query_id == "synthetic-retired-query"
    assert hygiene.fold_leakage_candidates(events)[31].disposition == "unresolved"
    assert ledger.read_text(encoding="utf-8") == historical


def test_fetch_doctor_and_ledger_share_operator_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from src.commands.doctor_checks.storage_checks import _tag_hygiene_check

    selected = _query("synthetic-operator-query", ado_filter="State eq 'New'")
    _knowledge(monkeypatch, _query("tag-hygiene-candidates"), selected)
    monkeypatch.setenv("VERTEX_LEAKAGE_QUERY_ID", selected.id)
    monkeypatch.setattr(
        hygiene, "load_program",
        lambda *_args, **_kwargs: Program(
            schema_version="3.0", id="demo", name="Synthetic fixture",
            ado=ADOConfig(
                organization="example", project="Synthetic", area_paths=("Synthetic\\Testing",),
                work_item_types=("Bug",), excluded_states=(), date_window_days=14,
            ),
        ),
    )
    calls = []

    class Client:
        def query_work_items(self, **kwargs: object) -> list[dict[str, object]]:
            calls.append(kwargs)
            return [{"WorkItemId": 31, "Title": "Synthetic candidate"}]

    candidates = hygiene.fetch_leakage_candidates_from_ado(
        "demo", programs_root=tmp_path, client_factory=lambda **_kwargs: Client(),
    )
    assert calls == [{"filter_expression": selected.ado_filter, "select_fields": ("WorkItemId", "Title")}]
    before_sync = _tag_hygiene_check("demo", programs_root=tmp_path)
    assert before_sync.label == "Tag Hygiene"
    assert before_sync.metadata == {"applicable": True, "synced": False}
    hygiene.sync_leakage_candidates(
        "demo", org="example", project="Synthetic", raw_candidates=candidates,
        discovery_run_id="synthetic-run", query_id=selected.id,
        query_version=hygiene.leakage_query_version(selected.ado_filter, selected.ado_select),
        programs_root=tmp_path,
    )
    assert hygiene.read_leakage_events("demo", programs_root=tmp_path)[0].query_id == selected.id
    assert _tag_hygiene_check("demo", programs_root=tmp_path).metadata["synced"] is True


@pytest.mark.parametrize("error", [ConfigError("Synthetic config error"), OSError("Synthetic root error")])
def test_query_root_errors_propagate_and_doctor_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception,
) -> None:
    from src.commands.doctor_checks.storage_checks import _tag_hygiene_check

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(hygiene, "load_program_knowledge", fail)
    with pytest.raises(type(error), match="Synthetic"):
        hygiene.find_leakage_query("demo", programs_root=tmp_path)
    result = _tag_hygiene_check("demo", programs_root=tmp_path)
    assert result.label == "Tag Hygiene"
    assert result.status == "warn"
    assert result.metadata == {"applicable": None, "configuration_error": True}


def test_missing_operator_query_is_not_doctor_not_applicable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from src.commands.doctor_checks.storage_checks import _tag_hygiene_check

    _knowledge(monkeypatch, _query("tag-hygiene-candidates"))
    monkeypatch.setenv("VERTEX_LEAKAGE_QUERY_ID", "synthetic-missing")
    result = _tag_hygiene_check("demo", programs_root=tmp_path)
    assert result.status == "warn"
    assert result.metadata == {"applicable": None, "configuration_error": True}
