from __future__ import annotations

from src.core.ban_list_validator import PolicyProfile, find_ban_list_violations
from src.core.config_loader import EditorialRules, VerbositySettings


def test_find_ban_list_violations_merges_default_editorial_and_program_rules() -> None:
    editorial_rules = EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=("critical",),
        banned_openings=("Starting here",),
        verbosity=VerbositySettings(None, None, None, None, None),
    )

    violations = find_ban_list_violations(
        rendered_strings={
            "exec_summary": "This week the roadmap is critical due to blockers.",
            "workstream": "Starting here we unlock progress.",
        },
        editorial_rules=editorial_rules,
        program_banned_phrases=("roadmap",),
    )
    phrases = {violation.phrase for violation in violations}
    locations = {violation.location for violation in violations}

    assert {"due to", "critical", "This week", "roadmap", "Starting here", "unlock"} <= phrases
    assert locations == {"exec_summary", "workstream"}


def test_find_ban_list_violations_flags_telemetry_strings() -> None:
    editorial_rules = EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=(),
        banned_openings=(),
        verbosity=VerbositySettings(None, None, None, None, None),
    )

    violations = find_ban_list_violations(
        rendered_strings={
            "exec_summary": "Probe failed with SEM0100 against https://adventure.kusto.windows.net and leaked kusto.windows.net in the draft.",
        },
        editorial_rules=editorial_rules,
    )

    phrases = {violation.phrase for violation in violations}

    assert {"SEM0100", "kusto.windows.net", "raw cluster URI"} <= phrases


def test_find_ban_list_violations_retrospective_profile_lifts_only_causal_phrases() -> None:
    editorial_rules = EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=("critical",),
        banned_openings=(),
        verbosity=VerbositySettings(None, None, None, None, None),
    )

    violations = find_ban_list_violations(
        rendered_strings={
            "lookback": "The slip happened due to a blocked rollout and was critical because of delayed sign-off.",
        },
        editorial_rules=editorial_rules,
        profile=PolicyProfile.RETROSPECTIVE,
    )
    phrases = {violation.phrase for violation in violations}

    assert "due to" not in phrases
    assert "because of" not in phrases
    assert "critical" in phrases


def test_find_ban_list_violations_supports_per_location_profiles() -> None:
    editorial_rules = EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=(),
        banned_openings=(),
        verbosity=VerbositySettings(None, None, None, None, None),
    )

    violations = find_ban_list_violations(
        rendered_strings={
            "incident_learning:attributed": "Rollback slipped due to blocked capacity allocation.",
            "incident_learning:unattributed": "Follow-up stalled because of missing owner.",
        },
        editorial_rules=editorial_rules,
        location_profiles={
            "incident_learning:attributed": PolicyProfile.RETROSPECTIVE,
        },
    )
    by_location = {
        location: {violation.phrase for violation in violations if violation.location == location}
        for location in {violation.location for violation in violations}
    }

    assert by_location["incident_learning:unattributed"] == {"because of"}
    assert "incident_learning:attributed" not in by_location
