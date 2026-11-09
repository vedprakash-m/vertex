"""Locally authored synthetic examples for the PII scrubber."""

from __future__ import annotations

from src.ai.safety.pii_scrubber import filter_text, scan_text


def test_scan_text_redacts_all_emails_by_default() -> None:
    result = scan_text("Contact first@sample.example.net and second@sample.example.net for details.")

    assert result.pii_detected is True
    assert result.signal_types == ["email", "email"]
    assert result.scrubbed_text == "Contact [PII-FILTERED-EMAIL] and [PII-FILTERED-EMAIL] for details."


def test_scan_text_preserves_allowed_email_domain() -> None:
    result = scan_text(
        "Contact external@sample.example.net and allowed@trusted.example.org for details.",
        allowed_email_domains=("trusted.example.org",),
    )

    assert result.pii_detected is True
    assert result.signal_types == ["email"]
    assert result.scrubbed_text == "Contact [PII-FILTERED-EMAIL] and allowed@trusted.example.org for details."


def test_scan_text_redacts_phone_numbers() -> None:
    result = scan_text("Call the synthetic contact at +1 (202) 555-0101 when the draft is ready.")

    assert result.pii_detected is True
    assert "phone" in result.signal_types
    assert "[PII-FILTERED-PHONE]" in result.scrubbed_text


def test_scan_text_redacts_ssn_shaped_strings() -> None:
    result = scan_text("Example SSN 000-00-0000 should never leave the machine.")

    assert result.pii_detected is True
    assert "ssn" in result.signal_types
    assert "[PII-FILTERED-SSN]" in result.scrubbed_text


def test_filter_text_returns_scrubbed_text_only() -> None:
    scrubbed = filter_text("Reach contact@sample.example.net or +1 (202) 555-0102.")

    assert scrubbed == "Reach [PII-FILTERED-EMAIL] or [PII-FILTERED-PHONE]."


def test_scan_text_leaves_clean_text_unchanged() -> None:
    result = scan_text("Sample program risk remains high and the owner is still Platform Team.")

    assert result.pii_detected is False
    assert result.signals == ()
    assert result.scrubbed_text == "Sample program risk remains high and the owner is still Platform Team."