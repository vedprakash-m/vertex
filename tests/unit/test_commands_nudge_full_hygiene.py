from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from typer.testing import CliRunner

from cli import app
from src.commands.nudge import (
    FullHygieneArtifacts,
    FullHygieneRow,
    FullHygieneSection,
    generate_full_hygiene_nudges,
    _word_truncate_title,
    _comment_has_keyword,
)
from src.core.alerts import read_alerts
from src.core.models import RiskLevel, WorkItem
from src.core.policy_loader import FreshnessPolicy
from tests.support.report_test_setup import stage_v2_report_workspace

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake ADO client for full hygiene tests
# ---------------------------------------------------------------------------

class _FullHygieneFakeADOClient:
    """Returns RAMPP1 items from execute_wiql and hydrated fields from query_work_items_batch."""

    RAMPP1_IDS = [801001, 801002]
    POST_RAMP_IDS = [802001]

    def __init__(self, *args, **kwargs) -> None:
        pass

    def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
        if "RAMPP1" in wiql:
            return self.RAMPP1_IDS
        if "POST RAMP" in wiql:
            return self.POST_RAMP_IDS
        return []

    def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
        del fields
        rows = []
        for wid in work_item_ids:
            rows.append({
                "id": wid,
                "fields": {
                    "System.Id": wid,
                    "System.WorkItemType": "Feature",
                    "System.Title": f"Work item {wid}",
                    "System.State": "Active",
                    "System.AssignedTo": {"displayName": "Test Owner", "uniqueName": "towner@example.com"},
                    "System.AreaPath": "One\\Adventure\\Acme",
                    "System.IterationPath": "FY26\\Sprint 20",
                    "System.ChangedDate": "2026-05-01T18:00:00+00:00",
                    "Microsoft.VSTS.Scheduling.TargetDate": "2026-06-30",
                    "System.Tags": "RAMPP1",
                    "Custom.RiskAssessment": "On Track",
                    "Custom.RiskAssessmentComment": "",
                    # Committed for items ending in 1 (mirrors the comment fixture below)
                    "Custom.CommitmentStatus": "Committed" if str(wid).endswith("1") else "",
                    "System.Description": "Description for work item.",
                },
            })
        return rows

    def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
        return []

    def list_work_item_comments(self, work_item_id: int) -> list[dict[str, object]]:
        # Return a recent comment with status keyword for items ending in 1
        if str(work_item_id).endswith("1"):
            return [{"createdDate": "2026-05-20T18:00:00+00:00", "text": "on track, no blockers"}]
        return []


class _DeduplicationFakeADOClient(_FullHygieneFakeADOClient):
    """Section B and C share work item 801002 — dedup should exclude it from C."""

    POST_RAMP_IDS = [801002, 802001]  # 801002 already in B


class _RegistryItemsFakeADOClient(_FullHygieneFakeADOClient):
    """Returns a registry item from query_work_items_batch for section A testing."""

    RAMPP1_IDS: list[int] = []
    POST_RAMP_IDS: list[int] = []

    def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
        del fields
        return [{
            "id": wid,
            "fields": {
                "System.Id": wid,
                "System.WorkItemType": "Feature",
                "System.Title": f"Registry item {wid}",
                "System.State": "Active",
                "System.AssignedTo": {"displayName": "Registry Owner", "uniqueName": "regowner@example.com"},
                "System.AreaPath": "One\\Adventure\\Acme",
                "System.IterationPath": "FY26\\Sprint 20",
                "System.ChangedDate": "2026-05-10T18:00:00+00:00",
                "Microsoft.VSTS.Scheduling.TargetDate": "2026-07-01",
                "System.Tags": "RAMPP1",
                "Custom.RiskAssessment": "On Track",
                "Custom.RiskAssessmentComment": "",
                "System.Description": "Registry item description.",
            },
        } for wid in work_item_ids]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_full_hygiene_config(programs_root: Path) -> None:
    """Inject full_hygiene config block into the nova_nudge edition YAML."""
    edition_path = programs_root / "acme" / "editions" / "nova_nudge.yaml"
    doc = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    doc["full_hygiene"] = {
        "ramp_p1_tag": "RAMPP1",
        "post_ramp_tag": "POST RAMP",
        "area_paths": ["One\\Adventure\\Acme", "One\\Adventure\\Contoso"],
        "recipient": "towner",
        "comment_window_days": 7,
        "compress_titles_with_ai": False,
        "stale_business_days": {"section_a": 2, "section_b": 4, "section_c": 6},
        "status_keywords": ["on track", "blocked", "ETA"],
        "risk_on_track_values": ["On Track", "on track"],
        "brand_label": "Program Hygiene",
    }
    edition_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _seed_people(knowledge_root: Path) -> None:
    people_path = knowledge_root / "people_directory.yaml"
    doc = yaml.safe_load(people_path.read_text(encoding="utf-8"))
    doc.setdefault("people", []).append({"alias": "towner", "email": "towner@example.com", "display_name": "Test Owner"})
    people_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _seed_registry(programs_root: Path, *, key_ado_items: list[int] | None = None) -> None:
    """Write a minimal workstream_registry.yaml for the acme program."""
    registry_path = programs_root / "acme" / "workstream_registry.yaml"
    registry = {
        "schema_version": "1.0",
        "workstreams": [
            {
                "id": "acme.test_ws",
                "name": "Test Workstream",
                "lifecycle_state": "active",
                "stakeholders": [{"name": "Test Owner", "role": "primary_owner"}],
                "key_ado_items": key_ado_items or [],
            }
        ],
    }
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit-level helper tests
# ---------------------------------------------------------------------------

