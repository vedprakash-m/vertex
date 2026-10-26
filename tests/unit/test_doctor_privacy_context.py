from __future__ import annotations

from pathlib import Path

import pytest

from src.commands.doctor_checks.privacy_checks import run_privacy_doctor
from src.core import privacy_scan
from src.core.exceptions import ConfigError
from src.core.knowledge_store import SHARED_KNOWLEDGE_ROOT, get_shared_knowledge_root
from src.core.profile_encryption import SensitiveProfileFileStatus


def _write_profiles(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "profiles:\n" + "".join(f"  - alias: synthetic_{index}\n" for index in range(count)),
        encoding="utf-8",
    )


@pytest.fixture
def ambient_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    sentinel = tmp_path / "ambient" / "knowledge" / "people_profiles.yaml"
    _write_profiles(sentinel, 3)
    ambient_profile = SHARED_KNOWLEDGE_ROOT / "people_profiles.yaml"
    exists = Path.exists
    inspect = privacy_scan.inspect_people_profiles_file
    probes: list[Path] = []

    def synthetic_exists(path: Path) -> bool:
        if path == ambient_profile:
            probes.append(path)
            return sentinel.exists()
        return exists(path)

    def synthetic_inspect(path: Path) -> SensitiveProfileFileStatus:
        return inspect(sentinel if path == ambient_profile else path)

    monkeypatch.setattr(Path, "exists", synthetic_exists)
    monkeypatch.setattr(privacy_scan, "inspect_people_profiles_file", synthetic_inspect)
    return probes


@pytest.mark.parametrize("shared_state", ["missing", "empty", "plaintext", "invalid"])
def test_profile_scan_uses_selected_workspace(
    tmp_path: Path, ambient_probes: list[Path], shared_state: str,
) -> None:
    programs_root = tmp_path / "selected" / "programs"
    shared_profile = get_shared_knowledge_root(programs_root) / "people_profiles.yaml"
    program_profile = programs_root / "synthetic" / "knowledge" / "people_profiles.yaml"
    _write_profiles(program_profile, 1)
    if shared_state != "missing":
        _write_profiles(shared_profile, 1 if shared_state == "plaintext" else 0)
    if shared_state == "invalid":
        shared_profile.write_text("profiles: invalid\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="Expected 'profiles' list"):
            privacy_scan.find_plaintext_sensitive_profile_files("synthetic", programs_root=programs_root)
    else:
        result = privacy_scan.find_plaintext_sensitive_profile_files("synthetic", programs_root=programs_root)
        assert result.plaintext_profile_count == (2 if shared_state == "plaintext" else 1)
        assert result.plaintext_relative_paths == (
            ("knowledge/people_profiles.yaml", "programs/synthetic/knowledge/people_profiles.yaml")
            if shared_state == "plaintext" else ("programs/synthetic/knowledge/people_profiles.yaml",)
        )
        assert result.encrypted_profile_count == 0
        assert result.encrypted_relative_paths == ()
    assert ambient_probes == []


@pytest.mark.parametrize("override_exists", [False, True])
def test_profile_scan_preserves_explicit_shared_override(
    tmp_path: Path, ambient_probes: list[Path], override_exists: bool,
) -> None:
    programs_root = tmp_path / "selected" / "programs"
    override = tmp_path / "override"
    _write_profiles(get_shared_knowledge_root(programs_root) / "people_profiles.yaml", 2)
    if override_exists:
        _write_profiles(override / "people_profiles.yaml", 1)

    result = privacy_scan.find_plaintext_sensitive_profile_files(
        "synthetic", programs_root=programs_root, shared_knowledge_root=override,
    )

    assert result.plaintext_profile_count == int(override_exists)
    assert result.plaintext_relative_paths == ((str(override / "people_profiles.yaml"),) if override_exists else ())
    assert ambient_probes == []


def test_profile_scan_preserves_default_workspace_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_profile = get_shared_knowledge_root(privacy_scan.PROGRAMS_ROOT) / "people_profiles.yaml"
    program_profile = privacy_scan.PROGRAMS_ROOT / "synthetic" / "knowledge" / "people_profiles.yaml"
    sentinel = tmp_path / "default" / "people_profiles.yaml"
    _write_profiles(sentinel, 2)
    exists = Path.exists
    inspect = privacy_scan.inspect_people_profiles_file
    inspected: list[Path] = []

    def synthetic_exists(path: Path) -> bool:
        if path == shared_profile:
            return True
        if path == program_profile:
            return False
        return exists(path)

    def synthetic_inspect(path: Path) -> SensitiveProfileFileStatus:
        assert path == shared_profile
        inspected.append(path)
        return inspect(sentinel)

    monkeypatch.setattr(Path, "exists", synthetic_exists)
    monkeypatch.setattr(privacy_scan, "inspect_people_profiles_file", synthetic_inspect)

    result = privacy_scan.find_plaintext_sensitive_profile_files("synthetic")

    assert inspected == [shared_profile]
    assert result.plaintext_profile_count == 2
    assert result.plaintext_relative_paths == ("knowledge/people_profiles.yaml",)


@pytest.mark.parametrize("storage", ["encrypted", "unexpected"])
def test_profile_scan_preserves_storage_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambient_probes: list[Path], storage: str,
) -> None:
    programs_root = tmp_path / "selected" / "programs"
    profile = get_shared_knowledge_root(programs_root) / "people_profiles.yaml"
    _write_profiles(profile, 1)

    def inspect(path: Path) -> SensitiveProfileFileStatus:
        assert path == profile
        return SensitiveProfileFileStatus(path=path, storage=storage, profile_count=1)

    monkeypatch.setattr(privacy_scan, "inspect_people_profiles_file", inspect)
    if storage == "unexpected":
        with pytest.raises(ConfigError, match="Unexpected people_profiles.yaml storage state"):
            privacy_scan.find_plaintext_sensitive_profile_files("synthetic", programs_root=programs_root)
    else:
        result = privacy_scan.find_plaintext_sensitive_profile_files("synthetic", programs_root=programs_root)
        assert result.plaintext_profile_count == 0
        assert result.plaintext_relative_paths == ()
        assert result.encrypted_profile_count == 1
        assert result.encrypted_relative_paths == ("knowledge/people_profiles.yaml",)
    assert ambient_probes == []


