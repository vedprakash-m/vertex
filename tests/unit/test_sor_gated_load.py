"""Track B.5 regression tests (specs/fix-data-flow.md §6.2b) for the shared
`sor_gated_family_load` helper — extracted after Track B proved the
SoR-gated overlay pattern for a second family (risk, mirroring milestone's
original shape). Parameterizes the same contract across two independent
families (risk-shaped and a synthetic decision-shaped family) to prove the
extraction is genuinely generic, not risk-specific.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.core.exceptions import ConfigError
from src.core.stages import sor_gated_load as sor_gated_load_module
from src.core.stages.sor_gated_load import sor_gated_family_load


@dataclass(frozen=True)
class _FakeRecord:
    id: str


@dataclass(frozen=True)
class _FakeLineage:
    source_document_key: str | None
    approval_event_id: str | None


@dataclass(frozen=True)
class _FakeAssessment:
    record: _FakeRecord
    lineage: _FakeLineage | None = None


class _FakeReality:
    def __init__(self, records: tuple[_FakeAssessment, ...]) -> None:
        self._records = records

    def items(self) -> tuple[_FakeAssessment, ...]:
        return self._records


def _load_program_reality_stub(records: tuple[_FakeAssessment, ...]):
    def _loader(program_id: str, *, programs_root, as_of, edition_name, archive_root):
        return _FakeReality(records)
    return _loader


@pytest.mark.parametrize("family,cross_check_label", [("judgment", "risk"), ("workitem.state", "action")])
def test_legacy_mode_calls_legacy_loader_only(monkeypatch: pytest.MonkeyPatch, family: str, cross_check_label: str) -> None:
    monkeypatch.setattr(sor_gated_load_module, "resolve_family_sor_mode", lambda *a, **kw: "legacy")

    calls = {"legacy": 0, "reality": 0}

    def legacy_loader():
        calls["legacy"] += 1
        return (_FakeRecord("x1"),)

    def reality_accessor(reality):
        calls["reality"] += 1
        return reality.items()

    records, assessments, warnings, lineage = sor_gated_family_load(
        program_id="acme",
        family=family,
        programs_root="unused",
        reality_accessor=reality_accessor,
        legacy_loader=legacy_loader,
        allow_legacy_rollback_env=f"VERTEX_ALLOW_LEGACY_{cross_check_label.upper()}_ROLLBACK",
        cross_check_label=cross_check_label,
    )

    assert records == (_FakeRecord("x1"),)
    assert assessments is None
    assert warnings == ()
    assert lineage is None
    assert calls == {"legacy": 1, "reality": 0}


@pytest.mark.parametrize("family,cross_check_label", [("judgment", "decision"), ("workitem.state", "workstream")])
def test_non_legacy_mode_reads_via_reality_and_preserves_lineage(
    monkeypatch: pytest.MonkeyPatch, family: str, cross_check_label: str,
) -> None:
    monkeypatch.setattr(sor_gated_load_module, "resolve_family_sor_mode", lambda *a, **kw: "shadow")

    assessment = _FakeAssessment(
        record=_FakeRecord("y1"),
        lineage=_FakeLineage(source_document_key="email:abc", approval_event_id="evt-1"),
    )

    records, assessments, warnings, lineage = sor_gated_family_load(
        program_id="acme",
        family=family,
        programs_root="unused",
        reality_accessor=lambda reality: reality.items(),
        legacy_loader=lambda: (),
        allow_legacy_rollback_env="VERTEX_ALLOW_LEGACY_ROLLBACK",
        cross_check_label=cross_check_label,
        load_program_reality=_load_program_reality_stub((assessment,)),
    )

    assert records == (_FakeRecord("y1"),)
    assert assessments == (assessment,)
    assert warnings == ()
    assert lineage == {"y1": {"source_document_key": "email:abc", "approval_event_id": "evt-1"}}


def test_empty_set_cross_check_warns_when_legacy_has_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sor_gated_load_module, "resolve_family_sor_mode", lambda *a, **kw: "primary")

    records, assessments, warnings, lineage = sor_gated_family_load(
        program_id="acme",
        family="judgment",
        programs_root="unused",
        reality_accessor=lambda reality: reality.items(),
        legacy_loader=lambda: (_FakeRecord("legacy-1"),),
        allow_legacy_rollback_env="VERTEX_ALLOW_LEGACY_ROLLBACK",
        cross_check_label="risk",
        load_program_reality=_load_program_reality_stub(()),
    )

    assert records == ()
    assert len(warnings) == 1
    assert "cross-check" in warnings[0]
    assert "legacy source has 1" in warnings[0]


def test_unexpected_reality_error_raises_configerror_without_rollback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sor_gated_load_module, "resolve_family_sor_mode", lambda *a, **kw: "shadow")
    monkeypatch.delenv("VERTEX_ALLOW_LEGACY_TEST_ROLLBACK", raising=False)

    def _broken_loader(*_a, **_kw):
        raise RuntimeError("boom")

    with pytest.raises(ConfigError):
        sor_gated_family_load(
            program_id="acme",
            family="judgment",
            programs_root="unused",
            reality_accessor=lambda reality: reality.items(),
            legacy_loader=lambda: (),
            allow_legacy_rollback_env="VERTEX_ALLOW_LEGACY_TEST_ROLLBACK",
            cross_check_label="risk",
            load_program_reality=_broken_loader,
        )


def test_unexpected_reality_error_falls_back_to_legacy_when_rollback_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sor_gated_load_module, "resolve_family_sor_mode", lambda *a, **kw: "shadow")
    monkeypatch.setenv("VERTEX_ALLOW_LEGACY_TEST_ROLLBACK", "1")

    def _broken_loader(*_a, **_kw):
        raise RuntimeError("boom")

    records, assessments, warnings, lineage = sor_gated_family_load(
        program_id="acme",
        family="judgment",
        programs_root="unused",
        reality_accessor=lambda reality: reality.items(),
        legacy_loader=lambda: (_FakeRecord("fallback-1"),),
        allow_legacy_rollback_env="VERTEX_ALLOW_LEGACY_TEST_ROLLBACK",
        cross_check_label="risk",
        load_program_reality=_broken_loader,
    )

    assert records == (_FakeRecord("fallback-1"),)
    assert "degraded to legacy" in warnings[0]


def test_config_error_in_legacy_mode_returns_empty_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sor_gated_load_module, "resolve_family_sor_mode", lambda *a, **kw: "legacy")

    def _broken_legacy_loader():
        raise ConfigError("bad config")

    records, assessments, warnings, lineage = sor_gated_family_load(
        program_id="acme",
        family="judgment",
        programs_root="unused",
        reality_accessor=lambda reality: reality.items(),
        legacy_loader=_broken_legacy_loader,
        allow_legacy_rollback_env="VERTEX_ALLOW_LEGACY_ROLLBACK",
        cross_check_label="risk",
    )

    assert records == ()
    assert assessments is None
    assert lineage is None
    assert "skipped" in warnings[0]
