from __future__ import annotations


def test_explicit_pending_override_context_is_forwarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from tests.unit.test_narrative_store import _selected_artifact_root
    from src.core import overrides_store as store

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "reports"
    seen = {}

    def bundle(edition, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(config=SimpleNamespace(scorecards=()))

    monkeypatch.setattr(store, "load_bundle", bundle)
    monkeypatch.setattr(store, "_resolve_pending_override_issue_number", lambda _path: 7)
    path = store.apply_pending_overrides("fixture_weekly", tmp_path / "issue_007", reports, programs_root=root)
    assert path == root / "fixture" / "overrides" / "issue_007.yaml"
    assert seen == {"reports_root": reports, "programs_root": root}
    assert not reports.exists()
    seen.clear()
    legacy = store.apply_pending_overrides("legacy", tmp_path / "issue_007", reports)
    assert legacy == reports / "legacy" / "overrides.yaml"
    assert seen == {"reports_root": reports}


def test_explicit_override_root_operations_are_scoped(tmp_path: Path) -> None:
    from tests.unit.test_narrative_store import _selected_artifact_root
    from src.core import overrides_store as store

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "reports"
    document = store.OverridesDocument(issue_number=7, top_3_now=(), scorecards=())
    path = store.save_overrides("fixture_weekly", document, reports, programs_root=root)
    assert path == root / "fixture" / "overrides" / "issue_007.yaml"
    assert store.load_overrides("fixture_weekly", reports, issue_number=7, programs_root=root) == document
    store.reset_overrides_for_next_issue("fixture_weekly", 8, (), reports, programs_root=root)
    assert store.delete_seedable_overrides("fixture_weekly", 8, reports, programs_root=root)
    assert not reports.exists()


from datetime import date
from pathlib import Path

import pytest

from src.core.models import ConfirmedDimension, RiskLevel
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides, Top3NowEntry
from src.core.overrides_store import archive_overrides, load_overrides, merge_overrides, reset_overrides_for_next_issue
from src.core.overrides_store import save_overrides
from src.core.exceptions import ConfigError


EDITION_NAME = "acme_weekly"


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(b"# authored\nissue_number: 8\nhealth_bluf: reviewed\n", id="authored"),
        pytest.param(b"issue_number: 8\nscorecards: [\n", id="malformed-yaml"),
        pytest.param(b"issue_number: 8\n# \xff\n", id="invalid-utf8"),
    ],
)
def test_reset_preserves_existing_program_override_bytes(tmp_path: Path, contents: bytes) -> None:
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    target = root / "fixture" / "overrides" / "issue_008.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(contents)

    result = reset_overrides_for_next_issue("fixture_weekly", 8, (), tmp_path / "reports", programs_root=root)

    assert target.read_bytes() == contents
    assert result is None


@pytest.mark.parametrize("explicit", [False, True], ids=["inferred-root", "selected-root"])
@pytest.mark.parametrize("existing", [False, True], ids=["create", "preserve"])
def test_reset_program_target_respects_inferred_and_selected_roots(
    tmp_path: Path, explicit: bool, existing: bool,
) -> None:
    root = tmp_path / "programs"
    editions = root / "fixture" / "editions"
    editions.mkdir(parents=True)
    (editions / "fixture_weekly.yaml").write_text("program_id: fixture\n", encoding="utf-8")
    target = root / "fixture" / "overrides" / "issue_008.yaml"
    if existing:
        target.parent.mkdir(parents=True)
        target.write_bytes(b"issue_number: 8\nhealth_bluf: future authoring\n")
    dimensions = (
        ConfirmedDimension(
            scorecard_name="Delivery",
            name="Readiness",
            risk=RiskLevel.HIGH,
            prior_risk=RiskLevel.LOW,
            item_count=1,
            ado_query_url="https://example.com/query",
        ),
    )
    kwargs = {"programs_root": root} if explicit else {}

    result = reset_overrides_for_next_issue("fixture_weekly", 8, dimensions, tmp_path / "reports", **kwargs)

    if existing:
        assert result is None
        assert target.read_bytes() == b"issue_number: 8\nhealth_bluf: future authoring\n"
    else:
        assert result is not None
        assert result.issue_number == 8
        assert result.scorecards[0].dimensions[0].risk is None
        assert load_overrides("fixture_weekly", tmp_path / "reports", issue_number=8, **kwargs) == result


