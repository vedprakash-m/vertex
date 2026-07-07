# Governance

Tracked root for AI policy, ADRs, role charters, decision logs, and graduation
records. **No governance artifact may live under `docs/` or `.archive/`** (both
git-ignored) — a CI drift test in WS-9 fails on violation.

## Layout

- `decisions/` — Architecture Decision Records (ADRs) and decision logs.
- `roles/` — Role charters (e.g. AI-safety approver — required by WS-5 step 0).
- `graduations/` — Per-feature AI graduation records (WS-5a).
- `threat-model.md` — Application threat model (WS-24).
- `model-cards.md` — AI model cards + lifecycle rules (WS-24).
- `data-classification.yaml` — Data classification × retention matrix (WS-15).

## Conventions

- Every artifact is dated; ADRs are immutable once recorded.
- All references to TRACKED specs (PRD, Tech, UX, this prod-vis) are by path
  + section anchor (e.g. `specs/vertex-prd.md §1`), not by line number
  (lines drift).
- All non-trivial design choices made by workstreams in `specs/prod-vis.md`
  file a one-paragraph ADR here (WS-9 step 4).