def test_word_truncate_short_title() -> None:
    assert _word_truncate_title("Short title") == "Short title"


def test_word_truncate_long_title() -> None:
    long = "This is a very long work item title that exceeds fifty characters"
    result = _word_truncate_title(long, max_len=50)
    assert len(result) <= 52  # "…" adds 1 char
    assert result.endswith("\u2026")


def test_word_truncate_exact_50() -> None:
    title = "x" * 50
    assert _word_truncate_title(title) == title


def test_comment_has_keyword_match() -> None:
    assert _comment_has_keyword("Status: on track, ETA next week", ("on track", "blocked"))


def test_comment_has_keyword_no_match() -> None:
    assert not _comment_has_keyword("No relevant words here", ("on track", "blocked"))


def test_comment_has_keyword_none() -> None:
    assert not _comment_has_keyword(None, ("on track",))


# ---------------------------------------------------------------------------
# generate_full_hygiene_nudges integration tests
# ---------------------------------------------------------------------------

def test_generate_full_hygiene_nudges_returns_three_sections(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert isinstance(artifacts, FullHygieneArtifacts)
    assert len(artifacts.sections) == 3
    section_a, section_b, section_c = artifacts.sections
    assert section_a.label == "A"
    assert section_b.label == "B"
    assert section_c.label == "C"
    assert section_a.total_count == 0   # no registry items seeded
    assert section_b.total_count == 2   # RAMPP1_IDS = [801001, 801002]
    assert section_c.total_count == 1   # POST_RAMP_IDS = [802001]


def _seed_audience_scope_registry(programs_root: Path) -> None:
    """specs/people.md PPL-W5a.6: a real schema-2.0 shared registry with one
    org_team ("platform") containing one active member ("newmember") who is
    NOT among nudge.py's own legacy-schema recipients -- proving the
    audience-scope pipeline is a genuinely ADDITIVE recipient source."""
    from datetime import datetime, timezone as tz

    from src.core.audience_scopes import audience_scopes_path_for_program
    from src.core.people_directory_schema import (
        ContactKind, ContactPoint, ContactStatus, PersonDirectory, PersonStatus,
        Team, TeamKind, TeamStatus, write_people_directory, write_teams,
    )
    from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, write_entities_document
    from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships
    from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_config
    from src.core.people_registry_modes import set_registry_flag

    now = datetime(2026, 5, 22, tzinfo=tz.utc)

    def alias(value: str) -> EntityAlias:
        return EntityAlias(
            value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
            source="test", source_ref=None, recorded_at=now, verified_at=now, verified_by_principal="steward",
        )

    knowledge_root = programs_root.parent / "knowledge"
    if load_registry_config(knowledge_root) is None:
        bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True, as_of=now)
    set_registry_flag(knowledge_root, "audience_scopes_enabled", True, actor="test-principal")
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                CanonicalEntity(workspace_id="ws", entity_id="team:platform", entity_type="team", canonical_name="Platform", aliases=(alias("platform"),), scope="org", created_at=now),
                CanonicalEntity(workspace_id="ws", entity_id="person:newmember", entity_type="person", canonical_name="New Member", aliases=(alias("newmember"),), scope="org", created_at=now),
            ),
        ),
    )
    write_teams(knowledge_root / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM, status=TeamStatus.ACTIVE),))
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(membership_id="m1", person_entity_id="person:newmember", team_entity_id="team:platform", role="member", valid_from=now, valid_until=None, source="test", source_ref=None, observed_at=now, verified_at=now, status=MembershipStatus.ACTIVE),
        ),
    )
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (
            PersonDirectory(
                entity_id="person:newmember", alias="newmember", status=PersonStatus.ACTIVE,
                contacts=(
                    ContactPoint(
                        kind=ContactKind.PRIMARY_EMAIL, value="newmember@example.com", status=ContactStatus.ACTIVE,
                        valid_from=None, valid_until=None, source="test", source_ref=None,
                        recorded_at=now, verified_at=now, verified_by_principal="steward", delivery_eligible=True,
                    ),
                ),
            ),
        ),
    )
    scope_path = audience_scopes_path_for_program("acme", programs_root=programs_root)
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text('schema_version: "1.0"\naudience_scopes:\n  engineering_hygiene:\n    team_refs: [platform]\n', encoding="utf-8")