def test_reset_does_not_replace_program_override_created_during_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    target = root / "fixture" / "overrides" / "issue_008.yaml"
    authored = b"issue_number: 8\nhealth_bluf: concurrent author\n"
    original_open = Path.open
    raced = False

    def competing_create(path: Path, mode: str = "r", *args, **kwargs):
        nonlocal raced
        if path == target and mode in {"w", "x"} and not raced:
            raced = True
            with original_open(path, "wb") as stream:
                stream.write(authored)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", competing_create)
    result = reset_overrides_for_next_issue("fixture_weekly", 8, (), tmp_path / "reports", programs_root=root)

    assert raced
    assert target.read_bytes() == authored
    assert result is None


def test_concurrent_program_resets_create_only_one_next_issue_file(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    barrier = Barrier(2)

    def reset():
        barrier.wait(timeout=10)
        return reset_overrides_for_next_issue("fixture_weekly", 8, (), tmp_path / "reports", programs_root=root)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: reset(), range(2)))

    created = [result for result in results if result is not None]
    assert len(created) == 1
    assert results.count(None) == 1
    assert load_overrides("fixture_weekly", tmp_path / "reports", issue_number=8, programs_root=root) == created[0]


@pytest.mark.parametrize("simulate_file_exists", [False, True], ids=["platform-error", "file-exists"])
def test_reset_preserves_nonfile_program_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, simulate_file_exists: bool,
) -> None:
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    target = root / "fixture" / "overrides" / "issue_008.yaml"
    target.mkdir(parents=True)
    (target / "sentinel").write_bytes(b"synthetic")
    if simulate_file_exists:
        original_open = Path.open

        def existing_directory(path: Path, mode: str = "r", *args, **kwargs):
            if path == target and mode == "x":
                raise FileExistsError("synthetic existing directory")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", existing_directory)

    with pytest.raises(IsADirectoryError, match="Next-issue overrides target is a directory"):
        reset_overrides_for_next_issue("fixture_weekly", 8, (), tmp_path / "reports", programs_root=root)
    assert (target / "sentinel").read_bytes() == b"synthetic"


def test_reset_propagates_program_override_creation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    target = root / "fixture" / "overrides" / "issue_008.yaml"
    original_open = Path.open

    def denied_create(path: Path, mode: str = "r", *args, **kwargs):
        if path == target and mode in {"w", "x"}:
            raise PermissionError("synthetic write refusal")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_create)
    with pytest.raises(PermissionError, match="synthetic write refusal"):
        reset_overrides_for_next_issue("fixture_weekly", 8, (), tmp_path / "reports", programs_root=root)
    assert not target.exists()


