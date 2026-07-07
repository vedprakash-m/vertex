from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config_loader_v2 import load_edition_bundle
from src.core.knowledge_store import load_program_knowledge
from src.core.profile_encryption import encrypt_people_profiles_file


class _FakeKeyring:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._values[(service_name, username)] = password


def test_load_program_knowledge_prefers_shared_root_with_program_fallback(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    program_knowledge_dir = program_dir / "knowledge"
    shared_knowledge_root.mkdir(parents=True)
    program_knowledge_dir.mkdir(parents=True)

    (shared_knowledge_root / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: shared\n'
            '    email: shared@example.com\n'
        ),
        encoding="utf-8",
    )
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (program_knowledge_dir / "people_profiles.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'profiles:\n'
            '  - alias: shared\n'
            '    cares_about: [throughput]\n'
        ),
        encoding="utf-8",
    )
    (program_knowledge_dir / "golden_queries.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'queries:\n'
            '  - id: velocity\n'
            '    cluster: https://cluster\n'
            '    database: demo\n'
            '    kql: Demo | take 1\n'
            '    section: Velocity\n'
            '    render_as: table\n'
            '    confidence: high\n'
            '    refresh_on_gather: true\n'
            '    label: Velocity P50\n'
            '    result_column: P50\n'
        ),
        encoding="utf-8",
    )

    knowledge = load_program_knowledge("demo", programs_root=programs_root)

    assert knowledge.people_directory[0].alias == "shared"
    assert knowledge.people_profiles[0].alias == "shared"
    assert knowledge.golden_queries[0].id == "velocity"
    assert knowledge.golden_queries[0].refresh_on_gather is True
    assert knowledge.golden_queries[0].label == "Velocity P50"
    assert knowledge.golden_queries[0].result_column == "P50"
    assert knowledge.golden_queries[0].validated is True


def test_load_program_knowledge_parses_wiql_golden_queries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    program_knowledge_dir = program_dir / "knowledge"
    shared_knowledge_root.mkdir(parents=True)
    program_knowledge_dir.mkdir(parents=True)

    (shared_knowledge_root / "people_directory.yaml").write_text('schema_version: "1.0"\npeople: []\n', encoding="utf-8")
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (program_knowledge_dir / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (program_knowledge_dir / "golden_queries.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'queries:\n'
            '  - id: schie-open\n'
            '    engine: wiql\n'
            '    wiql: SELECT [System.Id] FROM WorkItems\n'
            '    section: SCHIE Open\n'
            '    render_as: table\n'
            '    confidence: high\n'
            '    program_ids: [demo]\n'
        ),
        encoding="utf-8",
    )

    knowledge = load_program_knowledge("demo", programs_root=programs_root)

    assert knowledge.golden_queries[0].engine == "wiql"
    assert knowledge.golden_queries[0].wiql == "SELECT [System.Id] FROM WorkItems"
    assert knowledge.golden_queries[0].kql == ""


