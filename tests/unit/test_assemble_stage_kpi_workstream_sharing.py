"""WO-5 (specs/backlog.md BL-F2 step 0): pin today's *current*, not desired,
behaviour of the workstream-section KPI filter in
``src/commands/report_pipeline/assemble_stage.py`` (the backlog names this
file as ``src/core/report_pipeline/assemble_stage.py`` -- that path does not
exist; the module actually lives under ``src/commands/``).

``_kpi_tiles_for_section`` filters ``approved_signals`` into a section by
exact ``signal.workstream_id`` equality (line ~2734). A single ``KustoQuery``
can declare it is shared across *multiple* workstreams via its
``workstream_ids`` tuple, but a ``Signal`` carries exactly one
``workstream_id``. This test pins what happens today when a query is
configured as shared but the signal answering it is only ever tagged with
one of those workstream ids: the matching section renders a real data tile;
every other section that also lists the query in its ``workstream_ids``
renders an empty ``_kpi_tile_from_query`` placeholder instead of the shared
signal's value. This is deterministic (keyed off the signal's own fixed
``workstream_id``, not run-to-run ordering) but is very likely not the
intended "shared KPI" semantics -- BL-F2 tracks the real fix.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.models import Confidence, ReviewState
from src.core.models_v2 import KustoQuery, Signal, Workstream
from src.core.view_models import Citation, WorkstreamData
from src.commands.report_pipeline.assemble_stage import _kpi_tiles_for_section


def _workstream_data(section_id: str) -> WorkstreamData:
    return WorkstreamData(
        section_id=section_id,
        title=section_id,
        blurb="",
        dependency_cascades=(),
        items=(),
        citations=(),
        review_state=ReviewState.PENDING,
    )


def _signal(*, workstream_id: str, query_id: str, value: str, timestamp: datetime) -> Signal:
    return Signal(
        id=f"sig-{query_id}-{workstream_id}",
        timestamp=timestamp,
        source="kusto",
        program_id="demo",
        workstream_id=workstream_id,
        entity_refs=(),
        text="",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata={"query_id": query_id, "result_value": value},
    )


def test_query_shared_across_workstream_ids_only_gets_live_data_in_signals_own_workstream() -> None:
    shared_query = KustoQuery(
        id="q-shared",
        cluster="cluster",
        database="db",
        kql="X | take 1",
        section="ws-a",
        render_as="metric_highlight",
        confidence="high",
        workstream_ids=("ws-a", "ws-b"),
    )
    as_of = datetime(2026, 7, 21, tzinfo=timezone.utc)
    signal_tagged_ws_a = _signal(workstream_id="ws-a", query_id="q-shared", value="42", timestamp=as_of)

    workstreams = (Workstream(id="ws-a", name="Workstream A"), Workstream(id="ws-b", name="Workstream B"))

    tiles_for_a = _kpi_tiles_for_section(
        _workstream_data("ws-a"),
        approved_signals=(signal_tagged_ws_a,),
        workstreams=workstreams,
        configured_queries=(shared_query,),
    )
    tiles_for_b = _kpi_tiles_for_section(
        _workstream_data("ws-b"),
        approved_signals=(signal_tagged_ws_a,),
        workstreams=workstreams,
        configured_queries=(shared_query,),
    )

    assert len(tiles_for_a) == 1
    assert tiles_for_a[0].value == "42"
    assert tiles_for_a[0].source_signal_id == signal_tagged_ws_a.id

    # Pinned current behaviour: section B also configures the shared query,
    # but never sees the live value -- it gets an empty placeholder tile
    # because the filter only matches signals whose workstream_id == "ws-b".
    assert len(tiles_for_b) == 1
    assert tiles_for_b[0].value == ""
    assert tiles_for_b[0].source_signal_id is None


def test_two_signals_for_same_query_in_one_section_break_ties_by_first_inserted_on_equal_timestamp() -> None:
    """When two signals answer the same query_id within one section and
    share an identical timestamp, ``latest_by_query_id``'s strict ``>``
    comparison means the FIRST one encountered in ``approved_signals``'
    iteration order wins (dict insertion order), not the second."""
    as_of = datetime(2026, 7, 21, tzinfo=timezone.utc)
    first = _signal(workstream_id="ws-a", query_id="q-1", value="first", timestamp=as_of)
    second = _signal(workstream_id="ws-a", query_id="q-1", value="second", timestamp=as_of)

    query = KustoQuery(
        id="q-1",
        cluster="cluster",
        database="db",
        kql="X | take 1",
        section="ws-a",
        render_as="metric_highlight",
        confidence="high",
        workstream_ids=("ws-a",),
    )
    workstreams = (Workstream(id="ws-a", name="Workstream A"),)

    tiles = _kpi_tiles_for_section(
        _workstream_data("ws-a"),
        approved_signals=(first, second),
        workstreams=workstreams,
        configured_queries=(query,),
    )

    assert len(tiles) == 1
    assert tiles[0].value == "first"
