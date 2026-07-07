from __future__ import annotations

import json
from typing import Any


def render_reality_digest_text(payload_json: str) -> str:
    payload = json.loads(payload_json)
    lines = [
        f"Reality Digest - {payload['program_id']}",
        "-" * (17 + len(str(payload["program_id"]))),
        f"Health: {payload['health']}",
        f"As of: {payload['as_of']}",
        (
            "Counts: "
            f"confirmed={payload['confirmed_count']} "
            f"challenged={payload['challenged_count']} "
            f"stale={payload['stale_count']} "
            f"proposed={payload['proposed_count']}"
        ),
    ]

    freshness = payload.get("source_freshness", [])
    if freshness:
        lines.append("")
        lines.append("Source Freshness:")
        for entry in freshness:
            hours = entry["hours_since_last_observation"]
            hours_text = "unknown" if hours is None else f"{hours:.1f}h"
            quality_state = entry["quality_state"]
            if quality_state == "manual":
                quality_state = "📝 manual"
            lines.append(
                f"- {entry['metric_id']}: {quality_state} ({hours_text} since last observation)"
            )

    challenges = payload.get("open_challenges", [])
    if challenges:
        lines.append("")
        lines.append("Open Challenges:")
        for challenge in challenges:
            suffix = ""
            if challenge.get("ado_current_target"):
                suffix = f" (ADO target: {challenge['ado_current_target']})"
            lines.append(
                f"- {challenge['id']}: {challenge['challenge_kind']} [{challenge['severity']}] "
                f"for {challenge['hypothesis_id']}{suffix}"
            )
    else:
        lines.append("")
        lines.append("Open Challenges: none")

    suppressions = payload.get("suppressed_during_maintenance", [])
    if suppressions:
        lines.append("")
        lines.append("Suppressed During Maintenance:")
        for entry in suppressions:
            lines.append(f"- {entry['title']}: {entry['suppressed_count']} suppressed challenge(s)")

    return "\n".join(lines)


def render_reality_digest_json(payload_json: str) -> str:
    payload = json.loads(payload_json)
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)


def read_digest_payload(cache_row: Any) -> str:
    return str(cache_row["payload_json"])