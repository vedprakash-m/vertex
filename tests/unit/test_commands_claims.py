from __future__ import annotations

from datetime import date, datetime, timezone

from typer.testing import CliRunner

from cli import app
from src.core.claim_tracker import append_claim_entry, append_decision_ask, load_latest_claim_statuses
from src.core.models_v2 import ClaimEntry, DecisionAsk


runner = CliRunner()


def test_claims_cli_lists_and_resolves_open_claim(monkeypatch, tmp_path) -> None:
    programs_root = tmp_path / "programs"
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="acme",
            text="WI:1001 UD chunking fix expected by June 15",
            entity_refs=("WI:1001",),
            claim_date=date(2026, 5, 5),
            owner_alias="owner",
            due_date=date(2026, 6, 15),
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr("src.commands.claims.PROGRAMS_ROOT", programs_root)

    listed = runner.invoke(app, ["claims", "--program", "acme"])
    resolved = runner.invoke(app, ["claims", "resolve", "--program", "acme", "--id", "claim-1", "--status", "met", "--reviewer", "maintainer"])

    latest_statuses = load_latest_claim_statuses("acme", programs_root)

    assert listed.exit_code == 0
    assert "OPEN CLAIMS" in listed.stdout
    assert "claim-1" in listed.stdout
    assert resolved.exit_code == 0
    assert latest_statuses["claim-1"].new_status == "met"


def test_claims_cli_resolves_decision_ask_with_met_alias(monkeypatch, tmp_path) -> None:
    from src.core.claim_tracker import load_decision_asks

    programs_root = tmp_path / "programs"
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on SCHIE timeline",
            entity_refs=(),
            ask_date=date(2026, 5, 5),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr("src.commands.claims.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["claims", "resolve", "--program", "acme", "--id", "ask-1", "--status", "met", "--reviewer", "maintainer"])

    latest_statuses = load_latest_claim_statuses("acme", programs_root)
    asks = load_decision_asks("acme", programs_root)

    assert result.exit_code == 0
    assert asks[0].status == "open"
    assert latest_statuses["ask-1"].new_status == "resolved"


def test_claims_cli_supports_json_and_csv(monkeypatch, tmp_path) -> None:
    programs_root = tmp_path / "programs"
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="acme",
            text="WI:1001 UD chunking fix expected by June 15",
            entity_refs=("WI:1001",),
            claim_date=date(2026, 5, 5),
            owner_alias="owner",
            due_date=date(2030, 6, 15),
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on SCHIE timeline",
            entity_refs=("WI:1002",),
            ask_date=date(2026, 5, 5),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr("src.commands.claims.PROGRAMS_ROOT", programs_root)

    json_result = runner.invoke(app, ["claims", "--program", "acme", "--format", "json"])
    csv_result = runner.invoke(app, ["claims", "--program", "acme", "--format", "csv"])

    assert json_result.exit_code == 0
    assert '"program_id": "acme"' in json_result.stdout
    assert '"open_claims": [' in json_result.stdout
    assert '"open_decision_asks": [' in json_result.stdout
    assert '"effective_status": "open"' in json_result.stdout

    assert csv_result.exit_code == 0
    assert "entry_type,id,program_id,edition_id,issue_number,workstream_id,status,claim_date,due_date,ask_date,owner_alias,entity_refs,text,reason" in csv_result.stdout
    assert "claim,claim-1,acme,acme_weekly,77,acme,open,2026-05-05,2030-06-15,,owner,WI:1001,WI:1001 UD chunking fix expected by June 15," in csv_result.stdout
    assert "decision_ask,ask-1,acme,acme_weekly,77,,open,,,2026-05-05,lt,WI:1002,Need LT decision on SCHIE timeline," in csv_result.stdout