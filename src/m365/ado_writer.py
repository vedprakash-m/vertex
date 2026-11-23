from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, cast

import portalocker

from src.core.actuation_outbox import (
    DispatchResult,
    _load_valid_success_receipt,
    create_task_idempotency_key,
    dispatch_leased_create_task,
    enqueue_create_task_intent,
    outbox_db_path,
    reconcile_uncertain_create_task,
)
from src.core.actuation_reconciliation import (
    AuthorizedEffect,
    EffectReconciliation,
    ProviderEffectEvidence,
    audience_manifest_hash,
    desired_effect_hash,
    reconcile_effect,
)
from src.core.ledger.durable_outbox_store import OutboxError, load_entry, load_success_receipt
from src.core.operation_capabilities import (
    approved_remote_marker,
    create_target_identity,
    normalize_semantic_payload,
    semantic_effect_key,
)
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
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, LeaseHeldByAnotherOwner


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

    def _is_synthetic_provider(self) -> bool:
        return getattr(self._client, "_vertex_synthetic_provider", False) is True

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

                live_row = self._load_live_row(entry) if entry.action == "create_task" else None
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
            approved_marker = task_data.get("approved_remote_marker")
            intent_marker = f"vertex-intent-{operation_intent_id}"
            if approved_marker is None:
                remote_tags = intent_marker
            elif (
                isinstance(approved_marker, str)
                and approved_marker.startswith("vertex-effect-")
                and approved_marker.strip()
            ):
                remote_tags = f"{intent_marker}; {approved_marker}"
            else:
                return replace(
                    entry,
                    entry_status="failed",
                    status_reason="Create approved marker is malformed; no provider request was issued.",
                )
            idempotency_key = create_task_idempotency_key(
                program_id=proposal.program_id,
                org=self._client.organization,
                project=self._client.project,
                operation_intent_id=operation_intent_id,
            )
            outbox_payload_json = json.dumps(
                {
                    "program_id": proposal.program_id,
                    "operation": "create_task.v1",
                    "operation_intent_id": operation_intent_id,
                    "proposal_id": proposal.id,
                    "causal_occurrence_id": entry.reason,
                    "org": self._client.organization,
                    "project": self._client.project,
                    "title": title,
                    "description": task_data.get("description"),
                    "assigned_to": task_data.get("assigned_to"),
                    "area_path": area_path,
                    "iteration_path": task_data.get("iteration_path"),
                    "priority": task_data.get("priority"),
                    "target_date": task_data.get("target_date"),
                    "tags": remote_tags,
                    "approved_remote_marker": approved_marker,
                    "authorized_effect": task_data.get("authorized_effect"),
                }
            )
            try:
                authorized_effect, effect_id = _authorized_create_effect_binding({
                    **task_data,
                    "program_id": proposal.program_id,
                    "org": self._client.organization,
                    "project": self._client.project,
                    "operation_intent_id": operation_intent_id,
                    "tags": remote_tags,
                    "causal_occurrence_id": entry.reason,
                    "approved_remote_marker": approved_marker,
                })
            except ValueError as error:
                return replace(
                    entry,
                    entry_status="failed",
                    operation_intent_id=operation_intent_id,
                    status_reason=(
                        f"Create reconciliation requires qualified authorization: {error}"
                    ),
                )
            retained_payload = json.loads(outbox_payload_json)
            assert isinstance(retained_payload, dict)
            retained_payload["semantic_effect_id"] = effect_id
            retained_payload["target_identity"] = authorized_effect.target_identity
            outbox_payload_json = json.dumps(retained_payload)

            # Search-before-create/retry: only when a prior attempt was
            # persisted (attempted_at set). A never-attempted entry has
            # nothing to reconcile against, so skip the extra WIQL round trip.
            if entry.attempted_at is not None or entry.outbox_id is not None:
                if not self._is_synthetic_provider():
                    return replace(
                        entry,
                        entry_status="failed",
                        operation_intent_id=operation_intent_id,
                        status_reason=(
                            "Live provider reconciliation is disabled; retain the outbox row "
                            "and use separately authorized provider qualification."
                        ),
                    )
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
                    existing_id, existing_rev, remote_fields = self._search_existing_create(
                        operation_intent_id=operation_intent_id,
                        area_path=area_path,
                        title=title,
                        task_data={
                            **task_data,
                            "tags": remote_tags,
                            "approved_remote_marker": approved_marker,
                        },
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
                retained_outbox = (
                    load_entry(
                        outbox_db_path(proposal.program_id, programs_root=self._programs_root),
                        entry.outbox_id,
                    )
                    if entry.outbox_id is not None
                    else None
                )
                if existing_id is None:
                    if retained_outbox is None or retained_outbox.status not in {
                        "pending", "uncertain_remote_state",
                    }:
                        return replace(
                            entry,
                            entry_status="failed",
                            operation_intent_id=operation_intent_id,
                            status_reason=(
                                "Verified-absence reconciliation requires a retained qualified pending or uncertain "
                                "outbox row before dispatch can continue."
                            ),
                        )
                    if retained_outbox.status == "uncertain_remote_state":
                        absent = reconcile_uncertain_create_task(
                            program_id=proposal.program_id,
                            idempotency_key=idempotency_key,
                            marker_lookup=lambda _entry: None,
                            reconciled_by="ado_writer:verified-absent",
                            programs_root=self._programs_root,
                        )
                        if absent.status != "pending":
                            return replace(
                                entry,
                                entry_status="failed",
                                operation_intent_id=operation_intent_id,
                                status_reason="Verified absence could not reopen the retained outbox row.",
                            )
                else:
                    if retained_outbox is None:
                        return replace(
                            entry,
                            entry_status="failed",
                            operation_intent_id=operation_intent_id,
                            status_reason=(
                                "Marker adoption requires a retained qualified outbox row and receipt."
                            ),
                        )
                    assert remote_fields is not None
                    if retained_outbox.status == "uncertain_remote_state":
                        reconciliation = _reconcile_recovered_create(
                            authorized_effect=authorized_effect,
                            outbox_entry=retained_outbox,
                            remote_id=existing_id,
                            remote_rev=existing_rev,
                            remote_fields=remote_fields,
                        )
                        if not reconciliation.completed:
                            return replace(
                                entry,
                                entry_status="failed",
                                operation_intent_id=operation_intent_id,
                                status_reason=(
                                    "ADO task reconciliation remains uncertain; provider readback did not "
                                    "certify recipient, target, value and principal."
                                ),
                            )
                        closeout = reconcile_uncertain_create_task(
                            program_id=proposal.program_id,
                            idempotency_key=idempotency_key,
                            marker_lookup=lambda _entry: existing_id,
                            reconciled_by="ado_writer:provider-readback",
                            reconciliation=reconciliation,
                            programs_root=self._programs_root,
                        )
                        if closeout.status != "completed":
                            return replace(
                                entry,
                                entry_status="failed",
                                operation_intent_id=operation_intent_id,
                                status_reason="ADO task recovery could not close the retained outbox row",
                            )
                    elif retained_outbox.status == "completed":
                        receipt_json = load_success_receipt(
                            outbox_db_path(proposal.program_id, programs_root=self._programs_root),
                            retained_outbox.outbox_id,
                        )
                        if not _completed_create_receipt_binds(
                            receipt_json=receipt_json,
                            authorized_effect=authorized_effect,
                            remote_id=existing_id,
                            remote_rev=existing_rev,
                        ):
                            return replace(
                                entry,
                                entry_status="failed",
                                operation_intent_id=operation_intent_id,
                                status_reason=(
                                    "Marker adoption requires the retained completed reconciliation "
                                    "receipt to match the provider readback."
                                ),
                            )
                    else:
                        return replace(
                            entry,
                            entry_status="failed",
                            operation_intent_id=operation_intent_id,
                            status_reason=(
                                f"Marker adoption cannot certify retained outbox status "
                                f"{retained_outbox.status!r}."
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
            enqueued = enqueue_create_task_intent(
                program_id=proposal.program_id,
                idempotency_key=idempotency_key,
                operation_intent_id=operation_intent_id,
                proposal_id=proposal.id,
                payload_json=outbox_payload_json,
                programs_root=self._programs_root,
                effect_id=effect_id,
                target_identity=authorized_effect.target_identity,
            )
            if enqueued.outbox_id != idempotency_key:
                try:
                    owner_payload = json.loads(enqueued.payload_json)
                except (TypeError, ValueError) as error:
                    return replace(
                        attempted_entry,
                        entry_status="failed",
                        status_reason=(
                            f"Semantic effect owner has invalid retained payload: {error}"
                        ),
                    )
                owner_intent = (
                    owner_payload.get("operation_intent_id")
                    if isinstance(owner_payload, dict)
                    else None
                )
                if not isinstance(owner_intent, str) or not owner_intent:
                    return replace(
                        attempted_entry,
                        entry_status="failed",
                        status_reason=(
                            "Semantic effect owner is missing its retained operation intent."
                        ),
                    )
                attempted_entry = replace(
                    attempted_entry,
                    operation_intent_id=owner_intent,
                    outbox_id=enqueued.outbox_id,
                )
                if persist_attempt is not None:
                    persist_attempt(attempted_entry)
                if enqueued.status == "completed":
                    owner_authorized: AuthorizedEffect | None = None
                    try:
                        owner_document, owner_receipt = _load_valid_success_receipt(
                            outbox_db_path(
                                proposal.program_id,
                                programs_root=self._programs_root,
                            ),
                            enqueued,
                            program_id=proposal.program_id,
                        )
                        owner_authorized = _authorized_create_effect(owner_payload)
                    except (OutboxError, TypeError, ValueError, AttributeError):
                        owner_document = {}
                        owner_receipt = None
                    remote_id = owner_document.get("remote_id")
                    remote_rev = owner_document.get("remote_rev")
                    validated_remote_id = cast(int, remote_id)
                    validated_remote_rev = (
                        cast(int, remote_rev)
                        if type(remote_rev) is int
                        else None
                    )
                    if (
                        not isinstance(owner_document, dict)
                        or owner_authorized is None
                        or type(remote_id) is not int
                        or validated_remote_id <= 0
                        or (
                            remote_rev is not None
                            and type(remote_rev) is not int
                        )
                        or not _completed_create_receipt_binds(
                            receipt_json=owner_receipt,
                            authorized_effect=owner_authorized,
                            remote_id=validated_remote_id,
                            remote_rev=validated_remote_rev,
                        )
                    ):
                        return replace(
                            attempted_entry,
                            entry_status="failed",
                            status_reason=(
                                "Semantic effect owner has no exact retained completion receipt."
                            ),
                        )
                    applied_entry = replace(
                        attempted_entry,
                        entry_status="applied",
                        work_item_id=validated_remote_id,
                        remote_rev=validated_remote_rev,
                        status_reason=(
                            "adopted the existing completed semantic effect from "
                            f"outbox {enqueued.outbox_id}"
                        ),
                    )
                    self._append_audit_signal(
                        proposal,
                        applied_entry,
                        applied_at=applied_at,
                        existing_signals=existing_signals,
                        signal_store=signal_store,
                        raw_ref=f"ado:{applied_entry.work_item_id}",
                        adopted_existing=True,
                    )
                    return applied_entry
            dispatch_outbox_id = enqueued.outbox_id

            dispatch_owner = f"ado_writer:{uuid.uuid4().hex[:12]}"
            try:
                outcome = dispatch_leased_create_task(
                    program_id=proposal.program_id,
                    idempotency_key=dispatch_outbox_id,
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

        operation = {
            "add_comment": "add_comment.v1",
            "add_tag": "add_tag.v1",
            "remove_tag": "add_tag.v1",
            "set_tags": "add_tag.v1",
            "set_field": "set_field.v1",
        }.get(entry.action)
        if operation is not None:
            return replace(
                entry,
                entry_status="failed",
                status_reason=(
                    f"{operation} is not qualified for provider dispatch; "
                    "direct writes are retired and the outbox boundary remains blocked."
                ),
            )
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
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="failed",
                status_reason="add_comment.v1 compensation is not qualified; rollback provider write is blocked by the outbox boundary.",
            )

        if entry.action == "set_field":
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="failed",
                status_reason="set_field.v1 compensation is not qualified; rollback provider write is blocked by the outbox boundary.",
            )

        if entry.action in {"add_tag", "remove_tag", "set_tags"}:
            return ADORollbackEntryResult(
                work_item_id=entry.work_item_id,
                action=entry.action,
                status="failed",
                status_reason="add_tag.v1 compensation is not qualified; rollback provider write is blocked by the outbox boundary.",
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
        if not self._is_synthetic_provider():
            return DispatchResult(
                succeeded=False,
                failure_reason=(
                    "Live ADO provider dispatch is disabled; qualified outbox migration "
                    "does not authorize a real provider write."
                ),
            )
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
        if fields.get("operation", "create_task.v1") != "create_task.v1":
            return DispatchResult(
                succeeded=False,
                failure_reason="outbox payload operation is not qualified for provider dispatch",
            )
        approved_marker = fields.get("approved_remote_marker")
        if approved_marker is not None and (
            not isinstance(approved_marker, str)
            or not approved_marker.strip()
            or approved_marker not in _parse_tags(
                fields["tags"] if isinstance(fields.get("tags"), str) else ""
            )
        ):
            return DispatchResult(
                succeeded=False,
                failure_reason=(
                    "outbox payload does not bind the approved marker to the create patch"
                ),
            )
        try:
            authorized_effect, _ = _authorized_create_effect_binding(fields)
        except ValueError as error:
            return DispatchResult(
                succeeded=False,
                failure_reason=f"create dispatch lacks a qualified authorized effect: {error}",
            )
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
        reconciliation = _reconcile_create_response(
            authorized_effect=authorized_effect,
            outbox_entry=outbox_entry,
            response=response,
            provider_response_hash=hashlib.sha256(
                json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            ).hexdigest(),
        )
        return DispatchResult(
            succeeded=True, remote_id=new_id, remote_rev=remote_rev,
            remote_response_hash=hashlib.sha256(
                json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            ).hexdigest(),
            reconciliation=reconciliation,
        )

    def _search_existing_create(
        self,
        *,
        operation_intent_id: str,
        area_path: str | None,
        title: str,
        task_data: dict[str, Any],
    ) -> tuple[int | None, int | None, dict[str, Any] | None]:
        """Require complete unique marker lookup and matching requested-field readback."""
        approved_marker = task_data.get("approved_remote_marker")
        tag = (
            approved_marker
            if isinstance(approved_marker, str) and approved_marker.strip()
            else f"vertex-intent-{operation_intent_id}"
        )
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
        if not tag_hits:
            return None, None, None
        if len(tag_hits) != 1:
            raise QueryError("create marker lookup returned multiple candidates")
        candidate_id = tag_hits[0]
        if type(candidate_id) is not int or candidate_id <= 0:
            raise QueryError("create marker lookup returned an invalid candidate ID")
        desired_fields = {
            item["path"].removeprefix("/fields/"): item["value"]
            for item in _build_create_task_patch(task_data)
            if item["path"] not in {"/fields/System.Title", "/fields/System.Tags"}
        }
        rows = self._client.query_work_items_batch(
            tag_hits, fields=("System.Title", "System.Tags", "System.CreatedBy", *desired_fields),
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
        return remote_id, remote_rev, fields

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


def _authorized_create_effect(fields: dict[str, Any]) -> AuthorizedEffect:
    raw = fields.get("authorized_effect")
    if not isinstance(raw, dict):
        raise ValueError("authorized effect binding is missing")
    authorized = AuthorizedEffect.from_dict(raw)
    if authorized.program_id != fields.get("program_id"):
        raise ValueError("authorized effect program does not match the retained intent")
    if authorized.intent_id != fields.get("operation_intent_id"):
        raise ValueError("authorized effect intent ID does not match the retained intent")
    if authorized.provider != "ado":
        raise ValueError("authorized effect provider is not ADO")
    if authorized.tenant_id != fields.get("org"):
        raise ValueError("authorized effect tenant does not match the ADO destination")
    if authorized.project != fields.get("project"):
        raise ValueError("authorized effect project does not match the ADO destination")
    if authorized.operation != "create_task.v1":
        raise ValueError("authorized effect operation is not create_task.v1")
    desired_value = {
        item["path"].removeprefix("/fields/"): item["value"]
        for item in _build_create_task_patch(fields)
    }
    causal_occurrence_id = fields.get("causal_occurrence_id")
    marker_bound = isinstance(fields.get("approved_remote_marker"), str)
    if (
        marker_bound
        and isinstance(causal_occurrence_id, str)
        and causal_occurrence_id.strip()
    ):
        expected_target = create_target_identity(
            provider="ado",
            tenant=str(fields.get("org") or ""),
            project=str(fields.get("project") or ""),
            causal_occurrence_id=causal_occurrence_id,
            payload=normalize_semantic_payload("create_task.v1", fields),
        )
    else:
        expected_target = f"{fields.get('project')}:new"
    if authorized.target_identity != expected_target:
        raise ValueError("authorized effect target does not match the create destination")
    if desired_effect_hash(authorized.desired_value) != desired_effect_hash(desired_value):
        raise ValueError("authorized effect desired value does not match the retained create payload")
    preflight = reconcile_effect(authorized, ())
    if preflight.status == "failed_terminal":
        raise ValueError("; ".join(preflight.mismatches))
    return authorized


def _authorized_create_effect_binding(
    fields: dict[str, Any],
) -> tuple[AuthorizedEffect, str]:
    authorized = _authorized_create_effect(fields)
    causal_occurrence_id = fields.get("causal_occurrence_id")
    retained_marker = fields.get("approved_remote_marker")
    if not isinstance(causal_occurrence_id, str) or not causal_occurrence_id.strip():
        if retained_marker is not None:
            raise ValueError("authorized effect causal occurrence is missing")
        fallback_intent = fields.get("operation_intent_id")
        if not isinstance(fallback_intent, str) or not fallback_intent.strip():
            raise ValueError("authorized effect causal occurrence is missing")
        causal_occurrence_id = fallback_intent
    effect_id = semantic_effect_key(
        program_id=authorized.program_id,
        provider=authorized.provider,
        tenant=authorized.tenant_id,
        project=authorized.project,
        operation=authorized.operation,
        target_identity=authorized.target_identity,
        causal_occurrence_id=causal_occurrence_id,
        payload=normalize_semantic_payload("create_task.v1", fields),
    )
    if retained_marker is not None:
        expected_marker = approved_remote_marker(
            operation=authorized.operation,
            semantic_effect_key=effect_id,
        )
        if retained_marker != expected_marker:
            raise ValueError(
                "approved remote marker does not bind the retained semantic effect"
            )
    return authorized, effect_id


def _completed_create_receipt_binds(
    *,
    receipt_json: str | None,
    authorized_effect: AuthorizedEffect,
    remote_id: int,
    remote_rev: int | None,
) -> bool:
    if receipt_json is None:
        return False
    try:
        receipt = json.loads(receipt_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(receipt, dict):
        return False
    reconciliation = receipt.get("reconciliation")
    if (
        not isinstance(reconciliation, dict)
        or reconciliation.get("status") != "completed"
        or reconciliation.get("completed") is not True
        or reconciliation.get("mismatches") != []
    ):
        return False
    receipts = reconciliation.get("receipts")
    expected_recipients: list[str | None] = list(authorized_effect.intended_recipients) or [None]
    if not isinstance(receipts, list) or len(receipts) != len(expected_recipients):
        return False
    if (
        receipt.get("remote_id") != remote_id
        or receipt.get("remote_rev") != remote_rev
        or receipt.get("operation") != authorized_effect.operation
        or receipt.get("operation_intent_id") != authorized_effect.intent_id
    ):
        return False
    manifest_hash = audience_manifest_hash(authorized_effect.audience_manifest)
    effect_hash = desired_effect_hash(authorized_effect.desired_value)
    for child, expected_recipient in zip(receipts, expected_recipients):
        if not isinstance(child, dict):
            return False
        if (
            child.get("recipient") != expected_recipient
            or child.get("terminal_status") != "completed"
            or child.get("intent_id") != authorized_effect.intent_id
            or child.get("approval_id") != authorized_effect.approval_id
            or child.get("provider") != authorized_effect.provider
            or child.get("tenant_id") != authorized_effect.tenant_id
            or child.get("project") != authorized_effect.project
            or child.get("operation") != authorized_effect.operation
            or child.get("target_identity") != authorized_effect.target_identity
            or child.get("remote_effect_id") != str(remote_id)
            or child.get("resulting_revision") != remote_rev
            or child.get("desired_effect_hash") != effect_hash
            or child.get("execution_principal") != authorized_effect.execution_principal
            or child.get("audience_manifest_hash") != manifest_hash
            or child.get("authorized_audience_recipients")
            != list(authorized_effect.audience_manifest.included_person_entity_ids)
        ):
            return False
    return True


def _reconcile_create_response(
    *,
    authorized_effect: AuthorizedEffect,
    outbox_entry: Any,
    response: dict[str, Any],
    provider_response_hash: str,
) -> EffectReconciliation:
    response_fields = response.get("fields")
    if not isinstance(response_fields, dict):
        response_fields = {}
    created_by = response_fields.get("System.CreatedBy")
    if isinstance(created_by, dict):
        created_by = created_by.get("uniqueName")
    principal = created_by if isinstance(created_by, str) else ""
    observed_value = {
        field_name: response_fields[field_name]
        for field_name in authorized_effect.desired_value
        if field_name in response_fields
    }
    dispatched_at = _provider_timestamp(response.get("dispatched_at"))
    observed_at = _provider_timestamp(response.get("observed_at"))
    resulting_revision = response.get("rev")
    preflight_revision = response.get("preflight_revision")
    attempt_count = getattr(outbox_entry, "attempt_count", 0)
    evidence = ProviderEffectEvidence(
        intent_id=_response_text(response, "intent_id"),
        approval_id=_response_text(response, "approval_id"),
        provider=_response_text(response, "provider"),
        tenant_id=_response_text(response, "provider_tenant_id"),
        project=_response_text(response, "provider_project"),
        operation=_response_text(response, "operation"),
        recipient=None,
        target_identity=_response_text(response, "provider_target_identity"),
        remote_effect_id=str(response.get("id") or ""),
        resulting_revision=resulting_revision if type(resulting_revision) is int else 0,
        preflight_revision=preflight_revision if type(preflight_revision) is int else 0,
        observed_value=observed_value,
        execution_principal=principal,
        provider_request_id=_response_text(response, "provider_request_id"),
        provider_correlation_id=_response_text(response, "provider_correlation_id"),
        response_hash=provider_response_hash,
        attempt_count=attempt_count if type(attempt_count) is int else 0,
        dispatched_at=dispatched_at,
        observed_at=observed_at,
        evidence_version=_response_text(response, "evidence_version"),
        policy_version=_response_text(response, "policy_version"),
    )
    return reconcile_effect(authorized_effect, (evidence,))


def _reconcile_recovered_create(
    *,
    authorized_effect: AuthorizedEffect,
    outbox_entry: Any,
    remote_id: int,
    remote_rev: int | None,
    remote_fields: dict[str, Any],
) -> EffectReconciliation:
    # The caller's W5.4 readback has already compared each requested field;
    # this normalized snapshot is the certified provider value handed to W5.9.
    observed_value = dict(authorized_effect.desired_value)
    created_by = remote_fields.get("System.CreatedBy")
    if isinstance(created_by, dict):
        created_by = created_by.get("uniqueName")
    evidence = ProviderEffectEvidence(
        intent_id=authorized_effect.intent_id,
        approval_id=authorized_effect.approval_id,
        provider=authorized_effect.provider,
        tenant_id=authorized_effect.tenant_id,
        project=authorized_effect.project,
        operation=authorized_effect.operation,
        recipient=None,
        target_identity=authorized_effect.target_identity,
        remote_effect_id=str(remote_id),
        resulting_revision=remote_rev if type(remote_rev) is int else 0,
        preflight_revision=authorized_effect.preflight_revision,
        observed_value=observed_value,
        execution_principal=created_by if isinstance(created_by, str) else "",
        provider_request_id=f"recovery:{outbox_entry.outbox_id}",
        provider_correlation_id=authorized_effect.intent_id,
        response_hash=hashlib.sha256(
            json.dumps(remote_fields, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest(),
        attempt_count=outbox_entry.attempt_count,
        dispatched_at=outbox_entry.created_at,
        observed_at=outbox_entry.updated_at,
        evidence_version=authorized_effect.evidence_version,
        policy_version=authorized_effect.policy_version,
    )
    return reconcile_effect(authorized_effect, (evidence,))


def _provider_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return parsed


def _response_text(response: dict[str, Any], key: str) -> str:
    value = response.get(key)
    return value if isinstance(value, str) else ""


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