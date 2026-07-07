"""Direct coverage for the extracted integration presentation helpers (D-13)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import pytest

from src.commands.integration_format import (
    _display_title,
    _emit_delta_items,
    _format_optional_datetime,
    _print_table,
)


def test_display_title_reveal_and_hide() -> None:
    assert _display_title("Secret", reveal=True) == "Secret"
    assert _display_title(None, reveal=False) == ""
    hidden = _display_title("Secret", reveal=False)
    assert hidden.startswith("[hidden:") and hidden.endswith("]")
    # deterministic digest
    assert _display_title("Secret", reveal=False) == hidden


def test_format_optional_datetime() -> None:
    assert _format_optional_datetime(None) is None
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert _format_optional_datetime(naive) == "2026-01-02T03:04:05+00:00"
    aware = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert _format_optional_datetime(aware) == "2026-01-02T03:04:05+00:00"


class _Status(Enum):
    CONFIRMED = "confirmed"


@dataclass
class _Reg:
    ref_kind: str
    ref_id: str
    status: _Status
    workstream_ids: tuple[str, ...] = ()
    ref_title: str | None = None


def test_emit_delta_items_empty_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    _emit_delta_items("Added", ())
    assert capsys.readouterr().out == ""


def test_emit_delta_items_renders_rows(capsys: pytest.CaptureFixture[str]) -> None:
    _emit_delta_items("Added", (_Reg("ado", "123", _Status.CONFIRMED, ("ws1",), "Title"),))
    out = capsys.readouterr().out
    assert "Added:" in out
    assert "ado:123" in out
    assert "workstreams=ws1" in out
    assert "status=confirmed" in out


def test_print_table(capsys: pytest.CaptureFixture[str]) -> None:
    _print_table(("Name", "Count"), [("alpha", "1"), ("b", "22")])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["Name", "Count"]
    assert set(lines[1]) <= {"-", " "}
    assert "alpha" in lines[2] and "1" in lines[2]
    assert "22" in lines[3]
