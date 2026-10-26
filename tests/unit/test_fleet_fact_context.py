from pathlib import Path
from unittest.mock import Mock

import pytest

import src.commands.fleet as fleet
from tests.unit.test_commands_fleet import FROZEN_NOW, _seed_fleet_workspace


def test_all_fleet_fact_acquisitions_keep_selected_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))
    load_facts = Mock(wraps=fleet.load_program_facts)
    monkeypatch.setattr(fleet, "load_program_facts", load_facts)

    report = fleet.build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    assert len(report.programs) == 2
    assert load_facts.call_count == 6
    for call in load_facts.call_args_list:
        assert call.kwargs["programs_root"] == programs_root
        assert call.kwargs["archive_root"] == tmp_path / "archive"
