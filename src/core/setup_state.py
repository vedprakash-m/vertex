"""Conversational auto-discovery concierge state machine and draft types.

This module lives in Zone A (src/core/) and must NOT import from src/ai/ or
src/commands/. It defines the state machine, draft types, confidence tracking,
and session persistence for the ``vertex setup`` command.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import portalocker

# ---------------------------------------------------------------------------
# Confidence type
# ---------------------------------------------------------------------------

FieldConfidence = Literal["inferred", "user_confirmed", "default"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_TTL_DAYS: int = 7
"""Default session time-to-live in days. Overridable per-program via
``setup.session_ttl_days`` in program.yaml for --update mode."""

SESSION_DIR_NAME: str = ".vertex"
"""Directory inside the workspace where session files are stored."""


# ---------------------------------------------------------------------------
# Concept explanations (hard-coded defaults used when AI is unavailable)
# ---------------------------------------------------------------------------

CONCEPT_EXPLANATIONS: dict[str, str] = {
    "workstream": (
        "A workstream is a flexible grouping of related work — like a tag you "
        "apply across ADO items. It doesn't have to match your org chart."
    ),
    "scorecard": (
        "A scorecard is a collection of dimensions (like health categories) "
        "that track progress across your workstreams. Each dimension has an "
        "ADO filter that determines which work items feed into it."
    ),
    "altitude": (
        "Altitude controls how much detail the newsletter includes. "
        "'helicopter' gives a high-level overview; 'street' gives deep "
        "technical detail."
    ),
    "ado_filter": (
        "An ADO filter is a query expression that selects which work items "
        "appear in a scorecard dimension. Example: \"area_path contains "
        "'One\\\\Storage\\\\Compliance'\"."
    ),
    "edition": (
        "An edition defines the newsletter's format, cadence, and scope. "
        "Most programs start with a weekly 'detailed' edition."
    ),
    "dri": (
        "DRI stands for Directly Responsible Individual — the person "
        "accountable for a workstream's health and progress."
    ),
    "program": (
        "A program is the top-level organizational unit in Vertex. It "
        "represents a team or initiative that produces one or more newsletter "
        "editions."
    ),
    "area_path": (
        "An area path in ADO is a hierarchical label (like One\\\\Storage\\\\"
        "Compliance) that categorizes work items. Vertex uses them to group "
        "work into workstreams."
    ),
}


# ---------------------------------------------------------------------------
# Allowed state transitions
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "greeting": frozenset({"identity", "ado_probe"}),
    "identity": frozenset({"ado_probe"}),
    "ado_probe": frozenset({"ado_discovery", "structure_propose"}),
    "ado_discovery": frozenset({"structure_propose"}),
    "structure_propose": frozenset({"structure_confirm", "style_propose", "review"}),
    "structure_confirm": frozenset({"people_propose", "structure_propose"}),
    "people_propose": frozenset({"people_confirm"}),
    "people_confirm": frozenset({"style_propose", "people_propose"}),
    "style_propose": frozenset({"style_confirm"}),
    "style_confirm": frozenset({"preview", "style_propose"}),
    "preview": frozenset({"review"}),
    "review": frozenset({
        "write",
        "structure_propose",
        "people_propose",
        "style_propose",
        "preview",
        "greeting",
    }),
    "write": frozenset({"done"}),
}


# ---------------------------------------------------------------------------
# SetupDraft — in-memory draft state for the conversational concierge
# ---------------------------------------------------------------------------

@dataclass
class SetupDraft:
    """Accumulates conversational onboarding state across turns.

    Unlike the static ``OnboardDraft`` (which is frozen/slots), ``SetupDraft``
    supports partial fields, per-field confidence levels, and edit history for
    rollback. It is transformed to ``OnboardDraft`` via ``to_onboard_draft()``
    before writing config files.
    """

    identity: object | None = None  # IdentityStage from onboard.py
    ado: object | None = None  # ADOStage from onboard.py
    structure: object | None = None  # StructureStage from onboard.py
    people: object | None = None  # PeopleStage from onboard.py
    style: object | None = None  # StyleStage from onboard.py

    # True when live ADO discovery (Phase B) was used to populate ado fields.
    # Used by setup_command to transition through the ado_discovery state.
    ado_discovery_used: bool = False

    field_confidence: dict[str, FieldConfidence] = field(default_factory=dict)
    edit_history: list[SetupDraft] = field(default_factory=list)

    # Confidence gating: these fields must be "user_confirmed" before the
    # write state can be reached. All other fields default to "inferred" in
    # --auto mode, which is acceptable.
    REQUIRED_CONFIRMED_FIELDS: ClassVar[tuple[str, ...]] = (
        "identity.program_name",
        "identity.author_email",
        "ado.organization",
        "ado.project",
        "structure.workstreams",
    )

    def snapshot(self) -> SetupDraft:
        """Return a shallow copy for edit history."""
        return SetupDraft(
            identity=self.identity,
            ado=self.ado,
            structure=self.structure,
            people=self.people,
            style=self.style,
            ado_discovery_used=self.ado_discovery_used,
            field_confidence=dict(self.field_confidence),
            edit_history=[],  # Don't carry forward old history
        )

    def rollback(self, steps: int = 1) -> SetupDraft:
        """Roll back edit history by N steps. Default 1 restores previous draft.

        Raises IndexError if steps exceeds history length.
        """
        if steps > len(self.edit_history):
            raise IndexError(
                f"Cannot roll back {steps} steps; "
                f"only {len(self.edit_history)} in history"
            )
        return self.edit_history[-steps]

    def is_ready_to_write(self) -> bool:
        """Check if all REQUIRED_CONFIRMED_FIELDS have user_confirmed status."""
        for key in self.REQUIRED_CONFIRMED_FIELDS:
            if self.field_confidence.get(key) != "user_confirmed":
                return False
        return True

    def to_onboard_draft(self) -> tuple:
        """Return (identity, ado, structure, people, style) field tuple.

        The caller (command layer) constructs the actual ``OnboardDraft``
        using the stage types from ``src.commands.onboard``.  This avoids
        a Zone A → commands import; conversion lives in the orchestrator.
        """
        return (
            self.identity,
            self.ado,
            self.structure,
            self.people,
            self.style,
        )


# ---------------------------------------------------------------------------
# ConversationTurn — one turn in the conversational state machine
# ---------------------------------------------------------------------------

@dataclass
class ConversationTurn:
    """A single turn in the setup conversation."""
    role: Literal["system", "user", "tool"]
    content: str
    tool_calls: list[object] | None = None  # For structured AI responses


# ---------------------------------------------------------------------------
# ConversationStateMachine — drives the conversational concierge
# ---------------------------------------------------------------------------

class ConversationStateMachine:
    """State machine for the vertex setup conversational concierge.

    States follow a strict transition graph defined by ALLOWED_TRANSITIONS.
    Any attempt to transition to an invalid state raises ValueError.
    """

    states: tuple[str, ...] = (
        "greeting",
        "identity",
        "ado_probe",
        "ado_discovery",
        "structure_propose",
        "structure_confirm",
        "people_propose",
        "people_confirm",
        "style_propose",
        "style_confirm",
        "preview",
        "review",
        "write",
        "done",
    )

    def __init__(self, draft: SetupDraft) -> None:
        self.current: str = "greeting"
        self.draft: SetupDraft = draft
        self.turns: list[ConversationTurn] = []

    def transition(self, target: str) -> None:
        """Advance the state machine to ``target``.

        Raises ValueError if the transition is not in ALLOWED_TRANSITIONS.
        """
        allowed = ALLOWED_TRANSITIONS.get(self.current, frozenset())
        if target not in allowed:
            raise ValueError(
                f"Invalid transition: {self.current} → {target}. "
                f"Allowed: {sorted(allowed)}"
            )
        self.current = target

    def add_turn(self, role: Literal["system", "user", "tool"], content: str) -> None:
        """Record a conversation turn."""
        self.turns.append(ConversationTurn(role=role, content=content))


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

def _session_path(workspace: Path, edition_slug: str) -> Path:
    """Return the path for a setup session file."""
    return workspace / SESSION_DIR_NAME / f"setup_session_{edition_slug}.json"


def _session_lock_path(workspace: Path, edition_slug: str) -> Path:
    """Return the path for a setup session lock file."""
    return workspace / SESSION_DIR_NAME / f"setup_session_{edition_slug}.lock"


def _latest_session_path(workspace: Path) -> Path:
    """Return the path for the pre-slug session file."""
    return workspace / SESSION_DIR_NAME / "setup_session_latest.json"


def save_session(
    draft: SetupDraft,
    state_machine: ConversationStateMachine,
    workspace: Path,
    edition_slug: str | None = None,
) -> Path:
    """Atomically save session state to a JSON file.

    Uses portalocker + staging temp file + os.replace() pattern consistent
    with onboard.py's atomic write approach. A lock file prevents concurrent
    writes from two vertex setup processes targeting the same edition.

    Returns:
        Path to the saved session file.
    """
    slug = edition_slug or "latest"
    session_file = (
        _session_path(workspace, slug) if edition_slug
        else _latest_session_path(workspace)
    )
    lock_file = _session_lock_path(workspace, slug) if edition_slug else (
        workspace / SESSION_DIR_NAME / "setup_session_latest.lock"
    )
    session_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "current_state": state_machine.current,
        "edition_slug": edition_slug,
        "field_confidence": dict(draft.field_confidence),
        "saved_at": time.time(),
    }

    # Acquire lock
    lock_fd = open(lock_file, "w", encoding="utf-8")  # noqa: SIM115
    portalocker.lock(lock_fd, portalocker.LOCK_EX)

    try:
        # Write to staging temp file
        staging = session_file.with_suffix(".tmp")
        staging.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(str(staging), str(session_file))
    finally:
        portalocker.unlock(lock_fd)
        lock_fd.close()

    return session_file


def load_session(
    workspace: Path,
    edition_slug: str | None = None,
) -> tuple[SetupDraft, ConversationStateMachine, dict] | None:
    """Load a saved session from disk.

    If ``edition_slug`` is None, finds the most recent session file by mtime.

    Returns:
        Tuple of (draft, state_machine, raw_data) or None if no session found.
    """
    session_dir = workspace / SESSION_DIR_NAME

    if edition_slug:
        session_file = _session_path(workspace, edition_slug)
        if not session_file.exists():
            return None
        files = [session_file]
    else:
        # Find the most recent session file
        if not session_dir.exists():
            return None
        files = sorted(
            session_dir.glob("setup_session_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return None

    session_file = files[0]
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    draft = SetupDraft()
    draft.field_confidence = data.get("field_confidence", {})

    state_machine = ConversationStateMachine(draft)
    state_machine.current = data.get("current_state", "greeting")

    # Check session expiry
    saved_at = data.get("saved_at", 0)
    age_days = (time.time() - saved_at) / 86400
    if age_days > SESSION_TTL_DAYS:
        return None  # Session expired

    return draft, state_machine, data


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def generate_edition_slug(program_name: str) -> str:
    """Generate an edition slug from a program name.

    Rules:
      - Lowercase the program name
      - Replace spaces and special characters with underscores
      - Collapse consecutive underscores
      - Append '_weekly' (default cadence)

    Example: "Azure Storage Compliance" → "az_storage_compliance_weekly"
    """
    import re
    slug = program_name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = re.sub(r"_+", "_", slug)
    slug = slug.strip("_")
    if not slug:
        slug = "new_program"
    return f"{slug}_weekly"


# ---------------------------------------------------------------------------
# Typing workaround for ClassVar
# ---------------------------------------------------------------------------

from typing import ClassVar  # noqa: E402