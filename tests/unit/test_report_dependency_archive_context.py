from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from src.commands import report_scorecards


@pytest.mark.parametrize("explicit", [False, True])
def test_dependency_walk_preserves_archive_context_for_every_visited_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    dependency = SimpleNamespace(id="delivery", from_program_id="first", to_program_id="second")
    first = Mock()
    first.dependencies.return_value = (SimpleNamespace(record=dependency),)
    second = Mock()
    second.dependencies.return_value = ()
    boundary = Mock(side_effect=[first, second])
    monkeypatch.setattr(report_scorecards.ProgramReality, "load", boundary)
    options = {"archive_root": tmp_path / "archive"} if explicit else {}
    result = report_scorecards._load_reachable_dependency_network(
        "first", programs_root=tmp_path / "programs", **options,
    )
    assert result == (dependency,)
    assert boundary.call_args_list == [
        call("first", programs_root=tmp_path / "programs", **options),
        call("second", programs_root=tmp_path / "programs", **options),
    ]
    assert not list(tmp_path.iterdir())