def test_generate_full_hygiene_nudges_adds_audience_scope_recipients(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(programs_root)
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)
    _seed_audience_scope_registry(programs_root)

    # Opt this edition into the new audience scope, matching the same
    # full_hygiene: block additional_cc/to_leadership_rollup already live in.
    edition_path = programs_root / "acme" / "editions" / "nova_nudge.yaml"
    import yaml as yaml_module
    doc = yaml_module.safe_load(edition_path.read_text(encoding="utf-8"))
    doc["full_hygiene"]["audience_scope_ids"] = ["engineering_hygiene"]
    edition_path.write_text(yaml_module.safe_dump(doc, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    emails = {r.email for r in artifacts.to_recipients}
    assert "newmember@example.com" in emails


def test_generate_full_hygiene_nudges_without_audience_scope_ids_is_unaffected(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    """Zero-regression check: the SAME audience-scope registry exists on
    disk, but this edition never opts in (`audience_scope_ids` absent) --
    the new registry member must not silently appear as a recipient."""
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(programs_root)
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)
    _seed_audience_scope_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    emails = {r.email for r in artifacts.to_recipients}
    assert "newmember@example.com" not in emails


def test_generate_full_hygiene_nudges_deduplicates_b_from_c(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.ADOClient", _DeduplicationFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    _, section_b, section_c = artifacts.sections
    assert section_b.total_count == 2        # 801001, 801002
    assert section_c.total_count == 1        # only 802001; 801002 deduped
    c_ids = {r.work_item_id for g in section_c.groups for r in g.rows}
    assert 801002 not in c_ids
    assert 802001 in c_ids


def test_generate_full_hygiene_nudges_deduplicates_a_from_b(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    # Seed registry with one of the RAMPP1 IDs so it appears in section A
    _seed_registry(programs_root, key_ado_items=[801001])

    monkeypatch.setattr("src.commands.nudge.ADOClient", _RegistryItemsFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    section_a, section_b, _ = artifacts.sections
    assert section_a.total_count == 1  # registry item 801001
    a_ids = {r.work_item_id for g in section_a.groups for r in g.rows}
    b_ids = {r.work_item_id for g in section_b.groups for r in g.rows}
    assert 801001 in a_ids
    assert 801001 not in b_ids  # deduped from B


def test_generate_full_hygiene_nudges_row_signals_set_correctly(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    _, section_b, _ = artifacts.sections
    # Item 801001 ends in 1 — committed and has comment with "on track"
    all_b_rows = [r for g in section_b.groups for r in g.rows]
    row_1 = next(r for r in all_b_rows if r.work_item_id == 801001)
    assert row_1.has_valid_target_date is True   # target_date 2026-06-30 > 2026-05-22
    assert row_1.has_committed is True            # CommitmentStatus="Committed"
    assert row_1.has_risk_assessment is True      # "On Track"
    assert row_1.risk_is_on_track is True
    assert row_1.has_risk_reason is None          # N/A (on track)
    assert row_1.has_recent_comment is True       # fake returns comment for ids ending in 1
    assert row_1.comment_has_status_keyword is True  # "on track" in comment
    assert row_1.is_ready is True                 # target date + committed + risk assessment all green

    # Item 801002 ends in 2 — not committed, no comment returned
    row_2 = next(r for r in all_b_rows if r.work_item_id == 801002)
    assert row_2.has_committed is False           # no CommitmentStatus set
    assert row_2.has_recent_comment is False
    assert row_2.comment_has_status_keyword is False
    assert row_2.is_ready is False                # not committed (recent comment/keyword don't count)


def test_generate_full_hygiene_nudges_writes_eml_file(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=False,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert len(artifacts.eml_paths) == 1
    eml_path = artifacts.eml_paths[0]
    assert eml_path.exists()
    assert eml_path.name.endswith(".full.eml")
    assert "nova_nudge/full" in eml_path.as_posix()
    content = eml_path.read_text(encoding="utf-8")
    assert "RAMPP1" in content


def test_generate_full_hygiene_nudges_no_eml_when_no_items(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    class _EmptyADOClient(_FullHygieneFakeADOClient):
        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            return []

    monkeypatch.setattr("src.commands.nudge.ADOClient", _EmptyADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=False,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert artifacts.eml_paths == ()
    assert all(s.total_count == 0 for s in artifacts.sections)


def test_generate_full_hygiene_nudges_stale_threshold_flags(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        stale_a=1,
        stale_b=99,
        stale_c=99,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert artifacts.sections[0].stale_threshold_days == 1
    assert artifacts.sections[1].stale_threshold_days == 99
    assert artifacts.sections[2].stale_threshold_days == 99


def test_generate_full_hygiene_nudges_workstream_grouping(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    # Registry maps 801001 to acme.test_ws — 801002 will be Unclassified
    _seed_registry(programs_root, key_ado_items=[])

    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    _, section_b, _ = artifacts.sections
    # Since no key_ado_items in registry and area_path doesn't match workstream area_paths,
    # items go to Unclassified group
    group_names = {g.workstream_name for g in section_b.groups}
    # At minimum there should be some group
    assert len(section_b.groups) >= 1
    assert section_b.total_count == 2


def test_generate_full_hygiene_nudges_section_ready_count(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    _, section_b, _ = artifacts.sections
    # 801001 (ends in 1) is committed → is_ready=True; 801002 is not committed → is_ready=False
    assert section_b.ready_count == 1
    assert section_b.total_count == 2


def test_generate_full_hygiene_nudges_multi_tag_ramp_p1(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    """ramp_p1_tag as a list aggregates items from all tags and deduplicates."""
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    # Override ramp_p1_tag to be a list with two tags; 801001 appears in both (should deduplicate)
    edition_path = tmp_path / "programs" / "acme" / "editions" / "nova_nudge.yaml"
    doc = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    doc["full_hygiene"]["ramp_p1_tag"] = ["RAMPP1", "Acme-P1"]
    edition_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    class _MultiTagFakeADOClient(_FullHygieneFakeADOClient):
        NOVA_P1_IDS = [801001, 803001]  # 801001 also in RAMPP1 — must be deduped

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            if "Acme-P1" in wiql:
                return self.NOVA_P1_IDS
            return super().execute_wiql(wiql)

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            rows = super().query_work_items_batch(work_item_ids, fields)
            # Patch tag for 803001 to reflect Acme-P1
            for row in rows:
                if row.get("id") == 803001:
                    row["fields"]["System.Tags"] = "Acme-P1"  # type: ignore[index]
            return rows

    monkeypatch.setattr("src.commands.nudge.ADOClient", _MultiTagFakeADOClient)

    artifacts = generate_full_hygiene_nudges(
        program_id="acme",
        dry_run=True,
        as_of=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    _, section_b, _ = artifacts.sections
    b_ids = {r.work_item_id for g in section_b.groups for r in g.rows}
    # Items from both RAMPP1 (801001, 801002) and Acme-P1 (803001) appear; 801001 not duplicated
    assert 801001 in b_ids
    assert 801002 in b_ids
    assert 803001 in b_ids
    assert section_b.total_count == 3  # 801001 deduplicated, so 3 unique items
    # Section title reflects both tags joined by /
    assert section_b.title == "Remaining RAMPP1/Acme-P1"


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def test_nudge_cli_dry_run_exits_zero(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    """vertex nudge --program acme --dry-run completes with exit_code 0."""
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)

    result = runner.invoke(app, ["nudge", "--program", "acme", "--dry-run"])

    assert result.exit_code == 0, result.output


def test_nudge_cli_real_run_fires_enrichment_cadence_alert(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    """BL-E4 activation: a real (non-dry-run) 'vertex nudge' run ticks the
    people-registry enrichment cadence and raises a between-runs alert once
    the configured threshold is crossed -- the nudge_run side of the same
    trigger report.py's own real-run site already exercises."""
    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_full_hygiene_config(tmp_path / "programs")
    _seed_people(tmp_path / "knowledge")
    _seed_registry(programs_root)

    monkeypatch.setattr("src.commands.nudge.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.nudge.ADOClient", _FullHygieneFakeADOClient)
    monkeypatch.setattr(
        "src.core.people_enrichment.load_freshness_policy",
        lambda: FreshnessPolicy(
            fact_type_ttl_days={}, gather_cadence_hours={},
            people_registry_enrichment_nudge_every=1, people_registry_enrichment_report_every=None,
        ),
    )

    assert read_alerts("acme", programs_root=programs_root) == ()

    result = runner.invoke(app, ["nudge", "--program", "acme"])

    assert result.exit_code == 0, result.output
    assert "Routine WorkIQ enrichment is due" in result.output
    alerts = read_alerts("acme", programs_root=programs_root)
    assert len(alerts) == 1
    assert alerts[0].category == "people_enrichment_due"
    assert "vertex kb people enrich --program acme" in alerts[0].next_command
    assert "Dry run:" in result.output
