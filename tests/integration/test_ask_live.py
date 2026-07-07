from __future__ import annotations

import pytest


@pytest.mark.integration
def test_ask_live_smoke() -> None:
    pytest.skip("Live ask smoke requires Azure OpenAI routing configuration and local Acme command context.")