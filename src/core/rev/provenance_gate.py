"""Email provenance gate — forge-EML mitigation (activation.md §6.14.9 / AG-17 / RK-23).

The trust root of the whole substrate is "a human approved a fact from a real
EML." Azure Content Safety scans *content* (toxicity / injection markers), not
*provenance* — so a forged EML that **agrees with stale ADO** would sail
through content checks and be approved on agreement alone (PS-20). This module
implements the named v1 mitigation: a **sender allowlist** (roadmap: DKIM
signature validation) so a candidate's source must come from a trusted sender
before it can enter the authority pipeline.

Design contract:
- The gate is **opt-in**: when no allowlist is configured for a program, every
  sender is admitted and ``admit`` returns ``ProvenanceVerdict.ok`` (the
  operator has *not* asserted a provenance boundary, so we do not fabricate
  one — this is honest degradation, not a silent bypass).
- When an allowlist *is* configured (``programs/<id>/sender_allowlist.yaml`` or
  the ``VERTEX_PROVENANCE_ALLOWLIST`` env override), a sender outside it yields
  ``ProvenanceVerdict.denied`` and the pipeline quarantines the candidate with
  a provenance reason (visible in ``doctor --rev-health``, never silently
  dropped). Domain entries (``@example.com``) allow any address in that domain.
- ``dkim_verified`` is plumbed through the verdict so a future DKIM validator
  can populate it without changing the contract; today it defaults to ``None``
  (unknown) and does not block — the allowlist is the binding v1 control.

This is the *forge-EML* half of the AG-17 threat model; the *forge-approval*
half is owned by ``src/core/operator_identity.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_ENV_ALLOWLIST = "VERTEX_PROVENANCE_ALLOWLIST"


@dataclass(frozen=True, slots=True)
class ProvenanceVerdict:
    """The provenance-gate verdict for one EML sender."""

    verdict: str  # "ok" | "denied" | "unconfigured"
    sender: str
    reason: str
    matched_rule: str | None = None
    dkim_verified: bool | None = None

    @property
    def admitted(self) -> bool:
        """Whether the candidate may proceed past the provenance gate.

        ``unconfigured`` (no allowlist pinned) is admitted — the gate is opt-in
        and open until an operator asserts a provenance boundary. Only an
        explicit ``denied`` blocks.
        """
        return self.verdict in {"ok", "unconfigured"}


def _normalize_sender(sender: str) -> str:
    return (sender or "").strip().lower()


def _domain_of(sender: str) -> str | None:
    normalized = _normalize_sender(sender)
    if "@" not in normalized:
        return None
    _, _, domain = normalized.partition("@")
    return domain or None


def _parse_allowlist(raw: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (addresses, domains) lowercased from a YAML allowlist payload."""
    if raw is None:
        return (), ()
    items: list[str] = []
    if isinstance(raw, list):
        items = [str(x) for x in raw if isinstance(x, (str, int))]
    elif isinstance(raw, dict):
        seq = raw.get("senders") or raw.get("allowed") or raw.get("addresses")
        if isinstance(seq, list):
            items = [str(x) for x in seq if isinstance(x, (str, int))]
        elif isinstance(raw.get("senders"), str):
            items = [str(raw["senders"])]
    addresses: list[str] = []
    domains: list[str] = []
    for item in items:
        clean = item.strip().lower()
        if not clean:
            continue
        if clean.startswith("@"):
            domains.append(clean[1:])
        elif "@" in clean:
            addresses.append(clean)
        else:
            domains.append(clean)  # bare domain
    return tuple(dict.fromkeys(addresses)), tuple(dict.fromkeys(domains))


def load_allowlist(program_id: str, *, programs_root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Load the per-program sender allowlist (addresses, domains).

    Honors the ``VERTEX_PROVENANCE_ALLOWLIST`` env override (comma-separated)
    so a fleet operator can assert one boundary without per-program files.
    Returns empty tuples when no allowlist is configured (gate stays open).
    """
    import os

    env_raw = os.environ.get(_ENV_ALLOWLIST, "").strip()
    if env_raw:
        return _parse_allowlist([s.strip() for s in env_raw.split(",") if s.strip()])
    path = programs_root / program_id / "sender_allowlist.yaml"
    if not path.exists():
        return (), ()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return (), ()
    return _parse_allowlist(payload)


def evaluate_sender(
    sender: str,
    *,
    addresses: tuple[str, ...],
    domains: tuple[str, ...],
    dkim_verified: bool | None = None,
) -> ProvenanceVerdict:
    """Evaluate one sender against a loaded allowlist (pure, side-effect-free)."""
    normalized = _normalize_sender(sender)
    if not addresses and not domains:
        return ProvenanceVerdict(
            verdict="unconfigured",
            sender=normalized,
            reason="no sender allowlist configured; provenance gate open (opt-in)",
        )
    if normalized in addresses:
        return ProvenanceVerdict(
            verdict="ok",
            sender=normalized,
            reason="sender matched allowlist address",
            matched_rule=normalized,
            dkim_verified=dkim_verified,
        )
    domain = _domain_of(normalized)
    if domain and domain in domains:
        return ProvenanceVerdict(
            verdict="ok",
            sender=normalized,
            reason=f"sender matched allowlist domain @{domain}",
            matched_rule=f"@{domain}",
            dkim_verified=dkim_verified,
        )
    return ProvenanceVerdict(
        verdict="denied",
        sender=normalized,
        reason="sender outside configured provenance allowlist (forge-EML guard, §6.14.9)",
        dkim_verified=dkim_verified,
    )


def admit(
    sender: str,
    *,
    program_id: str,
    programs_root: Path,
) -> ProvenanceVerdict:
    """Convenience: load the allowlist + evaluate one sender (returns verdict)."""
    addresses, domains = load_allowlist(program_id, programs_root=programs_root)
    return evaluate_sender(sender, addresses=addresses, domains=domains)


__all__ = [
    "ProvenanceVerdict",
    "load_allowlist",
    "evaluate_sender",
    "admit",
]
