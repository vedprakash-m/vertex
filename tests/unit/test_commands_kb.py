from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess

from typer.testing import CliRunner

from cli import app
from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.client import AIClientError
from src.commands import kb as kb_module
from src.ai.llm_trace import AITraceContext
from src.core.kb_changelog import build_kb_changelog_report, render_kb_changelog_report
from src.core.profile_encryption import encrypt_people_profiles_file


runner = CliRunner()


class _FakeKeyring:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._values[(service_name, username)] = password


def test_build_kb_changelog_report_detects_people_changes(tmp_path: Path) -> None:
    repo_root = tmp_path
    knowledge_path = repo_root / "programs" / "demo" / "knowledge"
    knowledge_path.mkdir(parents=True)
    (knowledge_path / "people_directory.yaml").write_text("schema_version: \"1.0\"\npeople: []\n", encoding="utf-8")

    log_output = "abc1234\t2026-05-08T18:00:00+00:00\n"
    current_payload = (
        'schema_version: "1.0"\n'
        'people:\n'
        '  - alias: demo\n'
        '    title: Principal PM\n'
        '    team_ids: [platform]\n'
        '  - alias: hire\n'
        '    title: PM\n'
        '    team_ids: [growth]\n'
    )
    previous_payload = (
        'schema_version: "1.0"\n'
        'people:\n'
        '  - alias: demo\n'
        '    title: Senior PM\n'
        '    team_ids: [legacy]\n'
        '  - alias: stale\n'
        '    title: Lead\n'
        '    team_ids: [ops]\n'
    )

    def fake_git(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert cwd == repo_root
        rendered = " ".join(command)
        if "git log" in rendered:
            return subprocess.CompletedProcess(command, 0, stdout=log_output, stderr="")
        if command[:2] == ["git", "show"] and command[2].startswith("abc1234^"):
            return subprocess.CompletedProcess(command, 0, stdout=previous_payload, stderr="")
        if command[:2] == ["git", "show"] and command[2].startswith("abc1234:"):
            return subprocess.CompletedProcess(command, 0, stdout=current_payload, stderr="")
        raise AssertionError(f"Unexpected git command: {command}")

    report = build_kb_changelog_report(
        program_id="demo",
        since_week="2026-W18",
        repo_root=repo_root,
        git_runner=fake_git,
    )

    rendered = render_kb_changelog_report(report)

    assert report.since_date == date(2026, 4, 27)
    assert len(report.entries) == 4
    assert "+ hire: added (PM; teams: growth)" in rendered
    assert "- stale: removed (Lead; teams: ops)" in rendered
    assert "~ demo: title Senior PM -> Principal PM" in rendered
    assert "~ demo: teams legacy -> platform" in rendered


def test_kb_changelog_cli_renders_report(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_dir = programs_root / "demo" / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "people_directory.yaml").write_text("schema_version: \"1.0\"\npeople: []\n", encoding="utf-8")

    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.build_kb_changelog_report",
        lambda program_id, since_week, repo_root: build_kb_changelog_report(
            program_id=program_id,
            since_week=since_week,
            repo_root=repo_root,
            git_runner=lambda command, cwd: subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "def5678\t2026-05-09T10:00:00+00:00\n"
                    if command[:3] == ["git", "log", "--reverse"]
                    else (
                        'schema_version: "1.0"\npeople:\n  - alias: newhire\n    title: PM\n    team_ids: [growth]\n'
                        if command[:2] == ["git", "show"] and command[2].startswith("def5678:")
                        else 'schema_version: "1.0"\npeople: []\n'
                    )
                ),
                stderr="",
            ),
        ),
    )

    result = runner.invoke(app, ["kb", "changelog", "--program", "demo", "--since", "2026-W18"])

    assert result.exit_code == 0
    assert "KB Changelog: demo" in result.stdout
    assert "+ newhire: added (PM; teams: growth)" in result.stdout


