"""Operator identity attestation (activation.md §6.15.2 / AG-17 / RK-23).

``write_authority == "human"`` is the system's trust root: a fact is
authoritative because a human approved it. That makes the approval event itself
the highest-value forge target (PS-20). This module captures an *authenticated
operator identity* — minimum v1 is the OS principal + machine (+ a per-session
id), recorded immutably in lineage — so AG-17's forge-approval mitigation is
falsifiable rather than asserted.

The roadmap (out of v1 scope) is SSO/AAD attestation; the contract here is the
shape that attestation flows into, so a future AAD-backed ``capture`` can drop
in without touching lineage serialization.
"""

from __future__ import annotations

import getpass
import os
import platform
import uuid
from dataclasses import dataclass

_ENV_PRINCIPAL = "VERTEX_OPERATOR_PRINCIPAL"
_ENV_MACHINE = "VERTEX_OPERATOR_MACHINE"

# A process-stable session id: set once per CLI invocation so two triage
# decisions in the same run share a session, but a fresh process is distinct.
# Persisted in the environment so subprocesses (tests, batch) inherit it.
_ENV_SESSION = "VERTEX_OPERATOR_SESSION_ID"


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _ensure_session_id() -> str:
    """Return the process session id, creating + persisting one if absent."""
    existing = os.environ.get(_ENV_SESSION)
    if existing:
        return existing
    new = _new_session_id()
    os.environ[_ENV_SESSION] = new
    return new


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    """The attested operator behind an approve/edit/revoke (§6.15.2).

    ``actor`` is the operator-supplied display name (the existing ``--actor``
    flag). ``principal``/``machine`` are captured from the environment so they
    cannot be trivially spoofed by the actor string alone; ``session`` groups a
    single CLI run. All are optional so headless/CI runs still produce a valid
    (if thinner) attestation, and a future AAD principal can populate the same
    fields.
    """

    actor: str
    principal: str | None
    machine: str | None
    session: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "actor": self.actor,
            "principal": self.principal,
            "machine": self.machine,
            "session": self.session,
        }


def capture_operator_identity(actor: str) -> OperatorIdentity:
    """Capture the current operator's attested identity (§6.15.2).

    ``actor`` is the operator-supplied display name. Principal/machine honor
    explicit overrides (``VERTEX_OPERATOR_PRINCIPAL``/``VERTEX_OPERATOR_MACHINE``)
    so a fleet operator can attest a service identity; otherwise they fall back
    to the OS user + host, which the operator cannot set via the ``--actor``
    flag. A headless run (no OS user resolvable) yields ``principal=None`` —
    explicitly thin, never silently synthesized.
    """
    principal = os.environ.get(_ENV_PRINCIPAL)
    if not principal:
        try:
            principal = getpass.getuser() or None
        except Exception:
            principal = None
    machine = os.environ.get(_ENV_MACHINE) or platform.node() or None
    session = _ensure_session_id()
    return OperatorIdentity(
        actor=actor or "unknown",
        principal=principal or None,
        machine=machine or None,
        session=session,
    )


__all__ = ["OperatorIdentity", "capture_operator_identity"]
