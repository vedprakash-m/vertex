from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

import portalocker

from src.core.actuation_outbox import DispatchResult, create_task_idempotency_key, dispatch_leased_create_task, enqueue_create_task_intent, outbox_db_path
from src.core.ledger.durable_outbox_store import load_entry
from src.core.ado_client import ADOClient
from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, open_locked_proposal_manifest, read_proposal_manifest_from_handle, write_proposal_manifest_to_handle
from src.core.exceptions import AuthError, ConfigError, CredentialExpired, MutationOutcomeUnknown, QueryError, QueryTimeoutError, StateError
from src.core.integration_types import PaginationOutcome
from src.core.journal import PROGRAMS_ROOT
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalReviewDecision
from src.core.signal_dedup import is_duplicate_signal
from src.core.signal_classification import classify_signal as _classify_signal
from src.core.store_factory import build_signal_store_for_program_id
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, LeaseHeldByAnotherOwner, acquire_lease, release_lease


log = logging.getLogger(__name__)
_ACTION_ASSOCIATION_WARNING = (
    "ADO entry is applied; local action association may be incomplete. Review the action history."
)


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

    @contextmanager
    def _actuation_lease(self, program_id: str) -> Iterator[None]:
        """ADF-W1.10 (Appendix A.11): every ADO mutation -- not just
        create_task, which owns its own lease via the outbox
        (``dispatch_leased_create_task``) -- serializes against the
        program's ``actuation_dispatch`` workspace lease domain. A short
        per-call acquire/release (rather than one lease held across the
        whole ``apply_manifest``/``rollback_manifest`` call) avoids
        conflicting with create_task's own self-contained acquire/release
        pairing, at the cost of not serializing an entire multi-entry apply
        as one atomic unit -- acceptable, since each individual ADO write is
        already independently safe (revision test-ops, duplicate checks).
        """
        owner = f"ado_writer:{uuid.uuid4().hex[:12]}"
        lease = acquire_lease(program_id, owner, mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=self._programs_root)
        try:
            yield
        finally:
            release_lease(lease, programs_root=self._programs_root)

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
                    if (
                        entry.action == "create_task"
                        and entry.entry_status == "applied"
                        and _ACTION_ASSOCIATION_WARNING in (entry.status_reason or "")
                    ):
                        log.warning(_ACTION_ASSOCIATION_WARNING)
                    continue

                # ADF-W1.2: lets _apply_entry persist attempted_at (and the
                # stable operation_intent_id) to the manifest BEFORE it
                # dispatches a create POST, so a lost response can be
                # reconciled via search-before-create on the next run instead
                # of silently duplicating the work item.
                def _persist_attempt(attempted_entry: ADOUpdateEntry, *, _index: int = index) -> None:
                    interim_entries = tuple(updated_entries) + (attempted_entry,) + proposal.entries[_index + 1 :]
                    interim_proposal = replace(proposal, entries=interim_entries)
                    interim_status = _derive_proposal_status(interim_proposal.entries, expired=False)
                    write_proposal_manifest_to_handle(manifest_handle, interim_proposal, proposal_status=interim_status)

                live_row = self._load_live_row(entry)
                try:
                    updated_entry = self._apply_entry(
                        proposal,
                        entry,
                        live_row=live_row,
                        applied_at=resolved_applied_at,
                        existing_signals=existing_signals,
                        signal_store=signal_store,
                        persist_attempt=_persist_attempt,
                    )
                except LeaseHeldByAnotherOwner as error:
                    # ADF-W1.10: every mutating action (not just create_task,
                    # which handles its own lease internally via the outbox)
                    # is now covered by the actuation_dispatch workspace
                    # lease -- surface contention as a failed entry rather
                    # than crashing the whole apply run.
                    updated_entry = replace(
                        entry, entry_status="failed", status_reason=f"actuation dispatch lease busy, retry later: {error}"
                    )
                updated_entries.append(updated_entry)
                association_unverified = False
                try:
                    if updated_entry.action == "create_task" and updated_entry.entry_status == "applied" and updated_entry.work_item_id:
                        association_unverified = True
                        from src.core.action_tracker import associate_action_with_work_item
                        try:
                            associate_action_with_work_item(
                                program_id=proposal.program_id,
                                action_id=updated_entry.reason,
                                work_item_id=updated_entry.work_item_id,
                                programs_root=self._programs_root,
                            )
                        except (
                            StateError, ConfigError, OSError, sqlite3.Error,
                            portalocker.exceptions.LockException, ValueError, TypeError, KeyError,
                        ):
                            log.warning(_ACTION_ASSOCIATION_WARNING)
                        else:
                            association_unverified = False
                finally:
                    if association_unverified:
                        updated_entries[-1] = replace(
                            updated_entry,
                            status_reason="; ".join(filter(None, (updated_entry.status_reason, _ACTION_ASSOCIATION_WARNING))),
                        )
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

        results_list: list[ADORollbackEntryResult] = []
        for entry in proposal.entries:
            try:
                results_list.append(
                    self._rollback_entry(
                        proposal,
                        entry,
                        action_id=action_id,
                        rolled_back_at=resolved_rolled_back_at,
                    )
                )
            except LeaseHeldByAnotherOwner as error:
                # ADF-W1.10: same contention-surfacing behavior as apply_manifest.
                results_list.append(
                    ADORollbackEntryResult(
                        work_item_id=entry.work_item_id,
                        action=entry.action,
                        status="failed",
                        status_reason=f"actuation dispatch lease busy, retry later: {error}",
                    )
                )
        results = tuple(results_list)
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
        persist_attempt: Any | None = None,
    ) -> ADOUpdateEntry:
        if entry.action == "create_task":
            try:
                task_data = json.loads(entry.proposed_value)
            except Exception as e:
                return replace(entry, entry_status="failed", status_reason=f"invalid task proposal json: {e}")

            # ADF-W1.2 (Appendix B.8): stable intent id, assigned once at
            # proposal-build time normally, but generated here as a fallback
            # for pre-ADF-W1.2 manifests that predate the field.
            if (entry.attempted_at is not None or entry.outbox_id is not None) and not entry.operation_intent_id:
                return replace(
                    entry, entry_status="failed",
                    status_reason="Create intent is missing; reconciliation required before recovery.",
                )
            operation_intent_id = entry.operation_intent_id or _new_operation_intent_id()
            title = str(task_data.get("title") or "")
            area_path = task_data.get("area_path")
            idempotency_key = create_task_idempotency_key(
                program_id=proposal.program_id,
                org=self._client.organization,
                project=self._client.project,
                operation_intent_id=operation_intent_id,
            )
            outbox_payload_json = json.dumps(
                {
                    "operation_intent_id": operation_intent_id,
                    "proposal_id": proposal.id,
                    "org": self._client.organization,
                    "project": self._client.project,
                    "title": title,
                    "description": task_data.get("description"),
                    "assigned_to": task_data.get("assigned_to"),
                    "area_path": area_path,
                    "iteration_path": task_data.get("iteration_path"),
                    "priority": task_data.get("priority"),
                    "target_date": task_data.get("target_date"),
                    "tags": f"vertex-intent-{operation_intent_id}",
                }
            )

            # Search-before-create/retry: only when a prior attempt was
            # persisted (attempted_at set). A never-attempted entry has
            # nothing to reconcile against, so skip the extra WIQL round trip.
            if entry.attempted_at is not None or entry.outbox_id is not None:
                if entry.outbox_id is not None and entry.outbox_id != idempotency_key:
                    return replace(
                        entry, entry_status="failed",
                        status_reason="Create intent or destination changed; reconciliation required before recovery.",
                    )
                if entry.outbox_id is not None:
                    retained = load_entry(
                        outbox_db_path(proposal.program_id, programs_root=self._programs_root),
                        entry.outbox_id,
                    )
                    if (
                        retained is None
                        or retained.domain != ACTUATION_DISPATCH_DOMAIN
                        or retained.correlation_id != entry.outbox_id
                        or retained.payload_json != outbox_payload_json
                    ):
                        return replace(
                            entry, entry_status="failed",
                            status_reason="Retained create intent is missing or changed; reconciliation required before recovery.",
                        )
                try:
                    existing_id, existing_rev = self._search_existing_create(
                        operation_intent_id=operation_intent_id,
                        area_path=area_path,
                        title=title,
                        task_data=task_data,
                    )
                except (AuthError, QueryError) as error:
                    return replace(
                        entry,
                        entry_status="failed",
                        operation_intent_id=operation_intent_id,
                        status_reason=(
                            f"ADO task creation requires reconciliation ({type(error).__name__}): "
                            "intent-marker lookup or readback could not be verified."
                        ),
                    )
                self._emit_duplicate_prevented(
                    proposal,
                    operation_intent_id=operation_intent_id,
                    existing_remote_id=existing_id,
                    detection="preflight_search",
                )
                adopted_entry = replace(
                    entry,
                    entry_status="applied",
                    work_item_id=existing_id,
                    remote_rev=existing_rev,
                    operation_intent_id=operation_intent_id,
                    status_reason="adopted existing work item via search-before-create (ADF-W1.2)",
                )
                self._append_audit_signal(
                    proposal, adopted_entry, applied_at=applied_at,
                    existing_signals=existing_signals, signal_store=signal_store,
                    raw_ref=f"ado:{existing_id}", adopted_existing=True,
                )
                return adopted_entry

            # ADF-W1.2: persist attempted_at + operation_intent_id BEFORE
            # dispatch. If the process dies or the response is lost after
            # this point but the server committed the write, the next apply
            # run's search-before-create step (above) will find it.
            attempted_entry = replace(
                entry, operation_intent_id=operation_intent_id, attempted_at=applied_at,
                outbox_id=idempotency_key,
            )
            if persist_attempt is not None:
                persist_attempt(attempted_entry)

            # ADF-W1.3 (Sec 8.11): dispatch is outbox-backed rather than an
            # inline POST. Enqueue is idempotent on operation_intent_id, so a
            # crash-retry with the same (already-persisted) intent id reuses
            # the existing row instead of enqueueing a duplicate.
            enqueue_create_task_intent(
                program_id=proposal.program_id,
                idempotency_key=idempotency_key,
                operation_intent_id=operation_intent_id,
                proposal_id=proposal.id,
                payload_json=outbox_payload_json,
                programs_root=self._programs_root,
            )

            dispatch_owner = f"ado_writer:{uuid.uuid4().hex[:12]}"
            try:
                outcome = dispatch_leased_create_task(
                    program_id=proposal.program_id,
                    idempotency_key=idempotency_key,
                    owner=dispatch_owner,
                    dispatch_fn=self._dispatch_create_task_outbox_entry,
                    programs_root=self._programs_root,
                )
            except LeaseHeldByAnotherOwner as error:
                return replace(
                    attempted_entry,
                    entry_status="failed",
                    status_reason=f"actuation dispatch lease busy, retry later: {error}",
                )

            if outcome.status == "completed" and outcome.remote_id:
                applied_entry = replace(
                    attempted_entry, entry_status="applied",
                    remote_rev=outcome.remote_rev, work_item_id=outcome.remote_id,
                )
                self._append_audit_signal(
                    proposal,
                    applied_entry,
                    applied_at=applied_at,
                    existing_signals=existing_signals,
                    signal_store=signal_store,
                    raw_ref=f"ado:{outcome.remote_id}",
                )
                return applied_entry
            if outcome.status == "uncertain_remote_state":
                return replace(
                    attempted_entry,
                    entry_status="failed",
                    status_reason=(
                        f"ADO task creation outcome uncertain (outbox key {idempotency_key}); "
                        "operator must reconcile before retrying"
                    ),
                )
            return replace(
                attempted_entry,
                entry_status="failed",
                status_reason=outcome.entry.failure_reason or "ADO task creation failed",
            )

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
            try:
                duplicate = self._has_duplicate_comment(entry, update_type=proposal.update_type)
            except (AuthError, QueryError) as error:
                return replace(entry, entry_status="failed", status_reason=f"comment history lookup failed: {error}")
            if duplicate is None:
                return replace(
                    entry,
                    entry_status="conflict",
                    status_reason="comment history incomplete; cannot safely check for duplicate",
                )
            if duplicate:
                return replace(entry, entry_status="skipped", status_reason="duplicate comment")
            with self._actuation_lease(proposal.program_id):
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
                with self._actuation_lease(proposal.program_id):
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
                with self._actuation_lease(proposal.program_id):
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
            try:
                duplicate = self._has_duplicate_comment_text(entry.work_item_id, rollback_comment)
            except (AuthError, QueryError) as error:
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="failed",
                    status_reason=f"comment history lookup failed: {error}",
                )
            if duplicate is None:
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="conflict",
                    status_reason="comment history incomplete; cannot safely check for duplicate",
                )
            if duplicate:
                return ADORollbackEntryResult(
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    status="skipped",
                    status_reason="rollback comment already posted",
                )
            try:
                with self._actuation_lease(proposal.program_id):
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
                with self._actuation_lease(proposal.program_id):
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
                with self._actuation_lease(proposal.program_id):
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

    def _has_duplicate_comment(self, entry: ADOUpdateEntry, *, update_type: str | None = None) -> bool | None:
        return self._comment_history_has_duplicate(entry.work_item_id, entry.proposed_value, update_type=update_type)

    def _has_duplicate_comment_text(self, work_item_id: int, comment_text: str) -> bool | None:
        return self._comment_history_has_duplicate(work_item_id, comment_text)

    def _comment_history_has_duplicate(
        self, work_item_id: int, comment_text: str, *, update_type: str | None = None,
    ) -> bool | None:
        signature = comment_text if update_type == "meeting_action" else _comment_signature(comment_text)
        if not signature:
            return False
        outcomes: list[object] = []

        def capture_pagination(outcome: PaginationOutcome) -> None:
            outcomes.append(outcome)

        comments = self._client.list_work_item_comments(work_item_id, on_pagination=capture_pagination)
        if len(outcomes) != 1:
            return None
        outcome = outcomes[0]
        if (
            not isinstance(outcome, PaginationOutcome)
            or type(outcome.total_fetched) is not int
            or outcome.total_fetched != len(comments)
            or type(outcome.page_count) is not int
            or outcome.page_count < 1
            or type(outcome.is_truncated) is not bool
            or outcome.is_truncated
        ):
            return None
        return any(
            (_comment_text(comment) if update_type == "meeting_action" else _comment_signature(_comment_text(comment)))
            == signature for comment in comments
        )

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
        adopted_existing: bool = False,
    ) -> None:
        signal = Signal(
            id=f"{proposal.id}-{entry.work_item_id}-{entry.action}",
            timestamp=applied_at,
            source="vertex/ado_update",
            program_id=proposal.program_id,
            workstream_id=None,
            entity_refs=(f"WI:{entry.work_item_id}",),
            text=(
                f"Adopted existing WI:{entry.work_item_id} from proposal {proposal.id}."
                if adopted_existing
                else f"Applied {entry.action} to WI:{entry.work_item_id} from proposal {proposal.id}."
            ),
            raw_ref=raw_ref,
            confidence=Confidence.HIGH,
            metadata={
                "proposal_id": proposal.id,
                "work_item_id": entry.work_item_id,
                "update_type": proposal.update_type,
                "action": entry.action,
                "field_or_tag": entry.field_or_tag,
                **({"adopted_existing": True} if adopted_existing else {}),
            },
            thread_id=None,
        )
        duplicate = is_duplicate_signal(signal, existing_signals)
        if duplicate:
            if (
                entry.action != "create_task"
                or not any(existing.id == signal.id for existing in existing_signals)
                or signal.id in signal_store.read_reviews(proposal.program_id)
            ):
                return
        else:
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
        if not duplicate:
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
        # ADF-W1.1: mutations use the non-retrying session (INV-ADF-9). A lost
        # response after a committed write must never be silently retried by
        # the transport layer.
        response = self._client._mutation_session.request(
            method,
            url,
            headers=headers,
            timeout=self._client.timeout,
            json=json_body,
            allow_redirects=False,
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
        if response.status_code == 429:
            # ADF-W1.1: honor Retry-After once as rate-limit courtesy, then
            # fail closed -- no automatic retry loop for a mutation.
            _sleep_once_for_retry_after(response.headers.get("Retry-After"))
            raise QueryError(
                f"ADO write rate-limited (429); no automatic retry for mutations: {response.text[:500]}"
            )
        if response.status_code == 408 or not 200 <= response.status_code < 500:
            raise MutationOutcomeUnknown(
                f"ADO write outcome unknown after HTTP {response.status_code}; reconciliation required, no automatic retry"
            )
        if 300 <= response.status_code < 400:
            raise MutationOutcomeUnknown(
                f"ADO write redirect not followed (HTTP {response.status_code}); reconciliation required, no automatic retry"
            )
        if response.status_code >= 400:
            raise QueryError(
                f"ADO write failed with status {response.status_code}: {response.text[:500]}"
            )
        if not getattr(response, "text", ""):
            return {}
        try:
            body = response.json()
        except ValueError as error:
            raise MutationOutcomeUnknown(
                "ADO write response is not valid JSON; reconciliation required, no automatic retry"
            ) from error
        if not isinstance(body, dict):
            raise MutationOutcomeUnknown(
                "ADO write response is not an object; reconciliation required, no automatic retry"
            )
        return body

    def _dispatch_create_task_outbox_entry(self, outbox_entry: Any) -> DispatchResult:
        """ADF-W1.3: the ``dispatch_fn`` handed to ``dispatch_leased_create_task``.

        Reads its fields from the outbox row's own ``payload_json`` rather
        than from a captured closure, so a *different* process that reclaims
        a stale lease (the original worker crashed mid-dispatch) can still
        complete this exact intent correctly.
        """
        try:
            fields = json.loads(outbox_entry.payload_json)
        except Exception as error:
            return DispatchResult(succeeded=False, failure_reason=f"corrupt outbox payload: {error}")
        if (
            not isinstance(fields, dict)
            or fields.get("org") != self._client.organization
            or fields.get("project") != self._client.project
        ):
            return DispatchResult(succeeded=False, failure_reason="outbox payload has missing or mismatched ADO destination")
        patch = _build_create_task_patch(fields)
        try:
            response = self._request_json(
                "POST",
                f"{self._client._rest_base_url}workitems/$Task?api-version=7.1",
                json_body=patch,
                content_type="application/json-patch+json",
            )
        except (MutationOutcomeUnknown, QueryTimeoutError):
            return DispatchResult(
                succeeded=False, uncertain=True,
                failure_reason="ADO task creation response is inconclusive; reconciliation required, no automatic retry",
            )
        except QueryError as error:
            return DispatchResult(succeeded=False, failure_reason=f"ADO task creation failed: {error}")
        new_id = response.get("id")
        if type(new_id) is not int or new_id <= 0:
            return DispatchResult(
                succeeded=False, uncertain=True,
                failure_reason="Create response lacks a valid work-item ID; manual reconciliation required",
            )
        remote_rev = response.get("rev")
        if remote_rev is not None and (type(remote_rev) is not int or remote_rev <= 0):
            return DispatchResult(
                succeeded=False, uncertain=True, remote_id=new_id,
                failure_reason="Create response contains an invalid revision; manual reconciliation required",
            )
        return DispatchResult(
            succeeded=True, remote_id=new_id, remote_rev=remote_rev,
            remote_response_hash=hashlib.sha256(
                json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            ).hexdigest(),
        )

    def _search_existing_create(
        self,
        *,
        operation_intent_id: str,
        area_path: str | None,
        title: str,
        task_data: dict[str, Any],
    ) -> tuple[int, int | None]:
        """Require complete unique marker lookup and matching requested-field readback."""
        tag = f"vertex-intent-{operation_intent_id}"
        tag_wiql = f"SELECT [System.Id] FROM WorkItems WHERE [System.Tags] CONTAINS '{_escape_wiql_literal(tag)}'"
        if area_path:
            tag_wiql += f" AND [System.AreaPath] UNDER '{_escape_wiql_literal(area_path)}'"
        outcomes: list[PaginationOutcome] = []
        tag_hits = self._client.execute_wiql(tag_wiql, on_pagination=outcomes.append)
        if (
            len(outcomes) != 1
            or not isinstance(outcomes[0], PaginationOutcome)
            or type(outcomes[0].total_fetched) is not int
            or outcomes[0].total_fetched != len(tag_hits)
            or type(outcomes[0].page_count) is not int
            or outcomes[0].page_count < 1
            or outcomes[0].is_truncated is not False
        ):
            raise QueryError("create marker lookup completeness is unproven")
        if len(tag_hits) != 1:
            raise QueryError("create marker lookup did not return exactly one candidate")
        candidate_id = tag_hits[0]
        if type(candidate_id) is not int or candidate_id <= 0:
            raise QueryError("create marker lookup returned an invalid candidate ID")
        desired_fields = {
            item["path"].removeprefix("/fields/"): item["value"]
            for item in _build_create_task_patch(task_data)
            if item["path"] not in {"/fields/System.Title", "/fields/System.Tags"}
        }
        rows = self._client.query_work_items_batch(
            tag_hits, fields=("System.Title", "System.Tags", *desired_fields),
        )
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise QueryError("create marker readback did not return the unique candidate")
        remote_id = rows[0].get("id")
        if type(remote_id) is not int or remote_id <= 0 or remote_id != candidate_id:
            raise QueryError("create marker readback did not return the unique candidate")
        fields = rows[0].get("fields")
        if not isinstance(fields, dict):
            raise QueryError("create marker readback did not return a field mapping")
        tags = fields.get("System.Tags")
        row_title = fields.get("System.Title")
        if (
            not isinstance(tags, str)
            or tag not in _parse_tags(tags)
            or not isinstance(row_title, str)
            or _normalize_title(row_title) != _normalize_title(title)
        ):
            raise QueryError("create marker readback does not match the retained marker and title")
        for field_name, desired in desired_fields.items():
            observed = fields.get(field_name)
            if field_name == "System.AssignedTo":
                if isinstance(observed, dict):
                    observed = observed.get("uniqueName")
                if (
                    not isinstance(desired, str)
                    or not isinstance(observed, str)
                    or not desired.strip()
                    or desired.strip().casefold() != observed.strip().casefold()
                ):
                    raise QueryError("create readback does not prove the requested assignee identity")
            elif type(observed) is not type(desired) or observed != desired:
                raise QueryError(f"create readback does not match requested field {field_name}")
        remote_rev = rows[0].get("rev")
        if remote_rev is not None and (type(remote_rev) is not int or remote_rev <= 0):
            raise QueryError("create marker readback returned an invalid revision")
        return remote_id, remote_rev

    def _emit_duplicate_prevented(
        self,
        proposal: ADOUpdateProposal,
        *,
        operation_intent_id: str,
        existing_remote_id: int,
        detection: str,
    ) -> None:
        """ADF-W0.18/W1.2: durable audit record for an adopted duplicate.

        Best-effort: a ledger write failure must never block the idempotent
        adoption itself (the entry is already safely marked applied).
        """
        try:
            from src.core.ledger.event_log import (
                ConfidenceTier,
                TemporalConfidence,
                build_event_envelope,
                write_event,
            )
            from src.core.ledger.source_refs import ADOWorkItemRef

            now = datetime.now(timezone.utc)
            source_ref = ADOWorkItemRef(
                org=self._client.organization,
                project=self._client.project,
                work_item_id=existing_remote_id,
            )
            envelope = build_event_envelope(
                program_id=proposal.program_id,
                event_type="actuation.duplicate_prevented.v1",
                occurred_at=now,
                recorded_at=now,
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                actor="ado_writer",
                payload={
                    "operation_intent_id": operation_intent_id,
                    "detection": detection,
                    "existing_remote_id": str(existing_remote_id),
                    "evidence": (
                        f"WIQL search matched existing work item {existing_remote_id} "
                        f"for intent {operation_intent_id}"
                    ),
                },
                source_ref=source_ref,
            )
            write_event(envelope, programs_root=self._programs_root)
        except Exception:  # pragma: no cover - audit write must never break adoption
            pass


def _new_operation_intent_id() -> str:
    return uuid.uuid4().hex


def _build_create_task_patch(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """ADF-W1.3: builds the JSON-Patch body from an outbox row's
    ``payload_json`` fields (same field set the pre-outbox inline POST used)."""
    patch: list[dict[str, Any]] = []
    title = fields.get("title")
    if title:
        patch.append({"op": "add", "path": "/fields/System.Title", "value": title})
    description = fields.get("description")
    if description:
        patch.append({"op": "add", "path": "/fields/System.Description", "value": description})
    assigned_to = fields.get("assigned_to")
    if assigned_to:
        patch.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assigned_to})
    area_path = fields.get("area_path")
    if area_path:
        patch.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
    iteration_path = fields.get("iteration_path")
    if iteration_path:
        patch.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
    priority = fields.get("priority")
    if priority is not None:
        patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority})
    target_date = fields.get("target_date")
    if target_date:
        patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Scheduling.TargetDate", "value": target_date})
    tags = fields.get("tags")
    if tags:
        patch.append({"op": "add", "path": "/fields/System.Tags", "value": tags})
    return patch


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    """ADF-W1.2 (Appendix B.8) title-equality normalization: lowercase, collapsed whitespace."""
    return _WHITESPACE_RE.sub(" ", title.strip().lower())


def _escape_wiql_literal(value: str) -> str:
    """Escape a single-quoted WIQL string literal (double embedded quotes)."""
    return value.replace("'", "''")


_MAX_RETRY_AFTER_SLEEP_SECONDS = 60.0


def _sleep_once_for_retry_after(header_value: str | None) -> float:
    """ADF-W1.1: sleep once for a 429 ``Retry-After`` header, then return.

    This is rate-limit courtesy, not a retry: the caller always raises after
    this returns. Accepts either the delta-seconds or HTTP-date form (RFC
    9110 10.2.3). Returns the number of seconds actually slept (0 if the
    header is absent or unparseable), so tests can assert on it without
    depending on wall-clock sleep.
    """
    seconds = _parse_retry_after_seconds(header_value)
    if seconds > 0:
        time.sleep(min(seconds, _MAX_RETRY_AFTER_SLEEP_SECONDS))
    return seconds


def _parse_retry_after_seconds(header_value: str | None) -> float:
    if not header_value:
        return 0.0
    stripped = header_value.strip()
    try:
        return max(0.0, float(stripped))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return 0.0
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = (target - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


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