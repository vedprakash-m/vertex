"""WI-7.2: AdoAdapter — governed ADO actuation (§6.11).

Zone A module.  Does NOT import from src.ai or src.m365.

The real ADO I/O is injected via ``ado_client_fn`` so this module stays
testable in Zone A without live network calls.  Production code passes a
lambda that returns the live ``ADOClient``; tests pass a stub.

Supported operations (§6.11.3):
  state_transition — update work-item State field
  comment         — add a comment to a work item
  work_item_create — create a new work item via gap-fix rules ONLY

work_item_create idempotency (R-21, v3.2):
  Before creating, the adapter calls ``check_exists_fn(payload)`` if
  provided.  If the item already exists it returns a "already_exists" error
  without writing.  On terminal failure the engine writes an ``action.failed``
  fact and suppresses re-proposals — human retry only, never auto re-create.
"""
from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.integration_protocol import ActuationResult


class AdoActuationAdapter:
    """Concrete ActuationAdapter for Azure DevOps.

    Parameters
    ----------
    ado_client_fn:
        Zero-argument callable that returns a live ADO client.
        Must expose:
          .update_work_item_state(work_item_id, state) -> dict
          .add_comment(work_item_id, text) -> dict
          .create_work_item(area_path, title, description) -> dict
        Pass None for dry-run-only usage (client is never called in dry_run mode).
    check_exists_fn:
        Optional callable ``(payload: dict) -> bool`` that returns True when
        a work item described by *payload* already exists in ADO.
        Used for work_item_create idempotency (R-21).
    lineage_writer:
        Optional callable ``(fact_dict: dict) -> None`` that persists the
        actuation lineage fact.  When omitted, lineage is silently skipped
        (tests can inject a recorder).
    """

    def __init__(
        self,
        *,
        ado_client_fn: Callable[[], Any] | None = None,
        check_exists_fn: Callable[[dict[str, Any]], bool] | None = None,
        lineage_writer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._ado_client_fn = ado_client_fn
        self._check_exists_fn = check_exists_fn
        self._lineage_writer = lineage_writer

    def execute(
        self,
        action_type: str,
        payload: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> "ActuationResult":
        from src.core.integration_protocol import ActuationResult

        if action_type == "state_transition":
            return self._execute_state_transition(payload, dry_run=dry_run)
        elif action_type == "comment":
            return self._execute_comment(payload, dry_run=dry_run)
        elif action_type == "work_item_create":
            return self._execute_work_item_create(payload, dry_run=dry_run)
        else:
            return ActuationResult(
                success=False,
                error_message=f"AdoAdapter: unsupported action_type {action_type!r}",
            )

    # ------------------------------------------------------------------
    # Operation implementations
    # ------------------------------------------------------------------

    def _execute_state_transition(
        self, payload: dict[str, Any], *, dry_run: bool
    ) -> "ActuationResult":
        from src.core.integration_protocol import ActuationResult

        work_item_id = payload.get("record_id") or payload.get("work_item_id")
        target_state = payload.get("target_state", "Closed")

        if dry_run:
            return ActuationResult(
                success=True,
                dry_run=True,
                external_ref=str(work_item_id),
            )

        try:
            client = self._get_client()
            result = client.update_work_item_state(work_item_id, target_state)
            external_ref = str(result.get("id", work_item_id))
            self._write_lineage("state_transition", payload, external_ref)
            return ActuationResult(success=True, external_ref=external_ref, dry_run=False)
        except Exception as exc:
            return ActuationResult(
                success=False,
                error_message=f"state_transition failed: {exc}",
            )

    def _execute_comment(
        self, payload: dict[str, Any], *, dry_run: bool
    ) -> "ActuationResult":
        from src.core.integration_protocol import ActuationResult

        work_item_id = payload.get("record_id") or payload.get("work_item_id")
        text = payload.get("text") or payload.get("description", "")

        if dry_run:
            return ActuationResult(
                success=True,
                dry_run=True,
                external_ref=str(work_item_id),
            )

        try:
            client = self._get_client()
            result = client.add_comment(work_item_id, text)
            external_ref = str(result.get("id", work_item_id))
            self._write_lineage("comment", payload, external_ref)
            return ActuationResult(success=True, external_ref=external_ref, dry_run=False)
        except Exception as exc:
            return ActuationResult(
                success=False,
                error_message=f"comment failed: {exc}",
            )

    def _execute_work_item_create(
        self, payload: dict[str, Any], *, dry_run: bool
    ) -> "ActuationResult":
        from src.core.integration_protocol import ActuationResult

        area_path = payload.get("area_path", "")
        title = payload.get("title") or payload.get("description", "")
        description = payload.get("description", "")

        # R-21: existence check before any write (idempotency)
        if self._check_exists_fn is not None and self._check_exists_fn(payload):
            return ActuationResult(
                success=False,
                error_message="work_item_create blocked: item already exists (R-21 idempotency). Human retry required — no auto re-create.",
            )

        if dry_run:
            return ActuationResult(
                success=True,
                dry_run=True,
                external_ref=None,
            )

        try:
            client = self._get_client()
            result = client.create_work_item(area_path, title, description)
            external_ref = str(result.get("id", ""))
            self._write_lineage("work_item_create", payload, external_ref)
            return ActuationResult(success=True, external_ref=external_ref, dry_run=False)
        except Exception as exc:
            return ActuationResult(
                success=False,
                error_message=f"work_item_create failed: {exc}",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        if self._ado_client_fn is None:
            raise RuntimeError(
                "AdoActuationAdapter: no ado_client_fn provided. "
                "Pass ado_client_fn to enable live execution."
            )
        return self._ado_client_fn()

    def _write_lineage(self, operation: str, payload: dict[str, Any], external_ref: str) -> None:
        if self._lineage_writer is None:
            return
        from datetime import datetime, timezone
        self._lineage_writer({
            "fact_type": "action.executed",
            "entity_refs": [str(payload.get("record_id") or payload.get("entity_ref", ""))],
            "payload": {
                "operation": operation,
                "external_ref": external_ref,
                **{k: v for k, v in payload.items() if k not in ("text",)},
            },
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        })
