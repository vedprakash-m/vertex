from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.exceptions import ConfigError
from src.core.models import EvidenceBinding, RunEvidenceSnapshot, RunManifest, Snapshot, SourceScopeContract

if TYPE_CHECKING:
    from src.core.gather_run_manifest import GatherRunManifest
    from src.core.program_fact_store import FactSnapshotPin, ProgramFactSnapshot


REPO_ROOT = Path(__file__).resolve().parents[2]
from src.core.edition_resolver import PROGRAMS_ROOT, get_program_output_dir


def get_output_root(edition: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root)


def get_manifest_path(edition: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_output_root(edition, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"


def build_run_manifest(
    manifest_id: str,
    issue_number: int,
    edition: str,
    started_at: datetime,
    ended_at: datetime,
    config_payload: Any,
    snapshot: Snapshot,
    html_content: str,
    markdown_content: str,
    ado_calls: int,
    ai_calls: int,
    ai_cost_usd: float,
    freshness_summary: dict[str, int],
    qg_results: dict[str, bool],
    git_sha: str | None,
    ai_cost_by_model: dict[str, float] | None = None,
    notes: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
    gather_run_id: str | None = None,
    gather_run_hash: str | None = None,
) -> RunManifest:
    return RunManifest(
        manifest_id=manifest_id,
        issue_number=issue_number,
        edition=edition,
        started_at=started_at,
        ended_at=ended_at,
        config_hash=_hash_jsonable(config_payload),
        snapshot_hash=_hash_jsonable(snapshot),
        html_hash=hash_content(html_content),
        md_hash=hash_content(markdown_content),
        ado_calls=ado_calls,
        ai_calls=ai_calls,
        ai_cost_usd=ai_cost_usd,
        ai_cost_by_model=dict(ai_cost_by_model or {}),
        freshness_summary=dict(freshness_summary),
        qg_results=dict(qg_results),
        git_sha=git_sha,
        notes=tuple(notes),
        metadata=dict(metadata or {}),
        gather_run_id=gather_run_id,
        gather_run_hash=gather_run_hash,
    )


def _validate_source_scope_binding(
    gather: GatherRunManifest | None,
    snapshot: ProgramFactSnapshot,
    report_snapshot: Snapshot,
) -> None:
    fetched_item_ids = {item.id for item in report_snapshot.items}
    if gather is not None and gather.query_results:
        scope_item_ids: set[int] = set()
        for row in gather.query_results:
            for value in row.membership_ids:
                text = str(value)
                if not text.isdigit() or int(text) <= 0:
                    raise ConfigError(
                        "Run evidence source scope membership is invalid."
                    )
                scope_item_ids.add(int(text))
        raw_counts = [getattr(row, "raw_count", None) for row in gather.query_results]
        declared_item_count = None
        if all(raw_count is not None for raw_count in raw_counts):
            declared_item_count = sum(
                max(0, int(raw_count))
                for raw_count in raw_counts
                if raw_count is not None
            )
        if (
            declared_item_count is not None
            and declared_item_count == len(fetched_item_ids)
            and scope_item_ids != fetched_item_ids
        ):
            raise ConfigError(
                "Run evidence rendered items do not match source scope membership."
            )
    for fact in snapshot.facts:
        if any(
            work_item_id not in fetched_item_ids
            for work_item_id in _work_item_entity_ids(fact.entity_refs)
        ):
            raise ConfigError(
                "Run evidence source scope binding does not match its retained facts."
            )


def _work_item_entity_ids(entity_refs: tuple[str, ...]) -> tuple[int, ...]:
    result: list[int] = []
    for ref in entity_refs:
        prefix, separator, raw_id = ref.upper().partition(":")
        if (
            separator
            and prefix == "WI"
            and raw_id.isdigit()
            and int(raw_id) > 0
        ):
            result.append(int(raw_id))
    return tuple(result)


def write_run_manifest(
    edition: str,
    issue_number: int,
    manifest: RunManifest,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_manifest_path(edition, issue_number, programs_root=programs_root)
    payload = _to_jsonable(manifest)
    _write_atomic_json(path, payload)
    return path


def hash_content(content: str | bytes) -> str:
    raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    return f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"


def hash_jsonable(value: Any) -> str:
    payload = json.dumps(_to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hash_content(payload)


_hash_jsonable = hash_jsonable


def chart_gate_evidence_sha256(
    manifest_id: str, qg_results: Mapping[str, bool], chart_freshness_evidence: Any,
    kusto_sections: Any, *, edition_type: str | None = None, routed_chart_sections: Any = None,
) -> str:
    return hash_jsonable({
        "manifest_id": manifest_id,
        "chart_gate_results": {
            gate: qg_results[gate] for gate in ("QG-21", "QG-22") if gate in qg_results
        },
        "chart_freshness_evidence": chart_freshness_evidence,
        "kusto_sections": kusto_sections,
        **({"edition_type": edition_type} if edition_type is not None else {}),
        **({"routed_chart_sections": routed_chart_sections} if routed_chart_sections is not None else {}),
    })


def build_run_evidence_snapshot(
    pin: FactSnapshotPin | None, snapshot: ProgramFactSnapshot | None,
    gather: GatherRunManifest | None, manifest: RunManifest, report_snapshot: Snapshot,
    *, effective_family_modes: tuple[tuple[str, str], ...],
) -> RunEvidenceSnapshot:
    if pin is None or snapshot is None or pin.program_id != snapshot.program_id:
        raise ConfigError("Run evidence retained fact capture is missing or inconsistent.")
    from src.core.program_fact_store import fact_snapshot_content_sha256

    if (
        pin.membership_version != 2 or pin.pinned_revision_count != len(snapshot.facts)
        or pin.content_sha256 != fact_snapshot_content_sha256(snapshot.facts)
    ):
        raise ConfigError("Run evidence retained fact content does not match its pin.")
    if any(fact.program_id != pin.program_id for fact in snapshot.facts):
        raise ConfigError("Run evidence retained fact entity does not match its program.")
    if any(
        _foreign_program_entity_ref(ref, pin.program_id)
        for fact in snapshot.facts
        for ref in fact.entity_refs
    ):
        raise ConfigError("Run evidence retained fact entity binding is invalid.")
    if (
        (gather.run_id if gather else None, gather.manifest_hash if gather else None)
        != (manifest.gather_run_id, manifest.gather_run_hash)
        or (gather is not None and gather.program_id != pin.program_id)
        or hash_jsonable(report_snapshot) != manifest.snapshot_hash
    ):
        raise ConfigError("Run evidence source or rendered snapshot identity is inconsistent.")
    if gather is not None and any(
        isinstance(row.wiql_hash, str) and not row.wiql_hash.strip() for row in gather.query_results
    ):
        raise ConfigError(
            "Run evidence saved-query identity is unavailable. Inspect failed gather diagnostics "
            "and gather again before regenerating the draft."
        )
    _validate_source_scope_binding(gather, snapshot, report_snapshot)
    source_scopes = tuple(sorted((
        SourceScopeContract(row.query_id, row.scope_id, row.wiql_hash)
        for row in (gather.query_results if gather else ())
    ), key=lambda row: (row.query_id, row.scope_id)))
    binding_declarations = tuple(sorted((
        EvidenceBinding(fact.revision_id, tuple(sorted(set(fact.entity_refs))))
        for fact in snapshot.facts
    ), key=lambda row: row.fact_revision_id))
    selectors = tuple(sorted(effective_family_modes))
    capture = RunEvidenceSnapshot(
        program_id=pin.program_id, fact_snapshot_id=pin.snapshot_id,
        fact_content_sha256=pin.content_sha256 or "",
        as_of=_capture_clock(snapshot.as_of), valid_at=_capture_clock(snapshot.valid_at),
        gather_run_id=manifest.gather_run_id, gather_run_hash=manifest.gather_run_hash,
        source_scopes=source_scopes,
        binding_declarations=binding_declarations,
        fetched_item_ids=tuple(sorted(item.id for item in report_snapshot.items)),
        report_snapshot_hash=manifest.snapshot_hash, config_hash=manifest.config_hash, sha256="",
        effective_family_modes=selectors,
        source_scope_sha256=_selection_identity("source-scope-set.v1", source_scopes),
        binding_set_sha256=_selection_identity("evidence-binding-set.v1", binding_declarations),
        selector_sha256=_selection_identity("authority-selector-set.v1", selectors),
        schema_version="run-evidence-capture.v3",
    )
    capture = replace(capture, sha256=hash_jsonable(_capture_content(capture)))
    return validate_run_evidence_snapshot(capture)


def validate_run_evidence_snapshot(capture: RunEvidenceSnapshot) -> RunEvidenceSnapshot:
    return decode_run_evidence_snapshot(_to_jsonable(capture))


def _capture_content(capture: RunEvidenceSnapshot) -> dict[str, Any]:
    payload = asdict(capture)
    payload.pop("sha256")
    if capture.schema_version in {
        "run-evidence-capture.v1",
        "run-evidence-capture.v2",
    }:
        payload.pop("source_scope_sha256")
        payload.pop("binding_set_sha256")
        payload.pop("selector_sha256")
    if capture.schema_version == "run-evidence-capture.v1":
        payload.pop("effective_family_modes")
    return payload


def _legacy_capture_content_with_identity_slots(capture: RunEvidenceSnapshot) -> dict[str, Any]:
    payload = asdict(capture)
    payload.pop("sha256")
    if capture.schema_version == "run-evidence-capture.v1":
        payload.pop("effective_family_modes")
    return payload


def _selection_identity(kind: str, value: object) -> str:
    return hash_jsonable({"kind": kind, "value": _to_jsonable(value)})


def _foreign_program_entity_ref(value: object, program_id: str) -> bool:
    if not isinstance(value, str):
        return False
    prefix, separator, entity_program_id = value.partition(":")
    return (
        separator == ":"
        and prefix.upper() == "PROGRAM"
        and entity_program_id != program_id
    )


def _capture_clock(value: Any) -> datetime:
    clock = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Invalid capture clock")
    return clock.astimezone(timezone.utc)


def decode_run_evidence_snapshot(value: object) -> RunEvidenceSnapshot:
    try:
        if not isinstance(value, Mapping):
            raise ValueError("Invalid capture object")
        payload = dict(value)
        schema_version = payload.get("schema_version")
        expected_fields = {field.name for field in fields(RunEvidenceSnapshot)}
        actual_fields = set(payload)
        if schema_version in {
            "run-evidence-capture.v1",
            "run-evidence-capture.v2",
        }:
            for legacy_identity in (
                "source_scope_sha256",
                "binding_set_sha256",
                "selector_sha256",
            ):
                payload.pop(legacy_identity, None)
            expected_fields -= {
                "source_scope_sha256",
                "binding_set_sha256",
                "selector_sha256",
            }
            actual_fields = set(payload)
        if schema_version == "run-evidence-capture.v1":
            expected_fields.remove("effective_family_modes")
        if actual_fields != expected_fields:
            raise ValueError("Invalid capture fields")
        if schema_version == "run-evidence-capture.v1":
            payload["effective_family_modes"] = ()
        payload["as_of"] = _capture_clock(payload["as_of"])
        payload["valid_at"] = _capture_clock(payload["valid_at"])
        payload["source_scopes"] = tuple(SourceScopeContract(**row) for row in payload["source_scopes"])
        payload["binding_declarations"] = tuple(
            EvidenceBinding(**{**row, "entity_refs": tuple(row["entity_refs"])})
            for row in payload["binding_declarations"]
        )
        payload["fetched_item_ids"] = tuple(payload["fetched_item_ids"])
        payload["effective_family_modes"] = tuple(
            tuple(row) for row in payload["effective_family_modes"]
        )
        payload["limitations"] = tuple(payload["limitations"])
        capture = RunEvidenceSnapshot(**payload)
        texts = (
            capture.program_id, capture.fact_snapshot_id, capture.report_snapshot_hash, capture.config_hash,
            *(text for row in capture.source_scopes for text in (row.query_id, row.scope_id, row.query_hash)),
            *(text for row in capture.binding_declarations for text in (row.fact_revision_id, *row.entity_refs)),
        )
        scopes = tuple((row.query_id, row.scope_id) for row in capture.source_scopes)
        revisions = tuple(row.fact_revision_id for row in capture.binding_declarations)
        family_modes = capture.effective_family_modes
        if (
            any(not isinstance(text, str) or not text.strip() for text in texts)
            or not isinstance(capture.fact_content_sha256, str)
            or len(capture.fact_content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in capture.fact_content_sha256)
            or any(type(item) is not int or item <= 0 for item in capture.fetched_item_ids)
            or capture.fetched_item_ids != tuple(sorted(set(capture.fetched_item_ids)))
            or scopes != tuple(sorted(set(scopes))) or revisions != tuple(sorted(set(revisions)))
            or any(row.entity_refs != tuple(sorted(set(row.entity_refs))) for row in capture.binding_declarations)
            or any(
                _foreign_program_entity_ref(ref, capture.program_id)
                for row in capture.binding_declarations
                for ref in row.entity_refs
            )
            or (capture.gather_run_id is None) != (capture.gather_run_hash is None)
            or any(value is not None and (not isinstance(value, str) or not value.strip())
                   for value in (capture.gather_run_id, capture.gather_run_hash))
            or capture.schema_version not in {
                "run-evidence-capture.v1",
                "run-evidence-capture.v2",
                "run-evidence-capture.v3",
            }
            or (
                capture.schema_version == "run-evidence-capture.v1"
                and family_modes
            )
            or (
                capture.schema_version in {
                    "run-evidence-capture.v2",
                    "run-evidence-capture.v3",
                }
                and family_modes
                and (
                    any(
                        len(row) != 2 or not all(isinstance(value, str) for value in row)
                        or row[0] not in {"judgment", "workitem.state"}
                        or row[1] not in {"legacy", "shadow", "primary"}
                        for row in family_modes
                    )
                    or family_modes != tuple(sorted(set(family_modes)))
                    or len({family for family, _ in family_modes}) != len(family_modes)
                )
            )
            or (
                capture.schema_version in {
                    "run-evidence-capture.v1",
                    "run-evidence-capture.v2",
                }
                and (
                    capture.source_scope_sha256
                    or capture.binding_set_sha256
                    or capture.selector_sha256
                )
            )
            or (
                capture.schema_version == "run-evidence-capture.v3"
                and (
                    capture.source_scope_sha256
                    != _selection_identity("source-scope-set.v1", capture.source_scopes)
                    or capture.binding_set_sha256
                    != _selection_identity("evidence-binding-set.v1", capture.binding_declarations)
                    or capture.selector_sha256
                    != _selection_identity("authority-selector-set.v1", family_modes)
                )
            )
            or capture.capture_state != "unqualified_capture"
            or capture.limitations != RunEvidenceSnapshot.__dataclass_fields__["limitations"].default
            or capture.sha256 not in {
                hash_jsonable(_capture_content(capture)),
                *(
                    (hash_jsonable(_legacy_capture_content_with_identity_slots(capture)),)
                    if capture.schema_version in {
                        "run-evidence-capture.v1",
                        "run-evidence-capture.v2",
                    }
                    else ()
                ),
            }
        ):
            raise ValueError("Invalid capture content")
        if capture.schema_version == "run-evidence-capture.v3":
            captured_ids = set(capture.fetched_item_ids)
            if any(
                work_item_id not in captured_ids
                for binding in capture.binding_declarations
                for work_item_id in _work_item_entity_ids(binding.entity_refs)
            ):
                raise ValueError(
                    "Run evidence source scope binding does not match the captured snapshot."
                )
        return capture
    except (TypeError, ValueError, KeyError) as error:
        raise ConfigError("Declared run evidence capture is invalid. Regenerate and review the draft.") from error


def load_run_evidence_snapshot(
    manifest_payload: Mapping[str, Any], draft_state: Mapping[str, Any],
) -> RunEvidenceSnapshot | None:
    metadata = manifest_payload.get("metadata") or {}
    reference = draft_state.get("run_evidence_snapshot_sha256")
    if not isinstance(metadata, dict):
        raise ConfigError("Declared run evidence metadata is invalid.")
    if "run_evidence_snapshot" not in metadata and reference is None:
        return None
    capture = decode_run_evidence_snapshot(metadata.get("run_evidence_snapshot"))
    pin = draft_state.get("program_fact_snapshot")
    if (
        not isinstance(pin, dict) or reference != capture.sha256
        or (pin.get("program_id"), pin.get("snapshot_id")) != (capture.program_id, capture.fact_snapshot_id)
        or manifest_payload.get("config_hash") != capture.config_hash
        or manifest_payload.get("snapshot_hash") != capture.report_snapshot_hash
        or any(
            (payload.get("gather_run_id"), payload.get("gather_run_hash"))
            != (capture.gather_run_id, capture.gather_run_hash)
            for payload in (manifest_payload, draft_state)
        )
    ):
        raise ConfigError("Run evidence capture does not match the captured draft and manifest.")
    return capture


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value
