"""Stakeholder-communication budget and cooldown enforcement (RELO-W4.7).

Evaluation is pure and deterministic. Send consumers opt into the fail-before-send
assertion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from src.core.exceptions import StateError

DEFAULT_MAX_ASKS = 5


@dataclass(frozen=True, slots=True)
class CommunicationBudgetFinding:
    code: str
    audience_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class CommunicationAudienceManifest:
    audience_id: str
    recipients: tuple[str, ...]
    material_asks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommunicationBudgetPolicy:
    max_asks: int = DEFAULT_MAX_ASKS
    cooldown_days: int = 0


def communication_budget_policy_from_edition(
    raw_edition: Mapping[str, Any] | None,
    *,
    default_cooldown_days: int,
) -> CommunicationBudgetPolicy:
    raw = raw_edition.get("communication_budget") if isinstance(raw_edition, Mapping) else None
    if raw is None:
        return CommunicationBudgetPolicy(cooldown_days=default_cooldown_days)
    if not isinstance(raw, Mapping):
        raise StateError("communication_budget must be a mapping in the edition config.")
    max_asks = raw.get("max_asks", raw.get("max_material_asks", DEFAULT_MAX_ASKS))
    cooldown_days = raw.get("cooldown_days", default_cooldown_days)
    if isinstance(max_asks, bool) or not isinstance(max_asks, int) or max_asks < 0:
        raise StateError("communication_budget.max_asks must be a non-negative integer.")
    if isinstance(cooldown_days, bool) or not isinstance(cooldown_days, int) or cooldown_days < 0:
        raise StateError("communication_budget.cooldown_days must be a non-negative integer.")
    return CommunicationBudgetPolicy(max_asks=max_asks, cooldown_days=cooldown_days)


def build_communication_audience_manifest(
    *,
    audience: Sequence[str],
    asks: Sequence[str],
) -> CommunicationAudienceManifest:
    recipients = tuple(
        dict.fromkeys(
            str(recipient).strip().lower()
            for recipient in audience
            if str(recipient).strip()
        )
    )
    material_asks = tuple(
        dict.fromkeys(str(ask).strip() for ask in asks if str(ask).strip())
    )
    audience_id = ", ".join(recipients) or "unknown-audience"
    return CommunicationAudienceManifest(audience_id, recipients, material_asks)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def evaluate_communication_budget(
    *,
    audience_id: str,
    asks: Sequence[str],
    prior_sends: Sequence[datetime],
    cooldown_days: int,
    now: datetime,
    max_asks: int = DEFAULT_MAX_ASKS,
) -> tuple[CommunicationBudgetFinding, ...]:
    if cooldown_days < 0:
        raise ValueError("cooldown_days must be >= 0")
    if max_asks < 0:
        raise ValueError("max_asks must be >= 0")
    _require_aware(now, "now")
    for sent in prior_sends:
        _require_aware(sent, "prior_sends")

    findings: list[CommunicationBudgetFinding] = []
    if len(asks) > max_asks:
        findings.append(CommunicationBudgetFinding(
            code="over_ask_budget",
            audience_id=audience_id,
            detail=f"{len(asks)} material asks/changes exceed maximum {max_asks}",
        ))
    if cooldown_days and prior_sends:
        last_sent = max(prior_sends)
        if now - last_sent < timedelta(days=cooldown_days):
            findings.append(CommunicationBudgetFinding(
                code="within_cooldown",
                audience_id=audience_id,
                detail=f"last send {last_sent.isoformat()} is within {cooldown_days}-day cooldown",
            ))
    return tuple(sorted(findings, key=lambda f: (f.code, f.audience_id, f.detail)))


def assert_communication_budget(
    *,
    manifest: CommunicationAudienceManifest,
    prior_sends: Sequence[datetime],
    cooldown_days: int,
    now: datetime,
    max_asks: int = DEFAULT_MAX_ASKS,
) -> None:
    findings = evaluate_communication_budget(
        audience_id=manifest.audience_id,
        asks=manifest.material_asks,
        prior_sends=prior_sends,
        cooldown_days=cooldown_days,
        now=now,
        max_asks=max_asks,
    )
    if findings:
        details = "; ".join(
            f"{finding.code}: {finding.detail}" for finding in findings
        )
        raise StateError(
            f"Communication budget blocks send to {manifest.audience_id}: {details}"
        )


def assert_nudge_communication_budget(
    *,
    raw_edition: Mapping[str, Any],
    audience_manifest: Mapping[str, Any],
    prior_index: Sequence[Mapping[str, Any]],
    work_item_ids: Sequence[int],
    now: datetime,
    default_cooldown_days: int = 14,
) -> CommunicationAudienceManifest:
    policy = communication_budget_policy_from_edition(
        raw_edition, default_cooldown_days=default_cooldown_days,
    )
    audience = tuple(
        str(address)
        for key in ("to_aliases", "cc_aliases", "bcc_aliases")
        for address in audience_manifest.get(key, ())
        if str(address).strip()
    )
    manifest = build_communication_audience_manifest(
        audience=audience,
        asks=tuple(f"ADO#{item_id}" for item_id in work_item_ids),
    )
    prior_sends = tuple(
        datetime.fromisoformat(str(entry["claimed_sent_at"]).replace("Z", "+00:00"))
        for entry in prior_index
        if entry.get("claimed_sent_at")
        and {
            str(address).strip().lower()
            for key in ("to_aliases", "cc_aliases", "bcc_aliases")
            for address in (entry.get("audience") or {}).get(key, ())
        }
        == set(manifest.recipients)
    )
    assert_communication_budget(
        manifest=manifest,
        prior_sends=prior_sends,
        cooldown_days=policy.cooldown_days,
        now=now,
        max_asks=policy.max_asks,
    )
    return manifest
