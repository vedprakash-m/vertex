"""Deterministic golden EML snapshot tests for `vertex nudge` (full hygiene).

Spec: .archive/specs/fix-nudge.md §19.5, §24.7, §27.1, §28.2.

These tests stage the real local ``programs/nova`` and ``programs/armada`` trees
(via ``stage_v2_report_workspace``), inject a ``SeedADOClient`` that returns
synthetic work items/comments loaded from committed JSON fixtures, run
``generate_full_hygiene_nudges`` with a fixed ``as_of`` timestamp, normalize the
two nondeterministic EML fields (``Message-ID`` header + MIME boundary + run_id
token), and compare against committed ``.golden`` snapshots.

On a fresh-clone CI environment ``programs/`` is absent (gitignored) so the
staging helper skips cleanly.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import difflib
import pytest

from src.commands.nudge import generate_full_hygiene_nudges
from tests.support.report_test_setup import stage_v2_report_workspace

GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

# Fixed as_of timestamp — controls Date header, subject date label, and the
# strftime portion of the run_id. Must match the value used when the golden
# files were generated.
AS_OF = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# SeedADOClient — loads synthetic work items + comments from a JSON fixture.
# ---------------------------------------------------------------------------


class SeedADOClient:
    """Fake ADO client backed by a committed seed JSON fixture.

    Constructor swallows the ``organization=/project=/timeout=`` kwargs the
    production factory passes (we monkeypatch ``src.commands.nudge.ADOClient``
    with a lambda that only forwards the seed path).
    """

    def __init__(self, seed_path: Path, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self._seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
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


def _override_recipient(programs_root: Path, program_id: str, alias: str) -> None:
    """Pin ``full_hygiene.recipient`` to a generic alias in the staged edition.

    The real (gitignored) editions carry a personal recipient alias. We rewrite
    it to ``alias`` so the staged test data contains no personal identity; the
    matching people-directory entry is added by ``_seed_recipient``.
    """
    import yaml  # noqa: PLC0415

    edition_path = programs_root / program_id / "editions" / f"{program_id}_nudge.yaml"
    doc = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    doc.setdefault("full_hygiene", {})["recipient"] = alias
    edition_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _allow_synthetic_audience_domains(
    programs_root: Path,
    program_id: str,
    *,
    domains: tuple[str, ...] = ("microsoft.com", "nudge-synth.local"),
) -> None:
    """Relax the staged test audience policy for committed synthetic fixtures only."""
    import yaml  # noqa: PLC0415

    edition_path = programs_root / program_id / "editions" / f"{program_id}_nudge.yaml"
    doc = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    full_hygiene = doc.setdefault("full_hygiene", {})
    audience_policy = full_hygiene.setdefault("audience_policy", {})
    audience_policy["allowed_domains"] = list(domains)
    edition_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _seed_recipient(workspace_root: Path, alias: str, email: str, display_name: str) -> None:
    """Append a valid people-directory entry after staging sanitizes identities.

    ``stage_v2_report_workspace`` rewrites all aliases/emails in the shared
    knowledge dir to ``maintainer``/``maintainer@example.com`` (which
    ``_is_valid_email`` rejects). We re-add the generic recipient alias with a
    valid (non-``example.com``) email here.
    """
    import yaml  # noqa: PLC0415

    people_path = workspace_root / "knowledge" / "people_directory.yaml"
    doc = yaml.safe_load(people_path.read_text(encoding="utf-8"))
    doc.setdefault("people", []).append(
        {"alias": alias, "email": email, "display_name": display_name}
    )
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
    if update or golden is None:
        _save_golden(name, actual)
        if golden is None:
            pytest.skip(f"Created new golden file: {name}.golden")
        return

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
    stage_v2_report_workspace(
        repo_root,
        tmp_path,
        program_names=("nova", "armada"),
    )
    programs_root = tmp_path / "programs"
    # Rewrite the staged edition's recipient to a generic alias and add the
    # matching people-directory entry. A distinct alias (``tpm``) avoids
    # colliding with the ``maintainer`` entry staging writes into the shared
    # knowledge dir (whose maintainer@example.com email _is_valid_email rejects).
    _override_recipient(programs_root, program_id, alias="tpm")
    _allow_synthetic_audience_domains(programs_root, program_id)
    _seed_recipient(
        tmp_path,
        alias="tpm",
        email="tpm@microsoft.com",
        display_name="TPM Owner",
    )
    seed_path = FIXTURES_DIR / seed_name
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    # For registry-sourced programs (armada) every seed item is a registry key
    # item; for tag-sourced programs (nova) we register the non-tag item(s) so
    # the priority (registry) section is non-empty.
    _seed_registry(
        programs_root,
        program_id,
        key_ado_items=_registry_ids_for(seed),
    )

    monkeypatch.setattr(
        "src.commands.nudge.ADOClient",
        lambda *a, **kw: SeedADOClient(seed_path),  # noqa: E731
    )

    artifacts = generate_full_hygiene_nudges(
        program_id=program_id,
        dry_run=False,
        as_of=AS_OF,
        programs_root=programs_root,
        candidate_workers=1,
    )

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

    For armada (single registry section) all seed items are registry items.
    For nova the priority registry item is the one not present in any tag map
    (id 940010) plus we keep it minimal — only the non-tag item is registered.
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


def test_nudge_nova_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
    update_golden: bool,
) -> None:
    """NOVA full-hygiene EML matches committed golden snapshot."""
    _generate_normalized_eml(
        monkeypatch,
        repo_root,
        tmp_path,
        program_id="nova",
        seed_name="nudge_nova_seed.json",
        golden_name="nudge_nova_full",
        update=update_golden,
    )


def test_nudge_armada_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
    update_golden: bool,
) -> None:
    """Armada full-hygiene EML matches committed golden snapshot."""
    _generate_normalized_eml(
        monkeypatch,
        repo_root,
        tmp_path,
        program_id="armada",
        seed_name="nudge_armada_seed.json",
        golden_name="nudge_armada_full",
        update=update_golden,
    )