def test_reset_replaces_flat_active_override_for_the_next_issue(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    target = reports / "legacy_weekly" / "overrides.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("issue_number: 7\nhealth_bluf: previous issue\n", encoding="utf-8")

    result = reset_overrides_for_next_issue("legacy_weekly", 8, (), reports)

    assert result is not None and result.issue_number == 8
    assert load_overrides("legacy_weekly", reports) == result
    assert "previous issue" not in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("existing", [False, True])
def test_reset_still_refuses_hardlocked_program_issue(tmp_path: Path, existing: bool) -> None:
    from src.core.baseline_lock import BaselineLockError
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    target = root / "fixture" / "overrides" / "issue_008.yaml"
    (root / "fixture" / "trusted_baseline.yaml").write_text("trusted_issue_number: 8\n", encoding="utf-8")
    if existing:
        target.parent.mkdir(parents=True)
        target.write_bytes(b"authored")

    with pytest.raises(BaselineLockError):
        reset_overrides_for_next_issue("fixture_weekly", 8, (), tmp_path / "reports", programs_root=root)

    assert target.exists() is existing
    if existing:
        assert target.read_bytes() == b"authored"


def test_merge_overrides_preserves_adds_and_marks_removed(tmp_path: Path) -> None:
    existing = OverridesDocument(
        issue_number=77,
        top_3_now=(
            Top3NowEntry(
                type="decision",
                text="Need LT call.",
                owner="Priya Mehta",
                ado_link="https://dev.azure.com/your-org/One/_workitems/edit/123",
                anchor="exampleteam-gaps",
            ),
        ),
        scorecards=(
            ScorecardOverrides(
                name="Acme Adventure/SYN-A 100% Ramp Readiness",
                dimensions=(
                    DimensionOverride(
                        name="Deployment Velocity",
                        risk=RiskLevel.LOW,
                        label="Velocity",
                        note="carry",
                        eta=date(2026, 5, 15),
                        hide_details=True,
                    ),
                    DimensionOverride(name="Removed Dimension", risk=RiskLevel.HIGH),
                ),
            ),
        ),
    )

    merged, stats = merge_overrides(
        issue_number=78,
        expected_scorecards={
            "Acme Adventure/SYN-A 100% Ramp Readiness": (
                "Deployment Velocity",
                "Deployment Safety",
            ),
        },
        existing=existing,
    )

    reports_root = tmp_path / "reports"
    saved_path = save_overrides(EDITION_NAME, merged, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)

    assert stats.preserved_count == 1
    assert stats.added_count == 1
    assert stats.removed_count == 1
    assert merged.issue_number == 78
    assert merged.top_3_now == existing.top_3_now
    assert merged.scorecards[0].dimensions[0].risk == RiskLevel.LOW
    assert merged.scorecards[0].dimensions[0].label == "Velocity"
    assert merged.scorecards[0].dimensions[0].eta == date(2026, 5, 15)
    assert merged.scorecards[0].dimensions[0].hide_details is True
    assert merged.scorecards[0].dimensions[1].risk is None
    assert merged.removed_dimensions[0].dimension_name == "Removed Dimension"
    assert loaded is not None
    assert loaded.scorecards[0].dimensions[0].label == "Velocity"
    assert loaded.scorecards[0].dimensions[0].eta == date(2026, 5, 15)
    assert loaded.scorecards[0].dimensions[0].hide_details is True
    assert loaded.scorecards[0].dimensions[1].risk is None
    content = saved_path.read_text(encoding="utf-8")
    assert "label: Velocity" in content
    assert "eta:" in content
    assert "2026-05-15" in content
    assert "hide_details: true" in content
    assert "❓ Needs input" in content
    assert "# Active Vertex overrides.yaml" in content
    assert "# REMOVED — dimension no longer in config" in content


def test_reset_and_archive_overrides_seed_blank_risks_for_next_issue(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    document = reset_overrides_for_next_issue(
        edition=EDITION_NAME,
        next_issue_number=79,
        confirmed_dimensions=(
            ConfirmedDimension(
                scorecard_name="Acme Readiness",
                name="Deployment Velocity",
                risk=RiskLevel.MEDIUM,
                prior_risk=RiskLevel.LOW,
                item_count=3,
                ado_query_url="https://dev.azure.com/your-org/One/_queries/1",
            ),
        ),
        reports_root=reports_root,
    )

    archive_path = archive_overrides(
        edition=EDITION_NAME,
        issue_number=79,
        document=document,
        archive_root=archive_root,
    )

    assert document.issue_number == 79
    assert document.top_3_now == ()
    assert document.scorecards[0].dimensions[0].risk is None
    assert archive_path == archive_root / EDITION_NAME / "overrides" / "issue_079.yaml"
    assert "❓ Needs input" in archive_path.read_text(encoding="utf-8")


def test_governance_and_decisions_roundtrip(tmp_path: Path) -> None:
    from src.core.overrides_store import GovernanceState, DecisionRecord
    from src.core.decision_register import read_governance_decisions_from_overrides
    from src.core.models_v2 import DecisionStatus
    
    gov = GovernanceState(
        dfd_date=date(2026, 6, 3),
        dfd_history=(date(2026, 3, 31), date(2026, 6, 3)),
        escalation_active=True,
        escalation_workstreams=("acme-adventure-syn-a-100-ramp-readiness-bios",),
        lt_commitment="EXN LT committed to deliver P0 items",
        lt_commitment_date=date(2026, 5, 22),
    )
    
    dec = DecisionRecord(
        id="contoso-023-prod-gate",
        workstream="contoso-pilot-readiness",
        type="gate",
        statement="EXN LT committed to UD upgrade-alert intervention.",
        source_type="meeting",
        source_ref="series-1234",
        owner="Chris Chen",
        status="active",
        effective_date=date(2026, 5, 29),
        resolved_date=date(2026, 6, 3),
    )
    
    document = OverridesDocument(
        issue_number=80,
        top_3_now=(),
        scorecards=(),
        governance=gov,
        decisions=(dec,),
    )
    
    reports_root = tmp_path / "reports"
    saved_path = save_overrides(EDITION_NAME, document, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)
    
    assert loaded is not None
    assert loaded.issue_number == 80
    assert loaded.governance.dfd_date == date(2026, 6, 3)
    assert loaded.governance.dfd_history == (date(2026, 3, 31), date(2026, 6, 3))
    assert loaded.governance.escalation_active is True
    assert loaded.governance.escalation_workstreams == ("acme-adventure-syn-a-100-ramp-readiness-bios",)
    assert loaded.governance.lt_commitment == "EXN LT committed to deliver P0 items"
    assert loaded.governance.lt_commitment_date == date(2026, 5, 22)
    
    assert len(loaded.decisions) == 1
    assert loaded.decisions[0].id == "contoso-023-prod-gate"
    assert loaded.decisions[0].workstream == "contoso-pilot-readiness"
    assert loaded.decisions[0].type == "gate"
    assert loaded.decisions[0].status == "active"
    
    # Test adapter
    dec_entries = read_governance_decisions_from_overrides(loaded, program_id="demo")
    assert len(dec_entries) == 1
    assert dec_entries[0].id == "contoso-023-prod-gate"
    assert dec_entries[0].program_id == "demo"
    assert dec_entries[0].title == "[GATE] Decision contoso-023-prod-gate"
    assert dec_entries[0].status == DecisionStatus.DECIDED
    assert dec_entries[0].decision == "EXN LT committed to UD upgrade-alert intervention."
    assert dec_entries[0].decided_by == "Chris Chen"
    assert dec_entries[0].decision_date == date(2026, 5, 29)
    assert dec_entries[0].review_by == date(2026, 6, 3)
    
    content = saved_path.read_text(encoding="utf-8")
    assert "governance:" in content
    assert "dfd_date: '2026-06-03'" in content or "dfd_date: 2026-06-03" in content
    assert "escalation_active: true" in content
    assert "decisions:" in content
    assert "id: contoso-023-prod-gate" in content


# ---------------------------------------------------------------------------
# FR-SG-18: DimensionOverride provenance fields roundtrip
# ---------------------------------------------------------------------------

def test_dimension_override_provenance_fields_roundtrip(tmp_path: Path) -> None:
    """FR-SG-18: owner, reason, review_date, expiry_date survive save/load."""
    document = OverridesDocument(
        issue_number=81,
        top_3_now=(),
        scorecards=(
            ScorecardOverrides(
                name="Acme Readiness",
                dimensions=(
                    DimensionOverride(
                        name="Deployment Velocity",
                        risk=RiskLevel.MEDIUM,
                        owner="jsmith",
                        reason="Waiting for firmware sign-off",
                        review_date=date(2026, 5, 20),
                        expiry_date=date(2026, 6, 1),
                    ),
                ),
            ),
        ),
    )

    reports_root = tmp_path / "reports"
    saved_path = save_overrides(EDITION_NAME, document, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)

    assert loaded is not None
    dim = loaded.scorecards[0].dimensions[0]
    assert dim.owner == "jsmith"
    assert dim.reason == "Waiting for firmware sign-off"
    assert dim.review_date == date(2026, 5, 20)
    assert dim.expiry_date == date(2026, 6, 1)

    content = saved_path.read_text(encoding="utf-8")
    assert "owner: jsmith" in content
    assert "reason:" in content
    assert "review_date:" in content
    assert "expiry_date:" in content


def test_dimension_override_provenance_fields_optional(tmp_path: Path) -> None:
    """FR-SG-18: provenance fields are optional — existing overrides without them still load."""
    document = OverridesDocument(
        issue_number=82,
        top_3_now=(),
        scorecards=(
            ScorecardOverrides(
                name="Acme Readiness",
                dimensions=(
                    DimensionOverride(
                        name="Deployment Velocity",
                        risk=RiskLevel.LOW,
                    ),
                ),
            ),
        ),
    )

    reports_root = tmp_path / "reports"
    save_overrides(EDITION_NAME, document, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)

    assert loaded is not None
    dim = loaded.scorecards[0].dimensions[0]
    assert dim.owner is None
    assert dim.reason is None
    assert dim.review_date is None
    assert dim.expiry_date is None


def test_dimension_override_provenance_null_dates_roundtrip(tmp_path: Path) -> None:
    """FR-SG-18: review_date and expiry_date absent in YAML → None after load."""
    document = OverridesDocument(
        issue_number=83,
        top_3_now=(),
        scorecards=(
            ScorecardOverrides(
                name="Acme Readiness",
                dimensions=(
                    DimensionOverride(
                        name="Deployment Velocity",
                        risk=RiskLevel.HIGH,
                        owner="operator",
                        reason="Escalation active",
                    ),
                ),
            ),
        ),
    )

    reports_root = tmp_path / "reports"
    save_overrides(EDITION_NAME, document, reports_root)
    loaded = load_overrides(EDITION_NAME, reports_root)

    assert loaded is not None
    dim = loaded.scorecards[0].dimensions[0]
    assert dim.owner == "operator"
    assert dim.reason == "Escalation active"
    assert dim.review_date is None
    assert dim.expiry_date is None


def test_load_overrides_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    path = reports_root / EDITION_NAME / "overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("issue_number: '1'\nscorecards: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="issue_number must be an integer"):
        load_overrides(EDITION_NAME, reports_root)


def test_load_overrides_rejects_non_string_focused_include_entry(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    path = reports_root / EDITION_NAME / "overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("issue_number: 1\nfocused_include:\n  - 1\nscorecards: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="focused_include entries must be strings"):
        load_overrides(EDITION_NAME, reports_root)


def test_load_overrides_rejects_non_string_escalation_workstream(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    path = reports_root / EDITION_NAME / "overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "issue_number: 1\ngovernance:\n  escalation_workstreams:\n    - 1\nscorecards: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="governance.escalation_workstreams entries must be strings"):
        load_overrides(EDITION_NAME, reports_root)


def test_load_overrides_rejects_non_string_top_3_owner(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    path = reports_root / EDITION_NAME / "overrides.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "issue_number: 1\ntop_3_now:\n  - type: decision\n    text: Need LT call\n    owner: 1\nscorecards: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="top_3_now.owner must be a string"):
        load_overrides(EDITION_NAME, reports_root)
