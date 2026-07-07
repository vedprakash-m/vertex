from __future__ import annotations


def channel_source_profile(channel_name: str) -> dict[str, str]:
    profiles = {
        "ado": {
            "provider": "microsoft",
            "provider_system": "azure_devops",
            "source_kind": "work_tracker",
            "program_impact": "work-item evidence and PR-linked execution telemetry",
        },
        "kusto": {
            "provider": "microsoft",
            "provider_system": "azure_data_explorer",
            "source_kind": "telemetry",
            "program_impact": "metric/query-backed health and trend evidence",
        },
        "workiq": {
            "provider": "microsoft",
            "provider_system": "workiq_m365",
            "source_kind": "collaboration_search",
            "program_impact": "email and Teams message evidence",
        },
        "transcript": {
            "provider": "microsoft",
            "provider_system": "teams_transcript",
            "source_kind": "meeting_transcript",
            "program_impact": "meeting-series transcript and recap evidence",
        },
        "teams": {
            "provider": "microsoft",
            "provider_system": "teams_graph",
            "source_kind": "collaboration_channel",
            "program_impact": "meeting-series and chat registry evidence",
        },
        "icm": {
            "provider": "microsoft",
            "provider_system": "icm",
            "source_kind": "incident_management",
            "program_impact": "incident-state and severity evidence",
        },
    }
    default_profile = {
        "provider": "unknown",
        "provider_system": channel_name,
        "source_kind": "unknown",
        "program_impact": "unspecified",
    }
    return dict(profiles.get(channel_name, default_profile))