def test_kb_changelog_cli_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_dir = programs_root / "demo" / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "people_directory.yaml").write_text("schema_version: \"1.0\"\npeople: []\n", encoding="utf-8")

    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.build_kb_changelog_report",
        lambda program_id, since_week, repo_root: build_kb_changelog_report(
            program_id=program_id,
            since_week=since_week,
            repo_root=repo_root,
            git_runner=lambda command, cwd: subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "def5678\t2026-05-09T10:00:00+00:00\n"
                    if command[:3] == ["git", "log", "--reverse"]
                    else (
                        'schema_version: "1.0"\npeople:\n  - alias: newhire\n    title: PM\n    team_ids: [growth]\n'
                        if command[:2] == ["git", "show"] and command[2].startswith("def5678:")
                        else 'schema_version: "1.0"\npeople: []\n'
                    )
                ),
                stderr="",
            ),
        ),
    )

    json_result = runner.invoke(app, ["kb", "changelog", "--program", "demo", "--since", "2026-W18", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "demo"
    assert payload["since_week"] == "2026-W18"
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["alias"] == "newhire"

    csv_result = runner.invoke(app, ["kb", "changelog", "--program", "demo", "--since", "2026-W18", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "program_id,since_week,since_date,commit_sha,committed_at,alias,change_type,before,after"
    assert any(
        line.startswith("demo,2026-W18,2026-04-27,def5678,2026-05-09T10:00:00+00:00,newhire,new_hire,,PM; teams: growth")
        for line in lines[1:]
    )


def test_kb_update_preview_renders_diff_without_writing(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "update", "Set demo title to Principal PM", "--program", "demo", "--no-ai"])

    people_directory_path = programs_root / "demo" / "knowledge" / "people_directory.yaml"
    assert result.exit_code == 0
    assert "KB update preview: demo" in result.stdout
    assert "Planner: deterministic" in result.stdout
    assert "knowledge/people_directory.yaml" in result.stdout
    assert "+  title: Principal PM" in result.stdout or "+    title: Principal PM" in result.stdout
    assert "Preview only. Re-run with --apply" in result.stdout
    assert "Principal PM" not in people_directory_path.read_text(encoding="utf-8")


def test_kb_update_apply_writes_yaml_and_audit(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path, team_ids=("platform",))
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "update", "Add team platform to demo", "--program", "demo", "--apply", "--no-ai"])

    people_directory_path = programs_root / "demo" / "knowledge" / "people_directory.yaml"
    audit_path = programs_root / "demo" / "journal" / "kb_edits.jsonl"
    assert result.exit_code == 0
    assert "Applied 1 file(s)." in result.stdout
    assert people_directory_path.exists()
    assert "platform" in people_directory_path.read_text(encoding="utf-8")
    assert audit_path.exists()
    audit_record = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert audit_record["correction"] == "Add team platform to demo"
    assert audit_record["planner"] == "deterministic"
    assert audit_record["files"][0]["path"] == "knowledge/people_directory.yaml"


def test_kb_update_blocks_invalid_referential_change(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "update", "Add team missing to demo", "--program", "demo", "--no-ai"])

    people_directory_path = programs_root / "demo" / "knowledge" / "people_directory.yaml"
    assert result.exit_code != 0
    assert "Unknown team_id 'missing' referenced by person 'demo'." in result.output
    assert "missing" not in people_directory_path.read_text(encoding="utf-8")


def test_kb_update_surfaces_vertex_first_ai_guidance_for_unsupported_correction(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.kb._plan_kb_update_with_ai", lambda **kwargs: None)

    result = runner.invoke(app, ["kb", "update", "Do something broad and fuzzy", "--program", "demo"])

    assert result.exit_code != 0
    assert "AZURE_OPENAI_KB_DEPLOYMENT" in result.output
    assert "VERTEX_AI_DEPLOYMENT" in result.output
    assert "AZURE_OPENAI_DEPLOYMENT" in result.output
    assert "supported Vertex deployment aliases" in result.output


def test_kb_update_targets_shared_knowledge_root_when_present(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    shared_knowledge_root = tmp_path / "knowledge"
    shared_knowledge_root.mkdir(parents=True)
    (shared_knowledge_root / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: demo\n'
            '    display_name: Shared Demo\n'
        ),
        encoding="utf-8",
    )
    (shared_knowledge_root / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (shared_knowledge_root / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "update", "Set demo title to Principal PM", "--program", "demo", "--apply", "--no-ai"])

    shared_people_path = shared_knowledge_root / "people_directory.yaml"
    program_people_path = programs_root / "demo" / "knowledge" / "people_directory.yaml"
    assert result.exit_code == 0
    assert "Principal PM" in shared_people_path.read_text(encoding="utf-8")
    assert "Principal PM" not in program_people_path.read_text(encoding="utf-8")


def test_kb_profiles_encrypt_and_decrypt_commands(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    profiles_path = programs_root / "demo" / "knowledge" / "people_profiles.yaml"
    profiles_path.write_text(
        (
            'schema_version: "1.0"\n'
            'profiles:\n'
            '  - alias: demo\n'
            '    comm_style: concise\n'
        ),
        encoding="utf-8",
    )
    fake_keyring = _FakeKeyring()

    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)

    encrypt_result = runner.invoke(app, ["kb", "profiles", "encrypt", "--program", "demo", "--scope", "program"])

    assert encrypt_result.exit_code == 0
    assert "Encrypted 1 sensitive profile entry" in encrypt_result.stdout
    assert "comm_style: concise" not in profiles_path.read_text(encoding="utf-8")

    decrypt_result = runner.invoke(app, ["kb", "profiles", "decrypt", "--program", "demo", "--scope", "program"])

    assert decrypt_result.exit_code == 0
    assert "Decrypted 1 sensitive profile entry" in decrypt_result.stdout
    assert "comm_style: concise" in profiles_path.read_text(encoding="utf-8")


def _write_people_phase0c_fixture(programs_root: Path) -> None:
    # specs/people.md Phase 0c fixture: an alias shared by two programs.
    for program_id, other_alias in (("acme", "acme_only"), ("fabrikam", "fabrikam_only")):
        program_dir = programs_root / program_id
        program_dir.mkdir(parents=True, exist_ok=True)
        (program_dir / "program.yaml").write_text(
            (
                'schema_version: "1.0"\n'
                f'id: "{program_id}"\n'
                f'name: "{program_id.title()}"\n'
                "stakeholder_register:\n"
                "  - alias: shared_owner\n"
                f"  - alias: {other_alias}\n"
            ),
            encoding="utf-8",
        )


def test_kb_people_overlaps_cli_shows_legacy_warning_and_cross_program_alias(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_people_phase0c_fixture(programs_root)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "people", "overlaps"])

    assert result.exit_code == 0
    assert "WARNING: alias-based legacy result; identity not verified." in result.stdout
    assert "shared_owner" in result.stdout
    assert "acme_only" not in result.stdout  # Not an overlap -- only in one program.


