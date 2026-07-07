# Vertex — Project Instructions

## Project and Authority

Vertex is a governed intelligence platform for Technical Program Managers (TPMs) and Engineering Managers (EMs): ingest multi-source evidence, compute deltas/judgments, render Outlook-compatible HTML newsletters and related delivery surfaces.

Three-zone hybrid: A = deterministic core, B = AI, C = M365/external. Current phase: L0 (Deterministic Kernel) with L1 foundations; ~117K LOC, 163 Zone A / 28 Zone B / 13 Zone C modules, 101 CLI command modules, 2332+ tests. Roadmap: `specs/reimagine.md`.

**Read specs before changing anything.** The binding spec for each domain wins disagreements:

| Domain | Canonical spec |
|---|---|
| Requirements, semantics, phases | [PRD v0.8](../specs/vertex-prd.md) |
| Architecture, data models, CLI, config | [Tech Spec v0.16](../specs/vertex-tech-spec.md) |
| UX, ergonomics, layout, tokens, rendering | [UX Spec v3.4](../specs/vertex-ux-spec.md) |

Architecture decision records, superseded plans, and completed feature specs: `.archive/specs/` (gitignored, available locally). Canonical specs are reviewed by multiple AI models and the author: make precise, justified changes that preserve their structure. Use Python scripts for complex multi-site bulk edits across large Markdown specs.

## Code and Architecture

- Python 3.11+; type hints on all public functions; `@dataclass(frozen=True, slots=True)` for all value objects.
- UTC `datetime`; use `datetime.now(timezone.utc)`, never `datetime.utcnow()`. ADO work item IDs: `int`; enums: `str` enums for JSON portability.
- **Zone boundary:** `src/core/` must never import from `src/ai/` or `src/m365/`; enforced by `tests/contracts/test_import_boundaries.py`.
- **Single write path:** only `snapshot_store.write_confirmed()` writes confirmed snapshots; only `commands/confirm.py` calls it.
- **Narratives:** per-section Markdown in `narratives/issue_NNN/*.md`; same editing surface from Phase 1A through Phase 2 AI.
- **Overrides:** structured risk levels, `top_3_now`, metadata in `overrides/issue_NNN.yaml`; never narrative prose.
- **Config:** Tech Spec §8.1 is binding; PRD config examples are illustrative only.
- **ADO data:** use `ADOClient` (`src/core/ado_client.py`) directly via script or CLI for all work items, area paths, queries, etc. It provides exact OData/REST/WIQL results; do not use WorkIQ's non-deterministic M365 NL search. Use WorkIQ only for M365 data without a direct API path (emails, Teams messages, meeting transcripts).
- **CLI:** Typer subcommands, not argparse flags:
  ```
  vertex report --edition acme_weekly --dry-run
  vertex confirm --edition acme_weekly --issue 12
  vertex freshness --edition acme_weekly
  ```

## Publish and Rendering Invariants

- `❓ Needs Input` is a hard publish-block (QG-8); cannot confirm with missing risk levels.
- `--dry-run` produces output artifacts but no archive writes or external sends.
- Every published fact must be ADO-traceable (three-tier attribution).
- Run the editorial ban-list on ALL rendered content (Zone A + Zone B).
- Email maximum widths: 680px outer table, 640px content area.
- Font stack: `Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif`.

## Working Rules

- **Think before coding:** state assumptions and tradeoffs; present multiple interpretations rather than silently choosing. If uncertain or unclear, stop, name the confusion, and ask. Propose simpler approaches and push back when warranted.
- **Approval:** before non-trivial changes, trace downstream effects, explain changes and side effects, and obtain approval. Bias toward caution over speed; use judgment for trivial tasks.
- **Exact scope:** every changed line must trace to the request. No unrequested features, refactors, improvements, flexibility/configurability, single-use abstractions, or error handling for impossible scenarios. Write the minimum sufficient code; simplify if a senior engineer would find it overcomplicated (200 lines that could be 50 should be rewritten).
- **Surgical edits:** match existing style even if you prefer another; do not improve adjacent code/comments/formatting or add docstrings/comments/type annotations to unchanged code. Remove imports/variables/functions made unused by your changes. Mention unrelated dead code; do not remove pre-existing dead code unless asked.
- **Depth:** when asked to "think deeper" or provide comprehensive design, analyze thoroughly, not superficially.
- **Verification:** define measurable success criteria and iterate until verified. Validation: test invalid inputs, then make tests pass. Bug fixes: reproduce in a test, then fix. Refactors: ensure tests pass before and after. For multi-step tasks, state a brief plan pairing each step with its verification; avoid vague "make it work" goals.
- **Prose:** say what you mean directly. Prefer literal language over mannered metaphor/flourish (e.g., "a parameter worth varying," not "a dial worth turning"; "this point still matters," not "this point earns its keep"). Avoid imprecision, unintended connotations, and prose that displays the writer rather than conveying the idea.
- **Commits:** descriptive messages; list changes per file. Never force-push or amend published commits without asking.
- **Success indicators:** fewer unnecessary diff changes, fewer rewrites from overcomplication, and clarification before implementation rather than after mistakes.

## Build and Test

```bash
pip install -e ".[dev,ai,ai-local,m365,kusto,render]"
python -m pytest tests/ -q
```

## Feature Development Lifecycle

**Follow in order; do not skip steps:**
1. **Spec:** write `specs/<feature>.md` covering requirements, data model, alternatives considered, risks, assumptions, and acceptance criteria. Iterate with the user until approved; **no implementation before approval**.
2. **TDD:** write failing tests first, then implement to pass.
3. **Validate:** `pytest tests/`, coverage >= 95%, `scripts/verify_specs.py`, and parity checks.
4. **UAT:** generate real HTML (`python -m src run --view all`); visually confirm default and projector profiles.
5. **Finalize:**
   - Backfill the three canonical specs if the feature added components/rules.
   - Archive: `Move-Item specs/<feature>.md .archive/specs/`.
   - Update `CHANGELOG.md`: dated entry, feature table, modified files, test files.
   - Audit `.gitignore` for PII/secrets.
   - Commit (a) feature code + tests, (b) spec updates + archive.
   - Push and confirm CI passes.