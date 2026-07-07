from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from typing import Any

import yaml

from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, resolve_edition_paths


@dataclass(frozen=True, slots=True)
class TrustedBaselineHistoryEntry:
    issue: int
    at: datetime
    by: str | None
    action: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LastUntrustedIssue:
    issue: int
    at: datetime
    by: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class TrustedBaseline:
    schema_version: str
    edition: str
    trusted_issue_number: int | None
    established_at: datetime
    established_by: str | None
    notes: str | None = None
    history: tuple[TrustedBaselineHistoryEntry, ...] = ()
    last_untrusted: LastUntrustedIssue | None = None
    bridge_graduated: bool = False
    graduated_at: datetime | None = None
    graduation_issue: int | None = None
    # Hardlock: issue numbers whose confirmed/working artifacts (snapshot, overrides) are
    # protected from overwrite. The trusted issue is always treated as locked even if not
    # listed here (see baseline_lock.locked_issues_for_baseline).
    locked_issues: tuple[int, ...] = ()


def get_trusted_baseline_path(
    edition: str,
    *,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path | None:
    resolved_paths = resolve_edition_paths(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved_paths is None:
        return None
    return resolved_paths.program_dir / "trusted_baseline.yaml"


def load_trusted_baseline(
    edition: str,
    *,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrustedBaseline | None:
    path = get_trusted_baseline_path(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    return _load_trusted_baseline_from_path(path, edition_fallback=edition)


def load_trusted_baseline_for_program(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrustedBaseline | None:
    return _load_trusted_baseline_from_path(
        programs_root / program_id / "trusted_baseline.yaml",
        edition_fallback=program_id,
    )


def load_trusted_baseline_issue(
    edition: str,
    *,
    before_issue_number: int | None = None,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> int | None:
    baseline = load_trusted_baseline(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if baseline is None:
        return None
    if baseline.trusted_issue_number is None:
        return None
    if before_issue_number is not None and baseline.trusted_issue_number >= before_issue_number:
        return None
    return baseline.trusted_issue_number


def save_trusted_baseline(
    edition: str,
    document: TrustedBaseline,
    *,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path | None:
    path = get_trusted_baseline_path(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if path is None:
        return None

    payload = {
        "schema_version": document.schema_version,
        "edition": document.edition,
        "trusted_issue_number": document.trusted_issue_number,
        "established_at": document.established_at.isoformat(),
        "established_by": document.established_by,
        "notes": document.notes,
        "history": [
            {
                "issue": entry.issue,
                "at": entry.at.isoformat(),
                "by": entry.by,
                "action": entry.action,
                "reason": entry.reason,
            }
            for entry in document.history
        ],
        "last_untrusted": (
            {
                "issue": document.last_untrusted.issue,
                "at": document.last_untrusted.at.isoformat(),
                "by": document.last_untrusted.by,
                "reason": document.last_untrusted.reason,
            }
            if document.last_untrusted is not None
            else None
        ),
        "bridge_graduated": document.bridge_graduated,
        "graduated_at": (document.graduated_at.isoformat() if document.graduated_at is not None else None),
        "graduation_issue": document.graduation_issue,
        "locked_issues": list(document.locked_issues),
    }
    _write_atomic_yaml(path, payload)
    return path


def _load_trusted_baseline_from_path(path: Path | None, *, edition_fallback: str) -> TrustedBaseline | None:
    if path is None or not path.exists():
        return None

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    history_payload = payload.get("history", [])
    history = tuple(
        TrustedBaselineHistoryEntry(
            issue=int(entry["issue"]),
            at=_parse_datetime(entry.get("at")),
            by=_optional_string(entry.get("by")),
            action=str(entry.get("action", "established")),
            reason=_optional_string(entry.get("reason")),
        )
        for entry in history_payload
        if isinstance(entry, dict)
    )
    last_untrusted_payload = payload.get("last_untrusted")
    last_untrusted = None
    if isinstance(last_untrusted_payload, dict):
        last_untrusted = LastUntrustedIssue(
            issue=int(last_untrusted_payload["issue"]),
            at=_parse_datetime(last_untrusted_payload.get("at")),
            by=_optional_string(last_untrusted_payload.get("by")),
            reason=str(last_untrusted_payload.get("reason", "")).strip(),
        )
    return TrustedBaseline(
        schema_version=str(payload.get("schema_version", "1.0")),
        edition=str(payload.get("edition", edition_fallback)),
        trusted_issue_number=(
            int(payload["trusted_issue_number"])
            if payload.get("trusted_issue_number") is not None
            else None
        ),
        established_at=_parse_datetime(payload.get("established_at")),
        established_by=_optional_string(payload.get("established_by")),
        notes=_optional_string(payload.get("notes")),
        history=history,
        last_untrusted=last_untrusted,
        bridge_graduated=bool(payload.get("bridge_graduated", False)),
        graduated_at=(
            _parse_datetime(payload.get("graduated_at"))
            if payload.get("graduated_at") not in (None, "")
            else None
        ),
        graduation_issue=(int(payload["graduation_issue"]) if payload.get("graduation_issue") is not None else None),
        locked_issues=tuple(
            sorted({int(value) for value in (payload.get("locked_issues") or []) if value is not None})
        ),
    )


def advance_trusted_baseline(
    edition: str,
    issue_number: int,
    *,
    established_at: datetime,
    established_by: str | None,
    notes: str | None = None,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrustedBaseline | None:
    current = load_trusted_baseline(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if current is not None and current.trusted_issue_number is not None and issue_number <= current.trusted_issue_number:
        return current

    action = "established" if current is None else "advanced"
    history = (
        ()
        if current is None
        else current.history
    ) + (
        TrustedBaselineHistoryEntry(
            issue=issue_number,
            at=established_at,
            by=established_by,
            action=action,
        ),
    )
    document = TrustedBaseline(
        schema_version="1.0",
        edition=edition,
        trusted_issue_number=issue_number,
        established_at=established_at,
        established_by=established_by,
        notes=notes if notes is not None else (current.notes if current is not None else None),
        history=history,
        last_untrusted=(current.last_untrusted if current is not None else None),
        bridge_graduated=(current.bridge_graduated if current is not None else False),
        graduated_at=(current.graduated_at if current is not None else None),
        graduation_issue=(current.graduation_issue if current is not None else None),
        locked_issues=(current.locked_issues if current is not None else ()),
    )
    save_trusted_baseline(
        edition,
        document,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _append_baseline_trust_event_fact(
        edition=edition,
        issue_number=issue_number,
        action=action,
        at=established_at,
        by=established_by,
        reason=notes,
        programs_root=programs_root,
        source_signal_id=f"baseline-advance:{edition}:{issue_number}",
    )
    return document


def _append_baseline_trust_event_fact(
    *,
    edition: str,
    issue_number: int,
    action: str,
    at: datetime,
    by: str | None,
    reason: str | None,
    programs_root: Path,
    source_signal_id: str,
) -> None:
    """Append a `baseline.trust_event` fact for spec §22 Step 9 (D-10).

    Baseline-trust state changes become fact-store event transactions when
    the program is in ``shadow`` or ``primary`` SoR mode.  Every
    ``advance_trusted_baseline`` / ``record_untrusted_issue`` /
    ``mark_bridge_graduated`` / ``record_rollback_drill_passed`` call also
    appends a fact revision so the audit trail is replay-able through
    ``project_baseline_trust_events``.  In ``legacy`` mode the fact store is
    not yet authoritative, so the call is a no-op and the legacy YAML
    remains the single source.

    ``source_signal_id`` distinguishes which write path produced the event
    (``baseline-advance:<edition>:<issue>`` /
    ``baseline-untrusted:<edition>:<issue>`` /
    ``baseline-graduated:<edition>:<issue>`` /
    ``baseline-rollback-drill:<edition>:<issue>``).
    """
    from src.core.exceptions import ConfigError
    from src.core.fact_sor_state import load_fact_sor_state
    from src.core.program_fact_store import (
        FactPrecedence,
        ProgramFactInput,
        ProgramFactStore,
    )
    from src.core.edition_resolver import resolve_edition  # noqa: PLC0415

    try:
        resolved = resolve_edition(edition, programs_root=programs_root)
    except ConfigError:
        # No resolvable program (e.g. legacy test workspace with no
        # program.yaml) — fall back to the pre-rev-320 contract: keep
        # baseline promotion YAML-only.  The spec's gate (D-05 parity) is
        # about fact-store authority, not about always appending facts.
        return
    if resolved is None:
        return
    sor_state = load_fact_sor_state(resolved.program.id, programs_root=programs_root)
    # None (no state file) and ``legacy`` mode both mean "the fact store is
    # not yet authoritative" — keep baseline promotion YAML-only in that case
    # so we never silently introduce fact writes against a still-shim-backed
    # store (D-10 is gated on D-05 parity, which means shadow or primary).
    if sor_state is None or sor_state.mode == "legacy":
        return
    store = ProgramFactStore(resolved.program.id, db_root=programs_root.parent)
    store.initialize()
    entity_refs = (
        f"BASELINE_EVENT:{edition}:{issue_number}:{action}:{at.isoformat()}",
    )
    result = store.append_fact(
        ProgramFactInput(
            fact_type="baseline.trust_event",
            scope="program",
            entity_refs=entity_refs,
            payload={
                "edition": edition,
                "issue": issue_number,
                "at": at.isoformat(),
                "by": by,
                "action": action,
                "reason": reason,
            },
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
            source_signal_ids=(source_signal_id,),
        )
    )
    # result.action is one of created/noop/superseded/proposed_revision; we
    # don't surface it to the caller because each trust-state mutator's
    # contract is "return the new TrustedBaseline" — the fact write is a
    # side effect.  Tests asserting the side effect should use
    # load_program_facts(..., fact_types=("baseline.trust_event",)).
    del result


def record_untrusted_issue(
    edition: str,
    issue_number: int,
    *,
    recorded_at: datetime,
    recorded_by: str | None,
    reason: str,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrustedBaseline | None:
    current = load_trusted_baseline(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if (
        current is not None
        and current.last_untrusted is not None
        and current.last_untrusted.issue == issue_number
        and current.last_untrusted.reason == reason
    ):
        return current

    document = TrustedBaseline(
        schema_version="1.0",
        edition=edition,
        trusted_issue_number=(current.trusted_issue_number if current is not None else None),
        established_at=(current.established_at if current is not None else recorded_at),
        established_by=(current.established_by if current is not None else recorded_by),
        notes=(current.notes if current is not None else None),
        history=(
            ()
            if current is None
            else current.history
        ) + (
            TrustedBaselineHistoryEntry(
                issue=issue_number,
                at=recorded_at,
                by=recorded_by,
                action="untrusted",
                reason=reason,
            ),
        ),
        last_untrusted=LastUntrustedIssue(
            issue=issue_number,
            at=recorded_at,
            by=recorded_by,
            reason=reason,
        ),
        bridge_graduated=(current.bridge_graduated if current is not None else False),
        graduated_at=(current.graduated_at if current is not None else None),
        graduation_issue=(current.graduation_issue if current is not None else None),
        locked_issues=(current.locked_issues if current is not None else ()),
    )
    save_trusted_baseline(
        edition,
        document,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _append_baseline_trust_event_fact(
        edition=edition,
        issue_number=issue_number,
        action="untrusted",
        at=recorded_at,
        by=recorded_by,
        reason=reason,
        programs_root=programs_root,
        source_signal_id=f"baseline-untrusted:{edition}:{issue_number}",
    )
    return document


def mark_bridge_graduated(
    edition: str,
    issue_number: int,
    *,
    graduated_at: datetime,
    graduated_by: str | None,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrustedBaseline | None:
    current = load_trusted_baseline(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if current is None or current.trusted_issue_number is None:
        return None
    if current.bridge_graduated and current.graduation_issue == issue_number:
        return current

    document = TrustedBaseline(
        schema_version=current.schema_version,
        edition=current.edition,
        trusted_issue_number=current.trusted_issue_number,
        established_at=current.established_at,
        established_by=current.established_by,
        notes=current.notes,
        history=current.history + (
            TrustedBaselineHistoryEntry(
                issue=issue_number,
                at=graduated_at,
                by=graduated_by,
                action="graduated",
            ),
        ),
        last_untrusted=current.last_untrusted,
        bridge_graduated=True,
        graduated_at=graduated_at,
        graduation_issue=issue_number,
        locked_issues=current.locked_issues,
    )
    save_trusted_baseline(
        edition,
        document,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _append_baseline_trust_event_fact(
        edition=edition,
        issue_number=issue_number,
        action="graduated",
        at=graduated_at,
        by=graduated_by,
        reason=None,
        programs_root=programs_root,
        source_signal_id=f"baseline-graduated:{edition}:{issue_number}",
    )
    return document


def record_rollback_drill_passed(
    edition: str,
    *,
    recorded_at: datetime,
    recorded_by: str | None,
    checkpoint_name: str,
    rollback_exit_code: int,
    consistency_exit_code: int,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrustedBaseline | None:
    current = load_trusted_baseline(
        edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if current is None or current.trusted_issue_number is None:
        return None

    normalized_checkpoint_name = checkpoint_name.strip()
    if not normalized_checkpoint_name:
        raise ValueError("checkpoint_name must be non-empty.")

    reason = (
        "Rollback drill passed: "
        f"checkpoint={normalized_checkpoint_name}; "
        f"rollback_exit_code={rollback_exit_code}; "
        f"consistency_exit_code={consistency_exit_code}"
    )
    if (
        current.history
        and current.history[-1].issue == current.trusted_issue_number
        and current.history[-1].action == "rollback_drill_passed"
        and current.history[-1].reason == reason
    ):
        return current

    document = TrustedBaseline(
        schema_version=current.schema_version,
        edition=current.edition,
        trusted_issue_number=current.trusted_issue_number,
        established_at=current.established_at,
        established_by=current.established_by,
        notes=current.notes,
        history=current.history + (
            TrustedBaselineHistoryEntry(
                issue=current.trusted_issue_number,
                at=recorded_at,
                by=recorded_by,
                action="rollback_drill_passed",
                reason=reason,
            ),
        ),
        last_untrusted=current.last_untrusted,
        bridge_graduated=current.bridge_graduated,
        graduated_at=current.graduated_at,
        graduation_issue=current.graduation_issue,
        locked_issues=current.locked_issues,
    )
    save_trusted_baseline(
        edition,
        document,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _append_baseline_trust_event_fact(
        edition=edition,
        issue_number=current.trusted_issue_number,
        action="rollback_drill_passed",
        at=recorded_at,
        by=recorded_by,
        reason=reason,
        programs_root=programs_root,
        source_signal_id=f"baseline-rollback-drill:{edition}:{current.trusted_issue_number}",
    )
    return document


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid trusted baseline datetime: {value!r}")
    return datetime.fromisoformat(value)


def _write_atomic_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)