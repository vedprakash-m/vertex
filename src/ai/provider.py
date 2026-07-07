from __future__ import annotations

from typing import Any, Callable, Protocol, TypeVar, runtime_checkable


StructuredResponse = TypeVar("StructuredResponse")


@runtime_checkable
class LLMProvider(Protocol):
    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> str: ...

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], StructuredResponse],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> StructuredResponse: ...


class DisabledStructuredProvider:
    """Null-object LLM provider for AIMode.DISABLED.

    ``chat()`` always raises.  ``structured()`` returns ``parser(empty_payload)``
    when *empty_payload* is supplied (graceful empty-result path), and raises
    otherwise (strict guard when the caller should never reach AI invocation).
    """

    def __init__(
        self,
        *,
        feature_name: str = "AI",
        empty_structured_payload: dict[str, Any] | None = None,
    ) -> None:
        self._feature_name = feature_name
        self._empty_payload = empty_structured_payload

    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> str:
        del system, user, max_tokens, prompt_version
        raise AssertionError(
            f"{self._feature_name} should not call the AI client when AIMode.DISABLED."
        )

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], StructuredResponse],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> StructuredResponse:
        del system, user, max_tokens, prompt_version
        if self._empty_payload is not None:
            return parser(self._empty_payload)
        raise AssertionError(
            f"{self._feature_name} should not call the AI client when AIMode.DISABLED."
        )