from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from src.core.m365_payload_support import optional_string
from src.core.models_v2 import Workstream
from src.core.yaml_utils import load_yaml_mapping


def augment_m365_workstream_profiles(
    program_id: str,
    *,
    workstreams: tuple[Workstream, ...],
    programs_root: Path,
) -> tuple[Workstream, ...]:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists() or not workstreams:
        return workstreams

    raw_program = load_yaml_mapping(program_path)
    raw_people = raw_program.get("people")
    if not isinstance(raw_people, list):
        return workstreams

    workstream_labels = {
        workstream.id: {
            label.strip().lower()
            for label in (workstream.id, workstream.name, *workstream.aliases)
            if label.strip()
        }
        for workstream in workstreams
    }
    aliases_by_workstream_id: dict[str, list[str]] = {workstream.id: [] for workstream in workstreams}

    for person in raw_people:
        if not isinstance(person, dict):
            continue
        if (optional_string(person.get("role")) or "").strip().lower() != "dependency_owner":
            continue
        person_workstreams = {
            str(value).strip().lower()
            for value in person.get("workstreams", [])
            if str(value).strip()
        }
        if not person_workstreams:
            continue

        alias_candidates: list[str] = []
        email = optional_string(person.get("email"))
        if email and "@" in email:
            alias_candidates.append(email.split("@", 1)[0].strip())
        display_name = optional_string(person.get("display_name"))
        if display_name and display_name.strip():
            alias_candidates.append(display_name.strip())
        if not alias_candidates:
            continue

        for workstream in workstreams:
            if not (person_workstreams & workstream_labels[workstream.id]):
                continue
            aliases_by_workstream_id[workstream.id].extend(alias_candidates)

    augmented: list[Workstream] = []
    for workstream in workstreams:
        derived_aliases = aliases_by_workstream_id.get(workstream.id, [])
        if not derived_aliases:
            augmented.append(workstream)
            continue
        augmented.append(
            replace(
                workstream,
                aliases=tuple(
                    dict.fromkeys(
                        alias
                        for alias in (*workstream.aliases, *derived_aliases)
                        if alias.strip()
                    )
                ),
            )
        )
    return tuple(augmented)