def test_kb_people_overlaps_cli_json_envelope(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_people_phase0c_fixture(programs_root)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "people", "overlaps", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "people-query.v1"
    assert payload["confidence_mode"] == "legacy_alias"
    assert payload["items"][0]["alias"] == "shared_owner"
    assert {edge["program_id"] for edge in payload["items"][0]["edges"]} == {"acme", "fabrikam"}


def test_kb_people_programs_cli_finds_alias_across_programs(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_people_phase0c_fixture(programs_root)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "people", "programs", "--person", "shared_owner"])

    assert result.exit_code == 0
    assert "WARNING: alias-based legacy result; identity not verified." in result.stdout
    assert "acme" in result.stdout
    assert "fabrikam" in result.stdout


def test_kb_people_programs_cli_unknown_alias_returns_empty_not_error(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_people_phase0c_fixture(programs_root)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "people", "programs", "--person", "nobody_here"])

    assert result.exit_code == 0
    assert "No legacy accountability references found" in result.stdout


def test_kb_registry_bootstrap_cli_dry_run_creates_nothing(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "registry", "bootstrap", "--customer-boundary-id", "acme-corp"])

    assert result.exit_code == 0
    assert "Dry run" in result.stdout
    assert not (programs_root.parent / "knowledge" / "registry.yaml").exists()


def test_kb_registry_bootstrap_cli_apply_creates_identity_then_status_reports_it(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    bootstrap_result = runner.invoke(app, ["kb", "registry", "bootstrap", "--customer-boundary-id", "acme-corp", "--apply"])

    assert bootstrap_result.exit_code == 0
    assert "Created workspace registry identity" in bootstrap_result.stdout
    assert (programs_root.parent / "knowledge" / "registry.yaml").exists()
    assert (programs_root.parent / "knowledge" / "registry_manifest.json").exists()

    status_result = runner.invoke(app, ["kb", "registry", "status"])
    assert status_result.exit_code == 0
    assert "Customer boundary:   acme-corp" in status_result.stdout

    status_json = runner.invoke(app, ["kb", "registry", "status", "--format", "json"])
    payload = json.loads(status_json.stdout)
    assert payload["bootstrapped"] is True
    assert payload["customer_boundary_id"] == "acme-corp"

    # Re-running bootstrap --apply must not mint a second identity.
    second_bootstrap = runner.invoke(app, ["kb", "registry", "bootstrap", "--customer-boundary-id", "different-tenant", "--apply"])
    assert second_bootstrap.exit_code == 0
    assert "already exists" in second_bootstrap.stdout
    reloaded_status = json.loads(runner.invoke(app, ["kb", "registry", "status", "--format", "json"]).stdout)
    assert reloaded_status["customer_boundary_id"] == "acme-corp"  # Unchanged.


def test_kb_registry_status_cli_reports_no_identity_before_bootstrap(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "registry", "status"])

    assert result.exit_code == 0
    assert "No workspace registry identity yet" in result.stdout


def test_kb_registry_storage_status_cli_reports_local_human(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "registry", "storage-status"])

    assert result.exit_code == 0
    assert "Storage class:         local" in result.stdout
    assert "Qualified for primary: True" in result.stdout
    assert (programs_root.parent / "knowledge" / "registry_capability_status.yaml").exists()


