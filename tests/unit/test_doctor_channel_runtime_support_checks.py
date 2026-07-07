from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.channel_runtime_support_checks import (
    channel_auth_failure_detail,
    current_doctor_kusto_targets,
)


def test_current_doctor_kusto_targets_filters_queries_for_program(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    program = SimpleNamespace(
        id="demo",
        ado=SimpleNamespace(area_paths=("Area\\Team",), date_window_days=14),
        kusto=SimpleNamespace(
            queries=(
                SimpleNamespace(engine="kusto", program_ids=(), name="all"),
                SimpleNamespace(engine="kusto", program_ids=("demo",), name="demo-only"),
                SimpleNamespace(engine="kusto", program_ids=("other",), name="other-only"),
            )
        ),
    )

    def fake_load(program_id: str, *, template_context, direct_queries, programs_root: Path):
        captured["program_id"] = program_id
        captured["template_context"] = template_context
        captured["direct_queries"] = tuple(query.name for query in direct_queries)
        captured["programs_root"] = programs_root
        return ("query-a", "query-b")

    monkeypatch.setattr(
        "src.commands.doctor_checks.channel_runtime_support_checks.load_doctor_kusto_queries",
        fake_load,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.channel_runtime_support_checks.kusto_target_labels",
        lambda rendered_queries: ("cluster/db",),
    )

    targets = current_doctor_kusto_targets(program_id="demo", program=program, programs_root=tmp_path)

    assert targets == ("cluster/db",)
    assert captured["program_id"] == "demo"
    assert captured["direct_queries"] == ("all", "demo-only")
    assert captured["programs_root"] == tmp_path


def test_channel_auth_failure_detail_classifies_workiq_outage() -> None:
    detail = channel_auth_failure_detail("workiq", "Agency MCP timed out while contacting server.")

    assert detail == "Agency CLI not responding or WorkIQ access failed; verify 'agency mcp list'."
