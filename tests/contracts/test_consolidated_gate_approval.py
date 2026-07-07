from __future__ import annotations

from pathlib import Path

from src.core.consolidated_gate_approval import load_consolidated_gate_approval_status


def test_current_consolidated_gate_adr_is_not_accepted_yet() -> None:  # noqa: D103 — kept for historical traceability; ADR-0006 is now Accepted
    status = load_consolidated_gate_approval_status()

    assert status.record_exists is True
    assert status.adr_status == "Accepted"
    assert status.accepted is True
    assert status.blocking_reasons == ()


def test_consolidated_gate_approval_requires_status_approvers_and_checked_items(tmp_path: Path) -> None:
    record = tmp_path / "adr.md"
    record.write_text(
        """# ADR-9999: Test

**Status:** Accepted
**Approver(s):** Product Governance, Engineering

## Human Approval Checklist

- [x] Product/Governance records S-0c decision.
- [X] Engineering records S-NC-apply decision before NCFL apply.
""",
        encoding="utf-8",
    )

    status = load_consolidated_gate_approval_status(record)

    assert status.accepted is True
    assert status.blocking_reasons == ()
    assert status.checked_items == (
        "Product/Governance records S-0c decision.",
        "Engineering records S-NC-apply decision before NCFL apply.",
    )


def test_consolidated_gate_approval_rejects_placeholder_approvers(tmp_path: Path) -> None:
    record = tmp_path / "adr.md"
    record.write_text(
        """# ADR-9999: Test

**Status:** Accepted
**Approver(s):** TBD — required for Accepted

- [x] Product/Governance records S-0c decision.
""",
        encoding="utf-8",
    )

    status = load_consolidated_gate_approval_status(record)

    assert status.accepted is False
    assert "approvers are missing or placeholder" in status.blocking_reasons
