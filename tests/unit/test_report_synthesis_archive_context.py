from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.commands import report_ai
from src.core import program_synthesis
from src.core.program_reality import ProgramReality


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("consumer", ["exec", "blurb", "assembly"])
def test_synthesis_preserves_selected_archive_without_changing_legacy_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool, consumer: str,
) -> None:
    boundary = Mock(side_effect=RuntimeError("Selected archive boundary"))
    options = {"archive_root": tmp_path / "archive"} if explicit else {}
    if consumer == "exec":
        monkeypatch.setattr(program_synthesis, "assemble_program_synthesis_request", boundary)
        assert report_ai._program_synthesis_exec_summary_context_lines(
            "fixture", tmp_path / "programs", **options,
        ) == ()
    elif consumer == "blurb":
        monkeypatch.setattr(ProgramReality, "load", boundary)
        assert report_ai._program_synthesis_blurb_context_lines(
            "fixture", tmp_path / "programs", workstream_ids=("delivery",), item_ids={1},
            **options,
        ) == ()
    else:
        monkeypatch.setattr(program_synthesis, "load_ai_proposals", Mock(return_value=()))
        monkeypatch.setattr(ProgramReality, "load", boundary)
        with pytest.raises(RuntimeError, match="Selected archive boundary"):
            program_synthesis.assemble_program_synthesis_request(
                "fixture", programs_root=tmp_path / "programs", **options,
            )
    boundary.assert_called_once()
    assert boundary.call_args.args == ("fixture",)
    assert boundary.call_args.kwargs["programs_root"] == tmp_path / "programs"
    assert {
        key: value for key, value in boundary.call_args.kwargs.items() if key == "archive_root"
    } == options
    assert not list(tmp_path.iterdir())
