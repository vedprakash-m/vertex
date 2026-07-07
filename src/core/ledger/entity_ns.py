"""EntityNsMapper — bidirectional bridge between ledger entity IDs and signal-layer refs.

§6.2 compliance: ledger entity IDs (``work_item:ado-12345``, ``person:jdoe``) and the
existing signal-layer entity refs (``WI:12345``, ``P:jdoe``) are separate namespaces.
This module provides deterministic, reversible translation between them so ledger
provenance and signal provenance can be joined without polluting either namespace.

Zone boundary: Zone A (src/core/ledger/ → Zone A allowed).
"""
from __future__ import annotations


class EntityNsMapper:
    """Translates between ledger entity IDs and signal-layer entity refs.

    Ledger ID form: ``<type>:<slug>`` (e.g. ``work_item:ado-12345``, ``person:jdoe``)
    Signal-layer form: ``WI:<number>`` (work items), ``P:<alias>`` (people)

    The mapping is deterministic and reversible for the two pairs defined in §6.2:
    - Work items: ``work_item:ado-<n>`` ↔ ``WI:<n>``
    - People:     ``person:<alias>`` ↔ ``P:<alias>``
    """

    _WI_PREFIX = "WI:"
    _PERSON_PREFIX = "P:"
    _LEDGER_WI_PREFIX = "work_item:ado-"
    _LEDGER_PERSON_PREFIX = "person:"

    # ------------------------------------------------------------------
    # Work-item pair
    # ------------------------------------------------------------------

    def work_item_to_ledger(self, signal_ref: str) -> str:
        """``WI:12345`` → ``work_item:ado-12345``."""
        if not signal_ref.startswith(self._WI_PREFIX):
            raise ValueError(f"Not a work-item signal ref: {signal_ref!r}")
        number = signal_ref[len(self._WI_PREFIX):]
        return f"{self._LEDGER_WI_PREFIX}{number}"

    def ledger_to_work_item(self, ledger_id: str) -> str | None:
        """``work_item:ado-12345`` → ``WI:12345``; returns None if not a work-item ID."""
        if not ledger_id.startswith(self._LEDGER_WI_PREFIX):
            return None
        number = ledger_id[len(self._LEDGER_WI_PREFIX):]
        return f"{self._WI_PREFIX}{number}"

    # ------------------------------------------------------------------
    # Person pair
    # ------------------------------------------------------------------

    def person_to_ledger(self, signal_ref: str) -> str:
        """``P:jdoe`` → ``person:jdoe``."""
        if not signal_ref.startswith(self._PERSON_PREFIX):
            raise ValueError(f"Not a person signal ref: {signal_ref!r}")
        alias = signal_ref[len(self._PERSON_PREFIX):]
        return f"{self._LEDGER_PERSON_PREFIX}{alias}"

    def ledger_to_person(self, ledger_id: str) -> str | None:
        """``person:jdoe`` → ``P:jdoe``; returns None if not a person ID."""
        if not ledger_id.startswith(self._LEDGER_PERSON_PREFIX):
            return None
        alias = ledger_id[len(self._LEDGER_PERSON_PREFIX):]
        return f"{self._PERSON_PREFIX}{alias}"

    # ------------------------------------------------------------------
    # Convenience: translate any known signal ref → ledger form
    # ------------------------------------------------------------------

    def to_ledger(self, signal_ref: str) -> str | None:
        """Translate a signal-layer ref to ledger form; returns None if unknown."""
        if signal_ref.startswith(self._WI_PREFIX):
            return self.work_item_to_ledger(signal_ref)
        if signal_ref.startswith(self._PERSON_PREFIX):
            return self.person_to_ledger(signal_ref)
        return None

    def from_ledger(self, ledger_id: str) -> str | None:
        """Translate a ledger entity ID to signal-layer form; returns None if unknown."""
        result = self.ledger_to_work_item(ledger_id)
        if result is not None:
            return result
        return self.ledger_to_person(ledger_id)
