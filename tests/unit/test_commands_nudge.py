from __future__ import annotations

from pathlib import Path

import yaml

from src.commands import nudge as nudge_module
from src.core.workstream_documents import _parse_workstreams
from src.core.yaml_utils import load_yaml_mapping



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


def test_parse_workstreams_supports_signal_sources(tmp_path: Path) -> None:
    workstreams_path = tmp_path / "workstreams.yaml"
    keywords = ("Example deployment", "readiness review", "repair diagnostics")
    workstreams_path.write_text(yaml.safe_dump({
        "schema_version": "2.0",
        "workstreams": [
            {
                "id": "primary",
                "name": "Primary",
                "signal_sources": {
                    "workiq_keywords": list(keywords),
                    "workiq_exclude_keywords": ["Secondary deployment"],
                    "ado_coverage": {"min_ado_count": 5, "required_work_item_types": ["Feature"]},
                    "teams_meeting_series": [{"display_name": "Example Weekly Review"}],
                },
            },
            {
                "id": "secondary",
                "name": "Secondary",
                "signal_sources": {"ado_coverage": {"min_ado_count": 3}},
            },
        ],
    }), encoding="utf-8")

    workstreams = _parse_workstreams(load_yaml_mapping(workstreams_path), workstreams_path)

    primary = next(workstream for workstream in workstreams if workstream.id == "primary")
    secondary = next(workstream for workstream in workstreams if workstream.id == "secondary")

    assert primary.signal_sources is not None
    assert primary.signal_sources.workiq_keywords == keywords
    assert primary.signal_sources.workiq_exclude_keywords == ("Secondary deployment",)
    assert primary.signal_sources.ado_coverage is not None
    assert primary.signal_sources.ado_coverage.min_ado_count == 5
    assert primary.signal_sources.ado_coverage.required_work_item_types == ("Feature",)
    assert primary.signal_sources.teams_meeting_series[0].display_name == "Example Weekly Review"
    assert secondary.signal_sources is not None
    assert secondary.signal_sources.ado_coverage is not None
    assert secondary.signal_sources.ado_coverage.min_ado_count == 3


def test_parse_workstreams_keeps_signal_sources_optional(tmp_path: Path) -> None:
    workstreams_path = tmp_path / "workstreams.yaml"
    workstreams_path.write_text(
        "schema_version: '2.0'\nworkstreams:\n- id: demo\n  name: Demo\n",
        encoding="utf-8",
    )

    workstreams = _parse_workstreams(load_yaml_mapping(workstreams_path), workstreams_path)

    assert len(workstreams) == 1
    assert workstreams[0].signal_sources is None

