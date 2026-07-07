from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path


PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"


@dataclass(frozen=True, slots=True)
class LedgerVerifyStatus:
    program_id: str
    verified_at: datetime
    ok: bool
    deep: bool
    checked_event_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "program_id": self.program_id,
            "verified_at": self.verified_at.isoformat(),
            "ok": self.ok,
            "deep": self.deep,
            "checked_event_count": self.checked_event_count,
        }


def get_ledger_verify_status_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / "verify_status.json"


def load_ledger_verify_status(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> LedgerVerifyStatus | None:
    status_path = get_ledger_verify_status_path(program_id, programs_root=programs_root)
    if not status_path.exists():
        return None
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    verified_at = payload.get("verified_at")
    ok = payload.get("ok")
    deep = payload.get("deep")
    checked_event_count = payload.get("checked_event_count")
    if not isinstance(verified_at, str) or not isinstance(ok, bool) or not isinstance(deep, bool) or not isinstance(checked_event_count, int):
        raise ValueError(f"Invalid ledger verify status payload: {status_path}")
    return LedgerVerifyStatus(
        program_id=program_id,
        verified_at=datetime.fromisoformat(verified_at.replace("Z", "+00:00")).astimezone(timezone.utc),
        ok=ok,
        deep=deep,
        checked_event_count=checked_event_count,
    )


def write_ledger_verify_status(
    program_id: str,
    *,
    verified_at: datetime,
    ok: bool,
    deep: bool,
    checked_event_count: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> LedgerVerifyStatus:
    status = LedgerVerifyStatus(
        program_id=program_id,
        verified_at=verified_at.astimezone(timezone.utc),
        ok=ok,
        deep=deep,
        checked_event_count=checked_event_count,
    )
    status_path = get_ledger_verify_status_path(program_id, programs_root=programs_root)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return status