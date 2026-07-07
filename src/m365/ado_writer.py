from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.ado_client import ADOClient
from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, open_locked_proposal_manifest, read_proposal_manifest_from_handle, write_proposal_manifest_to_handle
from src.core.exceptions import CredentialExpired, QueryError
from src.core.journal import PROGRAMS_ROOT
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalReviewDecision
from src.core.signal_dedup import is_duplicate_signal
from src.core.signal_classification import classify_signal as _classify_signal
from src.core.store_factory import build_signal_store_for_program_id


@dataclass(frozen=True, slots=True)
class ADOApplyArtifacts:
    manifest_path: Path
    proposal: ADOUpdateProposal
    proposal_status: str
    applied_count: int
    skipped_count: int
    conflict_count: int
    failed_count: int


@dataclass(frozen=True, slots=True)
class ADORollbackEntryResult:
    work_item_id: int
    action: str
    status: str
    status_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ADORollbackArtifacts:
    manifest_path: Path
    proposal: ADOUpdateProposal
    action_id: str
    results: tuple[ADORollbackEntryResult, ...]
    rolled_back_count: int
    skipped_count: int
    conflict_count: int
    failed_count: int


class ADOWriter:
    def __init__(self, client: ADOClient, *, programs_root: Path = PROGRAMS_ROOT) -> None:
        self._client = client
        self._programs_root = programs_root

    def apply_manifest(
        self,
        manifest_path: Path,
        *,
        applied_at: datetime | None = None,
    ) -> ADOApplyArtifacts:
        with open_locked_proposal_manifest(manifest_path) as manifest_handle:
            proposal, proposal_status = read_proposal_manifest_from_handle(manifest_handle)
            resolved_applied_at = _ensure_utc(applied_at or datetime.now(timezone.utc))

            processable_statuses = {"pending", "failed"}
            has_pending_entries = any(entry.entry_status in processable_statuses for entry in proposal.entries)
            if has_pending_entries and resolved_applied_at > proposal.expires_at:
                expired_status = _derive_proposal_status(proposal.entries, expired=True)
                write_proposal_manifest_to_handle(manifest_handle, proposal, proposal_status=expired_status)
                raise ValueError(
                    f"Proposal '{proposal.id}' expired on {proposal.expires_at.isoformat()}. Re-run vertex ado propose."
                )

            signal_store = build_signal_store_for_program_id(proposal.program_id, programs_root=self._programs_root)
            existing_signals = list(signal_store.read(proposal.program_id))
            updated_entries: list[ADOUpdateEntry] = []

            for index, entry in enumerate(proposal.entries):
                if entry.entry_status not in processable_statuses:
                    updated_entries.append(entry)
                    continue

                live_row = self._load_live_row(entry)
                updated_entry = self._apply_entry(
                    proposal,
                    entry,
                    live_row=live_row,
                    applied_at=resolved_applied_at,
                    existing_signals=existing_signals,
                    signal_store=signal_store,
                )
                updated_entries.append(updated_entry)
                if updated_entry.action == "create_task" and updated_entry.entry_status == "applied" and updated_entry.work_item_id:
                    from src.core.action_tracker import associate_action_with_work_item
                    try:
                        associate_action_with_work_item(
                            program_id=proposal.program_id,
                            action_id=updated_entry.reason,
                            work_item_id=updated_entry.work_item_id,
                            programs_root=self._programs_root,
                        )
                    except Exception:
                        pass
                current_proposal = replace(
                    proposal,
                    entries=tuple(updated_entries + list(proposal.entries[index + 1 :])),
                )
                current_status = _derive_proposal_status(current_proposal.entries, expired=False)
                write_proposal_manifest_to_handle(manifest_handle, current_proposal, proposal_status=current_status)

            finalized_proposal = replace(proposal, entries=tuple(updated_entries))
            finalized_status = _derive_proposal_status(finalized_proposal.entries, expired=False)
            write_proposal_manifest_to_handle(manifest_handle, finalized_proposal, proposal_status=finalized_status)

            return ADOApplyArtifacts(
                manifest_path=manifest_path,
                proposal=finalized_proposal,
                proposal_status=finalized_status,
                applied_count=sum(1 for entry in finalized_proposal.entries if entry.entry_status == "applied"),
                skipped_count=sum(1 for entry in finalized_proposal.entries if entry.entry_status == "skipped"),
                conflict_count=sum(1 for entry in finalized_proposal.entries if entry.entry_status == "conflict"),
                failed_count=sum(1 for entry in finalized_proposal.entries if entry.entry_status == "failed"),
            )

    def rollback_manifest(
        self,
        manifest_path: Path,
        *,
        action_id: str,
        rolled_back_at: datetime | None = None,
    ) -> ADORollbackArtifacts:
        with open_locked_proposal_manifest(manifest_path) as manifest_handle:
            proposal, _ = read_proposal_manifest_from_handle(manifest_handle)
        resolved_rolled_back_at = _ensure_utc(rolled_back_at or datetime.now(timezone.utc))

        results = tuple(
            self._rollback_entry(
                proposal,
                entry,
                action_id=action_id,
                rolled_back_at=resolved_rolled_back_at,
            )
            for entry in proposal.entries
        )
        return ADORollbackArtifacts(
            manifest_path=manifest_path,
            proposal=proposal,
            action_id=action_id,
            results=results,
            rolled_back_count=sum(1 for result in results if result.status == "rolled_back"),
            skipped_count=sum(1 for result in results if result.status == "skipped"),
            conflict_count=sum(1 for result in results if result.status == "conflict"),
            failed_count=sum(1 for result in results if result.status == "failed"),
        )

    def _apply_entry(
        self,
        proposal: ADOUpdateProposal,
        entry: ADOUpdateEntry,
        *,
        live_row: dict[str, Any] | None,
        applied_at: datetime,
        existing_signals: list[Signal],
        signal_store,
    ) -> ADOUpdateEntry:
        if entry.action == "create_task":
            import json
            try:
                task_data = json.loads(entry.proposed_value)
            except Exception as e:
                return replace(entry, entry_status="failed", status_reason=f"invalid task proposal json: {e}")
            
            patch: list[dict[str, Any]] = []
            title = task_data.get("title")
            if title:
                patch.append({"op": "add", "path": "/fields/System.Title", "value": title})
            description = task_data.get("description")
            if description:
                patch.append({"op": "add", "path": "/fields/System.Description", "value": description})
            assigned_to = task_data.get("assigned_to")
            if assigned_to:
                patch.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assigned_to})
            area_path = task_data.get("area_path")
            if area_path:
                patch.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
            iteration_path = task_data.get("iteration_path")
            if iteration_path:
                patch.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
            priority = task_data.get("priority")
            if priority is not None:
                patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority})
            target_date = task_data.get("target_date")
            if target_date:
                patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Scheduling.TargetDate", "value": target_date})
            
            try:
                response = self._request_json(
                    "POST",
                    f"{self._client._rest_base_url}workitems/$Task?api-version=7.1",
                    json_body=patch,
                    content_type="application/json-patch+json",
                )
                new_id = response.get("id")
                if not new_id:
                    return replace(entry, entry_status="failed", status_reason="response did not return a new work item id")
                self._append_audit_signal(
                    proposal,
                    entry,
                    applied_at=applied_at,
                    existing_signals=existing_signals,
                    signal_store=signal_store,
                    raw_ref=f"ado:{new_id}",
                )
                return replace(entry, entry_status="applied", remote_rev=1, work_item_id=int(new_id))
            except QueryError as error:
                return replace(entry, entry_status="failed", status_reason=f"ADO task creation failed: {error}")

        if live_row is None:
            return replace(entry, entry_status="failed", status_reason="work item not found in ADO")

        live_rev = _coerce_int(live_row.get("rev"))

        if _has_revision_conflict(entry, live_row):
            return replace(
                entry,
                entry_status="conflict",
                status_reason="revision mismatch (preflight)",
                remote_rev=live_rev,
            )

        if entry.action == "add_comment":
            if self._has_duplicate_comment(entry):
                return replace(entry, entry_status="skipped", status_reason="duplicate comment")
            response = self._request_json(
                "POST",
                f"{self._client._rest_base_url}workItems/{entry.work_item_id}/comments?api-version=7.1-preview.4",
                json_body={"text": entry.proposed_value},
            )
            self._append_audit_signal(
                proposal,
                entry,
                applied_at=applied_at,
                existing_signals=existing_signals,
                signal_store=signal_store,
                raw_ref=_comment_raw_ref(response),
            )
            return replace(entry, entry_status="applied", remote_rev=live_rev)

        if entry.action in {"add_tag", "remove_tag", "set_tags"}:
            tag_value = self._apply_tag_update(entry, live_row)
            if tag_value is None:
                return replace(entry, entry_status="skipped", status_reason="tag already applied (no-op)")
            tag_patch: list[dict[str, Any]] = [{"op": "add", "path": "/fields/System.Tags", "value": tag_value}]
            if entry.revision_id is not None:
                tag_patch = _prepend_test_op(tag_patch, entry.revision_id)
            try:
                self._request_json(
                    "PATCH",
                    f"{self._client._rest_base_url}workItems/{entry.work_item_id}?api-version=7.1",
                    json_body=tag_patch,
                    content_type="application/json-patch+json",
                )
            except QueryError:
                return replace(entry, entry_status="conflict", status_reason="test op rejected (race)", remote_rev=live_rev)
            self._append_audit_signal(
                proposal,
                entry,
                applied_at=applied_at,
                existing_signals=existing_signals,
                signal_store=signal_store,
            )
            return replace(entry, entry_status="applied", remote_rev=live_rev)

        if entry.action == "set_field":
            live_value = _field_value(live_row, entry.field_or_tag)
            if _normalize_value(entry.current_value) != _normalize_value(live_value):
                return replace(entry, entry_status="conflict", status_reason="current value mismatch (staleness check)", remote_rev=live_rev)
            if _normalize_value(entry.proposed_value) == _normalize_value(live_value):
                return replace(entry, entry_status="skipped", status_reason="proposed value already in effect")
            field_patch: list[dict[str, Any]] = [{"op": "add", "path": f"/fields/{entry.field_or_tag}", "value": entry.proposed_value}]
            if entry.revision_id is not None:
                field_patch = _prepend_test_op(field_patch, entry.revision_id)
            try:
                self._request_json(
                    "PATCH",
                    f"{self._client._rest_base_url}workItems/{entry.work_item_id}?api-version=7.1",
                    json_body=field_patch,
                    content_type="application/json-patch+json",
                )
            except QueryError:
                return replace(entry, entry_status="conflict", status_reason="test op rejected (race)", remote_rev=live_rev)
            self._append_audit_signal(
                proposal,
                entry,
                applied_at=applied_at,
                existing_signals=existing_signals,
                signal_store=signal_store,
            )
            return replace(entry, entry_status="applied", remote_rev=live_rev)

        raise QueryError(f"Unsupported ADO proposal action: {entry.action}")

    def _rollback_entry(
        self,
        proposal: ADOUpdateProposal,
        entry: ADOUpdateEntry,
        *,
        action_id: str,
        rolled_back_at: datetime,
    ) -> ADORollbackEntryResult:
        if entry.entry_status != "applied":
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="skipped",
                status_reason="entry was not applied",
            )

        if entry.action == "create_task":
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="skipped",
                status_reason="rollback of task creation is not supported",
            )

        if entry.action == "add_comment":
            rollback_comment = _build_rollback_comment_body(
                proposal=proposal,
                entry=entry,
                action_id=action_id,
                rolled_back_at=rolled_back_at,
            )
            if self._has_duplicate_comment_text(entry.work_item_id, rollback_comment):
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="skipped",
                    status_reason="rollback comment already posted",
                )
            try:
                self._request_json(
                    "POST",
                    f"{self._client._rest_base_url}workItems/{entry.work_item_id}/comments?api-version=7.1-preview.4",
                    json_body={"text": rollback_comment},
                )
            except QueryError as error:
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="failed",
                    status_reason=str(error),
                )
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="rolled_back",
            )

        live_row = self._load_live_row(entry)
        if live_row is None:
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="failed",
                status_reason="work item not found in ADO",
            )

        live_rev = _coerce_int(live_row.get("rev"))
        if entry.action == "set_field":
            live_value = _field_value(live_row, entry.field_or_tag)
            if _normalize_value(live_value) == _normalize_value(entry.current_value):
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="skipped",
                    status_reason="field already reverted",
                )
            field_patch = _build_field_rollback_patch(entry, live_rev=live_rev)
            try:
                self._request_json(
                    "PATCH",
                    f"{self._client._rest_base_url}workItems/{entry.work_item_id}?api-version=7.1",
                    json_body=field_patch,
                    content_type="application/json-patch+json",
                )
            except QueryError:
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="conflict",
                    status_reason="rollback patch rejected (race)",
                )
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="rolled_back",
            )

        if entry.action in {"add_tag", "remove_tag", "set_tags"}:
            live_value = _field_value(live_row, "System.Tags")
            target_tags = _normalize_value(entry.current_value) or ""
            if _normalize_value(live_value) == _normalize_value(target_tags):
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="skipped",
                    status_reason="tags already reverted",
                )
            tag_patch: list[dict[str, Any]] = [{"op": "add", "path": "/fields/System.Tags", "value": target_tags}]
            if live_rev is not None:
                tag_patch = _prepend_test_op(tag_patch, live_rev)
            try:
                self._request_json(
                    "PATCH",
                    f"{self._client._rest_base_url}workItems/{entry.work_item_id}?api-version=7.1",
                    json_body=tag_patch,
                    content_type="application/json-patch+json",
                )
            except QueryError:
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="conflict",
                    status_reason="rollback patch rejected (race)",
                )
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="rolled_back",
            )

        return ADORollbackEntryResult(
            work_item_id=entry.work_item_id,
            action=entry.action,
            status="failed",
            status_reason=f"unsupported rollback action: {entry.action}",
        )

    def _load_live_row(self, entry: ADOUpdateEntry) -> dict[str, Any] | None:
        if entry.action == "create_task":
            return None
        fields = {"System.Id", "System.Rev"}
        if entry.action in {"add_tag", "remove_tag", "set_tags"}:
            fields.add("System.Tags")
        if entry.action == "set_field":
            fields.add(entry.field_or_tag)
        rows = self._client.query_work_items_batch([entry.work_item_id], fields=tuple(sorted(fields)))
        return rows[0] if rows else None

    def _has_duplicate_comment(self, entry: ADOUpdateEntry) -> bool:
        signature = _comment_signature(entry.proposed_value)
        if not signature:
            return False
        return any(_comment_signature(_comment_text(comment)) == signature for comment in self._client.list_work_item_comments(entry.work_item_id))

    def _has_duplicate_comment_text(self, work_item_id: int, comment_text: str) -> bool:
        signature = _comment_signature(comment_text)
        if not signature:
            return False
        return any(_comment_signature(_comment_text(comment)) == signature for comment in self._client.list_work_item_comments(work_item_id))

    def _apply_tag_update(self, entry: ADOUpdateEntry, live_row: dict[str, Any]) -> str | None:
        current_tags = _parse_tags(_field_value(live_row, "System.Tags"))
        target_tag = entry.proposed_value.strip()
        current_lookup = {tag.lower(): tag for tag in current_tags}

        if entry.action == "add_tag":
            if target_tag.lower() in current_lookup:
                return None
            return "; ".join(current_tags + [target_tag])

        if entry.action == "remove_tag":
            if target_tag.lower() not in current_lookup:
                return None
            return "; ".join(tag for tag in current_tags if tag.lower() != target_tag.lower())

        proposed_tags = _parse_tags(entry.proposed_value)
        if [tag.lower() for tag in proposed_tags] == [tag.lower() for tag in current_tags]:
            return None
        return "; ".join(proposed_tags)

    def _append_audit_signal(
        self,
        proposal: ADOUpdateProposal,
        entry: ADOUpdateEntry,
        *,
        applied_at: datetime,
        existing_signals: list[Signal],
        signal_store,
        raw_ref: str | None = None,
    ) -> None:
        signal = Signal(
            id=f"{proposal.id}-{entry.work_item_id}-{entry.action}",
            timestamp=applied_at,
            source="vertex/ado_update",
            program_id=proposal.program_id,
            workstream_id=None,
            entity_refs=(f"WI:{entry.work_item_id}",),
            text=f"Applied {entry.action} to WI:{entry.work_item_id} from proposal {proposal.id}.",
            raw_ref=raw_ref,
            confidence=Confidence.HIGH,
            metadata={
                "proposal_id": proposal.id,
                "work_item_id": entry.work_item_id,
                "update_type": proposal.update_type,
                "action": entry.action,
                "field_or_tag": entry.field_or_tag,
            },
            thread_id=None,
        )
        if is_duplicate_signal(signal, existing_signals):
            return
        signal_store.append(_classify_signal(signal))
        signal_store.append_review(
            proposal.program_id,
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=applied_at,
                reviewed_by="system",
                note=None,
            ),
        )
        existing_signals.append(signal)

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        headers = dict(self._client._headers())
        headers["Content-Type"] = content_type
        response = self._client._session.request(
            method,
            url,
            headers=headers,
            timeout=self._client.timeout,
            json=json_body,
        )
        if response.status_code == 401:
            www_auth = response.headers.get("WWW-Authenticate", "")
            raise CredentialExpired(
                f"ADO write returned 401 Unauthorized — PAT or AAD token may have expired. "
                f"Run 'vertex admin auth setup' to refresh credentials. "
                f"({www_auth[:200]})",
                auth_method=self._client.auth_method or "unknown",
                connector="ADO",
            )
        if response.status_code >= 400:
            raise QueryError(
                f"ADO write failed with status {response.status_code}: {response.text[:500]}"
            )
        if not getattr(response, "text", ""):
            return {}
        return response.json()


