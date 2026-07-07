from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


import pytest

from src.ai.ai_mode import AIMode, get_ai_mode, set_ai_mode
from src.ai.request_router import AIRequestRouter


REPO_ROOT = Path(__file__).resolve().parents[2]
AI_ROOT = REPO_ROOT / "src" / "ai"
ALLOWLIST = {"request_router.py"}
FORBIDDEN_PATTERNS = (
    ".chat.completions.create(",
    "AzureOpenAI(",
)


def test_ai_router_is_only_frontier_sdk_path() -> None:
    violations: list[str] = []
    for file_path in sorted(AI_ROOT.glob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if file_path.name in ALLOWLIST:
            continue
        source = file_path.read_text(encoding="utf-8")
        if any(pattern in source for pattern in FORBIDDEN_PATTERNS):
            violations.append(relative)

    assert violations == []


class _FakeRouterAzureOpenAI:
    """Minimal SDK stand-in that records calls and returns a fake response."""

    def __init__(self, **_kwargs) -> None:
        self.create_calls: int = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):  # noqa: ARG002 - kwargs are forwarded by router
        self.create_calls += 1
        return SimpleNamespace(
            id="fake-completion",
            choices=[SimpleNamespace(message=SimpleNamespace(content="fake response"))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12),
        )


def _build_router_with_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AIRequestRouter, _FakeRouterAzureOpenAI]:
    """Construct an AIRequestRouter wired to a fake AzureOpenAI client."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    fake_client = _FakeRouterAzureOpenAI()
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeRouterAzureOpenAI, Exception, Exception),
    )
    # Replace the SDK class binding so the router instantiates our fake.
    monkeypatch.setattr(AIRequestRouter, "__init__", AIRequestRouter.__init__)
    router = AIRequestRouter(rate_limit_scope="contract-test")
    router._client = fake_client  # noqa: SLF001 - test wiring
    return router, fake_client


def test_ai_router_refuses_frontier_calls_in_observe_only_mode(monkeypatch) -> None:
    """OBSERVE_ONLY mode must raise RuntimeError and never touch the SDK."""
    original_mode = get_ai_mode()
    router, fake_client = _build_router_with_fake_sdk(monkeypatch)
    try:
        set_ai_mode(AIMode.OBSERVE_ONLY)
        with pytest.raises(RuntimeError, match="observe-only"):
            router.route(
                deployment="acme-model",
                system="system prompt",
                user="user prompt",
                temperature=0.2,
                max_tokens=200,
            )
    finally:
        set_ai_mode(original_mode)

    assert fake_client.create_calls == 0


def test_ai_router_refuses_frontier_calls_in_disabled_mode(monkeypatch) -> None:
    """DISABLED mode must raise RuntimeError and never touch the SDK."""
    original_mode = get_ai_mode()
    router, fake_client = _build_router_with_fake_sdk(monkeypatch)
    try:
        set_ai_mode(AIMode.DISABLED)
        with pytest.raises(RuntimeError, match="disabled"):
            router.route(
                deployment="acme-model",
                system="system prompt",
                user="user prompt",
                temperature=0.2,
                max_tokens=200,
            )
    finally:
        set_ai_mode(original_mode)

    assert fake_client.create_calls == 0


def test_ai_router_returns_sdk_response_in_normal_mode(monkeypatch) -> None:
    """In default (NORMAL/ACTIVE) mode, route returns the SDK response unchanged."""
    original_mode = get_ai_mode()
    router, fake_client = _build_router_with_fake_sdk(monkeypatch)
    try:
        set_ai_mode(AIMode.ACTIVE)
        response = router.route(
            deployment="acme-model",
            system="system prompt",
            user="user prompt",
            temperature=0.2,
            max_tokens=200,
        )
    finally:
        set_ai_mode(original_mode)

    assert fake_client.create_calls == 1
    assert response is not None
    assert response.choices[0].message.content == "fake response"
