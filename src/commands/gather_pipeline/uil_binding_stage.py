from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.core.models_v2 import Program, Workstream


def resolve_uil_channel_binding_for_gather(
    program: Program,
    workstreams: tuple[Workstream, ...],
    channel: str,
    *,
    programs_root: Path,
    enabled_funcs: dict[str, Callable[[], bool]] | None,
    uil_channel_enabled_fn: Callable[[str], bool],
) -> Any | None:
    enabled_func = (enabled_funcs or {}).get(channel)
    if enabled_func is not None:
        if not enabled_func():
            return None
    elif not uil_channel_enabled_fn(channel):
        return None
    program_path = programs_root / program.id / "program.yaml"
    if not program_path.exists():
        return None
    from src.commands.channel_wiring import resolve_channel_binding

    return resolve_channel_binding(program, workstreams, channel, programs_root=programs_root)
