# ADR-0001: Three-Zone Import Boundary (Zone A / Zone B / Zone C + Orchestrator)

**Date:** 2026-06-09
**Status:** Accepted
**Workstream:** WS-9 (architecture sustainability)
**Author(s):** Vertex engineering
**Approver(s):** n/a (structural convention, enforced by CI)

## Context

The codebase grew to ~178 Zone A modules, 28 Zone B AI modules, 19 Zone C M365 modules, and 106
Orchestrator (command) modules. Without a formal boundary rule, AI and M365 imports leaked into core
logic, making offline testing and Zone A unit tests impossible. The CI contract test
`test_import_boundaries.py` (INV-1) was failing sporadically.

## Decision

Imports must flow inward only: Orchestrator → Zone B/C → Zone A. Zone A (`src/core/`) must never
import from `src/ai/` or `src/m365/`. Zone B must never import from Zone C or Orchestrator. This is
enforced by an AST-scan contract test (`tests/contracts/test_import_boundaries.py`) that runs on every
CI push. Violations are hard failures.

## Consequences

Easier: Zone A is fully testable offline without Azure or M365 credentials; AI models can be swapped
without touching core logic; Zone A is smaller/faster to lint and type-check. Harder: cross-cutting
concerns (e.g. a gateway that needs both an AI model and a database) must be wired at the Orchestrator
layer, not inside a Zone A helper. Explicitly rejected: a flat module structure (too much coupling) and
a plugin registry approach (premature, adds complexity).

## References

- `tests/contracts/test_import_boundaries.py` (INV-1)
- `specs/vertex-tech-spec.md §1.1` — zone definitions
- `specs/vertex-prd.md §9` — architecture overview