def test_kb_registry_storage_status_cli_json_envelope(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "registry", "storage-status", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["storage_class"] == "local"
    assert payload["qualified_for_primary"] is True


def test_kb_registry_lease_show_cli_reports_no_lease_then_held_lease(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    knowledge_root = programs_root.parent / "knowledge"

    empty_result = runner.invoke(app, ["kb", "registry", "lease", "show"])
    assert empty_result.exit_code == 0
    assert "No registry lease is currently held" in empty_result.stdout

    from src.core.people_registry_lease import acquire_registry_lease

    acquire_registry_lease("some-owner", knowledge_root=knowledge_root)

    held_result = runner.invoke(app, ["kb", "registry", "lease", "show"])
    assert held_result.exit_code == 0
    assert "Owner:          some-owner" in held_result.stdout

    json_result = runner.invoke(app, ["kb", "registry", "lease", "show", "--format", "json"])
    payload = json.loads(json_result.stdout)
    assert payload["lease"]["owner"] == "some-owner"


def test_kb_registry_lease_release_cli_requires_force_and_reason(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    no_force = runner.invoke(app, ["kb", "registry", "lease", "release", "--reason", "test"])
    assert no_force.exit_code != 0

    no_reason = runner.invoke(app, ["kb", "registry", "lease", "release", "--force"])
    assert no_reason.exit_code != 0


def test_kb_registry_lease_release_cli_force_releases_when_authorized(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    knowledge_root = programs_root.parent / "knowledge"

    from src.core.operator_identity import OperatorIdentity
    from src.core.people_registry_lease import acquire_registry_lease

    bootstrap = runner.invoke(app, ["kb", "registry", "bootstrap", "--customer-boundary-id", "acme-corp", "--apply"])
    assert bootstrap.exit_code == 0
    config_path = knowledge_root / "registry.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "directory_steward_principals: []",
            "directory_steward_principals:\n- test_steward\n",
        ),
        encoding="utf-8",
    )
    acquire_registry_lease("stuck-owner", knowledge_root=knowledge_root)

    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda actor: OperatorIdentity(actor=actor, principal="test_steward", machine="test-machine", session="test-session"),
    )

    result = runner.invoke(app, ["kb", "registry", "lease", "release", "--force", "--reason", "stuck writer"])

    assert result.exit_code == 0
    assert "Force-released" in result.stdout
    assert "test_steward" in result.stdout


def _seed_bootstrapped_registry(monkeypatch, tmp_path: Path):
    from src.core.operator_identity import OperatorIdentity

    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda actor: OperatorIdentity(actor=actor, principal="test_operator", machine="test-machine", session="test-session"),
    )
    bootstrap = runner.invoke(app, ["kb", "registry", "bootstrap", "--customer-boundary-id", "acme-corp", "--apply"])
    assert bootstrap.exit_code == 0
    return programs_root


def test_kb_registry_mode_status_cli_before_bootstrap(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["kb", "registry", "mode", "status"])

    assert result.exit_code == 0
    assert "No workspace registry identity yet" in result.stdout


def test_kb_registry_mode_status_cli_json_after_bootstrap(monkeypatch, tmp_path: Path) -> None:
    _seed_bootstrapped_registry(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "registry", "mode", "status", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["bootstrapped"] is True
    assert payload["persisted_write_mode"] == "legacy"
    assert payload["effective_write_mode"] == "legacy"


def test_kb_registry_mode_set_write_mode_cli_dry_run_does_not_persist(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_bootstrapped_registry(monkeypatch, tmp_path)
    knowledge_root = programs_root.parent / "knowledge"

    result = runner.invoke(app, ["kb", "registry", "mode", "set-write-mode", "shadow"])

    assert result.exit_code == 0
    assert "Dry run" in result.stdout
    config_path = knowledge_root / "registry.yaml"
    assert "write_mode: legacy" in config_path.read_text(encoding="utf-8")


def test_kb_registry_mode_set_write_mode_cli_apply_persists(monkeypatch, tmp_path: Path) -> None:
    _seed_bootstrapped_registry(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "registry", "mode", "set-write-mode", "shadow", "--apply"])

    assert result.exit_code == 0
    assert "set to 'shadow'" in result.stdout


def test_kb_registry_mode_set_program_mode_cli_apply_persists(monkeypatch, tmp_path: Path) -> None:
    _seed_bootstrapped_registry(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "registry", "mode", "set-program-mode", "acme", "shadow", "--apply"])

    assert result.exit_code == 0
    assert "Program 'acme' mode set to 'shadow'" in result.stdout


