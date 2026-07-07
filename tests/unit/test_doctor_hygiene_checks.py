from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.hygiene_checks import resolve_hygiene_workstream_lead_contact, run_hygiene_nudge_check


def test_resolve_hygiene_workstream_lead_contact_prefers_dri_email() -> None:
    alias, email = resolve_hygiene_workstream_lead_contact(
        workstream=SimpleNamespace(dri_email="Lead@Example.com", alternate_owner="other"),
        knowledge=SimpleNamespace(people_directory=()),
    )

    assert alias == "lead"
    assert email == "lead@example.com"


def test_run_hygiene_nudge_check_warns_on_unroutable_workstreams(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.commands.doctor_checks.hygiene_checks.load_program_knowledge",
        lambda program_id, *, programs_root: SimpleNamespace(people_directory=()),
    )
    resolved = SimpleNamespace(
        edition=SimpleNamespace(type="nudge"),
        raw_edition={
            "hygiene": {"stale_business_days": 5, "workstream_coverage_alerts": True},
            "send_day": "tuesday",
            "distribution": {"channels": ["email"]},
            "author": {"email": "owner@example.com"},
        },
        program=SimpleNamespace(id="demo"),
        workstreams=(
            SimpleNamespace(
                id="demo",
                dri_email=None,
                alternate_owner=None,
                signal_sources=SimpleNamespace(ado_coverage=None),
            ),
        ),
    )

    check = run_hygiene_nudge_check(resolved=resolved, programs_root=tmp_path)

    assert check is not None
    assert check.status == "warn"
    assert "missing lead email resolution for demo" in check.detail
    assert check.metadata is not None
    assert check.metadata["missing_workstream_leads"] == ["demo"]
