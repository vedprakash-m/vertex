from __future__ import annotations

from src.core.config_loader import EditionVerbosityLimits, VerbositySettings
from src.core.models import EditionType
from src.core.verbosity_enforcer import count_words, enforce_verbosity, split_sentences


def test_split_sentences_uses_canonical_regex_behavior() -> None:
    sentences = split_sentences("One sentence. Two sentence? Three sentence!")
    assert sentences == ("One sentence.", "Two sentence?", "Three sentence!")
    assert count_words("One sentence. Two sentence?") == 4


def test_count_words_ignores_markdown_link_urls() -> None:
    text = (
        "Acme ramp still depends on SCHIE follow-through. "
        "See [Azure CSI work item 3393076](https://azurecsi.visualstudio.com/Dev/_workitems/edit/3393076) "
        "for backlog detail."
    )
    visible_text = "Acme ramp still depends on SCHIE follow-through. See Azure CSI work item 3393076 for backlog detail."

    assert count_words(text) == count_words(visible_text)


def test_enforce_verbosity_flags_overlong_sections() -> None:
    verbosity = VerbositySettings(
        workstream_blurb_max_sentences=3,
        workstream_blurb_max_words=10,
        exec_bullet_max_words=25,
        exec_max_bullets=3,
        scorecard_summary_max_sentences=3,
        workstream_blurb_max_paragraphs=1,
    )
    workstream_blurb = (
        "One short sentence. Two short sentence. Three short sentence. Four short sentence.\n\n"
        "Another paragraph adds more words."
    )
    exec_summary = " ".join(f"word{i}" for i in range(151))
    scorecard_summary = "One. Two. Three. Four."
    subject_line = "S" * 81

    violations = enforce_verbosity(
        workstream_blurbs={"deployment": workstream_blurb},
        exec_summary_text=exec_summary,
        scorecard_summaries={"Deployment Velocity": scorecard_summary},
        subject_line=subject_line,
        verbosity=verbosity,
    )
    messages = {violation.message for violation in violations}

    assert "Workstream blurb exceeds 3 sentences." in messages
    assert "Workstream blurb exceeds 10 words." in messages
    assert "Workstream blurb must stay within one paragraph." in messages
    assert "Executive summary exceeds 150 words." in messages
    assert "Scorecard summary exceeds 3 sentences." in messages
    assert "Subject line exceeds 80 characters." in messages


def test_enforce_verbosity_uses_edition_specific_exec_summary_limit() -> None:
    verbosity = VerbositySettings(
        workstream_blurb_max_sentences=3,
        workstream_blurb_max_words=60,
        exec_bullet_max_words=25,
        exec_max_bullets=3,
        scorecard_summary_max_sentences=3,
        exec_summary_max_words_by_edition=EditionVerbosityLimits(condensed=75),
    )

    violations = enforce_verbosity(
        workstream_blurbs={},
        exec_summary_text=" ".join(f"word{i}" for i in range(76)),
        scorecard_summaries={},
        subject_line=None,
        verbosity=verbosity,
        edition_type=EditionType.CONDENSED,
    )

    assert tuple(violation.message for violation in violations) == ("Executive summary exceeds 75 words.",)


def test_enforce_verbosity_uses_edition_specific_blurb_limit() -> None:
    verbosity = VerbositySettings(
        workstream_blurb_max_sentences=3,
        workstream_blurb_max_words=60,
        exec_bullet_max_words=25,
        exec_max_bullets=3,
        scorecard_summary_max_sentences=3,
        workstream_blurb_max_words_by_edition=EditionVerbosityLimits(narrative=150),
    )

    violations = enforce_verbosity(
        workstream_blurbs={"deployment": " ".join(f"word{i}" for i in range(61))},
        exec_summary_text="",
        scorecard_summaries={},
        subject_line=None,
        verbosity=verbosity,
        edition_type=EditionType.NARRATIVE,
    )

    assert violations == ()


def test_enforce_verbosity_does_not_count_markdown_link_url_fragments_as_words() -> None:
    verbosity = VerbositySettings(
        workstream_blurb_max_sentences=4,
        workstream_blurb_max_words=90,
        exec_bullet_max_words=25,
        exec_max_bullets=3,
        scorecard_summary_max_sentences=3,
    )
    workstream_blurb = (
        "Acme ramp still depends on closing the remaining SCHIE diagnostics and repair gaps before the 05/15 checkpoint can hold.\n"
        "- Blocked for the 05/15 DFD on ADO#36923425 burn-in NeedsValidation, ADO#36928928 60K fault-code parity, "
        "ADO#36936967 post-repair diagnostics, and ADO#36762922 FC20160 hostname or IP mismatch.\n"
        "- P1 for the next 3 to 6 months, not the immediate ramp gate: "
        "[Azure CSI work item 3393076](https://azurecsi.visualstudio.com/Dev/_workitems/edit/3393076), ADO#36935551, "
        "and the HFM, AMS, diagnostics, Riptide RM, certificate-provisioning, and Coolville or Titan backlog, "
        "including Boot into MOS and NVDIMM support."
    )

    violations = enforce_verbosity(
        workstream_blurbs={"schie-gaps": workstream_blurb},
        exec_summary_text="",
        scorecard_summaries={},
        subject_line=None,
        verbosity=verbosity,
    )

    assert all(violation.location != "workstream:schie-gaps" for violation in violations)
