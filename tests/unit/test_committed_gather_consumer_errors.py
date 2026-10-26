from pathlib import Path
from unittest.mock import Mock

import pytest

import src.commands.doctor_checks.storage_checks as storage_checks
from src.commands import gather
from src.core import tag_hygiene
from src.core.exceptions import ConfigError
from src.core.gather_run_manifest import GATHER_MUTATION_DOMAIN
from src.core.workspace_lease import acquire_lease, release_lease
from tests.unit.test_gather_run_lifecycle import _fixture_program


def _invalid_manifest(*args, **kwargs):
    raise ValueError("sensitive manifest detail")


@pytest.mark.parametrize(
    "selector", ["resolve_latest_committed_manifest", "resolve_latest_full_committed_manifest"],
)
def test_gather_invalid_prior_evidence_releases_lease_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str,
) -> None:
    program, workstreams = _fixture_program()
    monkeypatch.setattr(gather, "_load_program_context", lambda *args, **kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *args, **kwargs: (14, 30))
    monkeypatch.setattr(gather, "resolve_latest_committed_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(gather, selector, _invalid_manifest)
    loader = Mock()

    with pytest.raises(ConfigError, match="(?i)committed gather") as failure:
        gather.gather_program("acme", programs_root=tmp_path, loader=loader)

    assert "sensitive manifest detail" not in str(failure.value)
    loader.assert_not_called()
    assert not tuple(tmp_path.rglob("manifest.json"))
    lease = acquire_lease(
        "acme", "retry", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=tmp_path,
    )
    release_lease(lease, programs_root=tmp_path)


def test_doctor_invalid_committed_evidence_is_explicit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_checks, "resolve_latest_committed_manifest", _invalid_manifest)

    result = storage_checks._gather_completeness_oracle_check("acme", programs_root=tmp_path)

    assert result.status == "error"
    assert result.metadata["manifest_invalid"] is True
    assert "sensitive manifest detail" not in result.detail


def test_leakage_invalid_committed_evidence_has_no_measured_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tag_hygiene, "resolve_latest_committed_manifest", _invalid_manifest)

    result = tag_hygiene.compute_leakage_rate("acme", programs_root=tmp_path)

    assert result.value is None
    assert result.confidence is tag_hygiene.ValueConfidence.UNAVAILABLE
    assert result.authoritative_scope_items is None
    assert "invalid" in result.detail.lower()
    assert "sensitive manifest detail" not in result.detail
