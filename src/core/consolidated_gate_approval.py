from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


DEFAULT_DECISION_RECORD_PATH = Path("governance/decisions/0006-consolidated-human-decision-gates.md")
_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s*(?P<value>.+?)\s*$", re.MULTILINE)
_APPROVERS_RE = re.compile(r"^\*\*Approver\(s\):\*\*\s*(?P<value>.+?)\s*$", re.MULTILINE)
_CHECKBOX_RE = re.compile(r"^- \[(?P<mark>[ xX])\]\s*(?P<label>.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ConsolidatedGateApprovalStatus:
    decision_record_path: str
    record_exists: bool
    adr_status: str | None
    approvers: str | None
    checked_items: tuple[str, ...]
    unchecked_items: tuple[str, ...]
    blocking_reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.blocking_reasons


def load_consolidated_gate_approval_status(
    decision_record_path: Path = DEFAULT_DECISION_RECORD_PATH,
) -> ConsolidatedGateApprovalStatus:
    """Read the human approval record for consolidated STOP gates.

    This is intentionally conservative: a proposed ADR, placeholder approvers,
    or any unchecked approval item keeps every STOP gate blocked.
    """

    if not decision_record_path.exists():
        return ConsolidatedGateApprovalStatus(
            decision_record_path=str(decision_record_path),
            record_exists=False,
            adr_status=None,
            approvers=None,
            checked_items=(),
            unchecked_items=(),
            blocking_reasons=("decision record is missing",),
        )

    text = decision_record_path.read_text(encoding="utf-8")
    adr_status = _extract_match(_STATUS_RE, text)
    approvers = _extract_match(_APPROVERS_RE, text)
    checked_items: list[str] = []
    unchecked_items: list[str] = []
    for match in _CHECKBOX_RE.finditer(text):
        label = match.group("label").strip()
        if match.group("mark").strip().lower() == "x":
            checked_items.append(label)
        else:
            unchecked_items.append(label)

    blocking_reasons: list[str] = []
    if (adr_status or "").strip().lower() != "accepted":
        blocking_reasons.append(f"ADR status is {adr_status or 'missing'}, not Accepted")
    if _is_placeholder_approver(approvers):
        blocking_reasons.append("approvers are missing or placeholder")
    if unchecked_items:
        blocking_reasons.append(f"{len(unchecked_items)} approval checklist item(s) remain unchecked")

    return ConsolidatedGateApprovalStatus(
        decision_record_path=str(decision_record_path),
        record_exists=True,
        adr_status=adr_status,
        approvers=approvers,
        checked_items=tuple(checked_items),
        unchecked_items=tuple(unchecked_items),
        blocking_reasons=tuple(blocking_reasons),
    )


def _extract_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    return match.group("value").strip()


def _is_placeholder_approver(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.strip().lower()
    return not normalized or normalized.startswith("tbd") or "required for accepted" in normalized
