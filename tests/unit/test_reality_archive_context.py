from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core import fact_sor_state, program_fact_store, program_reality


@pytest.mark.parametrize("explicit_archive", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
def test_reality_passes_its_selected_archive_to_actual_fact_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_archive: bool, scoped: bool,
) -> None:
    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", Mock(return_value="legacy"))
    monkeypatch.setattr(fact_sor_state, "load_fact_sor_state", Mock(return_value=None))
    monkeypatch.setattr(
        program_reality, "_resolve_committed_gather_run_requirement", Mock(return_value=False),
    )
    boundary = Mock(side_effect=RuntimeError("Selected fact boundary"))
    monkeypatch.setattr(program_reality, "load_program_facts", boundary)
    options = {"_shim_fact_types": ("dependency.link",)} if scoped else {}
    with pytest.raises(RuntimeError, match="Selected fact boundary"):
        if explicit_archive:
            program_reality.ProgramReality.load(
                "fixture", programs_root=tmp_path / "programs", archive_root=tmp_path / "archive", **options,
            )
        else:
            program_reality.ProgramReality.load("fixture", programs_root=tmp_path / "programs", **options)
    boundary.assert_called_once()
    assert boundary.call_args.kwargs["archive_root"] == (
        tmp_path / "archive" if explicit_archive else None
    )
    assert boundary.call_args.kwargs["programs_root"] == tmp_path / "programs"
    assert boundary.call_args.kwargs["fact_types"] == (("dependency.link",) if scoped else None)
    database = program_fact_store.ProgramFactStore(
        "fixture", db_root=program_fact_store._resolve_fact_db_root(tmp_path / "programs"),
    ).db_path
    expected = {
        database.with_name(database.name + ".capture.lock"),
        tmp_path / "programs" / "fixture" / "ledger.capture.lock",
        tmp_path / "knowledge.capture.lock",
        tmp_path / "programs" / "fixture" / "fact_store_sor.yaml.capture.lock",
    }
    files = {path for path in tmp_path.rglob("*") if path.is_file()}
    assert files == expected
    assert all(path.read_bytes() == b"" for path in files)


def test_reality_without_action_shim_keeps_invalid_milestone_diagnostic(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "fixture"
    program_root.mkdir(parents=True)
    (program_root / "milestones.yaml").write_text("invalid: [", encoding="utf-8")

    reality = program_reality.ProgramReality.load(
        "fixture", programs_root=programs_root, _omit_action_shim=True,
    )

    assert reality.milestones() == ()
    assert reality.to_dict()["source_diagnostics"][0]["source"] == "milestones.yaml"
    assert reality.to_dict()["source_diagnostics"][0]["status"] == "invalid"
