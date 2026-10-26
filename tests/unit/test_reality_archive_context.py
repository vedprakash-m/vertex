from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core import fact_sor_state, program_fact_store, program_reality


@pytest.mark.parametrize("explicit_archive", [False, True])
def test_reality_passes_its_selected_archive_to_actual_fact_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_archive: bool,
) -> None:
    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", Mock(return_value="legacy"))
    monkeypatch.setattr(fact_sor_state, "load_fact_sor_state", Mock(return_value=None))
    monkeypatch.setattr(
        program_reality, "_resolve_committed_gather_run_requirement", Mock(return_value=False),
    )
    boundary = Mock(side_effect=RuntimeError("Selected fact boundary"))
    monkeypatch.setattr(program_reality, "load_program_facts", boundary)
    with pytest.raises(RuntimeError, match="Selected fact boundary"):
        if explicit_archive:
            program_reality.ProgramReality.load(
                "fixture", programs_root=tmp_path / "programs", archive_root=tmp_path / "archive",
            )
        else:
            program_reality.ProgramReality.load("fixture", programs_root=tmp_path / "programs")
    boundary.assert_called_once()
    assert boundary.call_args.kwargs["archive_root"] == (
        tmp_path / "archive" if explicit_archive else None
    )
    assert boundary.call_args.kwargs["programs_root"] == tmp_path / "programs"
    assert not list(tmp_path.iterdir())
