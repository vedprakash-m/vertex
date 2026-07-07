from __future__ import annotations

from src.core.config_loader import ReportBundle
from src.core.models import PersonProfile, ProgramContext, Workstream as ReportWorkstream


def _build_model_program_context(bundle: ReportBundle) -> ProgramContext:
    if bundle.program_context is None:
        return ProgramContext(
            program_name=bundle.config.edition.name,
            mission=bundle.config.edition.title,
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        )
    return ProgramContext(
        program_name=bundle.program_context.program_name,
        mission=bundle.program_context.mission or bundle.program_context.objective or bundle.config.edition.title,
        pillars=bundle.program_context.pillars,
        workstreams=tuple(
            ReportWorkstream(
                name=workstream.name,
                aliases=workstream.aliases,
                area_paths=workstream.area_paths,
                dri_email=workstream.dri_email or "",
                description=workstream.description or "",
            )
            for workstream in bundle.program_context.workstreams
        ),
        glossary=bundle.program_context.glossary,
        people=tuple(
            PersonProfile(
                email=person.email,
                display_name=person.display_name or person.email,
                role=person.role or "",
                workstreams=person.workstreams,
            )
            for person in bundle.program_context.people
        ),
    )