@pytest.mark.parametrize("shared_state", ["missing", "empty", "plaintext", "invalid"])
def test_privacy_doctor_uses_selected_workspace(
    tmp_path: Path, ambient_probes: list[Path], shared_state: str,
) -> None:
    workspace = tmp_path / "selected"
    programs_root = workspace / "programs"
    program_dir = programs_root / "synthetic"
    edition_dir = program_dir / "editions"
    edition_dir.mkdir(parents=True)
    (edition_dir / "synthetic_weekly.yaml").write_text(
        "id: synthetic_weekly\nprogram_id: synthetic\nname: Synthetic Weekly\n"
        "type: newsletter\naltitude: program\ncadence: weekly\n",
        encoding="utf-8",
    )
    (program_dir / "program.yaml").write_text("id: synthetic\nname: Synthetic\n", encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text("workstreams: []\n", encoding="utf-8")
    (program_dir / "scorecards.yaml").write_text("scorecards: []\n", encoding="utf-8")
    shared_profile = get_shared_knowledge_root(programs_root) / "people_profiles.yaml"
    if shared_state != "missing":
        _write_profiles(shared_profile, 1 if shared_state == "plaintext" else 0)
    if shared_state == "invalid":
        shared_profile.write_text("profiles: invalid\n", encoding="utf-8")

    report = run_privacy_doctor(
        edition_name="synthetic_weekly", editions_root=workspace / "editions", programs_root=programs_root,
    )

    scan, profiles = report.checks
    assert scan.label == "Privacy Scan"
    assert scan.status == "ok"
    assert scan.detail == "Scanned 0 journal files; no obvious credential patterns found."
    assert profiles.label == "Privacy Profiles"
    assert profiles.status == {"plaintext": "warn", "invalid": "fail"}.get(shared_state, "ok")
    if shared_state == "plaintext":
        assert "1 sensitive profile entry remain plaintext in knowledge/people_profiles.yaml" in profiles.detail
    elif shared_state == "invalid":
        assert "Expected 'profiles' list" in profiles.detail
    else:
        assert profiles.detail == "No populated plaintext people_profiles.yaml files detected for this program."
    assert ambient_probes == []
