from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.overrides_store import OverridesDocument
from tests.support import report_test_setup


def _write_program_tree(root: Path, *, program_id: str = "acme", include_workstreams: bool = True) -> Path:
    program_root = root / program_id
    program_root.mkdir(parents=True, exist_ok=True)
    (program_root / "program.yaml").write_text("id: acme\n", encoding="utf-8")
    if include_workstreams:
        (program_root / "workstreams.yaml").write_text("workstreams: []\n", encoding="utf-8")
    return program_root


def test_validate_cached_program_tree_accepts_required_bundle_files(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_tree(programs_root)

    report_test_setup._validate_cached_program_tree(programs_root)


def test_validate_cached_program_tree_rejects_missing_workstreams_yaml(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_tree(programs_root, include_workstreams=False)

    with pytest.raises(FileNotFoundError, match="acme/workstreams.yaml"):
        report_test_setup._validate_cached_program_tree(programs_root)


def test_validate_cached_program_tree_ignores_template_container_dir(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "_templates").mkdir(parents=True)
    _write_program_tree(programs_root)

    report_test_setup._validate_cached_program_tree(programs_root)


def test_seed_overrides_are_loaded_from_the_selected_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = tmp_path / "sealed" / "reports"
    edition = reports_root / "synthetic_weekly"
    edition.mkdir(parents=True)
    (edition / "config.yaml").write_text(
        "scorecards:\n  - name: Sealed\n    dimensions:\n      - name: Scope\n", encoding="utf-8",
    )
    (edition / "overrides.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(report_test_setup, "_LOCAL_SOURCE_CACHE", tmp_path / "ambient-cache")
    monkeypatch.setattr(
        report_test_setup, "_SEED_OVERRIDES",
        {"ambient_weekly": OverridesDocument(issue_number=None, top_3_now=(), scorecards=())},
    )
    saved: list[tuple[str, OverridesDocument, Path]] = []

    def save(edition_name: str, document: OverridesDocument, *, reports_root: Path) -> None:
        saved.append((edition_name, document, reports_root))

    monkeypatch.setattr(report_test_setup, "save_overrides", save)
    report_test_setup.reset_overrides_to_seed_state(reports_root)

    assert len(saved) == 1
    edition_name, document, selected_root = saved[0]
    assert edition_name == "synthetic_weekly"
    assert selected_root == reports_root
    assert document.scorecards[0].name == "Sealed"
    assert document.scorecards[0].dimensions[0].name == "Scope"
    assert document.scorecards[0].dimensions[0].risk is None


def test_cached_program_validation_requires_a_root_but_allows_nonprogram_containers(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError, match="Cached programs root missing"):
        report_test_setup._validate_cached_program_tree(missing)
    (missing / "container").mkdir(parents=True)
    report_test_setup._validate_cached_program_tree(missing)


@pytest.mark.parametrize("configured", [False, True])
def test_provider_disabling_is_explicit_and_limited_to_the_staged_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool,
) -> None:
    reports = tmp_path / "reports"
    (reports / "schemas").mkdir(parents=True)
    monkeypatch.setattr(report_test_setup, "_SEED_OVERRIDES", None)
    if not configured:
        with pytest.raises(pytest.skip.Exception, match="Requires local"):
            report_test_setup.disable_kusto_in_report_copy(reports)
        return
    program = tmp_path / "programs" / "acme"
    program.mkdir(parents=True)
    path = program / "program.yaml"
    path.write_text(
        "id: acme\nkusto:\n  enabled: true\nm365:\n  enabled: true\nai:\n  enabled: true\n",
        encoding="utf-8",
    )
    report_test_setup.disable_kusto_in_report_copy(reports)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert payload["id"] == "acme"
    assert all(payload[provider]["enabled"] is False for provider in ("kusto", "m365", "ai"))


@pytest.mark.parametrize("state", [{"mode": "primary"}, {"mode": "legacy"}, ["synthetic"]])
def test_normalization_only_rewrites_staged_identity_and_backend_fields(
    tmp_path: Path, state: dict[str, str] | list[str],
) -> None:
    program = tmp_path / "program"
    program.mkdir()
    config = program / "program.yaml"
    config.write_text("id: synthetic\nado:\n  organization: contoso\n", encoding="utf-8")
    sor = program / "fact_store_sor.yaml"
    sor.write_text(yaml.safe_dump(state), encoding="utf-8")
    report_test_setup._normalize_program_org(program)
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == {
        "id": "synthetic", "ado": {"organization": "your-org"},
    }
    assert yaml.safe_load(sor.read_text(encoding="utf-8")) == (
        {"mode": "legacy"} if isinstance(state, dict) else state
    )
    assert not (program / "workstreams.yaml").exists()


def test_normalization_ignores_absent_programs_and_uses_placeholder_identities(tmp_path: Path) -> None:
    report_test_setup._normalize_program_org(tmp_path)
    assert not list(tmp_path.iterdir())
    assert report_test_setup._sanitize_yaml_values({
        "owner": "Synthetic Person", "email": "synthetic@example.com",
        "cc": ["synthetic@microsoft.com"],
    }) == {
        "owner": "maintainer", "email": "maintainer@example.com",
        "cc": ["maintainer@example.com"],
    }
