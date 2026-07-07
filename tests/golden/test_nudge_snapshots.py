"""Deterministic golden EML snapshot tests for `vertex nudge` (full hygiene).

Spec: .archive/specs/fix-nudge.md §19.5, §24.7, §27.1, §28.2.

These tests construct synthetic program and edition configuration, inject a
``SeedADOClient`` that returns synthetic work items/comments loaded from
committed, authored JSON fixtures, run
``generate_full_hygiene_nudges`` with a fixed ``as_of`` timestamp, normalize the
two nondeterministic EML fields (``Message-ID`` header + MIME boundary + run_id
token), and compare against committed ``.golden`` snapshots.

No local program configuration or provider access is required.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import difflib
import pytest
import yaml

from src.commands.nudge import generate_full_hygiene_nudges
from tests.support.report_test_setup import stage_v2_report_workspace
from tests.support.synthetic_report_source import synthetic_report_root as repo_root

GOLDEN_DIR = Path(__file__).resolve().parent / "public_snapshots"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "data" / "nudge"

# Fixed as_of timestamp — controls Date header, subject date label, and the
# strftime portion of the run_id. Must match the value used when the golden
# files were generated.
AS_OF = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# SeedADOClient — loads synthetic work items + comments from a JSON fixture.
# ---------------------------------------------------------------------------


class SeedADOClient:
    """Fake ADO client backed by an explicitly prepared synthetic seed.

    Constructor swallows the ``organization=/project=/timeout=`` kwargs the
    production factory passes (we monkeypatch ``src.commands.nudge.ADOClient``
    with a lambda that only forwards the synthetic seed).
    """

    def __init__(self, seed: dict, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self._seed = seed
        self._items_by_id: dict[int, dict[str, object]] = {
            int(item["id"]): dict(item["fields"])
            for item in self._seed.get("work_items", [])
        }
        self._tags_map: dict[str, list[int]] = {
            str(tag): [int(i) for i in ids]
            for tag, ids in self._seed.get("work_items_by_tag", {}).items()
        }

    def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
        del top
        # Scan the WIQL string for each known tag; return union of matching IDs
        # in insertion order (first-seen wins).
        seen: set[int] = set()
        out: list[int] = []
        for tag, ids in self._tags_map.items():
            if tag in wiql:
                for wid in ids:
                    if wid not in seen:
                        seen.add(wid)
                        out.append(wid)
        return out

    def query_work_items_batch(
        self, work_item_ids: list[int], fields: tuple[str, ...]
    ) -> list[dict[str, object]]:
        del fields
        rows: list[dict[str, object]] = []
        for wid in work_item_ids:
            fields_map = self._items_by_id.get(int(wid))
            if fields_map is not None:
                rows.append({"id": int(wid), "fields": dict(fields_map)})
        return rows

    def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
        del work_item_ids
        return []

    def list_work_item_comments(self, work_item_id: int) -> list[dict[str, object]]:
        raw = self._seed.get("comments_by_id", {}).get(str(int(work_item_id)), [])
        return list(raw)


# ---------------------------------------------------------------------------
# Registry seeding — writes a minimal workstream_registry.yaml for the staged
# program so registry-sourced sections have key_ado_items matching the seed.
# Mirrors tests/unit/test_commands_nudge_full_hygiene.py::_seed_registry.
# ---------------------------------------------------------------------------


def _seed_registry(
    programs_root: Path, program_id: str, *, key_ado_items: list[int]
) -> None:
    """Write a minimal workstream_registry.yaml for the staged program."""
    import yaml  # noqa: PLC0415

    registry_path = programs_root / program_id / "workstream_registry.yaml"
    registry = {
        "schema_version": "1.0",
        "workstreams": [
            {
                "id": f"{program_id}.priority",
                "name": f"{program_id} Priority",
                "lifecycle_state": "active",
                "stakeholders": [
                    {"name": "Maintainer", "role": "primary_owner"}
                ],
                "key_ado_items": key_ado_items,
            }
        ],
    }
    registry_path.write_text(
        yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
    )


def _seed_edition(programs_root: Path, program_id: str, seed: dict) -> None:
    sections = [{
        "id": "priority",
        "title": "Priority Workstreams",
        "letter": "A",
        "stale_business_days": 2,
        "criteria": {"source": "registry"},
    }]
    tag_groups = [
        ("release", "Release Readiness", ["RELEASE", "RELEASE-ALT"], 4),
        ("followup", "Release Follow-up", ["FOLLOWUP"], 6),
    ]
    for section_id, title, tags, stale in tag_groups:
        if any(tag in seed.get("work_items_by_tag", {}) for tag in tags):
            sections.append({
                "id": section_id,
                "title": title,
                "letter": chr(ord("A") + len(sections)),
                "stale_business_days": stale,
                "criteria": {"source": "tag", "tags": tags},
            })
    doc = {
        "schema_version": "2.1", "id": f"{program_id}_nudge",
        "program_id": program_id, "name": f"{program_id.title()} Hygiene",
        "type": "nudge", "cadence": "weekly",
        "full_hygiene": {
            "recipient": "tpm", "sections": sections,
            "comment_window_days": 7, "cooldown_days": 14,
            "compress_titles_with_ai": False,
            "brand_label": f"{program_id.title()} Hygiene",
            "audience_policy": {"allowed_domains": ["nudge-synth.local"]},
        },
    }
    edition_path = programs_root / program_id / "editions" / f"{program_id}_nudge.yaml"
    edition_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _seed_recipient(workspace_root: Path, alias: str, email: str, display_name: str) -> None:
    """Author the shared directory with its valid synthetic recipient."""
    import yaml  # noqa: PLC0415

    people_path = workspace_root / "knowledge" / "people_directory.yaml"
    people_path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "1.0",
        "people": [{"alias": alias, "email": email, "display_name": display_name}],
    }
    people_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Golden-snapshot infra (mirrors tests/golden/test_base_email_snapshots.py)
# ---------------------------------------------------------------------------


class GoldenFileMismatchError(AssertionError):
    pass


def _load_golden(name: str) -> str | None:
    golden_path = GOLDEN_DIR / f"{name}.golden"
    if golden_path.exists():
        return golden_path.read_text(encoding="utf-8")
    return None


def _save_golden(name: str, content: str) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / f"{name}.golden").write_text(content, encoding="utf-8")


def _compare_with_golden(name: str, actual: str, update: bool) -> None:
    golden = _load_golden(name)
    if update:
        _save_golden(name, actual)
        return
    assert golden is not None, f"Missing golden file: {name}.golden"

    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.golden",
                tofile="actual",
            )
        )
        raise GoldenFileMismatchError(
            f"Output does not match golden file: {name}.golden\n\nDiff:\n{diff}"
        )


# ---------------------------------------------------------------------------
# EML normalization — strips nondeterministic fields before comparison.
# ---------------------------------------------------------------------------

_MESSAGE_ID_RE = re.compile(r"(Message-ID:\s*)<[^>]*>", re.IGNORECASE)
# MIME boundary header value (e.g. boundary="===============...==") and the
# matching delimiter lines in the body (e.g. --===============...==).
_BOUNDARY_HEADER_RE = re.compile(r'boundary="[^"]*"')
_BOUNDARY_DELIM_RE = re.compile(r"(--=+)[0-9]+(==+)")
_RUN_ID_RE = re.compile(r"nudge_\d+T\d+Z_[0-9a-f]+")


def _normalize_eml(text: str) -> str:
    # (a) Message-ID header is auto-generated by EmailMessage.
    text = _MESSAGE_ID_RE.sub(r"\1<NORMALIZED>", text)
    # (b) MIME boundary — both the header value and the body delimiter lines.
    text = _BOUNDARY_HEADER_RE.sub('boundary="NORMALIZED"', text)
    text = _BOUNDARY_DELIM_RE.sub(r"\1NORMALIZED\2", text)
    # (c) run_id token carries a uuid hex suffix.
    text = _RUN_ID_RE.sub("nudge_RUNID", text)
    return text


# ---------------------------------------------------------------------------
# Shared driver — stages the workspace, injects the fake ADO client, runs the
# generator, and returns the normalized EML text.
# ---------------------------------------------------------------------------


def _generate_normalized_eml(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
    *,
    program_id: str,
    seed_name: str,
    golden_name: str,
    update: bool,
) -> str:
    stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    if program_id != "acme":
        program_root = programs_root / program_id
        shutil.copytree(programs_root / "acme", program_root)
        program_path = program_root / "program.yaml"
        program = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program.update(id=program_id, name=program_id.title(), chapter_namespace=program_id)
        program_path.write_text(yaml.safe_dump(program), encoding="utf-8")
    _seed_recipient(
        tmp_path,
        alias="tpm",
        email="tpm@nudge-synth.local",
        display_name="TPM Owner",
    )
    seed_path = FIXTURES_DIR / seed_name
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    assert seed["metadata"]["program"] == program_id
    _seed_edition(programs_root, program_id, seed)
    _seed_registry(
        programs_root,
        program_id,
        key_ado_items=_registry_ids_for(seed),
    )

    monkeypatch.setattr(
        "src.commands.nudge.ADOClient",
        lambda *a, **kw: SeedADOClient(seed),  # noqa: E731
    )

    artifacts = generate_full_hygiene_nudges(
        program_id=program_id,
        dry_run=False,
        as_of=AS_OF,
        programs_root=programs_root,
        candidate_workers=1,
    )

    expected_counts = [1, 4, 2] if program_id == "acme" else [5]
    assert [section.total_count for section in artifacts.sections] == expected_counts
    expected_ids = [{110}, {101, 102, 103, 104}, {105, 106}] if program_id == "acme" else [{201, 202, 203, 204, 205}]
    assert [
        {row.work_item_id for group in section.groups for row in group.rows}
        for section in artifacts.sections
    ] == expected_ids
    item_ids = [row.work_item_id for section in artifacts.sections for group in section.groups for row in group.rows]
    assert len(item_ids) == len(set(item_ids)) == len(seed["work_items"])
    recipient_emails = {recipient.email for recipient in artifacts.to_recipients}
    assert "tpm@nudge-synth.local" in recipient_emails
    assert recipient_emails <= {
        "tpm@nudge-synth.local",
        *(item["fields"]["System.AssignedTo"]["uniqueName"] for item in seed["work_items"]),
    }
    assert all(email.endswith("@nudge-synth.local") for email in recipient_emails)
    assert len(artifacts.eml_paths) == 1, (
        f"{program_id}: expected exactly one EML path, got {artifacts.eml_paths}"
    )
    eml_path = artifacts.eml_paths[0]
    assert eml_path.exists(), f"{program_id}: EML file not written: {eml_path}"
    raw_eml = eml_path.read_text(encoding="utf-8")
    normalized = _normalize_eml(raw_eml)
    _compare_with_golden(golden_name, normalized, update)
    return normalized


def _registry_ids_for(seed: dict) -> list[int]:
    """Registry key_ado_items to seed.

    Single-section seeds register every item. Tag-based seeds register only
    the priority items absent from all tag groups.
    """
    tag_ids = {int(i) for ids in seed.get("work_items_by_tag", {}).values() for i in ids}
    all_ids = [int(item["id"]) for item in seed.get("work_items", [])]
    # Items not appearing in any tag section are registry-only.
    registry_only = [i for i in all_ids if i not in tag_ids]
    if registry_only:
        return registry_only
    # No tag sections → all items are registry candidates.
    return all_ids


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


def test_nudge_acme_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
    update_golden: bool,
) -> None:
    """Synthetic tag and registry sections match the committed EML snapshot."""
    _generate_normalized_eml(
        monkeypatch,
        repo_root,
        tmp_path,
        program_id="acme",
        seed_name="acme.json",
        golden_name="nudge_acme_full",
        update=update_golden,
    )


def test_nudge_contoso_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
    update_golden: bool,
) -> None:
    """Synthetic registry-only sections match the committed EML snapshot."""
    _generate_normalized_eml(
        monkeypatch,
        repo_root,
        tmp_path,
        program_id="contoso",
        seed_name="contoso.json",
        golden_name="nudge_contoso_full",
        update=update_golden,
    )
