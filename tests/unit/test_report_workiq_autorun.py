from __future__ import annotations

from types import SimpleNamespace

from src.commands import report as report_module
from src.core.models_v2 import M365Config


def test_maybe_auto_run_workiq_enrich_invokes_enrich_when_schedule_pre_report(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        report_module,
        "resolve_edition",
        lambda edition_name, programs_root=None: SimpleNamespace(
            program=SimpleNamespace(
                m365=M365Config(
                    enabled=True,
                    prefer_agency=True,
                    workiq_enrich_schedule="pre_report",
                )
            )
        ),
    )

    def _fake_enrich_command(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("src.commands.enrich.enrich_command", _fake_enrich_command)

    report_module._maybe_auto_run_workiq_enrich(
        edition_name="acme_weekly",
        dry_run=True,
        offline=False,
        show_progress=False,
    )

    assert captured == {
        "edition": "acme_weekly",
        "dry_run": True,
        "accept": False,
        "output_format": "human",
    }


def test_maybe_auto_run_workiq_enrich_skips_when_schedule_not_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        report_module,
        "resolve_edition",
        lambda edition_name, programs_root=None: SimpleNamespace(
            program=SimpleNamespace(
                m365=M365Config(
                    enabled=True,
                    prefer_agency=True,
                    workiq_enrich_schedule=None,
                )
            )
        ),
    )

    monkeypatch.setattr(
        "src.commands.enrich.enrich_command",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError(f"Unexpected enrich call: {kwargs}")),
    )

    report_module._maybe_auto_run_workiq_enrich(
        edition_name="acme_weekly",
        dry_run=False,
        offline=False,
        show_progress=False,
    )
