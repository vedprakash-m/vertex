"""BL-F2 (D-19 shared-workstream attribution) real fix, 2026-07-25.

``_kpi_tiles_for_section`` (``src/commands/report_pipeline/assemble_stage.py``,
the backlog's own path, ``src/core/report_pipeline/assemble_stage.py``, does
not exist -- the module actually lives under ``src/commands/``) used to
filter ``approved_signals`` into a section by exact ``signal.workstream_id``
equality. WO-5 (2026-07-21) pinned the resulting interim behaviour: a
``KustoQuery`` shared across multiple workstreams via its ``workstream_ids``
tuple only ever rendered live data in the ONE section matching the
answering signal's single ``workstream_id``; every other section fell back
to an empty placeholder tile.

That interim behaviour is now superseded. ``Signal`` gained a plural
``workstream_ids`` field (BL-F2 steps 1-4), and both real Kusto signal
builders (``kusto_query_helpers.build_kusto_signal``,
``gather.py``'s ``_build_kusto_kpi_signal``) now populate it from the full
configured ``query.workstream_ids`` set for a shared query. This file's
filter was updated to membership-test against the plural field instead of
equality-testing the scalar one, and every KPI tile now carries a
``shared: bool`` flag (true whenever the underlying query/signal is
associated with more than one workstream) so report templates can render a
visible "shared" marker per the row's action (3).
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.models import Confidence, ReviewState
from src.core.models_v2 import KustoQuery, Signal, Workstream
from src.core.view_models import WorkstreamData
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


def _signal(
    *,
    workstream_id: str,
    query_id: str,
    value: str,
    timestamp: datetime,
    workstream_ids: tuple[str, ...] = (),
) -> Signal:
    return Signal(
        id=f"sig-{query_id}-{workstream_id}",
        timestamp=timestamp,
        source="kusto",
        program_id="demo",
        workstream_id=workstream_id,
        workstream_ids=workstream_ids,
        entity_refs=(),
        text="",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata={"query_id": query_id, "result_value": value},
    )


def test_shared_query_with_fanned_out_signal_renders_live_value_in_every_associated_section() -> None:
    """The real D-19 fix: a signal produced by the fixed builders carries
    the query's full ``workstream_ids`` set, so every section that
    configures the shared query now sees the live value -- not just the
    one matching the signal's primary ``workstream_id`` -- and both tiles
    are marked ``shared``."""
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
    signal_shared = _signal(
        workstream_id="ws-a",
        query_id="q-shared",
        value="42",
        timestamp=as_of,
        workstream_ids=("ws-a", "ws-b"),
    )

    workstreams = (Workstream(id="ws-a", name="Workstream A"), Workstream(id="ws-b", name="Workstream B"))

    tiles_for_a = _kpi_tiles_for_section(
        _workstream_data("ws-a"),
        approved_signals=(signal_shared,),
        workstreams=workstreams,
        configured_queries=(shared_query,),
    )
    tiles_for_b = _kpi_tiles_for_section(
        _workstream_data("ws-b"),
        approved_signals=(signal_shared,),
        workstreams=workstreams,
        configured_queries=(shared_query,),
    )

    assert len(tiles_for_a) == 1
    assert tiles_for_a[0].value == "42"
    assert tiles_for_a[0].source_signal_id == signal_shared.id
    assert tiles_for_a[0].shared is True

    assert len(tiles_for_b) == 1
    assert tiles_for_b[0].value == "42"
    assert tiles_for_b[0].source_signal_id == signal_shared.id
    assert tiles_for_b[0].shared is True


def test_shared_query_with_no_signal_yet_renders_placeholder_marked_shared() -> None:
    """Before any signal has answered a shared query, both sections still
    correctly show the empty placeholder tile -- but it is marked
    ``shared`` too, since sharedness is a property of the *configuration*,
    not of whether data has arrived yet."""
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
    workstreams = (Workstream(id="ws-a", name="Workstream A"), Workstream(id="ws-b", name="Workstream B"))

    tiles_for_b = _kpi_tiles_for_section(
        _workstream_data("ws-b"),
        approved_signals=(),
        workstreams=workstreams,
        configured_queries=(shared_query,),
    )

    assert len(tiles_for_b) == 1
    assert tiles_for_b[0].value == ""
    assert tiles_for_b[0].shared is True


def test_single_workstream_query_is_never_marked_shared() -> None:
    """Backward-compat proof: a query/signal associated with exactly one
    workstream -- the overwhelming common case today -- is never flagged
    as shared, matching pre-BL-F2 rendering exactly."""
    as_of = datetime(2026, 7, 21, tzinfo=timezone.utc)
    signal_single = _signal(workstream_id="ws-a", query_id="q-1", value="7", timestamp=as_of)

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
        approved_signals=(signal_single,),
        workstreams=workstreams,
        configured_queries=(query,),
    )

    assert len(tiles) == 1
    assert tiles[0].value == "7"
    assert tiles[0].shared is False


def test_two_signals_for_same_query_in_one_section_break_ties_by_first_inserted_on_equal_timestamp() -> None:
    """When two signals answer the same query_id within one section and
    share an identical timestamp, ``latest_by_query_id``'s strict ``>``
    comparison means the FIRST one encountered in ``approved_signals``'
    iteration order wins (dict insertion order), not the second. Unrelated
    to sharing; unchanged by the D-19 fix."""
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
