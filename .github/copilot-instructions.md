# Vertex — Copilot Instructions

## Project Overview

Vertex is a governed TPM intelligence platform for Technical Program Managers (TPMs) and Engineering Managers (EMs). It ingests multi-source evidence, computes deltas and judgments, and renders Outlook-compatible HTML newsletters and related delivery surfaces.

**Architecture:** Three-zone hybrid — Zone A (deterministic core), Zone B (AI layer), Zone C (M365/external). `src/core/` must never import from `src/ai/` or `src/m365/`.

**Current phase:** L0 (Deterministic Kernel) with L1 foundations. ~117K LOC across 163 Zone A modules, 28 Zone B modules, 13 Zone C modules, 101 CLI command modules. 2332+ tests. See `specs/reimagine.md` for evolution roadmap.

## Spec Authority (Read Before Changing Anything)

| Domain | Binding Spec | Path |
|--------|-------------|------|
| What to build (requirements, phases) | PRD | `specs/vertex-prd.md` |
| How to build it (data models, CLI, config) | Tech Spec | `specs/vertex-tech-spec.md` |
| What it looks like (layout, tokens, rendering) | UX Spec | `specs/vertex-ux-spec.md` |

When specs disagree, the binding spec for that domain wins. Architecture decision record: `.archive/specs/` (gitignored, available locally).

## Code Style

- Python 3.11+. Type hints on all public functions.
- `@dataclass(frozen=True, slots=True)` for all value objects.
- Times are UTC `datetime`. IDs are `int` for ADO work items. Enums are `str` enums for JSON portability.
- No `datetime.utcnow()` — use `datetime.now(timezone.utc)`.

## Architecture Rules

- **Zone boundary is sacred:** `src/core/` must not import from `src/ai/` or `src/m365/`. Enforced by `tests/contracts/test_import_boundaries.py`.
- **Single write path:** Only `snapshot_store.write_confirmed()` writes confirmed snapshots, called only from `commands/confirm.py`.
- **Narrative editing surface:** `narratives/issue_NNN/*.md` — per-section Markdown files. Same surface from Phase 1A through Phase 2 AI.
- **Overrides are structured data:** `overrides/issue_NNN.yaml` — risk levels, top_3_now, metadata. Never narrative prose.
- **Config schema:** Tech Spec §8.1 is binding. PRD config examples are illustrative only.

## CLI Convention

Typer subcommands, not argparse flags:
```
vertex report --edition acme_weekly --dry-run
vertex confirm --edition acme_weekly --issue 12
vertex freshness --edition acme_weekly
```

## Build and Test

```bash
pip install -e ".[dev,ai,ai-local,m365,kusto,render]"
python -m pytest tests/ -q
```

## Working Style Preferences

- **No unrequested changes.** Do not add features, refactor, or "improve" beyond the exact ask. Do not add docstrings/comments/type annotations to code you didn't change.
- **Analyze before acting.** For any non-trivial change: trace downstream effects, explain what will change (including side effects), get approval before implementing.
- **Spec changes require care.** These are canonical documents reviewed by multiple AI models and the author. Changes must be precise, justified, and preserve existing structure.
- **Think deeper when asked.** When the user says "think deeper" or asks for comprehensive design, provide thorough analysis — don't settle for surface-level answers.
- **Use Python scripts for bulk spec edits.** Complex multi-site replacements across large Markdown files are more reliable as Python scripts than sequential tool calls.
- **Commit discipline:** Descriptive commit messages. List what changed per file. Never `--force` push or amend published commits without asking.
- **Prefer direct ADO API over WorkIQ for ADO data.** When fetching work items, area paths, queries, or any ADO data, use `ADOClient` (`src/core/ado_client.py`) directly via a script or the CLI — not WorkIQ. WorkIQ is non-deterministic NL search over M365 data; `ADOClient` provides exact OData/REST/WIQL results. Use WorkIQ only for M365 data that has no direct API path (emails, Teams messages, meeting transcripts).

## Key Invariants

- `❓ Needs Input` = hard publish-block (QG-8). Cannot confirm with missing risk levels.
- `--dry-run` = produce output artifacts but no archive writes, no external sends.
- Every published fact must be ADO-traceable (three-tier attribution).
- Editorial ban-list runs on ALL rendered content (Zone A + Zone B output).
- Email max width: 680px outer table, 640px content area.
- Font stack: `Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif`.


---

# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.