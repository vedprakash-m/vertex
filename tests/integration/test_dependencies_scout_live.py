from __future__ import annotations

import pytest


pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="Live dependency scout smoke is not configured in CI.")
def test_dependencies_scout_live_smoke() -> None:
    pass