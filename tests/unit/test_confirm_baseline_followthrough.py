from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.commands.confirm_stages import baseline_followthrough
from src.core.exceptions import ConfirmError


def test_apply_baseline_followthrough_adds_calibration_warning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        baseline_followthrough,
        "compute_calibration_for_edition",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("calibration boom")),
    )

    warnings = baseline_followthrough.apply_baseline_followthrough(
        edition_name="acme_weekly",
        issue_number=1,
        confirmed_at=datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc),
        confirmed_by="tester",
        warnings=(),
        archive_root=tmp_path / "archive",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        resolved_v2=SimpleNamespace(
            program=SimpleNamespace(id="acme"),
            edition=SimpleNamespace(calibration_pilot=True),
        ),
        items=(),
        untrusted=False,
        untrusted_reason=None,
    )

    assert warnings == ("CalibrationPrior skipped: calibration boom",)


def test_apply_baseline_followthrough_raises_when_untrusted_marker_write_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        baseline_followthrough,
        "record_untrusted_issue",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("marker boom")),
    )

    with pytest.raises(ConfirmError, match="failed to record the untrusted baseline marker"):
        baseline_followthrough.apply_baseline_followthrough(
            edition_name="acme_weekly",
            issue_number=1,
            confirmed_at=datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc),
            confirmed_by="tester",
            warnings=(),
            archive_root=tmp_path / "archive",
            editions_root=tmp_path / "editions",
            programs_root=tmp_path / "programs",
            resolved_v2=SimpleNamespace(
                program=SimpleNamespace(id="acme"),
                edition=SimpleNamespace(calibration_pilot=False),
            ),
            items=(),
            untrusted=True,
            untrusted_reason="operator note",
        )
