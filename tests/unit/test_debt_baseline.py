from __future__ import annotations

from pathlib import Path

import pytest

from scripts.debt_baseline import build_debt_baseline


pytestmark = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "editions").exists(),
    reason="Requires private data",
)


def test_build_debt_baseline_collects_expected_schema(repo_root: Path) -> None:
    baseline = build_debt_baseline(repo_root=repo_root, require_clean_tree=False)

    assert baseline["git_sha"]
    assert baseline["captured_at"]
    # Phase 3 decomposition reduced gather.py from ~10,416 to current budget.
    # The exact ceiling is ratcheted downward per specs/debt.md §21.3.
    assert baseline["modules"]["src/commands/gather.py"]["loc"] > 0
    assert any(site.startswith("src/core/work_item_states.py:") for site in baseline["terminal_state_sites"])
    # After Phase 4 router extraction, the direct SDK edge lives in request_router.py.
    assert any(
        site.startswith(("src/ai/client.py:", "src/ai/request_router.py:"))
        for site in baseline["ai_call_sites"]
    )

    acme = baseline["golden_outputs"]["acme_weekly"]
    if acme.get("status") is not None:
        pytest.skip(f"Requires live acme_weekly output artifacts: {acme['status']}")
    assert acme["issue_number"] >= 1
    assert acme["report_html_sha256"].startswith("sha256:")
    assert acme["signal_id_set_sha256"].startswith("sha256:")
    assert acme["confirm_snapshot_sha256"].startswith("sha256:")
    assert acme["doctor_json_sha256"].startswith("sha256:")

    fabrikam = baseline["golden_outputs"]["fabrikam_weekly"]
    if fabrikam.get("status") is None:
        assert fabrikam["issue_number"] >= 1
        assert fabrikam["report_html_sha256"].startswith("sha256:")
        assert fabrikam["confirm_snapshot_sha256"].startswith("sha256:")
        assert fabrikam["doctor_json_sha256"].startswith("sha256:")
    else:
        assert fabrikam["status"] in {"unavailable_local_config", "unavailable_local_artifacts"}
