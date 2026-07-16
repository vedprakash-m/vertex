from __future__ import annotations

from pathlib import Path

import yaml

from src.commands import nudge as nudge_module
from src.core.workstream_documents import _parse_workstreams
from src.core.yaml_utils import load_yaml_mapping
from tests.support.report_test_setup import stage_v2_report_workspace



def test_word_truncate_title_is_the_sole_compression_path() -> None:
    # ADF-W5.4 (2026-07-13): AI title compression and its bespoke cache were
    # removed (_compress_titles_batch/_ai_batch_compress_titles/_load_title_cache
    # deleted) -- _word_truncate_title is the sole, deterministic path.
    short = nudge_module._word_truncate_title("Short title")
    assert short == "Short title"

    long_title = "A very long work item title that definitely exceeds the fifty character limit for display"
    truncated = nudge_module._word_truncate_title(long_title)
    assert len(truncated) <= 51  # 50 chars + ellipsis
    assert truncated.endswith("…")


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


