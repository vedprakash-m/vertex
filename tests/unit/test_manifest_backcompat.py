"""ADF-W1.12: archived manifests still containing the retired
``productivity_dividend_hours``/``productivity_dividend_published`` keys
must keep loading. The formula-derived productivity dividend claim is
retired from every writer (confirm.py, render_stage.py, validation_stage.py,
assemble_stage.py, EditionMeta, ReportConfig, the provenance footer
template, and the report_config JSON schema) -- see
``grep -rn productivity_dividend src/ --include=*.py`` for the sole
remaining (tolerant-reader) mention in ``config_loader.py``.

``RunManifest.metadata`` is a generic ``dict[str, Any]`` (src/core/models.py)
populated via a permissive mapping coercion (``_manifest_mapping`` in
src/commands/manifest.py), so an archived manifest JSON file with the legacy
keys already round-trips without any special-casing -- this test pins that
behavior so a future refactor of the manifest reader cannot silently
regress it.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.commands.manifest import _read_manifest


def _legacy_manifest_payload() -> dict[str, object]:
    return {
        "manifest_id": "manifest-legacy-1",
        "issue_number": 1,
        "edition": "xpf_weekly",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:05:00+00:00",
        "config_hash": "sha256:config",
        "snapshot_hash": "sha256:snapshot",
        "html_hash": "sha256:html",
        "md_hash": "sha256:md",
        "ado_calls": 0,
        "ai_calls": 0,
        "ai_cost_usd": 0.0,
        "freshness_summary": {},
        "qg_results": {},
        "git_sha": "abc123",
        "notes": [],
        "metadata": {
            "suggested_subject": "Weekly update",
            # Legacy fields a pre-ADF-W1.12 manifest may still carry.
            "productivity_dividend_hours": 2.5,
            "productivity_dividend_published": True,
        },
    }


def test_dividend_field_tolerated(tmp_path: Path) -> None:
    manifest_path = tmp_path / "issue_001.manifest.json"
    manifest_path.write_text(json.dumps(_legacy_manifest_payload()), encoding="utf-8")

    manifest = _read_manifest(manifest_path)

    assert manifest.manifest_id == "manifest-legacy-1"
    # The legacy keys are preserved in the generic metadata dict, not dropped
    # or rejected -- an operator inspecting an old confirmed issue still sees
    # exactly what was recorded at the time.
    assert manifest.metadata["productivity_dividend_hours"] == 2.5
    assert manifest.metadata["productivity_dividend_published"] is True


def test_manifest_without_dividend_fields_loads_identically(tmp_path: Path) -> None:
    """Current-shape manifests (no legacy keys at all) load the same way."""
    payload = _legacy_manifest_payload()
    del payload["metadata"]["productivity_dividend_hours"]
    del payload["metadata"]["productivity_dividend_published"]
    manifest_path = tmp_path / "issue_002.manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = _read_manifest(manifest_path)

    assert "productivity_dividend_hours" not in manifest.metadata
    assert manifest.metadata["suggested_subject"] == "Weekly update"