def test_load_program_knowledge_parses_engms_pages(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    program_knowledge_dir = program_dir / "knowledge"
    shared_knowledge_root.mkdir(parents=True)
    program_knowledge_dir.mkdir(parents=True)

    (shared_knowledge_root / "people_directory.yaml").write_text('schema_version: "1.0"\npeople: []\n', encoding="utf-8")
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (program_knowledge_dir / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (program_knowledge_dir / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")
    (program_knowledge_dir / "engms_pages.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'pages:\n'
            '  - id: acme-readiness-spec\n'
            '    title: Acme Readiness Spec\n'
            '    url: https://eng.ms/acme-readiness\n'
            '    workstream_ids: [acme]\n'
            '    program_ids: [demo]\n'
            '    tags: [readiness, parity]\n'
        ),
        encoding="utf-8",
    )

    knowledge = load_program_knowledge("demo", programs_root=programs_root)

    assert knowledge.engms_pages[0].id == "acme-readiness-spec"
    assert knowledge.engms_pages[0].url == "https://eng.ms/acme-readiness"
    assert knowledge.engms_pages[0].workstream_ids == ("acme",)


def test_load_program_knowledge_rejects_invalid_engms_url(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    program_knowledge_dir = program_dir / "knowledge"
    shared_knowledge_root.mkdir(parents=True)
    program_knowledge_dir.mkdir(parents=True)

    (shared_knowledge_root / "people_directory.yaml").write_text('schema_version: "1.0"\npeople: []\n', encoding="utf-8")
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (program_knowledge_dir / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (program_knowledge_dir / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")
    (program_knowledge_dir / "engms_pages.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'pages:\n'
            '  - id: broken\n'
            '    title: Broken\n'
            '    url: eng.ms/broken\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match=r"http\(s\) URL"):
        load_program_knowledge("demo", programs_root=programs_root)


def test_load_program_knowledge_accepts_sharepoint_reference_doc_url(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    program_knowledge_dir = program_dir / "knowledge"
    shared_knowledge_root.mkdir(parents=True)
    program_knowledge_dir.mkdir(parents=True)

    (shared_knowledge_root / "people_directory.yaml").write_text('schema_version: "1.0"\npeople: []\n', encoding="utf-8")
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (program_knowledge_dir / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (program_knowledge_dir / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")
    (program_knowledge_dir / "engms_pages.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'pages:\n'
            '  - id: acme-sharepoint-reference\n'
            '    title: Acme SharePoint Reference\n'
            '    url: https://microsoft.sharepoint.com/teams/Acme/_layouts/15/Doc.aspx?sourcedoc=%7B759D1F8A-1EA6-4906-8587-E1FE4063946E%7D&file=Adventure-Acme%20Ramp%20Plan.docx&action=default\n'
            '    workstream_ids: [acme]\n'
            '    program_ids: [demo]\n'
            '    tags: [ramp]\n'
        ),
        encoding="utf-8",
    )

    knowledge = load_program_knowledge("demo", programs_root=programs_root)

    assert knowledge.engms_pages[0].id == "acme-sharepoint-reference"
    assert knowledge.engms_pages[0].url.startswith("https://microsoft.sharepoint.com/")
    assert knowledge.engms_pages[0].workstream_ids == ("acme",)


def test_load_edition_bundle_uses_shared_knowledge_root(tmp_path: Path) -> None:
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    editions_root.mkdir(parents=True)
    program_dir.mkdir(parents=True)
    shared_knowledge_root.mkdir(parents=True)

    (editions_root / "demo_weekly.yaml").write_text(
        (
            'schema_version: "2.0"\n'
            'id: demo_weekly\n'
            'program_id: demo\n'
            'name: Demo Weekly\n'
            'type: detailed\n'
            'altitude: helicopter\n'
            'cadence: weekly\n'
        ),
        encoding="utf-8",
    )
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
    (shared_knowledge_root / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: shared\n'
            '    email: shared@example.com\n'
        ),
        encoding="utf-8",
    )
    (shared_knowledge_root / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (shared_knowledge_root / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")

    bundle = load_edition_bundle("demo_weekly", editions_root=editions_root, programs_root=programs_root)

    assert bundle is not None
    assert bundle.knowledge.people_directory[0].alias == "shared"


def test_load_edition_bundle_derives_key_dependency_chain_from_structured_dependencies(tmp_path: Path) -> None:
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    editions_root.mkdir(parents=True)
    program_dir.mkdir(parents=True)
    shared_knowledge_root.mkdir(parents=True)

    (editions_root / "demo_weekly.yaml").write_text(
        (
            'schema_version: "2.0"\n'
            'id: demo_weekly\n'
            'program_id: demo\n'
            'name: Demo Weekly\n'
            'type: detailed\n'
            'altitude: helicopter\n'
            'cadence: weekly\n'
        ),
        encoding="utf-8",
    )
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
            '  - id: ws_alpha\n'
            '    name: Alpha\n'
            '  - id: ws_beta\n'
            '    name: Beta\n'
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
            '        workstream_id: ws_alpha\n'
        ),
        encoding="utf-8",
    )
    (program_dir / "dependencies.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'dependencies:\n'
            '  - id: dep-alpha-beta\n'
            '    from_workstream_id: ws_alpha\n'
            '    to_workstream_id: ws_beta\n'
            '    dependency_type: blocks\n'
            '    risk_if_broken: Beta stalls until Alpha closes.\n'
            '    status: active\n'
        ),
        encoding="utf-8",
    )
    (shared_knowledge_root / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: shared\n'
            '    email: shared@example.com\n'
        ),
        encoding="utf-8",
    )
    (shared_knowledge_root / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (shared_knowledge_root / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")

    bundle = load_edition_bundle("demo_weekly", editions_root=editions_root, programs_root=programs_root)

    assert bundle is not None
    assert bundle.program_context_document["key_dependency_chain"] == [
        {
            "from_item": "ws_alpha",
            "to_item": "ws_beta",
            "impact": "Beta stalls until Alpha closes.",
        }
    ]


def test_shared_knowledge_changes_are_visible_to_multiple_program_bundles(tmp_path: Path) -> None:
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    editions_root.mkdir(parents=True)
    programs_root.mkdir(parents=True)
    shared_knowledge_root.mkdir(parents=True)

    for edition_id, program_id, program_name in (
        ("acme_weekly", "acme", "Acme"),
        ("fabrikam_weekly", "fabrikam", "Fabrikam"),
    ):
        program_dir = programs_root / program_id
        program_dir.mkdir(parents=True)
        (editions_root / f"{edition_id}.yaml").write_text(
            (
                'schema_version: "2.0"\n'
                f'id: {edition_id}\n'
                f'program_id: {program_id}\n'
                f'name: {program_name} Weekly\n'
                'type: narrative\n'
                'altitude: helicopter\n'
                'cadence: weekly\n'
            ),
            encoding="utf-8",
        )
        (program_dir / "program.yaml").write_text(
            (
                'schema_version: "2.0"\n'
                f'id: {program_id}\n'
                f'name: {program_name}\n'
            ),
            encoding="utf-8",
        )
        (program_dir / "workstreams.yaml").write_text(
            (
                'schema_version: "2.0"\n'
                'workstreams:\n'
                '  - id: ws_shared\n'
                '    name: Shared Workstream\n'
                '    dri_email: shared@example.com\n'
            ),
            encoding="utf-8",
        )
        (program_dir / "scorecards.yaml").write_text(
            (
                'schema_version: "2.0"\n'
                'scorecards:\n'
                '  - name: Shared Scorecard\n'
                '    dimensions:\n'
                '      - name: Shared Dimension\n'
                '        workstream_id: ws_shared\n'
            ),
            encoding="utf-8",
        )

    (shared_knowledge_root / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: shared\n'
            '    email: shared@example.com\n'
            '    title: Program Manager\n'
        ),
        encoding="utf-8",
    )
    (shared_knowledge_root / "people_profiles.yaml").write_text('schema_version: "1.0"\nprofiles: []\n', encoding="utf-8")
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (shared_knowledge_root / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")

    first_nova = load_edition_bundle("acme_weekly", editions_root=editions_root, programs_root=programs_root)
    first_armada = load_edition_bundle("fabrikam_weekly", editions_root=editions_root, programs_root=programs_root)

    assert first_nova is not None
    assert first_armada is not None
    assert first_nova.program_context_document["people"][0]["role"] == "Program Manager"
    assert first_armada.program_context_document["people"][0]["role"] == "Program Manager"

    (shared_knowledge_root / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: shared\n'
            '    email: shared@example.com\n'
            '    title: Principal Program Manager\n'
        ),
        encoding="utf-8",
    )

    updated_nova = load_edition_bundle("acme_weekly", editions_root=editions_root, programs_root=programs_root)
    updated_armada = load_edition_bundle("fabrikam_weekly", editions_root=editions_root, programs_root=programs_root)

    assert updated_nova is not None
    assert updated_armada is not None
    assert updated_nova.program_context_document["people"][0]["role"] == "Principal Program Manager"
    assert updated_armada.program_context_document["people"][0]["role"] == "Principal Program Manager"


def test_load_program_knowledge_reads_encrypted_people_profiles(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    shared_knowledge_root = tmp_path / "knowledge"
    program_dir = programs_root / "demo"
    program_knowledge_dir = program_dir / "knowledge"
    shared_knowledge_root.mkdir(parents=True)
    program_knowledge_dir.mkdir(parents=True)

    (shared_knowledge_root / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: shared\n'
            '    email: shared@example.com\n'
        ),
        encoding="utf-8",
    )
    (shared_knowledge_root / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (shared_knowledge_root / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (program_knowledge_dir / "people_profiles.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'profiles:\n'
            '  - alias: shared\n'
            '    comm_style: concise\n'
        ),
        encoding="utf-8",
    )
    (program_knowledge_dir / "golden_queries.yaml").write_text('schema_version: "1.0"\nqueries: []\n', encoding="utf-8")
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
    encrypt_people_profiles_file(program_knowledge_dir / "people_profiles.yaml")

    knowledge = load_program_knowledge("demo", programs_root=programs_root)

    assert knowledge.people_profiles[0].alias == "shared"
    assert knowledge.people_profiles[0].comm_style == "concise"