from __future__ import annotations

from pathlib import Path

import yaml

from tests.unit.test_config_loader import _write_demo_v2_layout


def stage_fact_store_doctor_workspace(workspace_root: Path) -> Path:
    editions, programs = _write_demo_v2_layout(workspace_root)
    program = programs / "acme"
    (programs / "demo").rename(program)
    editions.rename(program / "editions")
    program_path = program / "program.yaml"
    config = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    config.update(id="acme", name="Synthetic Acme")
    program_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    edition_path = program / "editions" / "demo_weekly.yaml"
    edition = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    edition.update(id="acme_weekly", program_id="acme")
    (edition_path.parent / "acme_weekly.yaml").write_text(
        yaml.safe_dump(edition), encoding="utf-8",
    )
    edition_path.unlink()
    reports_root = workspace_root / "reports"
    reports_root.mkdir()
    return reports_root