def test_kb_registry_mode_set_program_mode_cli_rejects_primary(monkeypatch, tmp_path: Path) -> None:
    _seed_bootstrapped_registry(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "registry", "mode", "set-program-mode", "acme", "primary", "--apply"])

    assert result.exit_code != 0


def test_kb_registry_mode_promotion_preview_apply_and_rollback(monkeypatch, tmp_path: Path) -> None:
    from src.core.people_registry_identity import load_registry_manifest
    from src.core.people_registry_modes import set_program_mode
    from src.core.people_registry_promotion import (
        PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED,
        PROGRAM_PROMOTION_REQUIRED_CONSUMERS,
        ProgramPromotionConsumerEvidence,
        ProgramPromotionCycleEvidence,
        record_program_promotion_cycle,
        record_program_rollback_restore_drill,
    )

    programs_root = _seed_bootstrapped_registry(monkeypatch, tmp_path)
    knowledge_root = programs_root.parent / "knowledge"
    set_program_mode(knowledge_root, "acme", "shadow", actor="test_operator")
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None
    record_program_rollback_restore_drill(
        knowledge_root,
        "acme",
        generation_id=manifest.generation_id,
        restore_verified=True,
    )
    evidence = ProgramPromotionCycleEvidence(
        generation_id=manifest.generation_id,
        load_succeeded=True,
        load_generation_id=manifest.generation_id,
        consumers=tuple(
            ProgramPromotionConsumerEvidence(
                consumer=consumer,
                generation_id=manifest.generation_id,
                succeeded=True,
            )
            for consumer in PROGRAM_PROMOTION_REQUIRED_CONSUMERS
        ),
        parity_divergence_count=0,
        unresolved_critical_identity_conflicts=0,
        nfr_compliant=True,
    )
    for _ in range(PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED):
        record_program_promotion_cycle(knowledge_root, "acme", evidence)

    preview = runner.invoke(app, ["kb", "registry", "mode", "promote", "acme"])
    assert preview.exit_code == 0
    assert "ready" in preview.stdout

    promoted = runner.invoke(app, ["kb", "registry", "mode", "promote", "acme", "--apply"])
    assert promoted.exit_code == 0
    assert "promoted to 'primary'" in promoted.stdout

    status = runner.invoke(app, ["kb", "registry", "mode", "status", "--program", "acme", "--format", "json"])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["program_promotion_status"]["clean_cycles"] == 5

    rollback = runner.invoke(app, ["kb", "registry", "mode", "rollback", "acme", "--target", "shadow", "--apply"])
    assert rollback.exit_code == 0
    assert "rolled back to 'shadow'" in rollback.stdout


def test_kb_registry_mode_set_flag_cli_apply_persists(monkeypatch, tmp_path: Path) -> None:
    _seed_bootstrapped_registry(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "registry", "mode", "set-flag", "provider_refresh_enabled", "true", "--apply"])

    assert result.exit_code == 0
    assert "provider_refresh_enabled set to True" in result.stdout


