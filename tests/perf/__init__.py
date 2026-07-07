from __future__ import annotations

import pytest


@pytest.mark.integration
def test_trust_live_smoke() -> None:
    pytest.skip("Live trust smoke requires local Acme journals and feedback state.")
