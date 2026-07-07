from __future__ import annotations

from src.ai.safety.pii_scrubber import filter_text, scan_text


def test_scan_text_redacts_all_emails_by_default() -> None:
    result = scan_text("Contact alice@example.com and bob@corp.com for details.")

    assert result.pii_detected is True
    assert result.signal_types == ["email", "email"]
    assert result.scrubbed_text == "Contact [PII-FILTERED-EMAIL] and [PII-FILTERED-EMAIL] for details."


def test_scan_text_preserves_allowed_email_domain() -> None:
    result = scan_text(
        "Contact alice@external.com and bob@myorg.com for details.",
        allowed_email_domains=("myorg.com",),
    )

    assert result.pii_detected is True
    assert result.signal_types == ["email"]
    assert result.scrubbed_text == "Contact [PII-FILTERED-EMAIL] and bob@myorg.com for details."


def test_scan_text_redacts_phone_numbers() -> None:
    result = scan_text("Call me at +1 (425) 555-0100 when the draft is ready.")

    assert result.pii_detected is True
    assert "phone" in result.signal_types
    assert "[PII-FILTERED-PHONE]" in result.scrubbed_text


def test_scan_text_redacts_ssn_shaped_strings() -> None:
    result = scan_text("Example SSN 123-45-6789 should never leave the machine.")

    assert result.pii_detected is True
    assert "ssn" in result.signal_types
    assert "[PII-FILTERED-SSN]" in result.scrubbed_text


def test_filter_text_returns_scrubbed_text_only() -> None:
    scrubbed = filter_text("Reach jane@yahoo.com or 206-555-0112.")

    assert scrubbed == "Reach [PII-FILTERED-EMAIL] or [PII-FILTERED-PHONE]."


def test_scan_text_leaves_clean_text_unchanged() -> None:
    result = scan_text("StorageX risk remains high and the owner is still Platform Infra.")

    assert result.pii_detected is False
    assert result.signals == ()
    assert result.scrubbed_text == "StorageX risk remains high and the owner is still Platform Infra."