def _derive_proposal_status(entries: tuple[ADOUpdateEntry, ...], *, expired: bool) -> str:
    if expired and any(entry.entry_status in {"pending", "failed"} for entry in entries):
        return "expired"
    if entries and all(entry.entry_status in {"applied", "skipped"} for entry in entries):
        return "applied"
    if any(entry.entry_status != "pending" for entry in entries):
        return "partially_applied"
    return "pending"


def _prepend_test_op(patch_body: list[dict[str, Any]], revision_id: int) -> list[dict[str, Any]]:
    """Prepend a JSON Patch test op to assert System.Rev before the write."""
    return [{"op": "test", "path": "/fields/System.Rev", "value": revision_id}] + patch_body


def _has_revision_conflict(entry: ADOUpdateEntry, live_row: dict[str, Any]) -> bool:
    if entry.revision_id is None:
        return False
    live_revision = _coerce_int(live_row.get("rev"))
    if live_revision is None:
        live_revision = _coerce_int(_field_value(live_row, "System.Rev"))
    return live_revision is None or live_revision != entry.revision_id


def _field_value(row: dict[str, Any], field_name: str) -> str | None:
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    value = fields.get(field_name)  # type: ignore[union-attr]
    if value is None:
        return None
    return str(value)


def _parse_tags(value: str | None) -> list[str]:
    if value is None:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def _normalize_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _comment_signature(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _comment_text(comment: dict[str, Any]) -> str:
    if isinstance(comment.get("text"), str):
        return str(comment["text"])
    if isinstance(comment.get("renderedText"), str):
        return str(comment["renderedText"])
    return ""


def _comment_raw_ref(response: dict[str, Any]) -> str | None:
    comment_id = _coerce_int(response.get("id"))
    if comment_id is None:
        return None
    return f"comment:{comment_id}"


def _build_rollback_comment_body(
    *,
    proposal: ADOUpdateProposal,
    entry: ADOUpdateEntry,
    action_id: str,
    rolled_back_at: datetime,
) -> str:
    header = f"Vertex rollback {action_id}"
    lines = [
        header,
        (
            f"This withdraws the prior Vertex {proposal.update_type} comment from proposal {proposal.id} "
            f"for WI:{entry.work_item_id}."
        ),
        f"Rolled back at {rolled_back_at.isoformat()}.",
    ]
    return "\n".join(lines)


def _build_field_rollback_patch(entry: ADOUpdateEntry, *, live_rev: int | None) -> list[dict[str, Any]]:
    if entry.current_value is None:
        patch_body: list[dict[str, Any]] = [{"op": "remove", "path": f"/fields/{entry.field_or_tag}"}]
    else:
        patch_body = [{"op": "add", "path": f"/fields/{entry.field_or_tag}", "value": entry.current_value}]
    if live_rev is not None:
        patch_body = _prepend_test_op(patch_body, live_rev)
    return patch_body


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)