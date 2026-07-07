from __future__ import annotations

import pytest


pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="Live meeting-close smoke is not configured in CI.")
def test_meeting_close_live_smoke() -> None:
    pass