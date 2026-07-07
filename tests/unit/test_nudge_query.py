"""Unit tests for src.core.nudge_query."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.exceptions import ConfigError
from src.core.nudge_models import NudgeSectionCriteria, NudgeSectionSpec
from src.core.nudge_query import build_nudge_wiql, escape_wiql_literal, fetch_section_candidates


# ---------------------------------------------------------------------------
# escape_wiql_literal
# ---------------------------------------------------------------------------


def test_escape_wiql_literal_doubles_apostrophes() -> None:
    assert escape_wiql_literal("O'Brien") == "O''Brien"


def test_escape_wiql_literal_multiple_apostrophes() -> None:
    assert escape_wiql_literal("it's O'Brien") == "it''s O''Brien"


def test_escape_wiql_literal_no_apostrophes_unchanged() -> None:
    assert escape_wiql_literal("RAMPP1") == "RAMPP1"


def test_escape_wiql_literal_rejects_empty() -> None:
    with pytest.raises(ConfigError, match="must not be empty"):
        escape_wiql_literal("  ", field_name="tag")


def test_escape_wiql_literal_rejects_control_char() -> None:
    with pytest.raises(ConfigError, match="control character"):
        escape_wiql_literal("bad\x01string", field_name="tag")


def test_escape_wiql_literal_rejects_del_char() -> None:
    with pytest.raises(ConfigError, match="control character"):
        escape_wiql_literal("bad\x7fstring", field_name="tag")


def test_escape_wiql_literal_rejects_null() -> None:
    with pytest.raises(ConfigError, match="control character"):
        escape_wiql_literal("\x00null", field_name="tag")


def test_escape_wiql_literal_preserves_unicode() -> None:
    result = escape_wiql_literal("Ärger")
    assert result == "Ärger"


# ---------------------------------------------------------------------------
# build_nudge_wiql
# ---------------------------------------------------------------------------


def _make_section(
    source: str = "tag",
    tags: tuple[str, ...] = ("RAMPP1",),
    area_path_filter: tuple[str, ...] = (),
    legacy_scope_override: bool = False,
) -> NudgeSectionSpec:
    return NudgeSectionSpec(
        id="test_sec",
        title="Test",
        criteria=NudgeSectionCriteria(
            source=source,  # type: ignore[arg-type]
            tags=tags,
            area_path_filter=area_path_filter,
            legacy_scope_override=legacy_scope_override,
        ),
        stale_business_days=3,
        letter="A",
    )


def _make_program(
    organization: str = "msazure",
    project: str = "One",
    area_paths: tuple[str, ...] = ("One\\Xstore",),
    work_item_types: tuple[str, ...] = (),
    excluded_states: tuple[str, ...] = (),
) -> Any:
    program = MagicMock()
    ado = MagicMock()
    ado.organization = organization
    ado.project = project
    ado.area_paths = list(area_paths)
    ado.work_item_types = list(work_item_types)
    ado.excluded_states = list(excluded_states)
    ado.api_timeout_seconds = 30
    program.ado = ado
    return program


def test_build_nudge_wiql_registry_raises() -> None:
    sec = _make_section(source="registry")
    program = _make_program()
    with pytest.raises(ConfigError, match="registry sections do not execute WIQL"):
        build_nudge_wiql(program=program, section=sec)


def test_build_nudge_wiql_tag_basic() -> None:
    sec = _make_section(source="tag", tags=("RAMPP1",))
    program = _make_program()
    wiql = build_nudge_wiql(program=program, section=sec)
    assert "[System.Tags] CONTAINS 'RAMPP1'" in wiql
    assert "ORDER BY [System.ChangedDate] ASC" in wiql
    assert "@project" in wiql


def test_build_nudge_wiql_tag_multiple_tags_uses_or() -> None:
    sec = _make_section(source="tag", tags=("RAMPP1", "RAMP P1"))
    program = _make_program()
    wiql = build_nudge_wiql(program=program, section=sec)
    assert " OR " in wiql
    assert "RAMPP1" in wiql
    assert "RAMP P1" in wiql


def test_build_nudge_wiql_area_path_uses_under() -> None:
    sec = NudgeSectionSpec(
        id="area_sec",
        title="Area",
        criteria=NudgeSectionCriteria(
            source="area_path",
            area_path_filter=("One\\Xstore",),
        ),
        stale_business_days=3,
        letter="B",
    )
    program = _make_program()
    wiql = build_nudge_wiql(program=program, section=sec)
    assert "UNDER" in wiql
    assert "One\\Xstore" in wiql


def test_build_nudge_wiql_excluded_states_present() -> None:
    sec = _make_section(source="tag", tags=("T1",))
    program = _make_program(excluded_states=("Closed", "Resolved"))
    wiql = build_nudge_wiql(program=program, section=sec)
    assert "[System.State] NOT IN" in wiql
    assert "Closed" in wiql
    assert "Resolved" in wiql


def test_build_nudge_wiql_escapes_apostrophes_in_tag() -> None:
    sec = _make_section(source="tag", tags=("O'Brien Tag",))
    program = _make_program()
    wiql = build_nudge_wiql(program=program, section=sec)
    assert "O''Brien Tag" in wiql


def test_build_nudge_wiql_wit_types_predicate() -> None:
    sec = _make_section(source="tag", tags=("T1",))
    program = _make_program(work_item_types=("Feature",))
    wiql = build_nudge_wiql(program=program, section=sec)
    assert "[System.WorkItemType] = 'Feature'" in wiql


def test_build_nudge_wiql_no_ado_raises() -> None:
    sec = _make_section(source="tag", tags=("T1",))
    program = MagicMock()
    program.ado = None
    with pytest.raises(ConfigError, match="program.ado is required"):
        build_nudge_wiql(program=program, section=sec)


# ---------------------------------------------------------------------------
# fetch_section_candidates — query_error path
# ---------------------------------------------------------------------------


def _make_client_that_raises() -> Any:
    client = MagicMock()
    from src.core.exceptions import QueryError  # noqa: PLC0415
    client.execute_wiql.side_effect = QueryError("ADO timeout")
    return client


def test_fetch_section_candidates_query_error_returns_degraded_result() -> None:
    sec = _make_section(source="tag", tags=("T1",))
    program = _make_program()
    client = _make_client_that_raises()

    result = fetch_section_candidates(
        program=program,
        section=sec,
        authored_registry=(),
        workstreams=(),
        client=client,
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )

    assert result.query_error is True
    assert result.candidates == ()
    assert result.error_details is not None
    assert "ADO timeout" in result.error_details


def test_fetch_section_candidates_empty_wiql_result_returns_empty() -> None:
    sec = _make_section(source="tag", tags=("T1",))
    program = _make_program()
    client = MagicMock()
    client.execute_wiql.return_value = []

    result = fetch_section_candidates(
        program=program,
        section=sec,
        authored_registry=(),
        workstreams=(),
        client=client,
        as_of=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )

    assert result.query_error is False
    assert result.candidates == ()
