from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlatformProofDefinition:
    proof_id: str
    label: str
    phase: str
    description: str
    archetype: str | None = None


_PLATFORM_PROOF_DEFINITIONS: tuple[PlatformProofDefinition, ...] = (
    PlatformProofDefinition(
        proof_id="p4a_clean_machine",
        label="PR:P4a Clean-Machine Proof",
        phase="P4a",
        description="Fresh-clone / clean-machine onboarding proof without repo edits.",
    ),
    PlatformProofDefinition(
        proof_id="p4b_ado_only",
        label="PR:P4b ADO-Only Proof",
        phase="P4b",
        description="ADO-only provider proof recorded against a real program.",
    ),
    PlatformProofDefinition(
        proof_id="p4c_multi_source",
        label="PR:P4c Multi-Source Proof",
        phase="P4c",
        description="Multi-source provider proof recorded against a real program.",
    ),
    PlatformProofDefinition(
        proof_id="p6b_ado_only",
        label="PR:P6b Archetype Proofs",
        phase="P6b",
        description="Archetype proof for an ADO-only program.",
        archetype="ADO-only",
    ),
    PlatformProofDefinition(
        proof_id="p6b_ado_kusto",
        label="PR:P6b Archetype Proofs",
        phase="P6b",
        description="Archetype proof for a combined ADO + Kusto program.",
        archetype="ADO + Kusto",
    ),
    PlatformProofDefinition(
        proof_id="p6b_ado_m365",
        label="PR:P6b Archetype Proofs",
        phase="P6b",
        description="Archetype proof for a combined ADO + M365 program.",
        archetype="ADO + M365",
    ),
    PlatformProofDefinition(
        proof_id="p6b_narrative_onboarding_light",
        label="PR:P6b Archetype Proofs",
        phase="P6b",
        description="Archetype proof for a narrative / onboarding-light program.",
        archetype="Narrative/onboarding-light",
    ),
    PlatformProofDefinition(
        proof_id="s7a_rollback_drill",
        label="PR:S7a Rollback Drill",
        phase="S7a",
        # Phase 6 §22 Step 10: a recorded rollback drill is a Phase 6
        # entry gate. The drill restores the program to a prior
        # checkpoint and confirms the post-rollback state is consistent
        # (parity pass + trusted baseline re-derivable). It is
        # recorded in `platform_proof_log.yaml` before the irreversible
        # default flip.
        description="Rollback drill: restore program to a prior checkpoint and verify post-rollback state (parity pass + trusted baseline re-derivable).",
    ),
)

_PLATFORM_PROOF_DEFINITIONS_BY_ID = {definition.proof_id: definition for definition in _PLATFORM_PROOF_DEFINITIONS}
_CANONICAL_ARCHETYPES = {
    definition.archetype.casefold(): definition.archetype
    for definition in _PLATFORM_PROOF_DEFINITIONS
    if definition.archetype is not None
}


def get_platform_proof_definition(proof_id: str) -> PlatformProofDefinition | None:
    return _PLATFORM_PROOF_DEFINITIONS_BY_ID.get(proof_id)


def iter_platform_proof_definitions() -> tuple[PlatformProofDefinition, ...]:
    return _PLATFORM_PROOF_DEFINITIONS


def iter_platform_required_proof_definitions() -> tuple[PlatformProofDefinition, ...]:
    return _PLATFORM_PROOF_DEFINITIONS


def iter_platform_archetype_proof_definitions() -> tuple[PlatformProofDefinition, ...]:
    return tuple(definition for definition in _PLATFORM_PROOF_DEFINITIONS if definition.archetype is not None)


def iter_platform_core_proof_definitions() -> tuple[PlatformProofDefinition, ...]:
    return tuple(definition for definition in _PLATFORM_PROOF_DEFINITIONS if definition.archetype is None)


def normalize_platform_archetype(archetype: str | None) -> str | None:
    if archetype is None:
        return None
    normalized = archetype.strip()
    if not normalized:
        return None
    return _CANONICAL_ARCHETYPES.get(normalized.casefold(), normalized)


def validate_platform_proof_identity(*, proof_id: str, archetype: str | None) -> tuple[PlatformProofDefinition, str | None]:
    definition = get_platform_proof_definition(proof_id)
    if definition is None:
        valid_ids = ", ".join(sorted(_PLATFORM_PROOF_DEFINITIONS_BY_ID))
        raise ValueError(f"Unknown proof_id {proof_id!r}. Expected one of: {valid_ids}.")

    normalized_archetype = normalize_platform_archetype(archetype)
    if definition.archetype is None and normalized_archetype is not None:
        raise ValueError(f"Proof {proof_id!r} does not accept --archetype.")
    if definition.archetype is not None and normalized_archetype is None:
        raise ValueError(
            f"Proof {proof_id!r} requires archetype {definition.archetype!r}."
        )
    if definition.archetype is not None and normalized_archetype != definition.archetype:
        raise ValueError(
            f"Proof {proof_id!r} requires archetype {definition.archetype!r}, not {normalized_archetype!r}."
        )
    return definition, normalized_archetype
