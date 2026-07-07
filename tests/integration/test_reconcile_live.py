from __future__ import annotations

import pytest


@pytest.mark.integration
def test_reconcile_live_smoke() -> None:
    pytest.skip("Live reconcile smoke requires local Acme/ADO access.")