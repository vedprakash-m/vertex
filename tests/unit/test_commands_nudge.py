from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from src.ai.ai_mode import AIMode, set_ai_mode
from src.commands import nudge as nudge_module
from src.core.workstream_documents import _parse_workstreams
from src.core.yaml_utils import load_yaml_mapping
from tests.support.report_test_setup import stage_v2_report_workspace



def test_ai_batch_compress_titles_returns_empty_when_ai_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.commands.nudge.FallbackStructuredClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("FallbackStructuredClient should not be constructed")),
        raising=False,
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        compressed = nudge_module._ai_batch_compress_titles(
            [(1, "A very long title that would otherwise need AI compression to fit the surface")],
            program=SimpleNamespace(ai=SimpleNamespace(blurb_deployment="demo")),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert compressed == {}


def test_compress_titles_batch_does_not_count_fallbacks_as_ai_when_disabled(tmp_path: Path) -> None:
    cache_path = tmp_path / "output" / "nova_nudge" / "title_cache.json"

    set_ai_mode(AIMode.DISABLED)
    try:
        compressed, ai_count = nudge_module._compress_titles_batch(
            [(1, "A very long title that would otherwise need AI compression to fit the surface cleanly")],
            cache_path=cache_path,
            enabled=True,
            program=SimpleNamespace(ai=SimpleNamespace(blurb_deployment="demo")),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert ai_count == 0
    assert compressed[1]
    assert len(compressed[1]) <= 50


def test_ai_batch_compress_titles_scrubs_pii_from_ai_output(monkeypatch) -> None:
    class _FakeFallbackStructuredClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def structured(self, system_prompt, user_prompt, *, parser, max_tokens=800, prompt_version=None):
            del system_prompt, user_prompt, max_tokens, prompt_version
            return parser({"content": "1: Follow up with foo@gmail.com about launch blockers"})

    monkeypatch.setattr("src.ai.deployment_fallback.resolve_ai_deployments", lambda **_kwargs: ("primary",))
    monkeypatch.setattr("src.ai.deployment_fallback.FallbackStructuredClient", _FakeFallbackStructuredClient)

    compressed = nudge_module._ai_batch_compress_titles(
        [(1, "Follow up with the vendor about launch blockers and readiness")],
        program=SimpleNamespace(ai=SimpleNamespace(blurb_deployment="demo")),
    )

    assert 1 in compressed
    assert "foo@gmail.com" not in compressed[1]
    assert "[PII-FILTERED-EMAIL]" in compressed[1]

def test_parse_workstreams_supports_signal_sources(repo_root: Path, tmp_path: Path) -> None:
    stage_v2_report_workspace(repo_root, tmp_path)
    workstreams_path = tmp_path / "programs" / "acme" / "workstreams.yaml"

    workstreams = _parse_workstreams(load_yaml_mapping(workstreams_path), workstreams_path)

    acme = next(workstream for workstream in workstreams if workstream.id == "acme")
    dd_on_pf = next(workstream for workstream in workstreams if workstream.id == "dd_on_pf")

    assert acme.signal_sources is not None
    assert acme.signal_sources.workiq_keywords == (
        "Adventure Northwind",
        "Adventure ramp",
        "SCHIE gaps",
        "deployment velocity Acme",
        "Northwind deployment",
        "ramp review",
        "ramp P1",
        "MAP Day",
        "BIOS compliance",
        "Wingtip",
        "NMAgent",
        "June 1",
        "ramp-resume",
        "unblocking Acme ramp",
        "NeedsValidation burn-in",
        "post-repair diagnostics Acme",
        "repair activity hostname",
        "Titan Acme",
    )
    assert acme.signal_sources.workiq_exclude_keywords == ("Direct Drive Northwind",)
    assert acme.signal_sources.ado_coverage is not None
    assert acme.signal_sources.ado_coverage.min_ado_count == 5
    assert acme.signal_sources.ado_coverage.required_work_item_types == ("Feature",)
    assert acme.signal_sources.teams_meeting_series[0].display_name == "Acme Weekly Ops Review"
    assert dd_on_pf.signal_sources is not None
    assert dd_on_pf.signal_sources.ado_coverage is not None
    assert dd_on_pf.signal_sources.ado_coverage.min_ado_count == 3


def test_parse_workstreams_keeps_signal_sources_optional(tmp_path: Path) -> None:
    workstreams_path = tmp_path / "workstreams.yaml"
    workstreams_path.write_text(
        "schema_version: '2.0'\nworkstreams:\n- id: demo\n  name: Demo\n",
        encoding="utf-8",
    )

    workstreams = _parse_workstreams(load_yaml_mapping(workstreams_path), workstreams_path)

    assert len(workstreams) == 1
    assert workstreams[0].signal_sources is None


