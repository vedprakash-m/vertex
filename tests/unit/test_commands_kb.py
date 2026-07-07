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
