from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands import report as report_module
from src.core.models_v2 import M365Config


def test_maybe_auto_run_workiq_enrich_never_calls_enrich_inline_even_when_schedule_pre_report(monkeypatch, tmp_path: Path) -> None:
    """ADF-W1.5 / INV-ADF-2: report must never perform a live WorkIQ call
    inline, even for a program still configured with the legacy
    ``workiq_enrich_schedule: pre_report`` setting."""
    # Isolate PROGRAMS_ROOT so the v1.31 workiq_inline_invocation_attempted
    # alert this path now emits does not write to the real programs/ dir.
    monkeypatch.setattr(report_module, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        report_module,
        "resolve_edition",
        lambda edition_name, programs_root=None: SimpleNamespace(
            program=SimpleNamespace(
                id="acme",
                m365=M365Config(
                    enabled=True,
                    prefer_agency=True,
                    workiq_enrich_schedule="pre_report",
                )
            )
        ),
    )

    monkeypatch.setattr(
        "src.commands.enrich.enrich_command",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError(f"Unexpected inline enrich call: {kwargs}")),
    )

    # Must not raise (proves no inline WorkIQ call is attempted) and must
    # not import/touch enrich_command at all.
    report_module._maybe_auto_run_workiq_enrich(
        edition_name="acme_weekly",
        dry_run=True,
        offline=False,
        show_progress=False,
    )


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
