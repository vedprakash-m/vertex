from __future__ import annotations

from contextlib import contextmanager

from src.core import engms_content
from src.core.engms_content import fetch_engms_page_summary


def test_fetch_rejects_non_engms_host_without_network(monkeypatch) -> None:
    def _boom(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("urlopen should not be called for an off-host URL")

    monkeypatch.setattr(engms_content, "urlopen", _boom)
    assert fetch_engms_page_summary("https://evil.example.com/eng.ms/page") is None


def test_fetch_rejects_redirect_to_off_host(monkeypatch) -> None:
    class _Resp:
        headers = {"Content-Type": "text/html"}

        def geturl(self) -> str:
            return "https://evil.example.com/landing"

        def read(self, _n: int) -> bytes:  # pragma: no cover - should not be reached
            return b"<html><title>Should not parse</title></html>"

    @contextmanager
    def _fake_urlopen(_request, timeout=5):
        yield _Resp()

    monkeypatch.setattr(engms_content, "urlopen", _fake_urlopen)
    # Initial host is eng.ms, but the response redirected off-host -> guard returns None.
    assert fetch_engms_page_summary("https://eng.ms/docs/redirect-canary") is None


def test_fetch_parses_summary_when_host_stays_engms(monkeypatch) -> None:
    class _Resp:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def geturl(self) -> str:
            return "https://eng.ms/docs/ok-canary"

        def read(self, _n: int) -> bytes:
            return b'<html><meta name="description" content="A valid eng.ms summary."></html>'

    @contextmanager
    def _fake_urlopen(_request, timeout=5):
        yield _Resp()

    monkeypatch.setattr(engms_content, "urlopen", _fake_urlopen)
    assert fetch_engms_page_summary("https://eng.ms/docs/ok-canary") == "A valid eng.ms summary."

def test_redirect_guard_blocks_off_host_before_following() -> None:
    guard = engms_content._EngMsRedirectGuard()
    import pytest

    with pytest.raises(engms_content.URLError):
        guard.redirect_request(
            req=None,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="https://evil.example.com/landing",
        )


def test_redirect_guard_allows_same_host_redirect() -> None:
    guard = engms_content._EngMsRedirectGuard()
    from urllib.request import Request

    original = Request("https://eng.ms/docs/start")
    result = guard.redirect_request(
        req=original,
        fp=None,
        code=302,
        msg="Found",
        headers={},
        newurl="https://eng.ms/docs/moved",
    )
    assert result is not None
    assert result.full_url == "https://eng.ms/docs/moved"
