"""WI-7.2 / RELO-W5.6: retained dry-run-only ADO actuation adapter.

Zone A module.  Does NOT import from src.ai or src.m365.

Live provider access is intentionally removed.  Qualified create-task effects
must enter the durable outbox owned by ``src.m365.ado_writer``; this class
retains the proposal dry-run contract without becoming a direct dispatch path.

Legacy constructor dependencies are retained for caller compatibility only;
they can no longer cause provider I/O or lineage writes.
"""
from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.integration_protocol import ActuationResult


class AdoActuationAdapter:
    """Retained dry-run actuation adapter; live execution is outbox-only."""

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

        if action_type not in {"state_transition", "comment", "work_item_create"}:
            return ActuationResult(
                success=False,
                error_message=f"AdoAdapter: unsupported action_type {action_type!r}",
            )
        if dry_run:
            work_item_id = payload.get("record_id") or payload.get("work_item_id")
            return ActuationResult(
                success=True,
                dry_run=True,
                external_ref=str(work_item_id),
            )
        return ActuationResult(
            success=False,
            error_message=(
                "AdoAdapter live dispatch is blocked; route the operation through "
                "the qualified durable outbox boundary."
            ),
        )
