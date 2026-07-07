from __future__ import annotations

from pathlib import Path

from src.commands.gather_pipeline.uil_binding_stage import resolve_uil_channel_binding_for_gather
from src.core.models_v2 import ADOConfig, Program


def test_resolve_uil_channel_binding_for_gather_requires_program_file(tmp_path: Path) -> None:
    program = Program(
        schema_version="3.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )

    resolved = resolve_uil_channel_binding_for_gather(
        program,
        (),
        "ado",
        programs_root=tmp_path,
        enabled_funcs={"ado": lambda: True},
        uil_channel_enabled_fn=lambda _channel: True,
    )

    assert resolved is None