def _write_synthetic_knowledge_for_shadow_parity(programs_root: Path, program_id: str) -> None:
    knowledge_dir = programs_root / program_id / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "people_directory.yaml").write_text('schema_version: "1.0"\npeople:\n  - alias: alice\n', encoding="utf-8")
    (knowledge_dir / "teams.yaml").write_text('schema_version: "1.0"\nteams:\n  - id: team-a\n    name: Team A\n', encoding="utf-8")
    (knowledge_dir / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (knowledge_dir / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (knowledge_dir / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")


def test_kb_registry_mode_shadow_parity_cli_preview_reports_zero_divergence(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    _write_synthetic_knowledge_for_shadow_parity(programs_root, "acme")

    result = runner.invoke(app, ["kb", "registry", "mode", "shadow-parity", "acme", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["is_zero_divergence"] is True
    assert payload["legacy_person_count"] == payload["canonical_person_count"] == 1


def test_kb_registry_mode_shadow_parity_cli_record_skips_legacy_mode_program(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_bootstrapped_registry(monkeypatch, tmp_path)
    _write_synthetic_knowledge_for_shadow_parity(programs_root, "acme")

    result = runner.invoke(app, ["kb", "registry", "mode", "shadow-parity", "acme", "--record"])

    assert result.exit_code == 0
    assert "not in shadow/primary mode" in result.stdout
    assert not (programs_root / "acme" / "knowledge" / ".state" / "shadow_parity.json").exists()


def test_kb_update_still_works_when_people_profiles_are_encrypted(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    profiles_path = programs_root / "demo" / "knowledge" / "people_profiles.yaml"
    profiles_path.write_text(
        (
            'schema_version: "1.0"\n'
            'profiles:\n'
            '  - alias: demo\n'
            '    comm_style: concise\n'
        ),
        encoding="utf-8",
    )
    fake_keyring = _FakeKeyring()

    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
    encrypt_people_profiles_file(profiles_path)

    result = runner.invoke(app, ["kb", "update", "Set demo title to Principal PM", "--program", "demo", "--apply", "--no-ai"])

    assert result.exit_code == 0
    assert "Applied 1 file(s)." in result.stdout
    assert "Principal PM" in (programs_root / "demo" / "knowledge" / "people_directory.yaml").read_text(encoding="utf-8")


def test_plan_kb_update_with_ai_falls_back_to_backup_deployment(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    attempts: list[str] = []

    class _RuntimeAIClient:
        def __init__(self, *, deployment: str, temperature: float, budget_usd: float) -> None:
            del temperature, budget_usd
            self.deployment = deployment

        def chat(self, system: str, user: str, *, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            attempts.append(self.deployment)
            if self.deployment == "kb-primary":
                raise AIClientError("primary deployment failed")
            return json.dumps(
                {
                    "operations": [
                        {
                            "path": "knowledge/people_directory.yaml",
                            "action": "set_fields",
                            "match_value": "demo",
                            "fields": {"title": "Principal PM"},
                        }
                    ]
                }
            )

    monkeypatch.setenv("AZURE_OPENAI_KB_DEPLOYMENT", "kb-primary")
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_DEPLOYMENT", raising=False)
    monkeypatch.setenv("VERTEX_AI_BACKUP_DEPLOYMENT", "kb-backup")
    monkeypatch.setattr("src.ai.deployment_fallback.AIClient", _RuntimeAIClient)

    plan = kb_module._plan_kb_update_with_ai(
        correction="Set demo title to Principal PM",
        program_id="demo",
        programs_root=programs_root,
    )

    assert plan is not None
    assert plan.planner == "ai"
    assert plan.operations[0].file_path == "knowledge/people_directory.yaml"
    assert plan.operations[0].field_mapping == {"title": "Principal PM"}
    assert attempts == ["kb-primary", "kb-backup"]


def test_plan_kb_update_with_ai_passes_trace_context_to_runtime_clients(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    seen_trace_contexts: list[object] = []

    class _RuntimeAIClient:
        def __init__(self, *, deployment: str, temperature: float, budget_usd: float, trace_context=None) -> None:
            del deployment, temperature, budget_usd
            seen_trace_contexts.append(trace_context)

        def chat(self, system: str, user: str, *, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            return json.dumps(
                {
                    "operations": [
                        {
                            "path": "knowledge/people_directory.yaml",
                            "action": "set_fields",
                            "match_value": "demo",
                            "fields": {"title": "Principal PM"},
                        }
                    ]
                }
            )

    monkeypatch.setenv("AZURE_OPENAI_KB_DEPLOYMENT", "kb-primary")
    monkeypatch.setattr("src.ai.deployment_fallback.AIClient", _RuntimeAIClient)

    plan = kb_module._plan_kb_update_with_ai(
        correction="Set demo title to Principal PM",
        program_id="demo",
        programs_root=programs_root,
    )

    assert plan is not None
    assert len(seen_trace_contexts) == 1
    trace_context = seen_trace_contexts[0]
    assert isinstance(trace_context, AITraceContext)
    assert trace_context.edition == "demo"
    assert trace_context.caller == "src.commands.kb._plan_kb_update_with_ai"
    assert trace_context.metadata["task_type"] == "kb_update_plan"


def test_build_kb_update_client_passes_trace_context_to_builder(monkeypatch, tmp_path: Path) -> None:
    seen_trace_contexts: list[object] = []

    def _fake_build_kb_update_client(*, trace_context=None):
        seen_trace_contexts.append(trace_context)
        return object()

    monkeypatch.setattr(kb_module, "_build_kb_update_client", _fake_build_kb_update_client)

    trace_context = kb_module._build_kb_update_trace_context(
        program_id="demo",
        programs_root=tmp_path / "programs",
    )
    client = kb_module._build_default_kb_update_client(trace_context=trace_context)

    assert client is not None
    assert seen_trace_contexts == [trace_context]


def test_build_kb_update_client_returns_none_when_invocation_ai_disabled(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_KB_DEPLOYMENT", "kb-primary")
    monkeypatch.setattr(
        "src.commands.kb.FallbackAIClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("FallbackAIClient should not be constructed when AIMode.DISABLED")),
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        client = kb_module._build_kb_update_client()
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert client is None


def test_build_default_kb_update_client_returns_none_when_invocation_ai_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        kb_module,
        "_build_kb_update_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_build_kb_update_client should not be called")),
    )
    trace_context = kb_module._build_kb_update_trace_context(
        program_id="demo",
        programs_root=tmp_path / "programs",
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        client = kb_module._build_default_kb_update_client(trace_context=trace_context)
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert client is None


def _seed_kb_update_layout(tmp_path: Path, *, team_ids: tuple[str, ...] = ()) -> Path:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)

    (program_dir / "program.yaml").write_text(
        (
            'schema_version: "2.0"\n'
            'id: demo\n'
            'name: Demo Program\n'
        ),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        (
            'schema_version: "2.0"\n'
            'workstreams:\n'
            '  - id: ws_demo\n'
            '    name: Demo Workstream\n'
            '    pm_owner: demo\n'
        ),
        encoding="utf-8",
    )
    (program_dir / "scorecards.yaml").write_text(
        (
            'schema_version: "2.0"\n'
            'scorecards:\n'
            '  - name: Demo Scorecard\n'
            '    dimensions:\n'
            '      - name: Demo Dimension\n'
            '        workstream_id: ws_demo\n'
        ),
        encoding="utf-8",
    )
    (knowledge_dir / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: demo\n'
            '    display_name: Demo Author\n'
            '    email: demo@example.com\n'
        ),
        encoding="utf-8",
    )
    teams_block = "\n".join(f"  - id: {team_id}\n    name: {team_id.title()}" for team_id in team_ids)
    (knowledge_dir / "teams.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'teams:\n'
            f"{teams_block}\n" if teams_block else 'schema_version: "1.0"\nteams: []\n'
        ),
        encoding="utf-8",
    )
    (knowledge_dir / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (knowledge_dir / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (knowledge_dir / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")
    return programs_root


def _seed_query_fixture(monkeypatch, tmp_path: Path) -> Path:
    """PPL-W3.1: a bootstrapped registry plus a typed entities/people_directory/teams/memberships
    shared fixture, written directly through the typed writers (not raw YAML) so the fixture stays
    byte-for-byte in sync with the real schema this session's loaders/writers implement."""
    from src.core.people_directory_schema import (
        ContactKind,
        ContactPoint,
        ContactStatus,
        PersonDirectory,
        Team,
        TeamKind,
        write_people_directory,
        write_teams,
    )
    from src.core.people_entity_schema import (
        ENTITIES_SCHEMA_VERSION,
        AliasStatus,
        CanonicalEntity,
        EntitiesDocument,
        EntityAlias,
        EntityStatus,
        write_entities_document,
    )
    from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships

    programs_root = _seed_bootstrapped_registry(monkeypatch, tmp_path)
    knowledge_root = programs_root.parent / "knowledge"
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)

    def alias(value: str) -> EntityAlias:
        return EntityAlias(
            value=value, kind="alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
            source="test", source_ref=None, recorded_at=now, verified_at=now, verified_by_principal="steward",
        )

    entities = (
        CanonicalEntity(
            workspace_id="ws-1", entity_id="person:alice", entity_type="person", canonical_name="Alice Adams",
            aliases=(alias("alice"),), scope="org", created_at=now, status=EntityStatus.ACTIVE,
        ),
        CanonicalEntity(
            workspace_id="ws-1", entity_id="team:platform", entity_type="team", canonical_name="Platform Team",
            aliases=(alias("platform"),), scope="org", created_at=now, status=EntityStatus.ACTIVE,
        ),
    )
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version=ENTITIES_SCHEMA_VERSION, entities=entities))

    people = (
        PersonDirectory(
            entity_id="person:alice", alias="alice", display_name="Alice Adams",
            contacts=(
                ContactPoint(
                    kind=ContactKind.PRIMARY_EMAIL, value="alice@example.com", status=ContactStatus.ACTIVE,
                    valid_from=None, valid_until=None, source="test", source_ref=None,
                    recorded_at=now, verified_at=now, verified_by_principal="steward", delivery_eligible=True,
                ),
            ),
        ),
    )
    write_people_directory(knowledge_root / "people_directory.yaml", people)
    write_teams(knowledge_root / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform Team", kind=TeamKind.ORG_TEAM),))
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(
                membership_id="m1", person_entity_id="person:alice", team_entity_id="team:platform", role="member",
                valid_from=now, valid_until=None, source="test", source_ref=None,
                observed_at=now, verified_at=now, status=MembershipStatus.ACTIVE,
            ),
        ),
    )
    return programs_root


def test_kb_people_show_cli_resolves_a_bare_alias(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "people", "show", "--person", "alice", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "people-query.v1"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["entity"]["entity_id"] == "person:alice"
    assert len(payload["items"][0]["memberships"]) == 1


def test_kb_people_show_cli_reports_not_found_with_exit_1(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "people", "show", "--person", "nobody"])

    assert result.exit_code == 1
    assert "No canonical person found" in result.stdout


def test_kb_people_find_cli_returns_scored_candidates(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "people", "find", "alice", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["items"][0]["alias"] == "alice"
    assert payload["items"][0]["match_kind"] == "exact"


def test_kb_people_stale_cli_reports_zero_with_a_tight_window(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "people", "stale", "--freshness-days", "-1", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["items"]) >= 1


def test_kb_people_conflicts_cli_rejects_an_invalid_status(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "people", "conflicts", "--status", "bogus"])

    assert result.exit_code != 0


def test_kb_people_conflicts_cli_reports_no_conflicts_on_a_clean_fixture(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "people", "conflicts", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["items"] == []


def test_kb_teams_show_cli_resolves_a_team_alias(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "teams", "show", "--team", "platform", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["items"][0]["entity"]["entity_id"] == "team:platform"


def test_kb_teams_members_cli_lists_the_current_roster(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "teams", "members", "--team", "platform", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["person_entity_id"] == "person:alice"


def test_kb_teams_show_cli_reports_not_found_with_exit_1(monkeypatch, tmp_path: Path) -> None:
    _seed_query_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["kb", "teams", "show", "--team", "nonexistent"])

    assert result.exit_code == 1
    assert "No canonical team found" in result.stdout


def _seed_refresh_fixture(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """PPL-W4.4: the query fixture plus an enabled local_directory_export
    provider config and provider_refresh_enabled=True."""
    from src.core.people_registry_modes import set_registry_flag

    programs_root = _seed_query_fixture(monkeypatch, tmp_path)
    knowledge_root = programs_root.parent / "knowledge"
    set_registry_flag(knowledge_root, "provider_refresh_enabled", True, actor="steward")
    (knowledge_root / "identity_providers.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            "providers:\n"
            '  - name: "acme_directory_export"\n'
            '    provider_type: "local_directory_export"\n'
            '    tenant_id: "acme-tenant"\n'
            '    capability_contract_version: "1.0"\n'
            "    allowed_fields:\n"
            "      - display_name\n"
            "      - title\n"
            "    enabled: true\n"
        ),
        encoding="utf-8",
    )
    export_path = tmp_path / "export.csv"
    export_path.write_text(
        "alias,display_name,title,department,manager_alias,email,teams\nalice,Alice Adams,Staff TPM,,,,\n",
        encoding="utf-8",
    )
    return programs_root, export_path


def test_kb_people_refresh_cli_preview_makes_no_write(monkeypatch, tmp_path: Path) -> None:
    programs_root, export_path = _seed_refresh_fixture(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["kb", "people", "refresh", "--provider", "acme_directory_export", "--person", "alice",
         "--import-file", str(export_path), "--reason", "test", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["transaction_id"] is None
    assert len(payload["accepted"]) >= 1


def test_kb_people_refresh_cli_apply_commits_a_transaction(monkeypatch, tmp_path: Path) -> None:
    programs_root, export_path = _seed_refresh_fixture(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["kb", "people", "refresh", "--provider", "acme_directory_export", "--person", "alice",
         "--import-file", str(export_path), "--reason", "test", "--apply", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["transaction_id"] is not None


def test_kb_people_refresh_cli_team_membership_diff(monkeypatch, tmp_path: Path) -> None:
    programs_root, _ = _seed_refresh_fixture(monkeypatch, tmp_path)
    export_path = tmp_path / "team_export.csv"
    export_path.write_text(
        "alias,display_name,title,department,manager_alias,email,teams\nalice,Alice Adams,Staff TPM,,,,\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["kb", "people", "refresh", "--provider", "acme_directory_export", "--team", "platform",
         "--import-file", str(export_path), "--reason", "membership sync", "--apply", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["team_membership_diffs"]) == 1
    diff = payload["team_membership_diffs"][0]
    assert diff["complete"] is True
    assert diff["removed_person_aliases"] == ["alice"]


def test_kb_people_refresh_cli_reports_kill_switch_disabled(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_query_fixture(monkeypatch, tmp_path)
    knowledge_root = programs_root.parent / "knowledge"
    (knowledge_root / "identity_providers.yaml").write_text(
        (
            'schema_version: "1.0"\nproviders:\n  - name: "acme_directory_export"\n'
            '    provider_type: "local_directory_export"\n    tenant_id: "acme-tenant"\n'
            '    capability_contract_version: "1.0"\n    enabled: true\n'
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["kb", "people", "refresh", "--provider", "acme_directory_export", "--person", "alice",
         "--reason", "test", "--format", "human"],
    )

    assert result.exit_code == 0
    assert "disabled" in result.stdout
