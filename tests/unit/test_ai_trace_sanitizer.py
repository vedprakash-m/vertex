from __future__ import annotations

from src.ai.safety.ai_trace_sanitizer import sanitize_ai_io


def test_empty_text_short_circuits() -> None:
    result = sanitize_ai_io("")
    assert result.text == ""
    assert not result.pii_detected
    assert not result.credential_detected
    assert not result.truncated
    assert result.classification == "sanitized-excerpt"


def test_redacts_email_pii() -> None:
    result = sanitize_ai_io("Contact jane.doe@example.com about the risk.")
    assert "jane.doe@example.com" not in result.text
    assert result.pii_detected


def test_redacts_azure_connection_string_credential() -> None:
    text = "conn=DefaultEndpointsProtocol=https;AccountName=x;AccountKey=abcdEFGH1234567890+/=;"
    result = sanitize_ai_io(text)
    assert "AccountKey=abcdEFGH1234567890" not in result.text
    assert result.credential_detected


def test_redacts_bearer_token_credential() -> None:
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
    result = sanitize_ai_io(text)
    assert "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF" not in result.text
    assert result.credential_detected


def test_truncates_over_max_bytes() -> None:
    # Repeated words (not a contiguous alnum run) so the high-entropy
    # credential heuristic doesn't fire and redact this down to nothing —
    # this test is specifically about the size bound, not credential scanning.
    long_text = "the milestone status update is unchanged. " * 300
    result = sanitize_ai_io(long_text, max_excerpt_bytes=100)
    assert result.truncated
    assert len(result.text.encode("utf-8")) <= 100
    assert result.text.endswith("...[TRUNCATED]")
    assert result.original_byte_length == len(long_text.encode("utf-8"))


def test_clean_text_passes_through_unmodified() -> None:
    result = sanitize_ai_io("The milestone is on track for Q3.")
    assert result.text == "The milestone is on track for Q3."
    assert not result.pii_detected
    assert not result.credential_detected
    assert not result.truncated


def test_never_leaks_raw_credential_even_when_truncated() -> None:
    secret = "AccountKey=" + "S" * 80 + "=="
    text = secret + " " + "padding " * 2000
    result = sanitize_ai_io(text, max_excerpt_bytes=50)
    assert secret not in result.text
