# Vertex — Technical Specification

**Version:** 1.0  
**Status:** Reflects implemented state as of 2026-07-08 (newsletter read-path closure, `specs/fix-data-flow.md`, archived, landed on top of the 2026-07-07 REV activation proof); remaining work is operator/human-paced, tracked in `specs/backlog.md`
**Companion docs:** [vertex-prd.md](vertex-prd.md) (requirements), [vertex-ux-spec.md](vertex-ux-spec.md) (visual design), `specs/backlog.md` (remaining real-data activation work), `.archive/specs/transform.md` (V2 transformation history, archived)  
**Scope split:** This document owns concrete schemas, module/file inventories, command signatures, and implementation contracts. Product intent and acceptance criteria remain in [vertex-prd.md](vertex-prd.md).  
**Current product scope:** any Microsoft TPM program within the declared supported archetypes/exclusions. Future expansion to broader TPM/EM audiences, global tenants, and non-ADO ecosystems is roadmap, not part of the current technical contract.

## Changelog

- Last updated: 2026-07-08 — Incorporated `specs/fix-data-flow.md` (v1.0→v1.13, archived to `.archive/specs/fix-data-flow.md`) into §13.6. Added §13.6.8 documenting the newsletter read-path closure across all 13 tracks (A–M): the bridge-default-on flip with a durable failure backlog and doctor warning (Track A, ADR-0011); `risk_stage.py`/dependency SoR migration onto `ProgramReality` plus the first-ever trust-badge Jinja markup (Track B); the extracted `sor_gated_family_load()` helper and its migration-protocol doc (Track B.5); the assumption migration, and the direct finding that action/decision/commitment have no current main-newsletter read path while workstream's content is narrative-driven (Track C); the incremental-projection write hook under WAL-mode concurrency (Track D, ADR-0010); render-pipeline call-site consolidation across 7 sites (Track E); regression tests for the six first-contact bugs (Track F); the structured `<!-- spec-posture -->` block and `scripts/verify_spec_citations.py` (Track G); `--pipeline-v2` removal (Track H); the AI-narrative `FactAssessment` audit and its token-budgeted implementation via `src/commands/reality_context.py` (Track J, PR-11a/PR-11b — both closed, ahead of the spec's own "next priority after Track C" schedule); the substrate health monitor and multi-database cleanup runbook (Track K); the fact-deserialization safety-net doctor check (Track L); and confirmation that the bridge privacy gate is already family-agnostic (Track M). Full suite: 6922 passed, 901 skipped, 0 failed.
- Last updated: 2026-07-07 (session 2) — Reconciled `specs/backlog.md`'s two remaining operator-paced items with direct evidence. **BL-A1 (Azure Content Safety):** provisioned and verified live (a real shield call returned a genuine `clean` verdict); the remaining 5-consecutive-clean-cycle gate is now purely wall-clock, not a config/engineering gap. **BL-A2 (corpus certification):** corrected an earlier miscalibration — `reachable_document_count=72` measured documents that might mention the keystone family broadly, not real extraction-sourced yield of the specific certifying claim; a direct candidate-store audit found only 1 real extraction-sourced `milestone.completed` instance (and single digits for the other two accessor families) across the pilot's entire real history, with Wilson-CI math confirming ≥25 such instances are needed even with perfect labels. `recommended_v1_authoritative` is accepted as the durable operating tier for single-program deployments; full statistical certification is deferred until fleet rollout (≥3 programs) makes pooled dual-annotation viable. Updated §13.6.5's operator-gate summary accordingly.
- Last updated: 2026-07-07 — Incorporated `specs/activation.md` (ACTIVATION-1 v1.0→v1.29, archived to `.archive/specs/activation.md`) into §13.6. Added §13.6.7 documenting the real-data activation proof (the activation sentence fired for the first time on a real pilot program's data), five previously-undetected code gaps found and fixed on first real contact (candidate-store schema drift, milestone stub `target_date`, the fact↔record entity-ref join, `source_document_key` bridge wiring, and the counterfactual-proof harness), the read-time `approval_event_id` reverse-lookup join, the hardening contracts (`is_clean_cycle()`, cycle classes, the composite privacy gate, entity-resolution wiring, cross-source conflict detection), and the `scripts/verify_activation.py` self-verification tool. Added REV-25 to the implementation-status table. Replaced the S-9e/Q7/S-10a operator-gate summary with a pointer to `specs/backlog.md`, which now carries the two remaining real-data items (Azure Content Safety provisioning; corpus dual-annotation) as an executable-gate-mapped feature spec.
- Last updated: 2026-06-29 — Closed the last model-implementable read-path gap (WS-1). Added §13.6 rows REV-23 (ProgramReality read-path overlays: S-8a milestone + S-8c commitment + S-8d workstream/ownership; 3 of 4 v1-authoritative families covered, with graceful legacy fallback) and REV-24 (deterministic extractor clears G-xtract-prec 86.7% / G-accept-prec 100% on the preliminary corpus via the status-table "Done" guard + R2 faithful event types). Documented the remaining operator-paced gates (ADR-0006 Amendment A4.2): S-9e corpus κ-certification/freeze (0 of 539 dual-labeled today), Q7 production-extractor promotion (deferred; deterministic clears floor), S-10a Azure Content Safety IT provisioning. All model-implementable engineering is now complete; what remains is operator/human-paced.

- Last updated: 2026-06-27 — Folded the consolidated implementation/decision details into the canonical tech spec and made PRD/Tech/UX the only GitHub-synced specs. ADR-0006 is accepted and machine-checked by `src/core/consolidated_gate_approval.py` / `scripts/audit_consolidated_decision_gates.py --require-accepted`. Added the accepted technical posture: S-5c family flip gate, S-NC-apply engine, S-2c entity resolution, S-8b synthetic authority G-slice, S-11 load/soak evidence path, S-12 Teams accepted-limitation posture, NCFL apply via canonical `save_*` functions, and deliverable/incident Phase 2 scaffolding without v1 authority.

- Last updated: 2026-06-25 — REV Waves 1–7 coding complete. §13.6 updated: (W1) normalizer data-loss fix (`min_chunk_len` boundary guard, `next.start ≤ prev.end` invariant); (W2) acceptance via review-state transition + shadow isolation (all 8 projectors filter `review_state==ACCEPTED`, 11 contract tests) + `domain_event_id`/`candidate_id` lineage fields on `ProgramFactInput`/`ProgramFactRevision` + idempotent `append_fact` via `domain_event_id` unique index + `event_type_registry.py` unified registry (`LedgerEventSpec`, `EventDisposition`, 16 entries) + selective family replay `--family` on `vertex ledger replay` + `GapMatchCriteria` / `check_contradiction` / `is_stale` / `evaluate_stale_gaps` in `gap_lifecycle.py`; (W3) `RealityCompletenessVector` 3-area dataclass in `src/core/reality_completeness.py` + `RevHealthReport.completeness_vector` wired into `vertex doctor --rev-health`; (W5-3) `PseudonymTable` + `build_pseudonym_table_from_display_names` + `pseudonymize_text` in `privacy.py`; `normalize(known_display_names=...)` and `NormalizationResult.pseudonym_table: dict[str, str] | None` in `normalizer.py`; `_extract_display_names(msg)` + `email.utils.getaddresses` wiring in `eml_hydrator.py`; (W6-1) ICS + local-file enumerators wired into `vertex rev run` via `--ics-inbox`; (W7-1) `src/core/ledger/candidate_sqlite_store.py` WAL-mode SQLite — `candidate_id` is the PRIMARY KEY (unique); `dedupe_key` has a non-unique index (dedupe enforced upstream, not at DB level); `candidate_decisions` is append-only (full history, no UPSERT); auto-migration from JSONL+rotated files; JSONL retained as audit-only; (W7-2) `_incremental_fold` in `program_views.py` — delete affected entity rows, re-fold with all visible events, full-rebuild triggers (delta>50, correction event, `program.*`/`schedule.*`/`workstream.*` prefix). 1455 contract tests. Working spec archived to `.archive/specs/still-gaps.md`.

- Last updated: 2026-06-24 — Incorporated `specs/program-context-intelligence.md` (REV v1.6, archived to `.archive/specs/`) into the canonical M365 integration contract. §13.6 REV — Program-Context Intelligence Pipeline added: full module inventory for `src/core/rev/` (14 modules), `src/ai/rev/` (3 modules), `src/m365/rev/` (8 modules), and `src/core/ledger/` additions (`rev_evidence.py`, `verification_assertions.py`, `gap_lifecycle.py`); Zone A port contracts (`CandidateEnumerator`, `ContentHydrator`, `ChangeFeed`, `EvidenceVerifier`); multi-budget governor schema; durable run-state machine; `VerificationAssertion` ledger; evidence metadata schema; capability profile config; implementation status (REV-00..16) with operator-gated items documented.

- Last updated: 2026-06-22 — Incorporated `specs/move-output-newsletter.md` (archived to `.archive/specs/`) into the canonical workspace layout contract. §10.2: `_resolve_output_dir(program_dir, edition_id)` is the single edition-output path resolver — hard-fails on split-brain (both `output/` and `publications/` exist simultaneously), falls back to legacy `output/` during the transition window while the canonical `publications/` directory is absent on disk. `get_program_output_root(program_id)` added for root-scoped callers (no edition suffix). `ResolvedEditionPaths.publications_dir` replaces `output_dir` as the canonical field; `output_dir` is a deprecated property alias (removed in Phase 5). `VertexMigrationError` added to `src/core/exceptions.py`. New storage doctor check PO-01 (`src/commands/doctor_checks/storage_checks.py`): 6-state workspace layout check (OK, WARN-mismatch, ERROR-split-brain, ERROR-unknown-label, INFO-partial, INFO-fresh). Migration runbook: `python scripts/migrate_edition_output.py --all --verify` (dry-run + SHA-256 manifest + per-program markers; rollback via `--rollback`); Phase 5 cleanup (remove legacy fallback + deprecated alias) triggers when PO-01 is clean for 2 consecutive `vertex doctor` runs.

- Last updated: 2026-06-22 — Incorporated `specs/nudge-gaps.md` into the canonical nudge contract and archived the working spec. The binding nudge surface now includes schema 2.1 `full_hygiene` ownership, ProgramReality-backed deadline/action-due resolution, audience-policy enforcement at draft-generation time, lifecycle-v2 cooldown anchored to `--mark-sent` / `--sent-at`, state schema 1.2, publication-index schema 1.1 with content hash + audience manifest, and `event.nudge.*` fact writes via `append_nudge_event()`.

- Last updated: 2026-06-21 — Consolidated the WorkIQ retrieval qualification and FQ-01 implementation into §13.1. The canonical contract now covers typed program/lane policy, deterministic structured prompts, uncached union repetitions, strict bounded validation, semantic identity fallback, privacy-safe capture, rollback, and release gates. The working specification was archived locally under `.archive/specs/fix-workiq.md`.

- Last updated: 2026-06-22 — Incorporated `specs/move-nudge.md` (v1.4, archived to `.archive/specs/`) into the canonical nudge contract. §2.8/§2.8b/§10.5/§10.6: nudge workspace relocated from `programs/<id>/output/<id>_nudge/` to `programs/<id>/nudge/`; generated EMLs go to `nudge/drafts/{run_id}.eml`; `nudge_state.json` / `nudge_audit.jsonl` / `title_cache.json` at `nudge/` root; `published_eml/` sub-directory stores human-attested sent EMLs with `index.json` manifest (schema 1.0). `NudgePaths` + `get_nudge_paths()` in `src/core/edition_resolver.py` is the single path construction point. New CLI flags: `--mark-sent <draft-ref>` copies a draft to `published_eml/`, `--list-drafts` lists available drafts; `--dry-run` is mutually exclusive with both. NQ-10 doctor check warns when legacy `output/<id>_nudge/` paths still exist. Path helper `get_legacy_nudge_output()` provides a single transition-window fallback source (Phase 4 removes both). Draft pruning: 20 most-recent files retained per `NUDGE_DRAFT_RETAIN`.

- Last updated: 2026-06-21 — Incorporated `specs/fix-nudge.md` (v2.3, archived to `.archive/specs/`) into the canonical nudge contract. §2.8/§2.8b: replaced the stale hardcoded three-section A/B/C (RAMP P1 / POST RAMP) description with the data-driven N-section engine (`NudgeConfig`/`NudgeSectionSpec`/`NudgeSectionCriteria` in `src/core/nudge_models.py`; loader in `src/core/nudge_config.py`; query layer in `src/core/nudge_query.py`; state in `src/core/nudge_state_store.py`). Documented tri-state `bool|None` comment signals (`None` = not evaluated, never a failure), `X-Unsent: 1` draft (never sent), per-item cooldown from `hygiene.cooldown_days` (minimum across sections), `nudge_audit.jsonl` append-only audit, state schema 1.1, exit codes 0/2/3, `vertex doctor --nudge` (NQ-1..NQ-9), and `vertex fleet` nudge summary. §14.4: nudge artifacts relocated to `src/core/nudge_models.py`. Legacy `--stale-a/b/c` flags remain as hidden deprecated options (`--stale-override` is canonical).
- Last updated: 2026-06-19 — Consolidated the WorkIQ/M365 newsletter-enrichment spec into the core technical contract. `report_ai.py` / `blurb_generator.py` now document the complete approved-evidence bundle used at synthesis time: review-gated `WorkstreamEvidence`, ADO comments, Kusto metrics, IcM, lookback intelligence, freshness-by-source, ADO telemetry summaries, reference-doc updates, and approved prior-issue feedback thread context. `vertex enrich` remains the Zone C WorkIQ ingress, with provenance/quality recording and approval-gated evidence admission enforced before AI synthesis. The working spec `specs/newsletter-workiq.md` is archived locally under `.archive/specs/`.

- Last updated: 2026-06-19 — Evidence extraction pipeline (ME-01 through ME-05, commit 3a853bb + program_id bug fix): `WorkIQ` provenance recording wired into `gather.py` (ME-01); `gather_pipeline/evidence_extraction_stage.py` created (ME-02) with `run_evidence_extraction_stage()` + `persist_evidence()`; `enrich.py` registered as `vertex enrich` command (ME-03); `edit_learner.py` extended with `append_evidence_correction()`/`load_evidence_corrections()` (ME-04); `evidence_quality.py` created in Zone A Stores with `record_evidence_quality()`/`load_evidence_quality()` (ME-05). Doctor checks updated: `check_eta_slippage` and `check_false_done_lanes` accept `evidence_override` param; `check_evidence_quality_drift` added. `context_checks.py` now loads evidence store and passes it to checks. 32 new tests (5 files). §1.2: `evidence_quality.py` added to Zone A Stores; `enrich.py` + `gather_pipeline/evidence_extraction_stage.py` added to Orchestrator commands. §1.4 Data Flow updated. §11.3 Gather step 10 added. §11.5 `enrich` command row added.

 §1.2: evidence pipeline modules added to Zone A (`evidence_models.py`, `evidence_provenance.py`), Zone B (`content_extractor.py`, `decision_brief_advisor.py`, `tiered_router.py`, `setup_assistant.py`, and 9 other missing Zone B modules), Zone C (`local_kb_reader.py` + 13 other missing Zone C modules). §10.1: `EDITIONS_ROOT` marked as legacy fallback constant. §10.2: edition resolver updated to describe programs-tree glob lookup (`programs/*/editions/<id>.yaml`) with backwards-compat root-level fallback. BL-09/10/11 doctor checks for empty workstream registry and missing `name` fields implemented in `src/commands/doctor_checks/context_checks.py`. Companion docs updated to remove stale `gaps.md` and `backlog.md` references (both archived).

- Last updated: 2026-07-13 — ADF-W5.3 (specs/arch-data-fix.md, local-only): evaluated `local_tier.py`'s Tier-1 economy-lane gate (§8.8.2 of that spec: requires a signed model artifact, model card, holdout evaluation, Wilson lower-bound ≥0.95, abstention, and a kill switch before any feature may route through it). Zero production callers existed (`LocalTierMatcher` was never wired into any feature's `local_fn`) and none of the gate's evidence requirements were ever produced — the spec's own explicit fallback ("If no feature clears the gate, delete `local_tier.py` and remove economy-lane claims") applied cleanly, confirmed by the user (2026-07-13) after the auto-mode safety classifier correctly blocked an initial autonomous attempt. Deleted `src/ai/local_tier.py`; removed it from the Zone B module list below. The generic `local_fn` extensibility seam in `tiered_router.py::route_through_tiers` is unaffected and remains available to a future Tier-1 implementation that does clear the gate.

- Last updated: 2026-06-16 — Incorporated Acme onboarding learnings: §11.2 Confirm: `--force`/`--ack-forecast` flag descriptions added (force overrides all forceable QGs; ack-forecast pre-acknowledges ETA warnings for non-interactive confirm); §11.3 Gather step 7: FR-SG-38 population auto-approval added (`compute_auto_approval_policies`, `min_sample=10`, `ceiling_rate=0.8`, `floor_rate=0.2` in `persistence_stage.py`); §11.5 `hints` row: path fix noted (commit 9adee32 — `bundle.program.id` not `edition_name` for proposal store path).

- Last updated: 2026-06-12 (rev 2) — Incorporated full data-model spec (archived to `.archive/specs/data-model.md`). §1.2: Added `entity_ns.py`, `redaction.py`, projection engine (`src/core/projections/`), protection tiers (`src/core/protection/`), and knowledge plane (`src/core/knowledge/`) rows. §9.17: Extended with entity namespace bridge, compliance redaction, protection/projection/knowledge modules, Zone B AI extractors, Zone C M365 connectors, QG-DM enforcement summary, and INV-DM invariant references. `docs/ledger-backfill-runbook.md` added as the operator runbook.

- Last updated: 2026-06-12 — Added the shipped Program Event Ledger inventory to §1.2 and a new §9.17 Program Event Ledger section covering the append-only event log, SQLite event index, candidate queue, projection store, evidence vault, and verify-status sidecar under `programs/<id>/ledger/`. This closes the long-standing tech-spec drift where the repo had landed `src/core/ledger/` and `vertex ledger` operator surfaces but `vertex-tech-spec.md` still stopped at §9.16 Program Fact Store.

- Last updated: 2026-06-10 — Spec consolidation: re-debt.md and remains.md archived to `.archive/specs/`; operational readiness backlog consolidated into `specs/backlog.md` (local-only); `specs/` folder now contains only canonical committed specs. All `remains.md` forward-references updated to `backlog.md`. §9.17 implementation status updated: all re-debt Phases 0–8 code-complete including Phase 5 (CP-3 PASSED 2026-06-10) and Phase 7 (CP-7 reviewed 2026-06-10; actuation policy all-false by default); claim_actuation.py (WI-7.1b) and projection migration to ProgramReality.load() (WI-1.2) committed.

- Last updated: 2026-06-15 — WI-8.1 update: §1.1 Zone A count 228→241 (13 new reality substrate modules); Zone B count 28→30 (tiered_router + local_tier); §1.2 new "Reality substrate" category (program_reality.py, truth_levels.py, truth_model.py, entity_registry.py, signal_normalizer.py, entity_alias_emitter.py, fact_schema_registry.py, commitment_store.py, source_trust.py, fact_sor_state.py, privacy_filter.py, signal_promotion.py, null_projection.py); reality-substrate contract coverage added for the ProgramReality facade, 5 truth levels, source authority family map, entity registry, trust ledger, actuation model, and external consumer surface. **Implementation status: Phases 0–4, 6–7.3/7.4 complete; Phase 5 pending CP-3; Phase 7.1/7.2 pending CP-7 + A-14.**
- Last updated: 2026-06-09 — Phase 4/5/6 debt-remediation wave closed (debt.md rev 277–351): persisted-state grounding hardened across all 28 file-backed stores; JSONL append-only rotation live for all 7 high-risk writers (`rotate_jsonl_if_oversize` with 10 MB per-stem cap); AI safety pipeline enforcement is now mandatory — `process_generated_text` is the required wrapper on every `src/ai/` generation path (contract test prevents inline re-implementation); `frontier_eligible` in `ai_policy.yaml` is a real operator kill switch enforced in `deployment_fallback.py::resolve_ai_deployments_for_feature` before any client is constructed; `AI_PROPOSAL_TTL_DAYS = 14` constant in `ai_proposal_store.py` governs proposal GC. New CLI commands: `vertex doctor --source-waivers` (schema validation + expiry), `vertex rollback --drill/--archetype/--notes` (sandbox simulation + `s7a_rollback_drill` proof), `vertex facts dual-read-log/pin-snapshot/detect-drift`. New Zone A items: `ProgramEvent` dataclass (`fact_type`, `natural_key`, `metadata`) + `append_program_event` helper as the canonical seam for event-style facts; `event.issue.skip` first migration; `workstream.association` and `baseline.trust_event` fact types dual-write to the Fact Store; `ProviderRegistry` extended with `register_connector`/`resolve_connector`/`connector_types()` (D-23 registry unification); `SourceWaiverSchema`/`SourceWaiverFieldSpec` dataclasses + `validate_waiver_against_schema` in `source_waiver_store.py`. `_impl` DI-adapter pattern documented; `use_trace_context` migration complete for all 7 AI client construction sites. Working docs archived to `.archive/specs/`; remaining work in `specs/backlog.md`.

- Last updated: 2026-06-03 — Final `discover.md` consolidation + archive: §13.4 "Safe auto-resolution" now documents the centralized Zone A `discovery_resolution.passes_auto_resolution_gate` predicate (HARD 0.85 / SOFT 0.75 thresholds plus per-source-type corroboration in the 0.75–0.85 band); module inventory adds `discovery_resolution.py` and `source_intent_audit.py`; end-to-end discovery coverage lands in `tests/unit/test_autonomous_discovery.py` (discover → accept/seed → registration → hydrate → signal yield). `discover.md` moved to `.archive/specs/discover.md`.
- Last updated: 2026-06-02 — P1 scope decision applied: this spec now aligns to any Microsoft TPM program within the declared supported archetypes/exclusions; broader global TPM/EM and non-ADO ecosystem expansion remains roadmap rather than current runtime contract.
- Last updated: 2026-06-02 — P7 spec-drift closure: §1.3a, §1.4, and §3 updated to reflect ADO UIL as default-on while Kusto/Teams/IcM remain env-gated; §8.3 and §10.1 corrected to match the existing `chart_renderer_registry.py` auto-discovery model instead of implying a second registry/config merge path; §9.16 migration status refined to shadow-write foundation landed / SoR flip pending; §10.6 corrected to remove the deleted `section_catalog.py` authority claim.
- Last updated: 2026-06-01 — §1.2: added "Signal fidelity & fact layer" Zone A category (`chronicle.py`, `checkpoint_store.py`, `maturity_engine.py`, `conversion_fidelity.py`, `measurement_spine.py`, `entity_resolution.py`, `cold_start_accelerator.py`, `review_packs.py`, `source_health.py`, `signal_ranking.py`) and "External connectors" category (`connector_config.py`, `connector_polling.py`, `external_connector.py`, `external_dependency.py`, `connectors/`); §11.5: `facts`, `connectors`, `rollback` commands. Reflects the implemented (coding-agent) portion of signals.md (FR-SG-03/05/06/08..53 + checkpoint/connector/fact-export). Remaining `[OPERATOR]`/`[HUMAN GATE]` work from `signals.md` and `backlog.md` is consolidated into [backlog.md](backlog.md); both are archived to `.archive/specs/`. The §9.16 Program Fact Store reconciliation (below) remains the binding storage+API contract.
- Last updated: 2026-06-16 — §1.1: Zone A module count updated (180→181); §1.2: `program_fact_store.py` added to Stores; §9.11: updated to note 15 total tables (13 RealityStore + 2 Program Fact Store); §9.16 Program Fact Store added (bitemporal `program_fact_revisions` + `program_fact_snapshot_pins` tables, public API, F1→F4 migration roadmap). Reconciles `vertex-tech-spec.md` to signals.md §7.4 storage+API contract per HUMAN GATE (e), clearing the FR-SG-54..73 P2 gate.
- Last updated: 2026-05-30 — §1.1: Zone A module count updated (175→180); §1.2: chart pipeline modules added (Rendering: `chart_cache_store.py`, `chart_renderer_registry.py`, `theme_context.py`, `charts/`; Configuration: `kusto_query_loader.py` chart fields); §8.3: chart pipeline subsection added; §10.1: chart config fields; §14.1: `evaluate_chart_gates()` added; QG-20/21/22 added to gate table. Reflects charts.md implementation (archived to `.archive/specs/`).
- Last updated: 2026-05-29 — §1.1: Zone A module count updated (159→175); command module count updated (106→111); §1.2: hint engine & governance modules added; §1.3a: QG-23/24/25 noted; §11.5: `hints` and `decisions governance` commands; §14.1: QG-23/24/25 added. Reflects hands-off.md implementation (archived to `.archive/specs/`).
- Last updated: 2026-05-27 — §1.2: 5 new context-maturity Zone A modules; §2: ProgramContext/ContextSnapshot data models; §9.13–9.15: ContextSnapshotStore, Plane1Changelog, ContextGapStore; §11.2 Confirm: context snapshot + maturity regression; §11.3 Gather: plane1 changelog; §11.5: doctor --context/--fix-hints, fleet context health columns. Reflects program-context-maturity.md (archived to `.archive/specs/`).
- Last updated: 2026-05-27 — §1.3a: UIL phases 0–5 confirmed complete (Phase 4a parity PASSED, Phase 5 old-path removal done); §1.4: UIL ADO gather path noted in data flow; §3: UIL note refined.
- Last updated: 2026-05-26 — Added UIL (Unified Integration Layer): §1.2 module inventory updated with 16 new Zone A modules, §1.3a implementation snapshot updated, §9.12 ChannelRegistryStore added, §11.5 integration commands added, §17.1 test count updated. UIL phases 0–3 and 4a (partial) are now code-complete.
- Last updated: 2026-05-23 — Refreshed module inventory, full-suite evidence, and implementation posture after consolidating the active backlogs into [backlog.md](backlog.md).

## Table of Contents

1. [Architecture](#1-architecture)
2. [Data Models](#2-data-models)
3. [ADO Client](#3-ado-client)
4. [Core Engines](#4-core-engines)
5. [Signal Journal & Trajectory Store](#5-signal-journal--trajectory-store)
6. [Analysis Engines](#6-analysis-engines)
7. [Vitality System](#7-vitality-system)
8. [Rendering System](#8-rendering-system)
9. [Storage Layer](#9-storage-layer)
10. [Configuration System](#10-configuration-system)
11. [Command Implementations](#11-command-implementations)
12. [AI Layer](#12-ai-layer)
13. [M365 Integration Layer](#13-m365-integration-layer)
14. [Quality Gates](#14-quality-gates)
15. [Error Handling & Resilience](#15-error-handling--resilience)
16. [Observability](#16-observability)
17. [Testing Strategy](#17-testing-strategy)
18. [Dependencies & Build](#18-dependencies--build)

---

## §1 Architecture

### 1.1 Three-Zone Hybrid

| Zone | Location | Purpose | AI/M365 Imports | External I/O |
|------|----------|---------|-----------------|-------------|
| **A — Deterministic Core** | `src/core/` (384 modules) | Models, engines, stores, renderers, validators | Forbidden | ADO/Kusto data acquisition only |
| **B — AI Layer** | `src/ai/` (55 modules + prompt assets) | Content generation, review, safety pipeline, M365 topic routing | Native | Via Zone A stores |
| **C — M365 Integration** | `src/m365/` (49 modules) | ADO writer, Agency bridge, Graph mail, discovery adapters, enricher | Forbidden | Native |
| **Orchestrator** | `src/commands/` (254 command modules) | CLI commands wiring all zones | Allowed | Allowed |

*Module counts are probe-derived (`scripts/derive_spec_counts.py`), re-derived 2026-07-09 as part of arch-fix.md Phase 0's count-probe repair — re-run the script rather than hand-editing these numbers.*

**Sacred invariant:** `src/core/` must not import from `src/ai/` or `src/m365/`. Enforced by `tests/contracts/test_import_boundaries.py` — AST-level scan of every `*.py` under `src/core/`. Any `import` or `from ... import` referencing `src.ai` or `src.m365` is a test failure.

**Note on Zone A I/O:** `ado_client.py` and `kusto_client.py` reside in Zone A and make HTTP calls for data acquisition via `requests` and `azure-kusto-data`. The zone boundary forbids AI and M365 imports — not all external I/O. ADO/Kusto calls are deterministic data acquisition, not probabilistic AI or M365 integration.

**Entry point:** `cli.py` → Typer app registered as `vertex` in `pyproject.toml [project.scripts]`.

### 1.2 Module Inventory

**Zone A (`src/core/`):**

| Category | Modules |
|----------|---------|
| Domain models | `models.py`, `models_v2.py`, `view_models.py`, `exceptions.py`, `evidence_models.py` (`WorkstreamEvidence`, `EtaRecord`, `SourceRef`, `EvidenceSourceType`, `parse_workiq_latest_date`, `extract_ado_ids`, `extract_icm_ids`, `build_placeholder_evidence`), `evidence_provenance.py` (`EvidenceProvenanceRecord`, `record_provenance`) |
| Data acquisition | `ado_client.py`, `query_builder.py`, `kusto_client.py`, `kusto_rendering.py` |
| Configuration | `config_loader.py`, `config_loader_v2.py`, `edition_resolver.py`, `knowledge_store.py`, `backfill_loader.py`, `kusto_query_loader.py` |
| Contract loaders | `chapter_contract_loader.py`, `slice_contract_loader.py`, `template_contract_loader.py` |
| Core engines | `delta_engine.py`, `evidence_engine.py`, `evidence_assembler.py`, `freshness_engine.py`, `scorecard_engine.py`, `forecast_engine.py`, `date_inference.py`, `business_days.py`, `contradiction_engine.py`, `readiness_engine.py`, `semantic_index.py` |
| Analysis engines | `trajectory.py`, `trajectory_analyzer.py`, `signal_dedup.py`, `anticipation_detector.py`, `altitude_guard.py`, `cascade_detector.py`, `coverage_gap.py`, `velocity_metrics.py`, `scorecard_trends.py`, `raid_graph.py`, `ask_lifecycle.py`, `dependency_scout.py`, `intervention_ranker.py` |
| Vitality | `vitality_scorer.py`, `vitality_reporting.py`, `leakage_detector.py`, `ado_semantics.py` |
| Editorial | `ban_list_validator.py`, `verbosity_enforcer.py`, `hygiene_engine.py`, `voice_validator.py`, `acme_voice.py` (shim), `quality_gates.py`, `quality_matrix_engine.py`, `remediation_engine.py` |
| Stores | `journal.py`, `snapshot_store.py`, `archive_store.py`, `narrative_store.py`, `overrides_store.py`, `review_status_store.py`, `summary_store.py`, `notification_state_store.py`, `claim_tracker.py`, `manifest_writer.py`, `continuation_contract.py`, `sqlite_stores.py`, `store_factory.py`, `file_stores.py`, `section_proposal_store.py`, `analytics_store.py`, `incident_journal_store.py`, `claim_extraction_calibration_store.py`, `brief_intervention_store.py`, `catchup_state_store.py`, `program_fact_store.py`, `evidence_quality.py` (`EvidenceQualityRecord`, `record_evidence_quality()`, `load_evidence_quality()` — ME-05 per-lane confidence drift tracking, writes to `journal/evidence_quality.jsonl`) |
| Program event ledger | `ledger/__init__.py`, `ledger/ulid.py`, `ledger/source_refs.py`, `ledger/event_types.py`, `ledger/event_log.py`, `ledger/event_index.py`, `ledger/program_views.py`, `ledger/candidate_store.py`, `ledger/discovery_run_recorder.py`, `ledger/discovery_candidate_builders.py`, `ledger/evidence_vault.py`, `ledger/fact_bridge.py`, `ledger/verify_status.py`, `ledger/entity_ns.py`, `ledger/redaction.py` |
| Projection engine | `projections/program_projection.py`, `projections/program_views.py`, `projections/snapshot_manager.py` |
| Protection tiers | `protection/supersession.py`, `protection/field_lock_store.py` |
| Knowledge plane | `knowledge/predicate_registry.py` (closed claim-predicate vocabulary); claim store integrated with `src/core/knowledge_claim_store.py` |
| Rendering | `html_renderer.py`, `deck_renderer.py`, `reviewer_renderer.py`, `teams_renderer.py`, `eml_writer.py`, `jinja_filters.py`, `chart_cache_store.py`, `chart_renderer_registry.py`, `theme_context.py`, `charts/` (package: `declarative.py`, `deployment_velocity.py`, `chart_config_schema.py`) |
| Attribution | `attribution_engine.py`, `lineage.py` |
| Workflow | `triage.py`, `publish_diff.py`, `owner_pack.py`, `catchup_runner.py`, `catchup_scan.py`, `signal_review.py`, `action_mapper.py` |
| Feedback | `feedback/_advisory_yaml.py`, `feedback/catchup_classifier.py`, `feedback/salience_modeler.py`, `feedback/calibration_router.py`, `feedback/edit_weight_updater.py`, `feedback/signal_approval_learner.py`, `feedback/anomaly_kinds.py` |
| Knowledge | `kb_updates.py`, `kb_changelog.py` |
| ADO write-back | `ado_proposal.py`, `ado_reconcile.py`, `ado_status.py` |
| Agent protocols | `agents/_base.py` |
| M365 Discovery | `m365_router_interface.py`, `keyword_topic_router.py`, `m365_registry_store.py`, `m365_signal_corpus.py`, `m365_discovery_support.py`, `discovery_intent.py`, `source_candidate_store.py`, `discovery_resolution.py`, `source_intent_audit.py` |
| Integration (UIL) | `integration_types.py`, `integration_protocol.py`, `channel_registry_store.py`, `channel_wiring.py`, `ado_discovery.py`, `ado_hydration.py`, `ado_signal_extractor.py`, `teams_discovery.py`, `teams_hydration.py`, `teams_signal_extractor.py`, `kusto_discovery.py`, `kusto_hydration.py`, `kusto_signal_extractor.py`, `icm_discovery.py`, `icm_hydration.py`, `icm_signal_extractor.py` |
| Context maturity | `program_context.py`, `context_snapshot_store.py`, `plane1_changelog.py`, `context_gap_store.py`, `staleness_check.py` |
| Hint engine & governance | `ado_narrative_hint_engine.py`, `exec_summary_diff_engine.py`, `ado_pr_client.py`, `engms_signal_extractor.py` |
| Signal fidelity & fact layer | `program_fact_store.py` (also Stores), `chronicle.py`, `checkpoint_store.py`, `source_health.py`, `signal_ranking.py`, `maturity_engine.py`, `conversion_fidelity.py`, `measurement_spine.py`, `entity_resolution.py`, `review_packs.py`, `cold_start_accelerator.py` |
| **Reality substrate** | `program_reality.py` (ProgramReality G-1 facade — WI-1.1; `load()`, domain accessors, `attention()`, `pending_actuations()`, `to_dict()`, `diff()`), `truth_levels.py` (TruthLevel enum — WI-0.7), `truth_model.py` (TruthContext, `derive_truth_level()` 5-rule ladder, `SourceAuthorityPolicy` loader, `MATERIALITY_PREDICATES`, `get_authority_family()` — WI-3.0), `entity_registry.py` (EntityRegistry with exact/casefold/fuzzy tier, rapidfuzz WRatio, per-scope thresholds — WI-2.0/2.1), `signal_normalizer.py` (`normalize_signal()`, idempotence, `backfill_entity_refs()` — WI-2.2), `entity_alias_emitter.py` (`emit_entity_alias_facts()`, idempotent natural-key dedup — WI-2.3), `fact_schema_registry.py` (`validate_fact_payload()`, 13 registered types — WI-1.5), `commitment_store.py` (CommitmentEntry, SlipRecord, direction, slip_history — WI-2.7), `source_trust.py` (trust ledger, Laplace score update, bootstrap grants, circuit breaker, O-16 bucket classification — WI-3.1), `fact_sor_state.py` (FactSorState — legacy/shadow/primary SoR flip, per-family resolution, clean-cycle gate evaluation), `privacy_filter.py` (`load_privacy_policy()`, `is_fact_visible()` — classification ceiling), `signal_promotion.py` (signal review state promotion — WI-3.2a), `null_projection.py` (NullProjection — O-15 proof; new app builds against facade without Zone A change — WI-7.4) |
| External connectors | `connector_config.py`, `connector_polling.py`, `external_connector.py`, `external_dependency.py`, `connectors/` (package: `github_issues.py`, `sharepoint_lists.py`), `provider_registry.py` (`ProviderRegistry` — D-23: the legacy `CONNECTOR_REGISTRY` dict is unified here; `register_connector`/`resolve_connector`/`connector_types()` are the authoritative connector-registry paths) |
| Infrastructure | `observability.py`, `retry.py`, `circuit_breaker.py` |

**Zone B (`src/ai/`):** `_pipeline.py`, `client.py`, `llm_trace.py`, `cost_guard.py`, `context_budget.py`, `blurb_generator.py`, `exec_summary_drafter.py`, `summary_generator.py`, `anticipation_engine.py`, `draft_reviewer.py`, `edit_learner.py` (`append_evidence_correction()`, `load_evidence_corrections()` — ME-04 human-correction capture for evidence feedback loop), `learning_distiller.py`, `intent_router.py`, `onboard_assistant.py`, `setup_assistant.py`, `backfill_extractor.py`, `claim_extractor.py`, `m365_topic_router.py`, `content_extractor.py` (`ContentExtractionAgent`, `ExtractionContext` — Phase 1-4 evidence pipeline), `decision_brief_advisor.py` (LLM-as-judge per decision item), `tiered_router.py` (Tier 0→1→2 centralized router), `deployment_fallback.py`, `provider.py`, `request_router.py`, `prompt_registry.py`, `synthesizer.py`, `ai_mode.py`, `ai_stage.py`, `action_extractor.py`, `grounding.py`, `injection_detector.py`

AI drafting contract note: approved `WorkstreamEvidenceBundle` inputs now include M365 evidence summaries, ADO comments, Kusto metrics, IcM signals, lookback intelligence, freshness timestamps, approved ADO telemetry summaries, approved reference-doc updates, and approved prior-issue feedback threads. `blurb_generator.py` preserves cited source refs for rendered section footnotes; `exec_summary_drafter.py` consumes the same governed context at exec altitude.

**Zone C (`src/m365/`):** `agency_bridge.py`, `ado_writer.py`, `enricher.py`, `backfill_m365.py`, `graph_mail_client.py`, `graph_calendar_client.py`, `graph_send_client.py`, `teams_reader.py`, `transcript_reader.py`, `local_kb_reader.py` (`read_local_kb_enrichments` — KB enrichment for evidence pipeline), `icm_client.py`, `adaptive_card_renderer.py`, `workiq_ask_support.py`, `workiq_calendar_discovery.py`, `workiq_mail_discovery.py`, `email_discovery.py`, `email_hydration.py`, `registry_id_discovery.py`, `series_id_resolver.py`, `discovery_diagnostics.py`, `autonomous_registry_discovery.py`, `teams_webhook_client.py`

### 1.3 Key Structural Properties

1. **Single write path:** Only `snapshot_store.write_confirmed()` writes confirmed snapshots, called only from `commands/confirm.py`.
2. **Author-in-the-loop:** The editorial pipeline runs multiple times per issue cycle (`report --dry-run` → edit → re-run → confirm). The pipeline is read-only; only `confirm` writes to the archive.
3. **Multi-output from single snapshot:** One confirmed snapshot → HTML email, Teams Markdown, reviewer HTML, deck Markdown, EML, freshness report.
4. **Journal immutability:** Signal journal files are append-only. Review decisions live in a separate `reviews.jsonl` sidecar.

### 1.3a Implementation Snapshot (2026-06-16)

The deterministic implementation is materially ahead of the old feature backlog language. The current code walk-through confirms:

- **re-debt Phases 0–8 code-complete:** All coding-implementable workstreams (WS-1–WS-25) are done. Remaining items are OPERATOR gates, HUMAN GATE blockers, and CALENDAR-gated proofs documented in `.archive/specs/gaps.md` (local-only gap register).
- `gather.py` owns ADO work items/revisions/comments, freshness, trajectories, Kusto, IcM fallback, ADO analytics, sprint, pipeline, PR (via `ado_pr_client.py`), dependency, M365 WorkIQ, channel telemetry, and L1 observation projection.
- `doctor.py` surfaces channel completeness, degraded optional sources, ADO PR repository coverage, M365 discovery debt, metric-binding rollout, readiness, assumptions, decisions, dependencies, circuit breakers, knowledge health, fact-store parity, adapter certs, confirm-readiness (B-1–B-5), operator gates, and platform readiness. `--context` mode validates all 20 Plane 1 files against 21 cross-file invariants and reports context maturity level (L0–L4); `--fix-hints` appends per-invariant remediation guidance.
- `quality_gates.py` implements QG-1 through QG-28 (including QG-26 external-dependency state gate, QG-27 truth-level/material-dispute gate, QG-28 KPI degradation gate, and QG-WS5B AI budget gate) plus bridge gates, with forceable vs hard-block behavior enforced by `confirm.py`. QG-29 is formally reserved (not yet implemented) for the arch-fix.md AF-3 fail-closed AI audit gate — `src/core/quality_gates/gate_registry.py` prevents unrelated work from claiming it (see §9.17.1).
- UIL (Unified Integration Layer) phases 0–5 are code-complete: 16 new Zone A modules, 14 `vertex integration` CLI subcommands, 160+ targeted UIL tests. The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated (`VERTEX_UIL_KUSTO=1`, `VERTEX_UIL_TEAMS=1`, `VERTEX_UIL_ICM=1`). Full migration design at `.archive/specs/unify.md`.
- **Reality substrate** (`ProgramReality` facade, 5 truth levels, entity registry, trust ledger, signal normalizer, fact schema registry, commitment store, per-family SoR mode, clean-cycle flip gates, actuation engine, ask named intents, reality export, null projection) is fully implemented and contract-tested for the deterministic/accepted policy slice.
- **Governance artifacts:** `governance/threat-model.md` (STRIDE analysis), `governance/privacy-matrix.md` (data-classification + DPA checklist), `governance/model-cards.md` (AI model version lifecycle, all 19 `ai_policy.yaml` features carded), `governance/test-evidence.md` (canonical test-evidence log), `governance/decisions/` (ADR templates), `governance/graduations/` (AI feature graduation records), `governance/nfr-budgets.yaml` (NFR/OpEx budget candidates pending ratification — see §9.17.1), `governance/runbooks/` (tracked, generic operator runbooks — SoR cutover rehearsal, ledger backfill).
- Full test-suite execution evidence is recorded in `governance/test-evidence.md` (the canonical evidence log); `output/__green_run.txt` is a stale local artifact — see `scripts/check_spec_drift.py` `p9-dead-green-run`. The current suite collection count is computed at CI time by `scripts/derive_spec_counts.py` (WS-9 step 2 deliverable).

### 1.4 Data Flow

> **UIL note (2026-06-02):** The Gather Phase ADO path now uses UIL by default. `ADODiscoveryProvider` + `ADOHydrationProvider` replace the removed OData broad-sweep and legacy `_load_freshness_program_items` path; signals are produced by `ADOSignalExtractor` with `source="ado"`. Kusto/Teams/IcM gather paths remain separately env-gated.

```
Gather Phase:
  ADO OData/REST → Signal Journal (programs/<prog>/journal/<week>.jsonl)
  ADO Revisions  → Trajectories  (programs/<prog>/trajectories/<id>.jsonl)
  WorkIQ/Kusto/IcM → Signal Journal (pending or auto-reviewed)
  WorkIQ signals → Provenance Record (programs/<prog>/journal/evidence_provenance.jsonl) [ME-01]
  Transcript signals → ContentExtractionAgent → WorkstreamEvidence (programs/<prog>/journal/evidence_store.jsonl) [ME-02]
  Evidence quality → EvidenceQualityRecord (programs/<prog>/journal/evidence_quality.jsonl) [ME-05]

Draft Phase:
  Edition YAML + Program YAML + Knowledge YAML → Edition Resolution
  ADO OData → Live Work Items
    Saved queries + slice contracts → Additional live work item membership
  Previous Snapshot (archive) + Live Items → Delta Engine → DeltaSet
  Items + Dimensions → Scorecard Engine → ScorecardEvidencePacket[]
    Workstream registry + scorecard packets + narratives → Workstream Issue Snapshot / Association Preview
  Items + Window → Evidence Engine → EvidencePacket[]
  Trajectories → Trajectory Analyzer → DriftPattern[]
  Trajectories + DriftPatterns → Forecast Engine → ETAForecast[]
  Approved WorkIQ evidence + ADO telemetry + reference-doc updates + approved feedback threads
    → WorkstreamEvidenceBundle / AI prompt context
  All above + Overrides + Narratives → RenderContext → HTML/EML/MD

Confirm Phase:
  Draft Artifacts + Quality Gates → Archive Write (atomic staging)
    Confirmed Narratives → AI/Regex Claim Extraction → claims.jsonl
  Draft vs Confirmed Narratives → Edit Pattern Recording → edit_patterns.jsonl
    Draft workstream associations → workstream_associations.jsonl
```

---

## §2 Data Models

All value objects use `@dataclass(frozen=True, slots=True)` unless mutation is required. Times are UTC `datetime`. Work item IDs are `int`. All enums are `str` enums with `EnumParserMixin` for fuzzy parsing.

### 2.1 Enums (`src/core/models.py`)

```python
class RiskLevel(EnumParserMixin, str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    DONE = "done"
    UNKNOWN = "unknown"  # Rendered/operator label: "❓ Needs Input"

class DeltaKind(EnumParserMixin, str, Enum):
    NEW = "new"
    CLOSED = "closed"
    RISK_UP = "risk_up"
    RISK_DOWN = "risk_down"
    ETA_CHANGED = "eta_changed"
    OWNER_CHANGED = "owner_changed"
    UNCHANGED = "unchanged"

class Confidence(EnumParserMixin, str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"

class EditionType(EnumParserMixin, str, Enum):
    DETAILED = "detailed"
    FOCUSED = "focused"
    CONDENSED = "condensed"
    NARRATIVE = "narrative"
    DECK = "deck"
    LOOKBACK = "lookback"

class ReviewState(EnumParserMixin, str, Enum):
    PENDING = "pending"
    SENT = "sent"
    APPROVED = "approved"
    SKIPPED_NO_DELTA = "skipped_no_delta"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"

class AttributionTier(EnumParserMixin, str, Enum):
    TIER1 = "tier1"
    TIER2 = "tier2"
    TIER3 = "tier3"
```

### 2.2 ADO Primitives

```python
@dataclass(frozen=True, slots=True)
class Revision:
    work_item_id: int
    rev_number: int
    changed_by: str
    changed_by_email: str
    changed_date: datetime
    fields_changed: dict[str, tuple[str | None, str | None]]

@dataclass(frozen=True, slots=True)
class Comment:
    work_item_id: int
    comment_id: int
    created_by: str
    created_by_email: str
    created_date: datetime
    text: str

@dataclass(slots=True)  # Mutable — enriched post-fetch
class WorkItem:
    id: int; type: str; title: str; state: str
    assigned_to: str | None; assigned_to_email: str | None
    area_path: str; iteration_path: str
    target_date: date | None; risk_level: RiskLevel
    tags: list[str]; custom_fields: dict[str, object]
    revisions: list[Revision]; comments: list[Comment]
    fetched_at: datetime
```

### 2.3 Evidence & Attribution

```python
@dataclass(frozen=True, slots=True)
class Enrichment:
    source: Literal["mail", "transcript", "teams_chat", "calendar"]
    source_id: str; author: str; timestamp: datetime
    excerpt: str; permalink: str | None

@dataclass(frozen=True, slots=True)
class EvidencePacket:
    work_item_id: int
    revisions: tuple[Revision, ...]; comments: tuple[Comment, ...]
    enrichments: tuple[Enrichment, ...]
    confidence: Confidence; tier: AttributionTier
    summary_for_reviewer: str

@dataclass(frozen=True, slots=True)
class ScorecardEvidencePacket:
    dimension_name: str; dimension_description: str
    total_items: int; items_by_risk: dict[str, int]
    stale_items: tuple[int, ...]; stale_count: int
    overdue_items: tuple[int, ...]; overdue_count: int
    blocked_items: tuple[int, ...]; blocked_count: int
    unowned_items: tuple[int, ...]; unowned_count: int
    high_activity_items: tuple[int, ...]
    item_ids: tuple[int, ...]
    prior_confirmed_risk: RiskLevel | None
    author_risk: RiskLevel | None
    ado_query_url: str; item_links: tuple[str, ...]
    derived_risk: RiskLevel
```

### 2.4 Deltas & Scorecards

```python
@dataclass(frozen=True, slots=True)
class ItemDelta:
    work_item_id: int; kind: DeltaKind
    field_changes: dict[str, tuple[str | None, str | None]]
    old_risk: RiskLevel | None; new_risk: RiskLevel | None
    old_eta: date | None; new_eta: date | None
    evidence: EvidencePacket

@dataclass(frozen=True, slots=True)
class DeltaSet:
    issue_number: int; previous_issue_number: int | None
    new_items: tuple[ItemDelta, ...]
    closed_items: tuple[ItemDelta, ...]
    risk_changes: tuple[ItemDelta, ...]
    eta_changes: tuple[ItemDelta, ...]
    owner_changes: tuple[ItemDelta, ...]
    unchanged_count: int

@dataclass(frozen=True, slots=True)
class DimensionRisk:
    name: str; risk: RiskLevel; summary: str
    evidence: EvidencePacket
    derived_risk: RiskLevel; override_risk: RiskLevel | None
```

### 2.5 Freshness

```python
@dataclass(frozen=True, slots=True)
class FreshnessItem:
    work_item_id: int; rule_id: str
    severity: Literal["block", "warn", "info"]
    message: str; suggested_fix: str | None

@dataclass(frozen=True, slots=True)
class FreshnessReport:
    issue_number: int; items: tuple[FreshnessItem, ...]
    blocks: int; warns: int; infos: int
    @property
    def is_clean(self) -> bool: return self.blocks == 0
```

### 2.6 Snapshot & Archive

```python
@dataclass(frozen=True, slots=True)
class SnapshotItem:
    id: int; type: str; title: str; state: str
    assigned_to: str | None; area_path: str
    target_date: date | None; risk_level: RiskLevel; tags: list[str]

@dataclass(frozen=True, slots=True)
class Snapshot:
    issue_number: int; generated_at: datetime; ado_data_as_of: datetime
    edition_type: EditionType
    items: tuple[SnapshotItem, ...]; scorecards: tuple[ConfirmedDimension, ...]
    schema_version: str = "1.0"

@dataclass(frozen=True, slots=True)
class RunManifest:
    manifest_id: str  # uuid4
    issue_number: int; edition: str
    started_at: datetime; ended_at: datetime
    config_hash: str; snapshot_hash: str; html_hash: str; md_hash: str
    ado_calls: int; ai_calls: int; ai_cost_usd: float
    freshness_summary: dict[str, int]
    qg_results: dict[str, bool]; git_sha: str | None
```

### 2.7 V2 Signal Models (`src/core/models_v2.py`)

```python
@dataclass(frozen=True, slots=True)
class Signal:
    id: str                  # UUID
    timestamp: datetime      # UTC
    source: str              # "ado/odata", "ado/revision", "workiq/email", etc.
    program_id: str
    workstream_id: str | None
    entity_refs: tuple[str, ...]  # e.g., ("WI:12345", "P:priya")
    text: str                # ≤500 chars, PII-scrubbed
    raw_ref: str | None      # Opaque source reference
    confidence: Confidence
    metadata: SignalMetadata | None

# Typed metadata discriminated by source:
SignalMetadata = ADOFieldChangeMetadata | KustoMetadata | WorkIQMetadata | IcMMetadata | dict

@dataclass(frozen=True, slots=True)
class SignalReviewDecision:
    record_type: Literal["review"] = "review"
    signal_id: str; decision: Literal["approved", "dismissed", "deferred"]
    reviewed_at: datetime; reviewed_by: str; note: str | None

@dataclass(frozen=True, slots=True)
class SignalUsageMarker:
    record_type: Literal["usage_marker"] = "usage_marker"
    signal_id: str; event_type: Literal["used_in_issue"]
    edition_id: str; issue_number: int
    confirmed_at: datetime; manifest_id: str

@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    date: date; state: str; assigned_to: str | None
    target_date: date | None; risk_level: RiskLevel
    area_path: str; tags: tuple[str, ...] = ()
```

### 2.8 V2 Program Models

```python
@dataclass(frozen=True, slots=True)
class Program:
    id: str; name: str; mission: str; current_phase: str
    pillars: tuple[str, ...]; key_dependencies: tuple[Dependency, ...]
    glossary: dict[str, str]; writing_style: WritingStyle
    tone_calibration: ToneCalibration
    leadership_readers: tuple[LeadershipReader, ...]
    recurring_themes: tuple[str, ...]
    ado: ADOConfig; ai: AIConfig | None = None
    kusto: KustoConfig | None = None; m365: M365Config | None = None

@dataclass(frozen=True, slots=True)
class Workstream:
    id: str; name: str; aliases: tuple[str, ...]
    description: str; why_it_matters: str
    area_paths: tuple[str, ...]; team_ids: tuple[str, ...]
    pm_owner: str; eng_owner: str | None; alternate: str | None
    style_note: str; leadership_sensitivity: Literal["high", "medium", "low"]
    status: Literal["active", "dormant", "closed"]
    ado_saved_query_ids: tuple[str, ...] = ()
    signal_sources: WorkstreamSignalSources | None = None

@dataclass(frozen=True, slots=True)
class EditionConfig:
    id: str; program_id: str; name: str
    type: EditionType; altitude: str
    cadence: Literal["daily", "weekly", "biweekly", "monthly", "quarterly", "ad-hoc"]
    # ... (see PRD §8.1 for full field list)
```

Altitude values are open-vocabulary strings used by templates to choose section depth. Acme uses `helicopter` (weekly tactical), `satellite` (LT/quarterly strategic), and `street` (daily/nudge ground-level). Programs may introduce additional values; `doctor --ids` validates only that the value is non-empty.

Additional workstream-memory types live outside `models_v2.py` because they are persistence and reporting-adjacent rather than shared core program identity models:

- `src/core/workstream_registry.py` — `WorkstreamRegistryEntry`, `WorkstreamIssueSnapshotEntry`, `WorkstreamIssueSnapshot`
- `src/core/workstream_association_store.py` — `WorkstreamAssociationRecord`
- `src/core/models_v2.py` — `TeamsMeetingSeries`, `TeamsChat`, `ADOCoverageRequirement`, `WorkstreamSignalSources`

These types carry durable lifecycle metadata plus draft-time and confirm-time workstream↔ADO association provenance. `TeamsMeetingSeries`, `TeamsChat`, and `EmailThreadSource` carry the optional durable `series_id` / `thread_id` identifiers plus optional explicit `work_item_ids` sourced from `workstreams.yaml`. Those identifiers feed the direct Teams/email discovery + hydration path, while the explicit `work_item_ids` provide deterministic config-backed `WI:` binding for otherwise provider-only collaboration artifacts. The WorkIQ NL search path (`agency_bridge.py`) does **not** require `series_id` / `thread_id`; `m365_activation: complete` does not depend on those fields being populated.

`src/core/models_v2.py` also carries `KustoQuery`, whose Phase-1 loader-facing metadata now includes `catalog_source`, `validated_at`, `owner_alias`, `expected_cardinality`, and `kusto_no_safety`. Runtime-derived query telemetry such as `last_cycle_succeeded`, `last_error`, row-count history, and freshness/variance fields is populated from `gather_state.json` rather than authored config.

### 2.8a Hygiene, Signal-Source, and Proposal Models

Additional V2 workflow models in `src/core/models_v2.py` capture the Wave A/D maturity surfaces:

```python
@dataclass(frozen=True, slots=True)
class WorkstreamSignalSources:
    workiq_keywords: tuple[str, ...] = ()
    workiq_exclude_keywords: tuple[str, ...] = ()
    kusto_query_ids: tuple[str, ...] = ()
    sharepoint_paths: tuple[str, ...] = ()    # engms_pages.yaml entry IDs (SP-2)
    engms_paths: tuple[str, ...] = ()         # eng.ms page IDs (same format, different host)

@dataclass(frozen=True, slots=True)
class HygieneItem:
    work_item_id: int
    missing_fields: tuple[str, ...]
    freshness_business_days: int
    workstream_id: str | None

@dataclass(frozen=True, slots=True)
class WorkstreamCoverageAlert:
    workstream_id: str
    workstream_name: str
    active_item_count: int
    min_required: int
    workstream_lead_alias: str | None
    workstream_lead_email: str | None

class SectionRevisionStatus(EnumParserMixin, str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    ACCEPTED_MODIFIED = "accepted_modified"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"

@dataclass(frozen=True, slots=True)
class SectionEvidenceBrief:
    section_id: str
    ado_delta_summary: str
    new_items: tuple[int, ...]
    closed_items: tuple[int, ...]
    risk_changed_items: tuple[int, ...]
    eta_changed_items: tuple[int, ...]
    top_signals: tuple[str, ...]
    stale_claims: tuple[str, ...]
    vitality_summary: str
    confidence: Confidence
    kpi_summary: str | None

@dataclass(frozen=True, slots=True)
class SectionRevisionProposal:
    proposal_id: str
    edition_id: str
    issue_number: int
    section_id: str
    current_text: str
    proposed_text: str | None
    evidence_brief: SectionEvidenceBrief
    status: SectionRevisionStatus
    generated_at: datetime
    resolved_at: datetime | None = None
    accepted_text: str | None = None
    rejection_reason: str | None = None
    source_hash: str | None = None
    ai_model_used: str | None = None
    ai_cost_usd: float | None = None
```

FullHygieneArtifacts carries the rendered N-section heat-map surface for `vertex nudge`. The full-hygiene engine is **data-driven**: sections, their candidate sources, stale thresholds, templates, cooldown, audience policy, and action-due policy are declared in per-edition `full_hygiene` config (`NudgeConfig`/`NudgeSectionSpec`/`NudgeSectionCriteria` in `src/core/nudge_models.py`; loaded/validated by `src/core/nudge_config.py`) — there is no hardcoded three-section model. Each `FullHygieneRow` is annotated with `has_valid_target_date`, `has_risk_assessment`, `has_recent_comment`, `comment_has_status_keyword`, and `is_ready`. Sections deduplicate earlier→later so an item appears once. `is_overdue` surfaces when the item's ID appears in `ado_live_state` with "OVERDUE" or "no ETA". The three comment signals (`has_recent_comment`/`comment_has_status_keyword`/`is_ready`) are **tri-state `bool | None`**: `None` means "not evaluated" (comment-fetch budget overflow or API failure) and is never rendered as a hygiene failure. The generated EML is marked `X-Unsent: 1` — it is a draft artifact written to `programs/{program_id}/nudge/drafts/{run_id}.eml`, never sent mail; a human reviews and forwards it. Milestone-linked section deadlines and `[Action DUE …]` subject context resolve via `resolve_sections(config, ProgramReality.load(...))`. Audience policy is enforced before a sendable draft is written, and `vertex nudge --approve-draft <draft-ref>` persists operator approval keyed to the draft content hash when the audience policy requires it. Per-item cooldown is anchored to the attested/imported send recorded by `vertex nudge --mark-sent <draft-ref> [--sent-at <iso>]` or `vertex nudge --import-sent <published-ref> [--sent-at <iso>]`, not to draft generation. Every run appends an audit event to `programs/{program_id}/nudge/nudge_audit.jsonl` (append-only JSONL, ISO-8601). State is persisted to `programs/{program_id}/nudge/nudge_state.json` at schema 1.2 (`item:<id> -> {triggered_at,origin,run_id}`; the reader still accepts legacy bare-numeric or bare-string entries). `--mark-sent` copies the EML from `drafts/` to `published_eml/`, verifies any required draft approval, updates cooldown state, and records publication metadata in `published_eml/index.json` at schema 1.1 (`content_hash`, audience manifest, item IDs, claimed/marked timestamps). `--import-sent` reconstructs the same lifecycle metadata for historical sent EMLs already present under `published_eml/`. Nudge fact writes use `append_nudge_event()` with `event.nudge.generated`, `event.nudge.draft_approved`, `event.nudge.sent_attested`, and `event.nudge.sent_imported`. `--list-drafts` lists available drafts. All nudge paths are derived from `get_nudge_paths(program_id)` in `src/core/edition_resolver.py`. `SectionRevisionProposal` records accepts and `ACCEPTED_MODIFIED` edits so confirm-time archival preserves the accepted narrative text.

### 2.8b Full Hygiene Models

```python
@dataclass(frozen=True, slots=True)
class FullHygieneRow:
    work_item_id: int; title: str; title_original: str; item_url: str
    item_type: str; owner_alias: str | None; owner_email: str | None
    workstream_id: str | None; workstream_name: str | None
    has_valid_target_date: bool; has_committed: bool
    has_risk_assessment: bool; risk_is_on_track: bool
    has_risk_reason: bool | None  # None = N/A (risk is on track)
    has_recent_comment: bool | None  # None = not evaluated (budget/API)
    comment_has_status_keyword: bool | None  # None = not evaluated
    is_ready: bool | None  # None = not evaluated
    stale_business_days: int; is_overdue: bool

@dataclass(frozen=True, slots=True)
class FullHygieneWorkstreamGroup:
    workstream_id: str | None; workstream_name: str | None
    workstream_owner_alias: str | None; workstream_owner_email: str | None
    rows: tuple[FullHygieneRow, ...]

@dataclass(frozen=True, slots=True)
class FullHygieneSection:
    label: str  # section letter (e.g. "A", "B", "C"), data-driven
    title: str; stale_threshold_days: int
    groups: tuple[FullHygieneWorkstreamGroup, ...]
    total_count: int; stale_count: int; ready_count: int

@dataclass(frozen=True, slots=True)
class FullHygieneArtifacts:
    sections: tuple[FullHygieneSection, ...]
    recipient: str; generated_at: datetime
    eml_paths: tuple[Path, ...]
    using_snapshot_fallback: bool; ai_titles_compressed: int
```

`generate_full_hygiene_nudges()` produces an N-section heat-map email whose sections are fully declared by per-edition `full_hygiene` config. Each section's `criteria.source` is one of `registry` (hydrate the workstream registry's `key_ado_items` via a direct batch fetch — zero WIQL calls), `tag` (one WIQL query per section over the program's `area_paths`), or `area_path` (one WIQL query over `area_path_filter`). Sections deduplicate earlier→later so an item appears once. WIQL results are hydrated in batches of `NUDGE_BATCH_SIZE` (200) via `src/core/nudge_query.py`. Each row carries the hygiene signals listed above plus a compressed title. Title compression optionally uses the AI layer (`FallbackStructuredClient`) with a JSON cache at `programs/{program_id}/nudge/title_cache.json`; falls back to word-boundary truncation at 50 chars. Before the EML is written, the resolved recipient set is filtered through `audience_policy` (allowed domains, opt-out set, unresolved-owner behavior, max-recipient cap, `to` vs `bcc` delivery mode). The generated EML is written to `programs/{program_id}/nudge/drafts/{run_id}.eml` and marked `X-Unsent: 1` (draft, never sent). Draft files are pruned to the 20 most recent (`NUDGE_DRAFT_RETAIN`). `vertex nudge --mark-sent <draft-ref> [--sent-at <iso>]` promotes a draft to `nudge/published_eml/` as a human-attested send record and starts cooldown at attested send time via the schema-1.2 state store. Per-section `stale_business_days` and the per-program `comment_fetch_limit` are edition-configured; legacy `--stale-a/b/c` CLI overrides are honored via a backwards-compat shim (`_parse_legacy_shim`) but are deprecated in favor of per-section config. **Exit codes:** 0 success/dry-run/no-items/audit/reset; 2 usage/config/auth/state/lock/EML failure; 3 degraded EML (a `query_error` or any comment-fetch errors). `vertex doctor --nudge` runs the legacy NQ checks plus the `NQD-*` governance checks in `src/commands/doctor_checks/nudge_checks.py`; `vertex fleet` surfaces a per-program nudge summary (`FleetNudgeSummary`).

### 2.9 Vitality Models

```python
@dataclass(frozen=True, slots=True)
class VitalityScore:
    work_item_id: int; owner_alias: str | None; workstream_id: str | None
    freshness_days: int; freshness_grade: Literal["green", "amber", "red"]
    richness_score: int; richness_missing: tuple[str, ...]
    leakage_events: int; composite_score: int
    suggested_update: str | None

@dataclass(frozen=True, slots=True)
class VitalityAggregate:
    scope_id: str; scope_type: Literal["owner", "workstream"]
    total_items: int; fresh_items: int; avg_richness: float
    total_leakage: int; composite_score: int
    trend: Literal["improving", "stable", "worsening"] | None
```

### 2.10a Context Maturity Models (`src/core/program_context.py`, `src/core/context_snapshot_store.py`, `src/core/context_gap_store.py`)

```python
class ContextMaturityLevel(int, Enum):
    L0_CRITICAL = 0    # Critical invariant errors — unusable
    L1_ERRORS   = 1    # Schema/structure errors — degraded
    L2_GAPS     = 2    # Critical context gaps — quality impacted
    L3_WARNINGS = 3    # Non-critical warnings — reduced accuracy
    L4_HEALTHY  = 4    # Zero errors and critical warnings

@dataclass(frozen=True, slots=True)
class StalenessFlag:
    file: str             # e.g. "program.yaml"
    field: str            # e.g. "reviewed_at"
    entity_id: str | None # workstream/milestone ID if applicable
    days_stale: int
    threshold: int
    severity: Literal["critical", "warning", "ok"]

@dataclass(frozen=True, slots=True)
class InvariantViolation:
    code: str             # e.g. "WS-01", "MS-02", "KB-03"
    severity: InvariantSeverity  # ERROR | WARNING
    detail: str

@dataclass(frozen=True, slots=True)
class ProgramContext:
    program_id: str
    maturity_level: ContextMaturityLevel
    invariant_violations: tuple[InvariantViolation, ...]
    staleness_flags: tuple[StalenessFlag, ...]  # only non-ok entries

@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    schema_version: str          # "1.0"
    program_id: str; edition: str; issue_number: int
    confirmed_at: datetime
    milestones: tuple[dict, ...]  # serialized Milestone snapshots
    risks: tuple[dict, ...]       # serialized RiskEntry snapshots
    workstreams: tuple[dict, ...]  # serialized Workstream snapshots
    decisions: tuple[dict, ...]   # serialized DecisionEntry snapshots
    plane1_change_count_since_prior: int  # Plane 1 changes since prior confirm
    context_maturity_level: int = 0  # ContextMaturityLevel.value

@dataclass(frozen=True, slots=True)
class ContextGapRecord:
    ts: datetime; feature: str; program: str
    lane: str | None; field: str
    severity: str; message: str
    impact_estimate: str | None = None
    # Ranked view:
    count: int = 1  # occurrence count after deduplication
```

**`load_program_context(program_id, programs_root, raise_on_error=True) -> ProgramContext`**

Compiled once per operation. Evaluates all 21 invariants across the 20 Plane 1 files and returns a frozen `ProgramContext`. Staleness flags are only appended for entries where `severity != "ok"`.

### 2.10 Claim Models

```python
@dataclass(frozen=True, slots=True)
class ClaimEntry:
    id: str; program_id: str; edition_id: str; issue_number: int
    workstream_id: str | None; text: str
    entity_refs: tuple[str, ...]; claim_date: date
    owner_alias: str | None; due_date: date | None
    status: Literal["open"] = "open"

@dataclass(frozen=True, slots=True)
class DecisionAsk:
    id: str; program_id: str; edition_id: str; issue_number: int
    text: str; entity_refs: tuple[str, ...]; ask_date: date
    owner_alias: str | None
    status: Literal["open", "resolved", "deferred"]
    resolution: str | None

@dataclass(frozen=True, slots=True)
class ClaimStatusUpdate:
    record_type: Literal["status_update"] = "status_update"
    claim_id: str; new_status: Literal["open", "met", "contradicted", "stale"]
    updated_at: datetime; updated_by: str; note: str | None
```

### 2.11 View Models (`src/core/view_models.py`)

Render-ready dataclasses consumed by Jinja2 templates:

| Type | Key Fields |
|------|-----------|
| `HealthSummary` | `overall_risk`, `high_count`, `medium_count`, `low_count`, `done_count`, `total_count`, `delta_direction`, `trajectory`, `bluf`, `risk_load`, `read_time_minutes` |
| `Top3Item` | `item_type` ("risk"\|"ask"\|"win"), `text`, `owner`, `ado_link`, `anchor`, `by_date` |
| `WorkstreamData` | `section_id`, `title`, `blurb`, `items`, `citations`, `risk`, `review_state`, `kpi_tiles` |
| `ScorecardData` | `scorecard_name`, `dimensions` |
| `EditionMeta` | `edition`, `issue_number`, `generated_at`, `ado_data_as_of`, `manifest_id`, `qg_status` |
| `AdoVitalitySectionData` | `items_updated`, `items_total`, `updated_percentage`, `freshness_average_days`, `leakage_events`, `best_documented_*`, `trend_*` |
| `ContinuityRenderData` | `brand_header_url`, `brand_name`, `cadence_note`, `scorecard_bands[]`, `chapters[]`, `jump_links[]`, `edition_intro` |

---

## §3 ADO Client

> **UIL note (2026-06-02):** The primary ADO ingest path is `ADODiscoveryProvider` (`src/core/ado_discovery.py`) + `ADOHydrationProvider` (`src/core/ado_hydration.py`) by default. The OData broad-sweep path (`_build_odata_filter`, `_load_program_items_from_ado`, `_load_freshness_program_items`) has been removed from `gather.py` (Phase 5 complete). `query_builder.py`'s `build_odata_filter` is retained and still used by `vertex freshness` and `vertex reconcile`. `ADOClient` is used directly by both UIL providers and legacy commands. `ADODiscoveryProvider._discover_scope_group()` deduplicates shared WIQL queries so each unique `(query_id, clause)` pair executes only once per gather cycle.

### 3.1 Authentication (`src/core/ado_client.py`)

**Auth waterfall** (first success wins):
1. PAT from environment variable (`ADO_PAT` by default)
2. `DefaultAzureCredential` (if `azure-identity` installed)
3. `AzureCliCredential` (if `azure-identity` installed)
4. Raise `AuthError`

The `auth_method` attribute records which method succeeded.

### 3.2 Constructor

```python
class ADOClient:
    def __init__(
        self,
        organization: str,
        project: str,
        timeout: int = 30,
        pat_env: str = "ADO_PAT",
        show_progress: bool = True,
        slow_warning_seconds: int = 15,
        progress_poll_seconds: float = 0.2,
    ) -> None
```

Edition-resolved commands may override the default ADO timeout via `edition.ado_fetch_timeout_seconds`; `ado_client.py` honors the resolved value instead of silently falling back to the program default.

### 3.3 OData Query (`src/core/query_builder.py`)

```python
def build_odata_filter(
    area_paths: tuple[str, ...],
    work_item_types: tuple[str, ...] = ("Feature", "Scenario", "Deliverable", "Task"),
    since: datetime | None = None,
    states_excluded: tuple[str, ...] = ("Removed",),
) -> str
```

- Parenthesizes boolean groups, ISO-8601-Z dates, single-quote string literals with `'` escaped as `''`
- Cap `$top` at 1000; paginate via `$skiptoken`
- OData requires `$expand=Area` for area path filtering

### 3.4 ADO REST Endpoints

| Endpoint | Use |
|----------|-----|
| `GET /_apis/wit/workItems/{id}/revisions?api-version=7.1` | Field-level revision history |
| `GET /_apis/wit/workItems/{id}/comments?api-version=7.1-preview.4` | Discussion comments |
| `POST /_apis/wit/workItemsBatch?api-version=7.1` (≤200 IDs) | Batch field expansion |

REST enrichment batches revisions by chunks of 50 items.

### 3.5 Retry & Circuit Breaker

- **Retryable:** 429, 5xx, `ConnectionError` → exponential backoff `0.5s · 2^n + jitter(0..0.3)`, max 5 attempts
- **Retry-After header:** Honored when present
- **Per-call timeout:** Configurable (default 30s)
- **Circuit breaker** (`src/core/circuit_breaker.py`): file-backed state (`CLOSED` → `OPEN` → `HALF_OPEN`). Opens after `failure_threshold` consecutive failures; recovers after `recovery_timeout`.

### 3.6 Kusto Client (`src/core/kusto_client.py`)

```python
class KustoClient:
    def __init__(self, credential=None, sleep_func=time.sleep) -> None
    def execute(self, cluster, database, kql, timeout=120, max_retries=3) -> list[dict]
```

Auth via `DefaultAzureCredential`. Caches client instances per cluster URI. Timeout as `timedelta`. Dependencies: `requirements.txt`.

Safety defaults: `kusto_client.py` injects `max_memory_consumption_per_query_per_node` (8 GB) and `request_timeout` (5 min) by default. Per-query opt-out is allowed via `kusto_no_safety: true` on `KustoQuery`.

---

## §4 Core Engines

### 4.1 Delta Engine (`src/core/delta_engine.py`)

```python
TERMINAL_STATES = {"closed", "done", "resolved", "completed", "removed", "cut"}

def build_deltas(
    current_items: tuple[WorkItem, ...] | list[WorkItem],
    previous_snapshot: Snapshot | None,
    issue_number: int,
    previous_issue_number: int | None,
    evidence_by_item: dict[int, EvidencePacket] | None = None,
    terminal_states: tuple[str, ...] = tuple(sorted(TERMINAL_STATES)),
) -> DeltaSet
```

**DeltaKind computation:**

| Kind | Trigger |
|------|---------|
| `NEW` | Item ID not in previous snapshot |
| `CLOSED` | Previous state non-terminal, current state terminal (case-insensitive) |
| `RISK_UP` | `_risk_delta_kind()`: ordinal comparison where `LOW=0 < MEDIUM=1 < HIGH=2`; `DONE` → always `RISK_DOWN`; `UNKNOWN` → `None` |
| `RISK_DOWN` | Inverse of RISK_UP |
| `ETA_CHANGED` | Both previous and current `target_date` non-None and differ |
| `OWNER_CHANGED` | Previous `assigned_to` not matching current assigned_to or assigned_to_email |

A single item can emit multiple deltas. `unchanged_count` increments only when zero changes detected.

### 4.2 Evidence Engine (`src/core/evidence_engine.py`)

```python
def build_evidence(
    item: WorkItem,
    window_start: datetime, window_end: datetime,
    enrichments_by_item: Mapping[int, tuple[Enrichment, ...]] | None = None,
) -> EvidencePacket
```

**Confidence logic:**

| Condition | Confidence | Tier |
|-----------|-----------|------|
| Revisions AND comments in window | `HIGH` | `TIER1` |
| Revisions OR comments (one present) | `MEDIUM` | `TIER1` |
| Only enrichments | `LOW` | `TIER3` |
| Nothing in window | `NONE` | `TIER3` |

### 4.3 Scorecard Engine (`src/core/scorecard_engine.py`)

```python
_RISK_PRIORITY = {HIGH: 4, MEDIUM: 3, LOW: 2, DONE: 1, UNKNOWN: 0}

def build_scorecard(
    items: tuple[WorkItem, ...] | list[WorkItem],
    dimensions: tuple[ScorecardDimensionSettings, ...],
    prev_confirmed: Snapshot | None,
    scorecard_name: str | None = None,
    slice_contracts: dict[tuple[str, str], SliceContract] | None = None,
    stale_warn_days: int = 14,
) -> list[ScorecardEvidencePacket]
```

**Dimension assignment:** Filter items by `ado_filter` DSL, then compute aggregates.

**ado_filter DSL grammar:**
```
filter     := predicate ( ("AND" | "OR") predicate )*
predicate  := field_name operator value
field_name := "area_path" | "tag" | "type" | "state" | "assigned_to" | "risk_level"
operator   := "contains" | "eq" | "ne"
value      := "'" [^']* "'"
```

**Derived risk:** `max(item.risk_level for matched_items)` using `_RISK_PRIORITY` ordering (HIGH wins). Override application happens in `commands/report.py`, not in the engine.

**Item classifications:**
- Stale: latest revision/comment older than `stale_warn_days` from `fetched_at`
- Overdue: `target_date < fetched_at.date()` and non-terminal state
- Blocked: "blocked" in tags, or `custom_fields["blocked"] == True`, or "blocked" in state
- High activity: ≥3 revisions + comments in last 7 days

### 4.4 Freshness Engine (`src/core/freshness_engine.py`)

```python
TERMINAL_STATES = {"closed", "done", "resolved", "completed", "removed", "cut"}
ACTIVE_STATES = {"active", "proposed", "on track", "at risk", "off track", "blocked"}
PLACEHOLDER_MARKERS = ("tbd", "wip", "updating", "to be determined", "no update")

def build_freshness_report(
    current_items: tuple[WorkItem, ...] | list[WorkItem],
    issue_number: int, as_of: datetime,
    stale_warn_days: int, stale_block_days: int,
    previous_snapshot: Snapshot | None = None,
    previous_notification_state: PriorNotificationState | None = None,
    program_context: NarrativeProgramContext | None = None,
    workstream_narrative_history: Mapping[str, tuple[str, ...]] | None = None,
) -> FreshnessReport
```

**Key rules (28 total, FR-20 through FR-47):**

| Rule | Severity | Trigger |
|------|----------|---------|
| FR-46 | `block` | Active item with no assigned owner |
| FR-21 | `block` | `target_date` in the past (overdue) |
| FR-43 | `block` | Target date ≤5 business days away |
| FR-22 | `warn` | No activity in ≥`stale_warn_days` days |
| FR-23 | `warn` | Risk level changed since last confirmed issue |
| FR-24 | `warn` | State changed since last confirmed issue |
| FR-25 | `info` | New item appeared in scope |
| FR-26 | `warn` | ≥3 changes in last 7 days (hot item) |
| FR-26a | `warn` | Ghost change — ADO changed but blurb unchanged across 3 issues |
| FR-42 | `warn` | Placeholder language in recent update |
| FR-42a | `warn` | Description >90% similar to previous confirmed issue |
| FR-44 | `warn` | At-risk/off-track without actionable next steps |
| FR-45 | `warn` | Non-responder after notify |
| FR-47 | `warn` | Escalation — suggests alternate owner |

Sort key: `(severity_rank[block=0, warn=1, info=2], work_item_id, rule_id)`.

### 4.5 Forecast Engine (`src/core/forecast_engine.py`)

```python
_MIN_HISTORY_ENTRIES = 4
_DEFAULT_SLIP_THRESHOLD_DAYS = 5

def forecast_etas(
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    drift_patterns: tuple[DriftPattern, ...],
    *, window_days: int = 90, as_of: date | None = None,
) -> dict[int, ETAForecast]
```

**Slip probability algorithm:**
1. Count `prior_slips` via `count_eta_slips()` (consecutive forward-only target_date changes)
2. Base probability by days-to-target: ≤0→0.9, ≤7→0.5, ≤14→0.35, ≤30→0.25, else→0.15
3. Pattern penalty: stale→+0.1, chronic_reassign/state_oscillation→+0.05 each (capped 0.2)
4. Trajectory probability by slip count:
   - 0 slips → `(base + penalty, HIGH)`
   - 1 slip → `(base + 0.2 + penalty, MEDIUM)`
   - 2 slips → `(base + 0.4 + penalty, LOW)`
   - 3+ slips → `(max(0.8, base + penalty), LOW)`

### 4.6 Attribution Engine (`src/core/attribution_engine.py`)

Three-tier citation system:

| Tier | Surface | Limit |
|------|---------|-------|
| Tier 1 (inline) | Table cells: `[#WI]` link | Max 5 per cell; overflow → "+N more" |
| Tier 2 (section) | Narrative trailer: `Sources: #a #b #c` | Unbounded |
| Tier 3 (reviewer) | Full evidence packet | Never published |

---

## §5 Signal Journal & Trajectory Store

> **UIL note:** ADO signal production now routes through `ADOSignalExtractor` (`src/core/ado_signal_extractor.py`), a pure Zone A transform over `ADOHydrationOutput`. It emits both `ado:{id}` and legacy `WI:{id}` entity refs for downstream compatibility. Teams signals use `TeamsSignalExtractor`; IcM signals use `IcMSignalExtractor`.

### 5.1 Signal Journal (`src/core/journal.py`)

**Partitioning:** Weekly JSONL at `programs/<prog>/journal/<YYYY>-W<WW>.jsonl`.

**Write safety:** `portalocker.LOCK_EX` (exclusive file lock) + `os.fsync()` for durability.

**Key functions:**
```python
def append_signal(signal: Signal, programs_root=PROGRAMS_ROOT, *, partition_at=None) -> Path
def append_review_decision(program_id, decision: SignalReviewDecision, ...) -> Path
def append_usage_marker(program_id, marker: SignalUsageMarker, ...) -> Path
def append_signal_thread_link(program_id, link: SignalThreadLink, ...) -> Path
def read_signals(program_id, *, start, end, workstream_id=None, ...) -> tuple[Signal, ...]
def load_latest_review_decisions(program_id, ...) -> dict[str, SignalReviewDecision]
```

**Review sidecar:** `reviews.jsonl` — append-only. Entries carry `record_type` discriminator (`"review"` or `"usage_marker"`). Last-write-wins for same `signal_id` and `record_type`.

**Journal archival:** `archive_weekly_journal_files(program_id, before_week)` moves old files to `journal_archive/`. Reads scan both active + archived directories.

### 5.2 Signal Dedup (`src/core/signal_dedup.py`)

Source-specific fingerprinting:

| Source | Fingerprint Components |
|--------|----------------------|
| `ado/odata` | source, raw_ref, entity_refs, field, prior, current |
| `ado/revision` | source, raw_ref, entity_refs, revision_number |
| `kusto` | source, query_id, entity_refs, event_timestamp |
| `workiq/*` | source, message_id, entity_refs |
| `icm` | source, incident_id, entity_refs |
| `manual` | source, sha256(normalized_text), entity_refs |
| `vertex/ado_update` | source, proposal_id, work_item_id, update_type |
| `vertex/freshness` | source, program_id, work_item_id, finding_type, date |
| fallback | source, raw_ref, entity_refs, stable_json(metadata), text |

Format: `"|".join(normalized_parts)` — parts lowercased, tuples comma-joined, `None` → empty.

### 5.3 Trajectory Store (`src/core/trajectory.py`)

**Storage:** Per-item JSONL at `programs/<prog>/trajectories/<work_item_id>.jsonl`.

```python
def append_trajectory_point(program_id, work_item_id, point: TrajectoryPoint, ...) -> bool
def backfill_trajectory_points(program_id, work_item_id, points, ...) -> int
def read_trajectory(program_id, work_item_id, *, start=None, end=None, ...) -> tuple[TrajectoryPoint, ...]
```

**Change detection:** `_has_material_change()` compares `state, assigned_to, target_date, risk_level, area_path, tags`. Returns `False` (skip write) if all identical.

**Backfill dedup:** Builds set of existing `(date, state, assigned_to, target_date, risk_level, area_path, tags)` tuples; only novel points appended.

---

## §6 Analysis Engines

### 6.1 Trajectory Analyzer (`src/core/trajectory_analyzer.py`)

```python
def analyze_trajectories(
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    *, window_days: int = 90, as_of: date | None = None,
) -> tuple[DriftPattern, ...]
```

| Pattern | Detection Rule | Severity |
|---------|---------------|----------|
| `eta_drift` | ≥2 target_date changes, all forward slips in window | `high` if ≥3, else `medium` |
| `chronic_reassign` | ≥3 assigned_to changes in window | `medium` |
| `state_oscillation` | ≥2 Active↔Resolved toggles in window | `medium` |
| `stale` | Zero trajectory points in window AND state is Active | `low` |

### 6.2 Altitude Guard (`src/core/altitude_guard.py`)

```python
def apply_altitude_guard(
    altitude: str,
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    escalation_item_id: int | None = None,
) -> AltitudeGuardResult
```

| Altitude | Filtering |
|----------|-----------|
| `satellite` | Only severity=high drift patterns, risk escalations, exec summary |
| `helicopter` | All approved signals; low-confidence shown with caveats |
| `street` | All signals including low-confidence |
| `escalation` | Only signals related to the escalation item |

### 6.3 Cascade Detector (`src/core/cascade_detector.py`)

```python
def detect_dependency_cascades(
    dependencies: tuple[Dependency, ...],
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    items: ..., scorecards: ..., workstreams: ...,
) -> tuple[DependencyCascade, ...]
```

When a signal or drift pattern fires on a `from_item` in program `key_dependencies`, surfaces downstream impact in the affected workstream. **Single-hop only** — no transitive closure.

### 6.4 Coverage Gap Detection (`src/core/coverage_gap.py`)

```python
def build_coverage_gaps(
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    narratives: dict[str, str],
    as_of: datetime, min_age_days: int = 7,
) -> tuple[CoverageGap, ...]
```

Active items (state ∉ excluded, age > `min_age_days`, not in dormant workstreams) with no approved signals AND no narrative mention.

### 6.5 Velocity Metrics (`src/core/velocity_metrics.py`)

```python
def build_velocity_metrics(
    trajectories_by_item: dict[int, tuple[TrajectoryPoint, ...]],
    as_of: date, window_days: int = 90,
) -> VelocityMetrics | None
```

Computes Active→Resolved throughput and cycle-time (median, P90) from trajectory state transitions. Used as a deterministic fallback when Kusto is disabled.

### 6.6 Scorecard Trends (`src/core/scorecard_trends.py`)

```python
def load_scorecard_trends(
    edition: str, current_dimensions: ...,
    archive_root: Path, history_window: int = 4,
) -> dict[tuple[str, str], ScorecardTrend]
```

Loads last N confirmed scorecards from archive. Per-dimension: current risk, prior sequence, direction (improving/stable/worsening), consecutive-high count.

### 6.7 Anticipation Detector (`src/core/anticipation_detector.py`)

```python
def detect_anticipated_questions(
    readers: tuple[LeadershipReader, ...],
    workstreams: ..., drift_patterns: ...,
    approved_signals: ..., summaries: ..., dependencies: ...,
) -> tuple[AnticipationFinding, ...]
```

Deterministic Layer 1: matches drift patterns against reader `cares_about` topics. Yields `AnticipationFinding` records for AI Layer 2 question generation.

---

## §7 Vitality System

### 7.1 Vitality Scorer (`src/core/vitality_scorer.py`)

```python
def score_vitality(
    items: tuple[WorkItem, ...], as_of: datetime,
    workstream_resolver: ..., leakage: LeakageReport | None = None,
    exempt_aliases: tuple[str, ...] = (),
    sparse_workiq_threshold: int = 5,
) -> tuple[VitalityScore, ...]
```

**Per-item scoring:**

1. **Freshness** — days since last meaningful ADO update:
   - `<7 days` → green (100%)
   - `≤14 days` → amber (60%)
   - `>14 days` → red (20%)

2. **Richness rubric** (0–100):

   | Check | Points | Rule |
   |-------|--------|------|
   | Target date present | 25 | Set and non-null |
   | Recent owner comment | 25 | By `AssignedTo` within 14 days |
   | Risk assessment | 15 | Override or custom field or risk tags |
   | Description quality | 15 | Non-empty, >50 chars, differs from title |
   | Blocker clarity | 10 | Blocked items mention blocker + owner |
   | Next step | 10 | Action verb + owner/date token in latest comment |

3. **Composite score:**
   - Pre-WorkIQ: `round(freshness_pct × 0.6 + richness_score × 0.4)`
   - Full: `round(freshness_pct × 0.4 + richness_score × 0.3 + leakage_pct × 0.3)`
   - Sparse fallback: if `total_workiq_signals < sparse_workiq_threshold` → pre-WorkIQ formula

**Exclusions:** Terminal states (Resolved/Closed/Removed/Done/Completed/Cut). Initial states (New/Proposed) with age <7 days. Aliases marked `exempt_from_vitality`.

### 7.2 Leakage Detector (`src/core/leakage_detector.py`)

```python
def detect_leakage(
    items: tuple[WorkItem, ...],
    signals: tuple[Signal, ...],
    *, trajectory_loader: Callable[[int], tuple[TrajectoryPoint, ...]],
) -> LeakageReport
```

1. Filter to high-confidence entity-linked WorkIQ signals only
2. For each `WI:<id>` reference: check for post-signal ADO update in trajectory
3. No post-signal ADO update → leakage event (information flowed via email/Teams but not ADO)
4. Per-owner ratio: `leaks / total_signals_for_owner`

### 7.3 ADO Semantics (`src/core/ado_semantics.py`)

Shared helpers for meaningful-update classification:
- `is_vertex_generated_comment(comment)` — detects `📊 Vertex` header
- `is_meaningful_owner_comment(comment, owner_alias)` — non-Vertex comment by assigned owner
- `is_meaningful_vitality_change(field_name)` — state/target/assignment/risk/discussion changes
- `latest_meaningful_ado_update(item)` → datetime of last meaningful change

---

## §8 Rendering System

### 8.1 Template Hierarchy

Jinja2 `FileSystemLoader` search path (in order):
1. `templates/` (global)

**Base templates (4):**
- `base.email.j2` — 680px outer / 640px content table, Segoe UI font stack, inline CSS
- `base.deck.j2` — Markdown deck format
- `base.reviewer.j2` — Two-pane reviewer HTML (CSS vars, light theme)
- `base.teams.j2` — Teams/Adaptive Card Markdown

**Archetype templates (7):**
- `archetypes/detailed.j2` — full weekly newsletter
- `archetypes/continuity.j2` — band scorecard + chapter layout
- `archetypes/condensed.j2` — daily digest
- `archetypes/narrative.j2` — narrative-driven
- `archetypes/deck.j2` — LT deck Markdown
- `archetypes/lookback.j2` — quarterly retrospective
- `archetypes/digest.j2` — digest variant

**Partial templates (24):** `nav_bar.j2`, `health_banner.j2`, `top_3_now.j2`, `what_changed.j2`, `scorecard.j2`, `exec_summary.j2`, `workstream.j2`, `risk_chip.j2`, `delta_badge.j2`, `verify_chip.j2`, `provenance_footer.j2`, `ado_vitality.j2`, `vitality_reviewer.j2`, `reviewer_anticipated_questions.j2`, `brand_header.j2`, `cadence_note.j2`, `continuity_chapter.j2`, `continuity_exec_summary.j2`, `continuity_provenance_comment.j2`, `continuity_scorecard_band.j2`, `edition_intro.j2`, `jump_to_section.j2`, `kusto_section.j2`, `orientation_footer.j2`

### 8.2 Archetype Selection

```python
# In html_renderer.py _select_template():
LOOKBACK                    → archetypes/lookback.j2
DETAILED/FOCUSED+continuity → archetypes/continuity.j2
NARRATIVE                   → archetypes/narrative.j2
CONDENSED                   → archetypes/condensed.j2
DETAILED/FOCUSED (default)  → archetypes/detailed.j2
```

For `DECK` edition type, `DeckRenderer` is used instead of `HTMLRenderer`.

**Note:** `FOCUSED` routes to the same templates as `DETAILED` — the difference is in the ordering and visibility rules, not the archetype. Explicit section filters still win, but when no explicit filter is present the focused path may keep authored sections visible if registry preview relevance plus authored narrative show that the section still carries live reporting context. `digest.j2` is a legacy alias for `condensed.j2` and is targeted for removal after 2026-12-31 once legacy references are cleaned up.

### 8.3 RenderContext (`src/core/html_renderer.py`)

The master render payload (frozen dataclass, ~35 fields):

| Field | Type |
|-------|------|
| `title`, `subtitle`, `preheader` | `str` |
| `report` | `ReportData` |
| `edition_meta` | `EditionMeta` |
| `layout_mode` | `str` (default `"dashboard"`) |
| `health` | `HealthSummary \| None` |
| `top_items` | `tuple[Top3Item, ...]` |
| `scorecards` | `tuple[ScorecardData, ...]` |
| `kusto_sections` | `tuple[KustoSectionData, ...]` |
| `ado_vitality` | `AdoVitalitySectionData \| None` |
| `workstreams` | `tuple[WorkstreamData, ...]` |
| `scorecard_packets` | `dict[str, dict[str, ScorecardEvidencePacket]]` |
| `eta_forecasts` | `dict[int, ETAForecast]` |
| `continuity` | `ContinuityRenderData \| None` |
| `template_contract` | `TemplateFamilyContract \| None` |
| `is_dry_run` | `bool` |
| `header_label` | `str \| None` |
| `footer_label` | `str \| None` |
| `auto_suggestions` | `tuple[Top3Item, ...]` |
| `forwarding_context` | `str \| None` |
| `decision_strip_ack_required` | `bool` |
| `exec_summary_citations` | `tuple[Citation, ...]` |
| `manifest` | `RunManifest \| None` |
| `sections` | `tuple[SectionLink, ...]` |
| `prior_date_label` | `str \| None` |
| `changes_url` | `str \| None` |
| `item_urls` | `dict[int, str]` |
| `scorecard_deltas` | `dict[str, dict[str, ScorecardDelta]]` |
| `scorecard_urls` | `dict[str, str]` |
| `workstream_urls` | `dict[str, str]` |
| `workspace_root` | `str \| None` |
| `mobile_safe_scorecards` | `str \| None` |
| `show_footer` | `bool` |

### 8.3a DraftState (`src/commands/report.py`)

The internal draft state model used for `--diff` mode comparison. Serialized to `issue_NNN.draft.json`.

```python
@dataclass(frozen=True, slots=True)
class DraftState:
    issue_number: int
    generated_at: datetime
    ado_data_as_of: datetime
    edition_type: EditionType
    items: tuple[WorkItem, ...]
    workstream_blurbs: dict[str, str]
    kusto_sections: tuple[KustoSectionData, ...]
    override_snapshot: dict[str, dict[str, dict[str, Any]]]
    top_3_now: tuple[str, ...]
    exec_summary_text: str
```

### 8.4 Section Dispatch

`_build_ordered_sections` maps section IDs to `RenderSection` objects with `kind` values:
`"health"`, `"provenance"`, `"top_3"`, `"exec_summary"`, `"selected_changes"`, `"scorecard"`, `"kusto"`, `"ado_vitality"`, `"workstream"`

Section IDs use namespacing: `scorecard:<anchor>`, `kusto:<id>`, `workstream:<id>`.

If a `template_contract` is present, its `.order` list drives ordering (with `scorecards:all` / `kusto:all` expansion). Otherwise `_default_section_order` is used.

### 8.5 Outlook HTML Constraints

- Inline `style=""` on every element — no `<style>` blocks
- `<table>` layout with `cellpadding="0" cellspacing="0"` — no `<div>` layout containers; the hidden preheader `<div>` is the one allowed email-client exception
- Max 680px outer, 640px content area
- Font: `Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif`
- Hex colors only — no CSS variables, `rgb()`, or `hsl()`
- Images: CID or base64 ≤100KB; prefer Unicode emoji
- No JavaScript, no media queries

### 8.6 Color System (Single Source: `jinja_filters.py`)

```python
RISK_COLORS = {
    "high":    {"bg": "#E97132", "fg": "#FFFFFF", "icon": "🔴", "border": "#FFFFFF"},
    "medium":  {"bg": "#FFE699", "fg": "#000000", "icon": "🟡", "border": "#BF8F00"},
    "low":     {"bg": "#B4E5A2", "fg": "#000000", "icon": "🟢", "border": "#4EA72E"},
    "done":    {"bg": "#4EA72E", "fg": "#FFFFFF", "icon": "✅", "border": "#FFFFFF"},
    "unknown": {"bg": "#C00000", "fg": "#FFFFFF", "icon": "⚪", "border": "#FFFFFF"},  # Also displayed as "Blocked"
}

DELTA_COLORS = {
    "risk_up": "#991B1B", "risk_down": "#065F46", "new": "#1E40AF",
    "closed": "#4B5563", "eta_changed": "#92400E", "owner_changed": "#4B5563",
    "unchanged": "#9CA3AF",
}
```

### 8.7 EML Writer (`src/core/eml_writer.py`)

```python
def build_eml_bytes(
    to, cc, subject, html_body, text_body,
    from_display_name, from_email, generated_at, mark_as_draft=True,
) -> bytes

def write_eml(path, eml_bytes) -> Path
```

Produces RFC 2822 `.eml` file with `X-Unsent: 1` for draft marking.

---

### 8.3 Chart Pipeline

The chart pipeline extends Kusto sections (`render_as: chart_image`) with a gather-time cache, renderer registry, placement routing, and quality gates. PNG is the only chart artifact format for confirmed publish.

#### Registry

```python
class ChartRendererRegistry:
    def register(self, renderer_id: str, builder: ChartBuilder) -> None  # renderer_id must contain "::"
    def get_renderer(self, renderer_id: str) -> ChartBuilder | None
    def merge(self, other: ChartRendererRegistry) -> None

def build_default_registry() -> ChartRendererRegistry  # auto-discovers CHART_RENDERERS in src/core/charts/*
```

`src/core/chart_renderer_registry.py` is the single runtime registry. `build_default_registry()` walks `src/core/charts/`, imports modules exposing `CHART_RENDERERS`, and registers namespaced IDs such as `vertex::declarative` and `acme::deployment_velocity`. There is no second registry in `src/core/charts/__init__.py`.

#### Theme Context

```python
@dataclass(frozen=True, slots=True)
class ChartThemeContext:
    primary_color: str = "#2563EB"
    success_color: str = "#16A34A"
    warning_color: str = "#D97706"
    danger_color: str = "#DC2626"
    muted_color: str = "#6B7280"
    grid_color: str = "#F3F4F6"
    font_family: str = "Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif"
    font_size_pt: int = 9

@dataclass(frozen=True, slots=True)
class ThemeContext:
    content_width_px: int = 608
    chart: ChartThemeContext = field(default_factory=ChartThemeContext)
```

`build_theme_context()` is a standalone factory that returns a `ThemeContext` from active edition config.

#### Cache Store (`src/core/chart_cache_store.py`)

Zone A. PII pre-scrub contract enforced at write time (`pii_prescrubbed=True` required). 5 MiB per-entry cap. Eviction formula: `max(ttl * 3, 168)` hours.

```python
def write_chart_cache(edition_root, query_id, rows, *, pii_prescrubbed: bool, captured_at, ttl_hours) -> None
def load_chart_cache(edition_root, query_id) -> ChartCacheEntry | None
def chart_cache_age_hours(edition_root, query_id) -> float | None
def evict_stale_caches(edition_root, *, max_age_hours: int = 168) -> int
```

#### Rendering Decision (kusto_rendering.py)

For a Kusto query with `chart_config` set:
1. Try live render via registry → write result back to cache.
2. If live fails → fall back to cache.
3. If cache miss → fall back to table.
4. Zero-row policy: `fallback_on_empty_rows=True` → table; `False` → placeholder PNG card (608×200px, `#F3F4F6` background).

Internal `render_mode` values: `"chart"` for registry path, `"chart_image"` for legacy path. YAML authoring always uses `render_as: chart_image`.

#### Placement Routing (html_renderer.py)

`_partition_render_kusto_sections()` dispatches sections by `section_placement`:
- `"workstream:<ws_id>"` → `WorkstreamData.attached_charts`
- `"exec_summary"` → `RenderContext.exec_summary_chart` (at most one)
- `"standalone"` → normal `kusto_sections` list


## §9 Storage Layer

### 9.1 Snapshot Store (`src/core/snapshot_store.py`)

```python
ARCHIVE_ROOT = REPO_ROOT / "programs"  # Archive lives under programs/<prog>/archive/<edition>/
LOCK_MAX_AGE = 1800  # 30 minutes

def write_confirmed(
    edition: str, issue_number: int, snapshot: Snapshot,
    archive_root: Path = ARCHIVE_ROOT, promote: bool = True,
    acquire_lock: bool = True,
) -> Path
```

**Atomic write pipeline:**
1. Check for orphaned staging → raise `StateError` if found
2. `ArchiveLock` context manager — PID + timestamp lockfile; refuses if lock held <30 min
3. Write to `staging/snapshots/issue_NNN.snapshot.json` via `_write_atomic_json`
4. If `promote=True`: `os.replace()` staging → final, then `shutil.rmtree(staging_root)`
5. Exception → `shutil.rmtree(staging_root)` cleanup

**File naming:** `issue_{issue_number:03d}.snapshot.json` (zero-padded 3-digit).

### 9.2 Archive Store (`src/core/archive_store.py`)

```python
def read_archive_index(edition, archive_root) -> ArchiveIndex
def find_latest_confirmed_entry(index, before_issue_number=None) -> ArchiveEntry | None
def read_scorecard_history(edition, archive_root) -> tuple[dict, ...]
```

**Archive paths** (`ConfirmedIssueArchivePaths`):
```
programs/<prog>/archive/<edition>/
  index.json, scorecards.json, vitality.json
  snapshots/issue_NNN.snapshot.json
  html/issue_NNN.html
  md/issue_NNN.md
  eml/issue_NNN.eml
  manifests/issue_NNN.json
  overrides/issue_NNN.yaml
  review/issue_NNN.review.yaml
  narratives/issue_NNN/
```

### 9.3 Override Store (`src/core/overrides_store.py`)

```python
NEEDS_INPUT_VALUE = "❓ Needs input"
```

**OverridesDocument** fields: `issue_number`, `top_3_now`, `scorecards`, `focused_include`, `edition_intro`, `chapter_subtitles`, `chapter_owner_overrides`, `forwarding_context`, `health_bluf`, `leadership_ask`, `show_orientation`, `decision_strip_ack`, `removed_dimensions`

**Merge semantics:** For each expected dimension: existing → preserve; new → `DimensionOverride(risk=None)`; missing from expected → `removed_dimensions`. Never silently drops author data.

### 9.4 Narrative Store (`src/core/narrative_store.py`)

Files at `programs/<prog>/narratives/issue_NNN/<section_id>.md`, with section proposal sidecars at `programs/<prog>/narratives/issue_NNN/proposals.jsonl` and confirm-time accepted sidecars archived as `archive/<edition>/narratives/issue_NNN/proposals_accepted.jsonl`.

- `load_narratives(edition, issue_number, ...) -> dict[str, str]`
- `strip_scaffold_comments(text) -> str` — removes `<!-- ... -->` scaffold markers
- `REMOVED_SECTION_MARKER` — sentinel for suppressed sections
- `write_narrative_section(edition, issue_number, section_id, text, ...) -> Path` — atomic narrative replacement helper used by `apply-proposals`

### 9.4a Section Proposal Store (`src/core/section_proposal_store.py`)

- `load_proposals(program_id, issue_number, ...) -> tuple[SectionRevisionProposal, ...]`
- `append_proposal(proposal, program_id, issue_number, ...) -> Path`
- `update_proposal_status(proposal_id, new_status, accepted_text=None, rejection_reason=None, ...) -> SectionRevisionProposal`
- `supersede_pending_proposals(program_id, issue_number, ...) -> tuple[SectionRevisionProposal, ...]`
- `write_accepted_proposals_archive(proposals, archive_dir) -> Path`

Core proposal models live in `src/core/models_v2.py`: `SectionEvidenceBrief`, `SectionRevisionProposal`, `SectionRevisionStatus`. Nudge artifacts (`FullHygieneRow`, `FullHygieneSection`, `FullHygieneWorkstreamGroup`, `FullHygieneArtifacts`) and the config/state models (`NudgeConfig`, `NudgeSectionSpec`, `NudgeSectionCriteria`) live in `src/core/nudge_models.py`; the query layer (`build_nudge_wiql`, `fetch_section_candidates`, `NudgeADOClient`) lives in `src/core/nudge_query.py`; and state persistence (`load_nudge_state`/`record_nudge_state`/`update_nudge_state`/`reset_nudge_item_state`) lives in `src/core/nudge_state_store.py`.

### 9.5 Review Status Store (`src/core/review_status_store.py`)

YAML at `publications/<edition>/review_status.yaml`.

States: `pending` → `sent` → `approved` | `changes_requested` | `rejected` | `skipped_no_delta`. Archived on confirm.

### 9.6 Claim Tracker (`src/core/claim_tracker.py`)

```python
MAX_CLAIMS_PER_CONFIRM = 20
_DUE_DATE_DEDUP_WINDOW_DAYS = 7
_SIMILARITY_THRESHOLD = 0.82
```

**Claim hints:** `"expected by"`, `"expect by"`, `"targeting"`, `"on track for"`, `"will deliver by"`, `"scheduled for"`, `"follow up by"`, `"commit to"`

**Ask hints:** `"need decision"`, `"needs decision"`, `"decision required"`, `"need lt decision"`, `"ask:"`, `"request decision"`

**Extraction:** `confirm.py` orchestrates an AI-first extraction path when `ai.enabled` and claim-extractor mode permit it, passing a validated `ClaimExtractionResult` DTO into `claim_tracker.py` for deduplication and persistence. Regex extraction remains the deterministic fallback for disabled AI, transient AOAI failure, and explicit `--legacy-regex-extractor` runs. Decision asks are prioritized. Truncate to `MAX_CLAIMS_PER_CONFIRM`. Dedup: same `entity_refs` + due dates within 7 days + text similarity ≥0.82.

**ID generation:** Deterministic via `uuid5(NAMESPACE_URL, "<kind>|<fields>")`.

**Storage:** Append-only JSONL at `programs/<prog>/journal/claims.jsonl` with `portalocker.LOCK_EX`.

### 9.7 Summary Store (`src/core/summary_store.py`)

Rolling per-workstream Markdown at `programs/<prog>/summaries/ws_<id>.md`.

```python
def load_summary(program_id, workstream_id, ...) -> RollingSummary | None
def save_summary(program_id, summary: RollingSummary, ...) -> None
```

`RollingSummary` embeds JSON metadata block in Markdown header.

### 9.8 Workstream Registry (`src/core/workstream_registry.py`)

Optional registry at `programs/<prog>/workstream_registry.yaml`.

Responsibilities:

- load authored workstream lifecycle/background/stakeholder memory when present
- derive fallback entries from slice contracts when absent
- build `WorkstreamIssueSnapshot` from current scorecard packet membership, slice contracts, and authored narratives
- render draft-time Markdown and JSON snapshots for operator review

Draft artifacts:

- `publications/<edition>/issue_NNN.workstream_snapshot.json`
- `publications/<edition>/issue_NNN.workstream_snapshot.md`
- `publications/<edition>/issue_NNN.workstream_associations.json`

The draft-time association artifact is inspectable in `--dry-run` and includes provenance such as `curated_slice`, `slice_membership`, `query_derived`, `area_path_derived`, and `narrative_reference`.

### 9.9 Workstream Association Store (`src/core/workstream_association_store.py`)

Durable JSONL store at `programs/<prog>/journal/workstream_associations.jsonl`.

```python
def append_workstream_association_records(program_id, records, programs_root=PROGRAMS_ROOT) -> Path
def read_workstream_association_records(program_id, programs_root=PROGRAMS_ROOT) -> tuple[WorkstreamAssociationRecord, ...]
```

Behavior:

- append-only with `portalocker`
- written only on successful non-dry-run confirm
- source of long-memory workstream↔ADO association history across issues

### 9.10 Backend-Aware Store Layer (`src/core/sqlite_stores.py`, `store_factory.py`, `file_stores.py`)

Vertex supports two storage backends for signal and trajectory stores, selectable per-program via `program.yaml → storage_backend: file | sqlite`.

**`src/core/store_factory.py`** — dispatches store construction based on `storage_backend`:

```python
def build_signal_store(program_id, storage_backend, programs_root=PROGRAMS_ROOT) -> SignalStore
def build_trajectory_store(program_id, storage_backend, programs_root=PROGRAMS_ROOT) -> TrajectoryStore
```

**`src/core/sqlite_stores.py`** — protocol-compatible SQLite implementations:

| Class | Protocol | Backing Store |
|-------|----------|--------------|
| `SQLiteSignalStore` | `SignalStore` | `programs/<prog>/signals.db` |
| `SQLiteTrajectoryStore` | `TrajectoryStore` | `programs/<prog>/trajectories.db` |

Both stores implement the same read/write/query interface as the file-backed JSONL stores. Usage markers are tracked in SQLite with the same semantics as file-backed stores.

**`src/core/file_stores.py`** — shared builders for file-backed store construction (JSONL + YAML); consumed by `store_factory.py` when `storage_backend: file`.

**Migration:** `vertex migrate --edition <id>` reads all JSONL trajectories and signals from the file-backed store and writes them into the SQLite store for the program. Idempotent and dry-run safe.

### 9.11 L1 Reality Store (`src/core/reality_store.py`)

Per-program SQLite database at `~/.vertex/<program_id>/vertex.sqlite3` (overridable via `VERTEX_DB_PATH` env var or `db_root` constructor param). WAL mode, `PRAGMA busy_timeout=5000`. 13 RealityStore tables + 2 Program Fact Store tables (§9.16) = **15 total**:

| Table | Purpose |
|-------|---------|
| `reality_metric_source_bindings` | MetricSourceBinding records (metric → data source mapping) |
| `reality_telemetry_assertions` | TelemetryAssertion policy records with versioning and `valid_until` archival |
| `reality_hypotheses` | Hypothesis lifecycle records (`proposed/confirmed/invalidated/rejected`) |
| `reality_challenges` | RealityChallenge records (threshold/delivery/staleness/manual breaches) |
| `reality_metric_observations` | MetricObservation time-series (including `MANUAL` quality state) |
| `reality_assertion_evaluations` | AssertionEvaluation per-evaluation records |
| `reality_maintenance_windows` | MaintenanceWindow suppression windows |
| `reality_suppression_events` | Audit log for suppression decisions |
| `reality_metric_binding_health` | MetricBindingHealth freshness/source-health records |
| `reality_ingestion_runs` | IngestionRun provenance records |
| `reality_digest_cache` | Serialized RealityDigestModel cache per program |
| `schema_versions` | Migration version tracking |
| `reality_assertion_evidence_urls` | Evidence URL attachments to assertions |

**Key invariants:** `upsert_challenge` stores `state_changed_at=NULL` for newly-opened challenges (preserves round-trip identity). `list_telemetry_assertions` returns active assertions before archived ones (`CASE WHEN valid_until IS NULL THEN 1 ELSE 0 END DESC`).

**`src/core/reality_reconciler.py`** — deterministic truth loop. `reconcile_reality(program_id, metric_id)` evaluates threshold, delivery-date, and staleness checks; handles maintenance window suppression, snooze/cooldown lifecycle, and challenge dedup. Called from `vertex reality digest` and related commands.

**`src/core/hypothesis_models.py`** — all L1 dataclasses: `Hypothesis`, `TelemetryAssertion`, `RealityChallenge`, `AssertionEvaluation`, `RealityDigestModel`, `DigestDelta`, `MetricFreshnessEntry`, `StaleHypothesisEntry`.

**`src/core/metric_models.py`** — `MetricDefinition`, `MetricSourceBinding`, `MetricObservation`, `ObservationWindow`, `MetricQualityState` (includes `MANUAL` for injected observations).

**`src/core/metric_registry.py`** — loads `knowledge/metrics/*.yaml` into a `MetricDefinition` map. Optional validation: inject observations for metrics not yet registered without blocking.

**`src/core/telemetry_assertion_evaluator.py`** — evaluates a `TelemetryAssertion` against a metric observation window; returns `AssertionEvaluation` with `pass_/fail/indeterminate` result.

### 9.12 Channel Registry Store (`src/core/channel_registry_store.py`)

Per-program SQLite database at `programs/<prog>/channel_registry.sqlite3`. WAL mode on network filesystems; DELETE journal mode fallback on `Q:` / UNC paths. `busy_timeout=5000`. `SCHEMA_VERSION = "1"`.

**7 tables:**

| Table | Purpose |
|-------|---------|
| `registrations` | Canonical registration record per `(program_id, channel, provider_instance_id, ref_kind, ref_id)` with status, governance fields, and TTL |
| `registration_bindings` | N:M join table — workstream and source assignments per registration |
| `registry_deltas` | Delta summaries per discovery run (capped at 1,000 rows, 30-day retention) |
| `scope_health` | Per-scope consecutive failure/success counts for circuit breaker tracking |
| `scope_state` | Last-seen completeness and discovery timestamp per scope |
| `registry_feedback` | Governance feedback events (migrated from `M365RoutingFeedbackEvent`; 180-day retention) |
| `schema_meta` | Schema version guard — fail-closed on unknown version with data |

**Key invariants:**
- `ref_id` format validated per `ref_kind` at write boundary (`_REFID_VALIDATORS`).
- `metadata` values constrained to `dict[str, str | int | float | bool | None]` — no nested objects.
- `mark_hydration_failed()` transitions registration to `STALE` after 3 consecutive failures.
- Shrinkage guard: `ShrinkageGuardError` fires when FULL discovery removes >40% of active registrations; signals still produced from existing registry.
- `ensure_schema()` is idempotent; unknown `SCHEMA_VERSION` with data raises `SchemaVersionError` (fail-closed).
- `apply_discovery_result()` handles FULL (authoritative replacement), INCREMENTAL (additive), and PARTIAL (scoped additive) completeness modes.
- Expired registrations auto-retired after 2× TTL for incremental channels.
- Network-filesystem WAL fallback uses `PRAGMA journal_mode=DELETE` on paths matching `Q:` or UNC (`\\`).

**Key methods:**

```python
class ChannelRegistryStore:
    def apply_discovery_result(self, result: DiscoveryResult, seen_at: datetime | None = None) -> RegistryDelta
    def confirm(self, channel, ref_id, pm_alias, note=None, provider_instance_id=None) -> ChannelRegistration | None
    def suppress(self, channel, ref_id, pm_alias, note=None, provider_instance_id=None) -> ChannelRegistration | None
    def promote(self, channel, ref_id, pm_alias, note=None, provider_instance_id=None) -> ChannelRegistration | None
    def reassign(self, channel, ref_id, new_workstream_ids, pm_alias, note=None, provider_instance_id=None) -> ChannelRegistration | None
    def set_signal_yield(self, channel, ref_id, yield_value, pm_alias, provider_instance_id=None) -> ChannelRegistration | None
    def registration_count(self, channel, provider_instance_id=None) -> int
    def recent_deltas(self, channel, *, limit=10, provider_instance_id=None) -> list[RegistryDelta]
    def last_discovery_at(self, channel, provider_instance_id=None) -> datetime | None
    def recent_scope_health(self, channel, provider_instance_id=None) -> dict[str, dict]
    def prune(self, channel, *, before: datetime, dry_run=False) -> int
    def prune_feedback_events(self, *, before: datetime, dry_run=False) -> int
    def schema_migrate(self, *, force=False) -> None
```

`vertex integration schema-migrate` calls `schema_migrate()`. On Windows network drives, connection teardown uses `sqlite3.connect("").close()` to flush WAL.

### 9.13 Context Snapshot Store (`src/core/context_snapshot_store.py`)

Forensic snapshot of Plane 1 state written at confirm time (§22 E2).

**Storage:** `programs/<prog>/archive/<edition>/context_snapshots/issue_NNN.context.json`

```python
def write_context_snapshot(
    program_id: str, edition_id: str, issue_number: int,
    milestones: list[Milestone], risks: list[RiskEntry],
    workstreams: list[Workstream], decisions: list[DecisionEntry],
    confirmed_at: datetime,
    plane1_change_count_since_prior: int,
    *,
    archive_root: Path,
    context_maturity_level: int = 0,
) -> Path

def load_context_snapshot(
    program_id: str, edition_id: str, issue_number: int,
    archive_root: Path,
) -> ContextSnapshot | None
```

`write_context_snapshot` serializes milestone, risk, workstream, and decision records into `ContextSnapshot` (including `context_maturity_level` and `plane1_change_count_since_prior`) atomically. Used by `confirm.py` to enable maturity regression detection (§13.2): if the current context maturity level is lower than the prior confirmed issue's snapshot, confirm emits a `⚠ Context maturity regression` warning on stderr.

### 9.14 Plane 1 Changelog (`src/core/plane1_changelog.py`)

Append-only JSONL audit trail of field-level mutations to the 20 Plane 1 program YAML files (§22 E1).

**Storage:**
- Changelog: `programs/<prog>/changelog/plane1_changes.jsonl`
- Last-seen snapshot: `programs/<prog>/changelog/plane1_last_seen.json`

```python
def append_plane1_changes(
    program_id: str, changes: list[dict], programs_root: Path = PROGRAMS_ROOT,
) -> Path

def write_plane1_last_seen(
    program_id: str, snapshot: dict, programs_root: Path = PROGRAMS_ROOT,
) -> Path
```

Called by `gather.py` after loading the Plane 1 bundle: computes a field-level diff against `plane1_last_seen.json`, appends changed fields to `plane1_changes.jsonl`, then updates the last-seen snapshot.

### 9.15 Context Gap Store (`src/core/context_gap_store.py`)

Append-only JSONL feedback store for context gaps detected at feature-run time (§21).

**Storage:** `programs/<prog>/_feedback/context_gaps.jsonl`

```python
def append_context_gap(
    feature: str, program: str,
    field: str, message: str,
    lane: str | None = None,
    severity: str = "quality_degraded",
    impact_estimate: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path

def load_context_gaps(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> list[ContextGapRecord]

def rank_context_gaps(gaps: list[ContextGapRecord]) -> list[ContextGapRecord]
```

`rank_context_gaps` deduplicates by `(feature, field, lane)`, sums occurrences into `count`, and sorts by impact (`high` > `medium` > `low` > `None`) then by `count` descending. `vertex doctor --context` reads and surfaces ranked context gaps.

### 9.16 Program Fact Store (`src/core/program_fact_store.py`)

System-of-record for Vertex's synthesized program beliefs (signals.md §7.4). Extends the RealityStore database (`~/.vertex/<program_id>/vertex.sqlite3`) with two recorded-time-travel tables (SD-15: `as_of` queries filter on `recorded_at` only; `valid_from`/`valid_until` are stored but not yet a query axis — GAP-36d implements the valid-time dimension), bringing the per-program SQLite total to **15 tables**.

**Architecture (four-store responsibility model — signals.md §7.4.1):**
- **Raw Evidence Journal** — immutable source events (`journal/*.jsonl`, `vertex_store.sqlite3`)
- **Program Fact Store** — synthesized program beliefs (this module; canonical SoR)
- **Projection Stores** — disposable views over facts (narratives, analytics `confirmed_*`, risk registry view)
- **Archive / Snapshots** — immutable point-in-time audit artifacts (`overrides/issue_NNN.yaml`, `confirmed_*`)

No 5th SQLite database is created. Fact CRUD lives in `program_fact_store.py` (new module; `reality_store.py` budget unaffected). `fact_tables.py` extraction deferred to F3 if `program_fact_store.py` hits budget.

**Key types:**

| Type | Purpose |
|------|---------|
| `ProgramFactInput` | Write contract — immutable input to `append_fact()` |
| `ProgramFactRevision` | Read contract — one row from `program_fact_revisions` |
| `ProgramFactSnapshot` | Point-in-time snapshot returned by `snapshot(as_of=)` |
| `FactSnapshotPin` | Draft pin record from `program_fact_snapshot_pins` |
| `FactPrecedence` | `active_pm_judgment > confirmed_governance_decision > verified_system_signal > raw_telemetry` (INV-SG-11) |
| `FactReviewState` | `accepted` \| `proposed` (F2 adds: `candidate`, `disputed`, `superseded`) |
| `FactLifecycleState` | `active` \| `closed` |

**Public API:**

```python
class ProgramFactStore:
    def append_fact(self, fact: ProgramFactInput, *, recorded_at: datetime | None = None) -> ProgramFactWriteResult
    def snapshot(self, *, as_of: datetime | None = None) -> ProgramFactSnapshot
    def pin_snapshot(self, *, metadata: dict | None = None, created_at: datetime | None = None) -> FactSnapshotPin
    def load_snapshot_pin(self, snapshot_id: str) -> FactSnapshotPin | None
    def detect_drift(self, snapshot_id: str) -> tuple[ProgramFactRevision, ...]
    def list_proposed_revisions(self) -> tuple[ProgramFactRevision, ...]

def load_program_facts(program_id, *, as_of=None, fact_types=None, ...) -> ProgramFactSnapshot
def build_natural_key(fact_type, *, entity_refs, scope) -> str
def persist_program_fact_snapshot(snapshot, *, recorded_at=None, ...) -> tuple[ProgramFactWriteResult, ...]

# Projection helpers (snapshot → domain type by fact_type):
def project_action_items(snapshot) -> tuple[ActionItem, ...]
def project_risk_entries(snapshot) -> tuple[RiskEntry, ...]
def project_dependencies(snapshot) -> tuple[Dependency, ...]
def project_decision_entries(snapshot) -> tuple[DecisionEntry, ...]
def project_assumptions(snapshot) -> tuple[Assumption, ...]
def project_milestones(snapshot) -> tuple[Milestone, ...]
def project_workstreams(snapshot) -> tuple[Workstream, ...]

# Convenience wrappers — load_program_facts() + project_*() in one call:
def load_current_action_items(program_id, ...) -> tuple[ActionItem, ...]
# ... analogues for risk_entries, dependencies, decision_entries, assumptions, milestones, workstreams
```

**Schema:**

```sql
-- Recorded-time-travel fact revisions — each row is one revision; fact_id groups revisions of the same fact.
-- (valid_from/valid_until stored but as_of queries filter on recorded_at only — GAP-36d adds valid-time axis)
CREATE TABLE program_fact_revisions (
    revision_id TEXT PRIMARY KEY,            -- pfr_<uuid>
    fact_id TEXT NOT NULL,                   -- pf_<uuid> (shared across revisions of the same fact)
    program_id TEXT NOT NULL,
    natural_key TEXT NOT NULL,               -- hash(fact_type + sorted(entity_refs) + scope)
    fact_type TEXT NOT NULL,
    scope TEXT NOT NULL,                     -- 'program' | workstream_id | dimension_id
    entity_refs_json TEXT NOT NULL,          -- JSON array of entity reference strings
    payload_json TEXT NOT NULL,              -- typed per fact_type (signals.md §7.4.6)
    source_signal_ids_json TEXT NOT NULL,
    confidence TEXT,
    precedence TEXT NOT NULL,                -- FactPrecedence (INV-SG-11 write precedence)
    review_state TEXT NOT NULL,              -- 'accepted' | 'proposed'
    lifecycle_state TEXT NOT NULL,           -- 'active' | 'closed'
    valid_from TEXT,                         -- business/valid time (ISO UTC)
    valid_until TEXT,
    recorded_at TEXT NOT NULL,               -- system/belief time (ISO UTC) — as_of filter axis
    superseded_at TEXT,                      -- closed when superseded (append-only: INV-SG-13)
    projection_history_json TEXT NOT NULL,   -- array of issue/artifact references
    proposed_against_revision_id TEXT,       -- Proposed Revision linkage (INV-SG-11)
    created_by TEXT NOT NULL                 -- 'vertex' | pipeline stage name
    -- F2 additions (ALTER TABLE): privacy_classification TEXT DEFAULT 'internal', accepted_by TEXT
);
CREATE INDEX idx_program_fact_current
  ON program_fact_revisions(program_id, natural_key, review_state, superseded_at);
CREATE INDEX idx_program_fact_recorded_at
  ON program_fact_revisions(program_id, recorded_at);

-- Draft snapshot pins for State Drift Warning (signals.md §7.4.9).
CREATE TABLE program_fact_snapshot_pins (
    snapshot_id TEXT PRIMARY KEY,            -- pfs_<uuid>
    program_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    pinned_recorded_at TEXT,
    pinned_revision_count INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
);
-- current truth: superseded_at IS NULL AND review_state = 'accepted'
-- as_of(t): recorded_at <= t AND (superseded_at IS NULL OR superseded_at > t)
```

**Key invariants and policies:**
- **WAL mode:** every connection executes `PRAGMA journal_mode=WAL` (database-level setting; persists across connections).
- **Append-only (INV-SG-13):** facts are never updated in place; a new revision row is inserted and the prior row's `superseded_at` is set.
- **Write precedence (INV-SG-11):** a lower-precedence write contradicting an accepted fact becomes a `proposed` revision instead of overwriting. `append_fact()` returns `action="proposed_revision"` for triage surfacing.
- **Bitemporal query:** `snapshot(as_of=t)` filters `review_state='accepted' AND recorded_at <= t AND (superseded_at IS NULL OR superseded_at > t)`.
- **Natural key:** `build_natural_key(fact_type, entity_refs, scope)` → SHA-256 hex. Stable dedup identity across pipeline runs.
- **Draft pinning:** `pin_snapshot()` stores a `FactSnapshotPin`; `detect_drift(snapshot_id)` returns revisions recorded after `pinned_recorded_at`, powering QG-SG-20 State Drift Warning.

**F1 compatibility shim:** `load_program_facts()` merges live SQLite revisions with a shim over existing YAML/JSONL stores (`risk_register.yaml`, `decisions.yaml`, `actions.jsonl`, etc.) so all call sites read a unified `ProgramFactSnapshot` before F2 shadow writes begin. Shim facts are merged with `setdefault` so any stored revision always wins.

**W1 reader migration:** all ~30 W1 command surfaces call `load_program_facts()` / `project_*()` / `load_current_*()` rather than individual `load_risk_register()` / `load_decisions()` / `load_actions()` calls.

**ProgramEvent — event-style fact seam:**
`ProgramEvent` is a `@dataclass(frozen=True, slots=True)` in `program_fact_store.py` with three fields: `fact_type: str`, `natural_key: str`, `metadata: dict`. `append_program_event(store, event)` is the canonical one-line helper for writing event-style facts — it converts to `ProgramFactInput` and calls `store.append_fact()`. This decouples event writers from ProgramFactStore constructor details.

**Registered event-style fact types:**
| `fact_type` | Description | First writer |
|-------------|-------------|-------------|
| `event.issue.skip` | Canonical seam replacing ad-hoc journal entries when an issue is intentionally skipped | `cli.py --skip-issue` |
| `workstream.association` | Dual-write of workstream–entity association changes to the Fact Store alongside `workstreams.yaml` | `integration.py` governance ops |
| `baseline.trust_event` | Dual-write of trusted-baseline promotion events for the State Drift Warning (QG-SG-20) audit trail | `trusted_baseline_store.py` |

**F1→F4 migration roadmap (signals.md §7.4.4):**
| Phase | Status | Description |
|-------|--------|-------------|
| F1 Unified read | **Shipped** | Shim over YAML/JSONL; SQLite tables initialized; W1 commands migrated |
| F2 Shadow write | In progress | Unified read API and draft snapshot pinning are shipped; shadow-write foundation is landed; confirm-time shadow writes, `privacy_classification` + `accepted_by` schema migration, and `confirmed_*` → materialized view remain pending |
| F2b Shadow isolation / family mode | Complete | All projectors filter accepted facts, per-family SoR resolution is implemented, divergence is reported per family, and shadow/primary isolation is contract-tested. |
| F3 Flip to SoR | Policy accepted / gated by evidence | ADR-0006 accepts the `source_authority.yaml` family map and `sor_flip` defaults. `evaluate_family_flip_gate()` runs only on complete cycles and requires clean-cycle evidence (`clean_cycles_to_flip=5`, `critical_zero=true`, bounded divergence tolerance, rollback posture). YAML/JSONL remain authoritative until the family gate passes. |
| F4 Override→fact | Pending | `Judgment` fact type; Issue 78 backfill; `❓ Needs input` treadmill eliminated |

### 9.17 Program Event Ledger (`src/core/ledger/`)

Append-only event-sourced ledger for per-program reality capture, operator triage, replayable projections, and discovery governance. This substrate is separate from the Program Fact Store: the ledger is the event/candidate/projection system under `programs/<id>/ledger/`, while the Program Fact Store remains the synthesized belief SoR in the shared per-program SQLite database.

**Storage layout:**
- Event log: `programs/<id>/ledger/events/*.jsonl` — monthly append-only event segments, content-hash chained, with write-time payload validation and monotonic ULID event ids.
- Event index: `programs/<id>/ledger/<id>-index.sqlite3` — SQLite metadata/index over the authoritative JSONL event log (`event_index.py`).
- Candidate queue: `programs/<id>/ledger/candidates/pending.jsonl` and `triaged.jsonl` — append-only staged discovery candidates plus immutable triage audit (`candidate_store.py`).
- Current projection: `programs/<id>/ledger/projections/current.sqlite3` — disposable replay output built from the event log (`program_views.py`).
- Projection snapshots: `programs/<id>/ledger/projections/snapshots/` — per-issue baseline artifacts written by confirm-time ledger follow-through.
- Evidence vault: `programs/<id>/ledger/evidence/<hh>/<hash>` + `.meta.json` — content-addressed external-origin evidence excerpts (`evidence_vault.py`).
- Verify sidecar: `programs/<id>/ledger/verify_status.json` — latest persisted `vertex ledger verify` result for operator status surfaces (`verify_status.py`).

**Primary modules and responsibilities:**
- `event_log.py` — typed envelope construction, append-only writes, replay reads, chain verification, and monthly rotation.
- `event_types.py` — registered ledger event taxonomy and write-time payload validation.
- `source_refs.py` — typed provenance refs plus self-containment validation for vault-backed external sources.
- `event_index.py` — SQLite event metadata/entity/vault index; rebuildable from the JSONL log.
- `program_views.py` — deterministic SQLite projection materialization and replay helpers for current/as-of ledger state.
- `candidate_store.py` — staged candidate persistence, active-queue derivation, triage audit, and batch-progress summaries.
- `discovery_run_recorder.py` / `discovery_candidate_builders.py` — Zone A discovery governance seam translating pipeline results into staged candidates and governance events.
- `evidence_vault.py` — content-addressed storage for external evidence excerpts and deep-verify parity helpers.
- `fact_bridge.py` — optional ledger→Program Fact Store bridge families for the currently mapped event types.
- `verify_status.py` — persisted latest verify snapshot surfaced by `vertex ledger status`.
- `entity_ns.py` — bidirectional namespace bridge between signal-layer refs (`WI:<n>`, `P:<alias>`) and ledger entity IDs (`work_item:ado-<n>`, `person:<alias>`). `EntityNsMapper` provides `to_ledger()` / `from_ledger()` round-trip conversion for all supported entity types (§6.2).
- `redaction.py` — §10.8 compliance redaction. `redact_event()` atomically rewrites a payload to `{"redacted": true}` under `portalocker LOCK_EX`, then appends an `EventRedactionRecord` to `.redactions.jsonl` preserving `original_envelope_hash` for hash-chain continuity. This is the sole INV-DM-1 exception and the sole registered exception in the state-reader authority allowlist.

**Protection and projection modules (`src/core/protection/`, `src/core/projections/`):**
- `protection/supersession.py` — conflict-resolution total order (confidence tier → occurred_at → source-ref priority → ULID), correction-chain folding, tombstone events, cycle detection, shadow-not-overwrite for Tiers 2–4.
- `protection/field_lock_store.py` — field lock CRUD managed as ledger events; lock expiry evaluated at projection time.
- `projections/program_projection.py` — pure `project_program_events()` pipeline: upcast → supersession → lock application → TTL/sunset → projection folds. No wall-clock reads (`datetime.now()` banned in this module — INV-DM-5).
- `projections/program_views.py` — `current_state()`, `as_of(t)`, `timeline()`, `diff(t1, t2)` views over a materialized projection.
- `projections/snapshot_manager.py` — confirm-time projection snapshot with hardlock event; content-addressed; backed by `programs/<id>/ledger/projections/snapshots/`.

**Knowledge plane modules (`src/core/knowledge/`, `src/core/knowledge_claim_store.py`):**
- `knowledge/predicate_registry.py` — closed vocabulary of claim predicates (e.g., `depends_on`, `is_deprecated`, `owner_changed`, `risk_accepted`); unknown predicates are rejected at claim-write time (QG-DM-12 variant).
- `src/core/knowledge_claim_store.py` — claim assertion, supersession, scope-hierarchy resolution (`knowledge_context()` view), per-scope watermarks, and context-digest generation for snapshot manifests.

**Zone B AI extractors (`src/ai/discovery/`):**
Discovery extractors return `DiscoveryRunResult` objects; they never call the event write API (INV-DM-6). Zone A `discovery_run_recorder.py` translates results into ledger candidates.
- `lt_deck_extractor.py` — LT deck PPTX/EML extraction via python-pptx and MIME parsing.
- `newsletter_extractor.py` — prior newsletter HTML/EML extraction.
- `email_extractor.py` — inbox email extraction.
- `sharepoint_doc_extractor.py` — SharePoint document extraction.
- `kb_claim_extractor.py`, `kb_event_extractor.py`, `kb_decision_log_extractor.py` — Knowledge Base extraction with recorded LLM fixtures for CI.

**Zone C M365 connectors (`src/m365/discovery/`):**
M365 connectors enumerate and hydrate sources; they write only to candidate store via Zone A seam.
- `workiq_pipeline.py`, `teams_pipeline.py`, `sharepoint_pipeline.py`, `outlook_pipeline.py` — four connector implementations. `sharepoint_pipeline.py` is the primary gather path for SharePoint ref docs and LT decks; change-detection via `gather_state.json doc_states`; `--force-refresh` bypasses cadence throttle.

**Quality gate enforcement:**
All 13 data-model quality gates are registered and enforced. Hard-block gates enforced at write time or CI: QG-DM-1 (chain integrity), QG-DM-2 (projection determinism), QG-DM-4 (hardlock immutability), QG-DM-8 (source-ref completeness), QG-DM-11 (knowledge-context determinism), QG-DM-12 (self-containment). Advisory gates surfaced through `vertex ledger status`, `vertex doctor`, and `vertex ledger triage`: QG-DM-3, QG-DM-5, QG-DM-6, QG-DM-7, QG-DM-9, QG-DM-10, QG-DM-13. CI registry anchors in `tests/contracts/test_dm_ci_gate_contract_registry.py`.

**Invariant enforcement (INV-DM-1..6):**
- INV-DM-1 (append-only + §10.8 redaction exception): code review + `verify_event_log` redaction-awareness.
- INV-DM-2 (ULID monotonicity): enforced at write time in `ulid.py`.
- INV-DM-3 (hash-chain integrity): `verify_event_log` + `tests/unit/test_ledger_event_log.py`.
- INV-DM-4/5 (projection determinism + no wall-clock): `tests/golden/test_ledger_projection.py::test_qg_dm_2_projection_golden` and `test_no_wall_clock_in_projection`.
- INV-DM-6 (Zone B/C no event write): AST scan in `tests/contracts/test_import_boundaries.py::test_inv_dm_6_zone_b_c_discovery_never_calls_event_write_api`.

**Operational contract:**
- JSONL is authoritative; SQLite artifacts (`<id>-index.sqlite3`, `current.sqlite3`) are rebuildable caches/projections.
- `vertex ledger replay` and `vertex ledger verify` operate over the append-only log, not the projection database.
- `vertex backup` captures the full ledger tree implicitly via recursive backup of `programs/`, including event logs, candidate audit, projections, snapshots, and evidence-vault artifacts.
- Backfill procedure for Tier A (LT decks), Tier B (newsletters), and Tier C (KB/Artha): see `governance/runbooks/ledger-backfill-runbook.md` (tracked, generic) or a workspace's own `docs/ledger-backfill-runbook.md` (gitignored, program-specific detail).
- NFR-3 (50K replay budget): `project_program_events()` for 50K events must complete in < 60 seconds. Validated by `tests/unit/test_ledger_perf.py::test_replay_perf_50k` (`@pytest.mark.slow`); actual measured time is ~14s.

### 9.17.1 Common Persistence Kernel (CPK) — arch-fix.md Part A, Phases 0–1

Foundational persistence primitives closing the architecture-remediation program's Phase 0/1 (see `.archive/specs/arch-fix.md` for the full audit trail and `specs/backlog.md` §7 for remaining Phase 2/2b/3 work). These exist as reusable Zone A infrastructure — they are not yet wired into any AI-safety or actuation call path (that wiring is the remaining `arch-fix` work).

**Event-log hardening.** `event_log.py`'s append path was O(n) per write (a full `read_events()` re-scan for the hash chain plus a full-file `_count_lines()` for the index line number). It now maintains a small, self-healing O(1) tail cache (`programs/<id>/ledger/events/_tail.json` — last event hash, last `recorded_at`, active file name/size/line-count) that falls back to the exact prior full-scan behavior whenever the cache is missing, stale, or doesn't name the currently-resolved target file (rotation, cross-process drift, first-ever write). The cache is a pure write-path optimization; every reader (`read_events`, `verify_event_log`, projections) still derives truth from the JSONL files, never the cache. `event_index.py` now opens through `open_program_db()` (network-aware WAL/DELETE journal-mode selection) instead of hardcoding `PRAGMA journal_mode=WAL`.

**New primitives (`src/core/ledger/` unless noted):**
- `program_sequence.py` — monotonic per-program sequence allocator (`next_sequence()`/`current_sequence()`), atomic via SQLite `UPDATE ... RETURNING`.
- `src/core/workspace_lease.py` — coarse-grained pessimistic workspace lease (owner + TTL + fencing token; `acquire_lease()`/`renew_lease()`/`release_lease()`), atomic via `BEGIN IMMEDIATE`. Resolves the multi-host single-writer paradox: a local queue can't serialize writers on other hosts against a shared network workspace. Not yet wired into any mutating command's write path. Uses `open_program_db_with_retry()` (`src/core/_db.py`) rather than `open_program_db()` directly: `PRAGMA busy_timeout` doesn't reliably cover the brief exclusive lock a brand-new database file's WAL-mode conversion needs, so many hosts racing to acquire a lease against a not-yet-existing lease file can otherwise hit `sqlite3.OperationalError: database is locked` — a real failure surfaced by `test_workspace_lease.py`'s 8-thread concurrent-acquisition test, fixed with bounded jittered-backoff retry on the connect+PRAGMA phase specifically (not a blanket retry-everything). `program_sequence.py` and `durable_outbox_store.py` use the same retry wrapper for the same reason.
- `projection_checkpoint_store.py` — per-projection watermark (event id + `recorded_at`) plus projector/policy version and an optional checksum, for future replay/drift detection.
- `src/core/unit_of_work.py` — `open_unit_of_work({alias: path, ...})` commits correlated writes across multiple separate SQLite files as one atomic transaction, via SQLite's transactional `ATTACH` (each attached database still gets its own network-aware journal mode, matching `open_program_db()`'s per-store behavior rather than forcing one mode connection-wide).
- `durable_outbox_store.py` — generic, table-agnostic lease/attempt/dead-letter engine: `pending→leased→dispatched→succeeded→audited→completed`, with `failed_retryable` (re-leasable once due) and true terminals `uncertain_remote_state`/`failed_terminal`/`compensation_required`/`dead_letter` requiring an explicit `mark_manually_resolved()`. Stale leases (expired TTL) are reclaimed by a different owner via a bumped fencing token; a caller presenting a superseded fencing token gets `LeaseNoLongerCurrent`, never a silent overwrite.
- `program_checkpoint_manifest.py` — `ProgramCheckpointManifest` composes event-log position + `program_sequence` + `projection_checkpoint_store` + caller-supplied outbox watermarks/tracked-file hashes/schema versions into one content-addressed (self-hashing) checkpoint; `verify_manifest_against_disk()` detects drift between a captured manifest and current on-disk store state (the restore-drill check).
- `src/core/quality_gates/gate_registry.py` — central `RESERVED_GATE_IDS` map + a code scanner (`scan_defined_gate_ids()`) so a new gate ID can be checked for collision before it's claimed (QG-29 is reserved for the not-yet-built AF-3 fail-closed audit gate).
- `src/core/authority_registry.py` — composed, read-only view over `source_authority.yaml`, the ledger event registry, `fact_sor_state.py`, and `state_reader_registry.py`; answers, per authority family, which source is primary, which fact types/event prefixes back it, and its SoR-flip configuration.

**AI-safety groundwork (`src/ai/safety/`):** `ai_trace_sanitizer.py` (`sanitize_ai_io()` — credential redaction via `src/core/rev/privacy.py::scan_credentials`, then PII scrub via `pii_scrubber.scan_text`, then a size bound — never returns raw text) and `ai_trace_capture.py` (`capture_ai_io()` — opt-in via `VERTEX_AI_TRACE_FULL_IO`, off by default; writes sanitized excerpts to `ai/llm_trace_full_io.jsonl`, a 90-day-TTL sidecar registered in `governance/data-classification.yaml`/`privacy_matrix.py`). This exists to bake a real-I/O corpus for the not-yet-built AI Safety Boundary's (`AF-1`/`AF-2`) semantic-validator eval harness — `llm_trace.py` itself remains metadata-only.

**Test coverage:** `tests/unit/test_program_sequence.py`, `test_workspace_lease.py` (includes a multi-thread concurrent-acquisition test), `test_projection_checkpoint_store.py`, `test_unit_of_work.py` (cross-database commit/rollback atomicity), `test_durable_outbox_store.py` (full lifecycle + stale-fencing-token rejection), `test_program_checkpoint_manifest.py`, `test_ai_trace_sanitizer.py`, `test_ai_trace_capture.py`; contract tests `test_qg_gate_reservation_contract.py`, `test_authority_registry_contract.py`, `test_nfr_budget_freeze_contract.py`.

---

## §10 Configuration System

### 10.1 Constants (`src/core/config_loader.py`)

```python
REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
EDITIONS_ROOT = REPO_ROOT / "editions"  # legacy fallback; primary path is programs/<id>/editions/
PROGRAMS_ROOT = REPO_ROOT / "programs"
SCHEMAS_ROOT = REPORTS_ROOT / "schemas"
```

### 10.2 Edition Resolution (`src/core/edition_resolver.py`)

```python
def resolve_edition(edition_id, editions_root=None, programs_root=None) -> ResolvedEdition | None
```

1. Globs `programs_root/*/editions/<id>.yaml` → programs-tree lookup; falls back to legacy `editions/<id>.yaml` for backwards compatibility
2. Extracts `program_id` from the resolved YAML; builds paths: `programs/<program_id>/`, `programs/<program_id>/archive/<edition_id>/`, `programs/<program_id>/publications/<edition_id>/`
3. Loads: `program.yaml`, `workstreams.yaml`, `scorecards.yaml` from program dir
4. Parses each into typed models

`workstream_registry.yaml` is an optional adjunct, not part of the typed edition bundle contract yet. It is loaded lazily by report, narrative-stage, and confirm-time workstream-memory flows.

**Area path aggregation:** If `edition.ado.area_paths` set → use directly. Otherwise: filter workstreams by `workstream_filter` (modes: `all`/`include`/`exclude`), collect + sort + deduplicate area paths.

**Config merge:** Edition overrides program defaults field-by-field. Absent edition fields inherit from program. Implemented via `dataclasses.replace()`.

**ADO proposal TTL contract:** `program.yaml -> ado.proposal_ttl_hours` is parsed into the shared ADO config model with a default of `72`. That shared TTL governs `comment`, `vitality_nudge`, and `vitality_tag` proposal expiry when `vertex ado propose` resolves a program/edition.

**Program config fields consumed by the live runtime:** `program.yaml` now also carries `maturity_level` (L0-L4 gating), `communication_plan` (edition cadence and primary-surface selection), `charter` (scope, success criteria, constraints, stakeholder register), `raci` (responsibility mapping used by review/escalation/owner surfaces), `storage_backend` (`file | sqlite` store selection), `catchup` (session-start catchup interval and WorkIQ budgets), `salience` (attention-model tuning and floors), `readiness` (gate enablement and snapshot freshness), `scorecard.include_dependency_risk`, `audit` (retention and archive warning thresholds), and `gather.backend`.

**AI config fields consumed by the live runtime:** `ai.enabled`, deployment/budget settings, `ai.semantic_index`, and `ai.claim_extractor` subfields (`mode`, `calibration_min_confirms`) are now parsed through the governed config surface. Entering production claim-extractor mode is guarded by `vertex config set ai.claim_extractor.mode production` checks rather than direct YAML edits.

**M365 config fields consumed by the live runtime:** `m365.enabled`, `m365.prefer_agency`, optional `m365.workiq_queries`, and optional `m365.icm_incidents_url`. When `m365.icm_incidents_url` is configured, `vertex gather --icm` prefers the direct app-only IcM client (`src/m365/icm_client.py`) and falls back to Agency or Kusto when direct access is unavailable.

#### 10.2.1 Autonomy Ladder (`program.yaml -> maturity_level`)

Vertex uses the L0-L4 Autonomy Ladder below as the canonical meaning of `program.yaml.maturity_level`. L0 is retained as the baseline current state; higher levels describe earned operating envelopes, not permission to bypass deterministic validation or rollback requirements.

| Level | Meaning | System authority | Author role | Promotion gate |
|-------|---------|------------------|-------------|----------------|
| **L0** | Deterministic compiler | Computes, renders, validates | Drives every step | N/A - current baseline |
| **L1** | Advisory detection | Surfaces anomalies and contradictions | Reviews, ignores, or acts | False-positive rate judged acceptable over >=5 sessions |
| **L2** | Proposal staging | Drafts ADO comments, nudges, briefs | Explicitly approves or dismisses each proposal | Prior acceptance rate >= 70% for the action type |
| **L3** | Approved bounded writes | Applies explicitly approved proposals within a defined blast radius | Approves bounded batches, can halt and rollback | Blast radius <= defined bound, rollback path exists, prior acceptance rate >= 90% |
| **L4** | Scheduled low-risk writes | Runs specific low-risk action types on schedule | Receives audit log and can halt at any time | Action type proven at L3 for >=10 cycles and zero contradictions in the prior 5 cycles |

**Approval contract:** L2 and L3 are still author-approved modes; L4 is the first standing-policy mode. No maturity level waives the requirement that external writes pass through deterministic command handlers, record an autonomy-audit entry, and have a documented rollback path.

**Audit invariant:** Every L2+ approval or rejection writes `programs/<prog>/journal/autonomy_audit.jsonl` first and is then projected into `vertex_analytics.sqlite3`. `maturity_level` gates behavior, but the audit and rollback invariants remain unconditional.

#### 10.2.2 Readiness Config Schema (`programs/<prog>/readiness.yaml`)

The readiness engine is defined by `src/core/readiness_engine.py` and configured per program in `programs/<prog>/readiness.yaml`.

**Top-level schema:**

| Field | Type | Notes |
|-------|------|-------|
| `schema_version` | string | Major version `1` is currently supported |
| `snapshot_max_age_days` | int | Optional; defaults to `7` |
| `dimensions` | mapping | Built-in readiness dimensions keyed by dimension id |
| `custom_dimensions` | mapping | Optional additional dimensions keyed by custom id |

At least one dimension must be present across `dimensions` and `custom_dimensions`.

**Built-in dimension ids and default gate bindings:**

| Dimension id | Default name | Default gate |
|--------------|--------------|--------------|
| `slo_definition_complete` | SLO definition complete | `QG-RD1` |
| `dependency_health` | Dependency health | `QG-RD2` |
| `observability_coverage` | Observability coverage | `QG-RD3` |
| `rollback_plan` | Rollback plan | `QG-RD4` |
| `capacity_validation` | Capacity validation | `QG-RD5` |
| `incident_response_owner` | Incident response owner | `QG-RD6` |
| `support_handoff_complete` | Support handoff complete | `QG-RD7` |
| `dora_change_fail_rate` | DORA change fail rate | `QG-RD8` |

Custom dimensions use the same payload shape as built-in dimensions. If a custom dimension omits `gate_id` or supplies a non-`QG-RD-...` gate id, the runtime normalizes it to `QG-RD-<dimension_id>`.

**Per-dimension schema:**

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | Optional; defaults to the built-in label or a humanized custom id |
| `gate_id` | string | Optional; defaults from the built-in map or normalized custom id |
| `source` | mapping | Required |
| `pass_condition` | mapping | Required |

**`source` mapping fields:**

| Field | Type | Notes |
|-------|------|-------|
| `type` | string | Required; supported runtime values are `ado_query`, `kusto_query`, `manual_attestation`, `people_directory`, `dependency_health`, `workstream_risk`, `incident_journal` |
| `query_id` | string | Optional; used by ADO/Kusto-backed dimensions |
| `alias` | string | Optional; used by people-directory backed dimensions |
| `attested_at` | date | Optional manual-attestation date |
| `attested_by` | string | Optional manual-attestation owner |
| `notes` | string | Optional operator note |
| `workstream_id` | string | Optional workstream scoping hint |

**`pass_condition` mapping fields:**

| Field | Type | Notes |
|-------|------|-------|
| `kind` | string | Required; source-specific evaluation mode enforced by `src/core/readiness_engine.py` |
| `allowed_states` | list[string] | Used by ADO state-based checks |
| `operator` | string | Numeric operators supported: `>`, `>=`, `<`, `<=`, `==`, `!=` |
| `threshold` | number | Optional numeric threshold |
| `days` | int | Optional age threshold |
| `result_column` | string | Optional preferred Kusto result column |
| `risk_level` | string | Optional risk threshold parsed through `RiskLevel` |

`vertex readiness fetch` writes the evaluated snapshot to `programs/<prog>/readiness_snapshot.yaml` with `schema_version`, `program_id`, `fetched_at`, `snapshot_max_age_days`, `content_sha256`, and the ordered `dimensions` payload. Snapshot reads fail closed on unsupported schema versions or hash mismatches.

**Program-owned adjunct state files:**
1. `programs/<prog>/trusted_baseline.yaml` stores the weekly trusted issue number, baseline history, optional untrusted gap marker, and bridge graduation state used by continuity resolution and `vertex bridge-status`.
2. `programs/<prog>/capability_status.yaml` stores machine-readable completion or deferral state for later-wave external dependencies such as `kusto_activation`, `m365_activation`, and `graph_app_only_auth`; `vertex status`, `vertex fleet`, and `vertex maturity-check` project this state directly.
3. `vertex fleet` (`fleet.py`) now includes three context health columns on each `FleetProgramSummary`: `context_maturity_level` (int, 0–4), `context_invariant_errors` (count of ERROR-severity violations), `context_stale_file_count` (count of non-ok staleness flags). Populated via `load_program_context(raise_on_error=False)` per program. Rendered as a `Context L{level} / {errors} errors / {stale} stale` line in human, Markdown, and CSV formats.
3. `programs/<prog>/journal/autonomy_audit.jsonl` is the durable governance primary for L2+ approvals/rejections, mirrored into `vertex_analytics.sqlite3`.
4. `programs/<prog>/journal/incident_journal.jsonl` is the append-only incident-learning store populated by `vertex gather --icm` and consumed by lookback/history/brief/triage/readiness/decision workflows.
5. `programs/<prog>/_feedback/author_salience.yaml` and `programs/<prog>/_feedback/signal_approval_rules.yaml` store learned-but-governed local feedback policy state.
6. `programs/<prog>/vertex_analytics.sqlite3` and semantic-index state files are maintained by confirm-time projection plus explicit `vertex migrate --rebuild-analytics` and `vertex index` operator flows.

#### 10.2.3 Program Directory Taxonomy (`src/core/program_paths.py`, declutter.md §5)

The program root `programs/<id>/` is partitioned into lifecycle tiers. The single Zone-A source of truth is `src/core/program_paths.py`: `ROOT_ENTRIES` (T-1/T-2/root T-4 files, hidden markers, recognized subdirectories) and `RUNTIME_ARTIFACTS` (the T-3 platform-internal files that live in `runtime/` after Phase 1). `vertex doctor --storage` (DC-01/DC-02/DC-03, `storage_checks.py`) enforces this taxonomy; `scripts/program_inventory.py` reports it. This subsection is **extended** by `specs/declutter.md` §5 (per-file delete-safety contracts, retention, and operator-editability) where it does not yet cover a directory.

| Tier | What lives here | Lifecycle | Operator-editable? | Delete-safe? |
|------|-----------------|-----------|--------------------|--------------|
| **T-1** | Authored config (`program.yaml`, `workstreams.yaml`, `scorecards.yaml`, `editorial_rules.yaml`, `review.yaml`, `chapter_contract.yaml`, `slice_contracts.yaml`, `source_contracts.yaml`, `source_waivers.yaml`, `kpi`/`backfill`/`escalation_rules.yaml`, `ado_comment_template.md`) + `editions/`, `knowledge/` | Stable, operator-owned | Yes | No — operator data |
| **T-2** | Mutable program state (`baseline.yaml`, `trusted_baseline.yaml`, `decisions.yaml`, `assumptions.yaml`, `dependencies.yaml`, `risk_register.yaml`, `milestones.yaml`, `manual_metrics.yaml`, `readiness.yaml`, `earned_autonomy_state.yaml`, `capability_status.yaml`, `fact_store_sor.yaml`, `workstream_registry.yaml`) | Operator + platform written | Sometimes | No — re-derivation cost varies; some is unrecoverable operator judgement |
| **T-3** | Platform-internal runtime files in `runtime/`: `gather_state.json` (T-3b), `run_telemetry.jsonl` (T-3a), `dedup_drop_log.jsonl` (T-3a), `vertex_analytics.sqlite3` (T-3b, **checkpointed** — rebuild not lossless per A-11), `readiness_snapshot.yaml` (T-3a), `m365_registry.yaml` (T-3b, **checkpointed** — `pm_confirmed`), `channel_registry.sqlite3` (T-3b, **checkpointed** — `pm_confirmed`) | Regenerated by the owning command (`gather`, readiness fetch, analytics rebuild, registry/discovery) | No | T-3a delete-safe (regenerated with re-fetch cost); T-3b NOT delete-safe without checkpoint/backup (G-5) |
| **T-4** | Root append-only/proof logs: `platform_proof_log.yaml` (root, **not** `runtime/`), `chronicle.jsonl`, `external_dependencies.jsonl`, `migration_log.jsonl`; `journal/`, `journal_archive/`, `ledger/`, `trajectories/`, `changelog/` | Grow-only, platform-owned | No | No — append-only evidence/audit (durability of `platform_proof_log.yaml` is open under A-10/OQ-7) |
| **T-5** | Outputs: `publications/`, `archive/` | Per-edition render + archived render | No (rendered) | Rendered outputs are reproducible; `archive/` is the confirmed-issue ledger — not delete-safe |
| **T-6** | Operational: `narratives/`, `summaries/`, `nudge/`, `overrides/`, `checkpoints/`, `metrics/`, `backfill/`, `gold_corpus/` | Rolling/operational | No (machine); `overrides/` is operator-controlled | Mostly reproducible; `checkpoints/` + `gold_corpus/` have their own retention (see below) |
| **T-7** | Feedback: `_feedback/` (learned-but-governed policy: `author_salience.yaml`, `signal_approval_rules.yaml`, `claim_extraction_calibration.jsonl`) | Learned state | No (governed) | Not delete-safe — learned policy (R-5; 10+ readers) |
| **T-8** | Docs/research: `docs/` (one-time human documents), `_spike/` (research scratch) | One-time / ephemeral | Yes (`docs/`, `_spike/`) | `docs/` is human record (not delete-safe without review); `_spike/` is clutter-candidate (>50 files → DC-01-b prune) |

**T-3-class sidecars (`_state/`, `_alerts/`):** lazily created by the platform; may not appear in a fresh inventory. Classified T-3-class (not T-7): their contents are platform-read runtime state, distinct from the governed `_feedback/` policy store.

**Per-file delete-safety (declutter.md §5):** T-3a files (`run_telemetry`, `dedup_drop_log`, `readiness_snapshot`) are safe to delete but regenerated only by their owning command — deleting `readiness_snapshot.yaml` blocks readiness-gated `confirm` flows until `vertex readiness fetch` refreshes it (A-8). T-3b files (`gather_state`, `vertex_analytics`, `m365_registry`, `channel_registry`) are NOT delete-safe: the registry marks them `checkpointed=True` so Phase 1 migration and checkpoint/backup treat them as protected state (G-5 zero silent data loss).

**Runtime path API (R-14):** every runtime artifact has a **canonical write getter** (`get_*_path` — `runtime/<file>` after Phase 1-B; no legacy fallback) and a **transitional read resolver** (`resolve_*_path_for_read` — canonical-first, legacy fallback during the compatibility window). Writers must use the canonical getter; using a fallback resolver for writes creates split-brain state. The R-1a architecture-fitness test (`tests/contracts/test_runtime_path_construction.py`) bans inline construction of any runtime filename outside `program_paths.py`.

**`platform_proof_log.yaml`** is T-4 at root (NOT in `runtime/` — purgeable `runtime/` would risk durable proof evidence). Atomic-write/rotation durability is open work under A-10/OQ-7.

**`journal_archive/` — Reserved.** Reserved for future monthly journal rotation. Currently empty in all live programs. The monthly archival strategy (threshold, tooling, operator confirm) is deferred to a dedicated spec when `journal/` size becomes operationally significant (declutter.md OQ-3). It is a recognized root directory (in `ROOT_ENTRIES`, T-4) so `vertex doctor` DC-01-c does not flag it as unrecognized, but no platform code reads from or writes to it today.

### 10.3 Bundle Loading (`src/core/config_loader.py`)

`load_bundle(edition_name)` dispatches to `config_loader_v2.load_edition_bundle()`. The V1 `reports/` fallback is removed (V2.3 sunset). Returns `ReportBundle`:

| Field | Type |
|-------|------|
| `config` | `ReportConfig` |
| `editorial_rules` | `EditorialRules` |
| `review` | `ReviewSettings` |
| `program_context` | `NarrativeProgramContext \| None` |
| `template_contract` | `TemplateContract \| None` |
| `slice_contracts` | `tuple[SliceContract, ...] \| None` |
| `chapter_contract` | `ChapterContract \| None` |

### 10.4 Knowledge Store (`src/core/knowledge_store.py`)

```python
def load_knowledge(knowledge_root, fallback_root=None) -> KnowledgeStore
def load_program_knowledge(program_id, ...) -> KnowledgeStore
def validate_knowledge(store) -> list[str]
```

Resolution: `knowledge/` (shared root) → `programs/<prog>/knowledge/` (fallback). Validates referential integrity: team_ids, alias refs, workstream_ids, program refs.

### 10.5 Editorial Rules

`programs/<prog>/editorial_rules.yaml`:

```yaml
banned_phrases: [...]           # Forbidden in all rendered content
banned_openings: [...]          # Forbidden sentence starters
verbosity:
  workstream_blurb_max_sentences: 4
  workstream_blurb_max_words: 90
  exec_bullet_max_words: 25
  exec_max_bullets: 3
  scorecard_summary_max_sentences: 3
stale_warn_days: 14
stale_block_days: 30
```

### 10.6 Contract Files

| File | Purpose |
|------|---------|
| `template_contract.yaml` | Section ordering per edition family; `families.<name>.order[]`, `.mandatory[]`, `.optional[]`, `.rules` |
| `slice_contracts.yaml` | Per-dimension: source_of_truth, ADO assignment, freshness SLAs, degradation templates |
| `chapter_contract.yaml` | Chapter grouping: chapter definitions → sections → dimensions; used by continuity layout |

`programs/<program>/kpis.yaml` is the authoritative KPI-to-chapter binding surface via each `KustoQuery.chapter` value. `chapter_contract.yaml` governs continuity-layout grouping/visibility for chapter-based rendering, and contract checks validate those references against the canonical configured dimensions and workstream surfaces.

### 10.7 Edition YAML Schema

All editions carry `schema_version: "2.0"`. Required fields: `id`, `program_id`, `type`, `altitude`, `cadence`. Key optional fields: `layout_mode` (default `"dashboard"`), `scorecard_sort` (default `"risk_desc"`), `ado` overrides, `ai` overrides, `distribution`, `brand_name`, `cadence_note`, `forecast_enabled`, `workstream_filter`.

`chapter_contract.yaml` `include_in` accepts `[detailed, focused, deck, condensed, lookback]`; `nudge`-type editions bypass `chapter_contract.yaml` entirely.

`template_contract.yaml` family `allowed` values accept `[detailed, focused, deck, condensed, lookback, nudge]`.

**Nudge-type edition additional fields:**

| Field | Level | Default | Description |
|-------|-------|---------|-------------|
| `brand_label` | top-level | `"ADO Hygiene"` | Brand name used in email subject and header labels |
| `send_day` | top-level | (none) | Target day of week for sending (`monday`–`friday`); validated by `doctor` as a valid weekday |
| `send_time_local` | top-level | (none) | Local time for delivery, `HH:MM` format |
| `timezone` | top-level | (none) | IANA timezone string, e.g. `America/Los_Angeles` |
| `hygiene.stale_business_days` | hygiene | `3` | Business-day staleness threshold for flagging items |
| `hygiene.comment_window_days` | hygiene | `14` | Days within which a comment counts as “recent” for `recent_comment` field checks |
| `hygiene.deadline_offset_days` | hygiene | `1` | How many days from run-time to set the action-needed deadline date |
| `hygiene.cooldown_days` | hygiene | `14` | Minimum days before re-nudging the same owner on the same item |
| `hygiene.per_owner_dedup` | hygiene | `false` | Suppress duplicate item rows across owner emails |
| `hygiene.consolidated_email` | hygiene | `false` | Send one consolidated EML to all owners instead of per-owner emails |
| `hygiene.coverage_alert_threshold` | hygiene | `0.6` | Minimum field-population ratio before a coverage alert fires |
| `hygiene.workstream_coverage_alerts` | hygiene | `true` | Whether to compute per-workstream coverage alerts |
| `hygiene.required_fields` | hygiene | see code | List of ADO fields that must be populated (`assigned_to`, `target_date`, `description`, `risk_assessment`, `recent_comment`) |
| `hygiene.sub_program_filter` | hygiene | (none) | Restrict scope to workstreams with matching `sub_program_id` |
| `hygiene.include_children` | hygiene | `false` | Include child work items (Tasks, Bug) in field-quality checks |
| `hygiene.min_ado_count` | hygiene | `1` | Minimum number of active ADO items in a workstream before coverage alerts fire |

**Full-hygiene-type edition additional fields (`full_hygiene:` block):**

The canonical config is a data-driven `sections:` list. Each `NudgeSectionSpec` declares its own `id`, `title`, `letter`, `stale_business_days`, `template`, and a `criteria` block (`source` ∈ `registry` | `tag` | `area_path`, plus `tags` / `area_path_filter` / `required_tags` as the source requires). There is no fixed section count or hardcoded program-specific model.

| Field | Default | Description |
|-------|---------|-------------|
| `sections` | (required) | Ordered list of `NudgeSectionSpec`; sections deduplicate earlier→later. Each section: `criteria.source` `registry`/`tag`/`area_path`, per-section `stale_business_days`, `template`, `letter` |
| `recipient` | (required) | Alias or email for the consolidated heat-map email (resolved via the shared people directory; `example.com` addresses are rejected) |
| `cooldown_days` | per-edition `full_hygiene.cooldown_days` (schema 2.1 canonical; `hygiene.cooldown_days` fallback-only) | Per-item nudge cooldown (minimum across matching sections), anchored to attested send |
| `comment_fetch_limit` | `100` | Max work items whose comments are fetched per run; overflow renders as tri-state `None` ("not evaluated") |
| `comment_window_days` | `7` | Days within which a comment counts as "recent" for `has_recent_comment` signal |
| `compress_titles_with_ai` | `false` | Enable AI-powered title compression; falls back to word-boundary truncation |
| `status_keywords` | see code | Keywords that must appear in a recent comment to set `comment_has_status_keyword` |
| `risk_on_track_values` | `["On Track", "on track"]` | Risk-assessment field values that count as "on track" (suppresses `has_risk_reason` check) |
| `brand_label` | `"ADO Hygiene"` | Brand name used in the heat-map email subject and header |

> **Legacy backwards-compat shim (deprecated).** Editions may still use `section_a_tag`/`ramp_p1_tag`/`post_ramp_tag`, `stale_business_days.section_a/b/c`, and `area_paths`; `_parse_legacy_shim` (`src/core/nudge_config.py`) translates these into the equivalent three-section `sections:` list and emits a `DeprecationWarning`. New editions should use `sections:` directly. Existing production editions have been migrated to the canonical form.

**Shared ADO config fields:** `organization`, `project`, `area_paths`, `work_item_types`, `excluded_states`, `date_window_days`, `api_timeout_seconds`, optional `proposal_ttl_hours`.

For query-backed runtime state, `programs/<prog>/gather_state.json` is the source of truth for per-query freshness/variance telemetry and operator-visible execution history across Kusto, WIQL, ADO analytics, sprint, pipeline, PR, and dependency surfaces. `kpis.yaml` remains authored config and is never mutated by gather.

**Proposal TTL precedence:**
1. `program.yaml -> ado.proposal_ttl_hours` sets the default expiry for comment and vitality proposals.
2. `programs/<prog>/ado_field_map.yaml -> proposal_ttl_hours` overrides the shared default for `field` proposals only.
3. If neither value is present, the expiry default remains `72` hours.

---

#### 10.4 Chart Configuration Fields

Chart-related fields on `KustoQuery` (edition `queries` list):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `chart_renderer_id` | `str \| None` | `None` | Registered renderer ID (`namespace::name`). Required when `chart_config` is set. |
| `chart_config` | `dict \| None` | `None` | Renderer-specific chart configuration. Activates the registry pipeline. |
| `chart_cache_ttl_hours` | `int` | `26` | Per-query cache TTL in hours. |
| `chart_blocks_publish` | `bool` | `False` | When true, a missing or expired chart cache blocks publish (QG-22). |
| `attachment` | `AttachmentConfig \| None` | `None` | Routes chart to a workstream or exec summary. |
| `fallback_on_empty_rows` | `bool` | `True` | When false, empty Kusto result renders a placeholder PNG. |

`AttachmentConfig` fields: `target` (workstream ID or `"exec_summary"`), `position` (`"after"`, default), `fallback` (`"standalone"`, default).

Edition-level chart settings (`KustoQuerySettings` / `ChartEditionSettings`):

| Field | Description |
|-------|-------------|
| `chart.enabled` | Global kill switch for chart pipeline. Set `VERTEX_CHARTS=0` env var to disable at runtime. |
| `chart.renderer_modules` | Parsed extension hook for future module-based chart registration. Current runtime behavior is built-in auto-discovery from `src/core/charts/*`; this field is not yet merged into the default registry. |


## §11 Command Implementations

### 11.1 Report Pipeline (`src/commands/report.py`)

**Entry:** `report_command(edition, issue, dry_run, reseed, no_seed, diff_mode, send_draft, ai_review, no_ai, as_of, edition_type, lookback_range, stdout, format, verbose)`

`--reseed` is a dry-run-only operator flag that deletes seedable draft narratives and seed-like overrides for the target issue before trusted-baseline seeding runs again.
`--no-seed` suppresses trusted-baseline narrative seeding for the current run so the draft falls back to scaffold templates or existing authored narratives. A `.no-seed` sentinel file in the target issue narrative directory provides the same behavior without changing the command line.

**`generate_report_draft()` — 74-step pipeline:**

| Phase | Steps | Key Operations |
|-------|-------|---------------|
| **Resolution** (1–10) | Load bundle, resolve V2 edition, read archive index, determine issue number, load previous snapshot |
| **Data fetch** (11–13) | Early return for LOOKBACK; fetch ADO items via OData plus best-effort saved-query hydration with persisted ADO integration diagnostics and batched ADO analytics-history backfill; load ETA forecasts |
| **Computation** (14–22) | Build evidence packets, compute deltas, merge overrides, build scorecard packets/data, load signal context, build exec summary |
| **Narrative** (23–33) | Build top items, resolve chapters/workstreams, load narratives, apply focused visibility rules, build workstream snapshot and association preview, resolve blurbs |
| **Freshness** (34–37) | Build freshness report, ensure review status |
| **AI synthesis** (38–40) | Prepare AI context (summaries, signals, drift), generate blurbs + exec summary (Zone B, optional) |
| **Assembly** (41–53) | Build ReportData, workstream data, continuity data, health summary, Kusto sections, vitality snapshot, quality matrix |
| **Rendering** (54–59) | Build citations, email subject/preheader, RenderContext; render HTML + MD (or Deck MD) |
| **Validation** (60–68) | Build snapshot, ban-list check, voice check, verbosity check, hygiene check, manifest build, quality gate evaluation |
| **Output** (69–74) | Classify warnings, build readiness, diff summary, write files, open browser |

**Output files** (under `publications/<edition>/`):

| File | Condition |
|------|-----------|
| `issue_NNN.html` | Non-DECK |
| `issue_NNN.eml` | Non-DECK |
| `issue_NNN.md` | Non-DECK |
| `issue_NNN.deck.md` | DECK only |
| `issue_NNN.snapshot.json` | Always |
| `issue_NNN.draft.json` | Always |
| `issue_NNN.manifest.json` | Always |
| `issue_NNN.workstream_snapshot.json` | Always when workstream memory can be resolved |
| `issue_NNN.workstream_snapshot.md` | Always when workstream memory can be resolved |
| `issue_NNN.workstream_associations.json` | Always when workstream memory can be resolved |
| `issue_NNN.quality_matrix.{md,json}` | Always |
| `issue_NNN.remediation.{md,json}` | Always |

Exit codes: 0 = clean, 2 = warnings, 3 = blockers.

### 11.2 Confirm (`src/commands/confirm.py`)

```python
def confirm_issue(
    edition_name: str, issue_number: int,
    dry_run: bool = False, force: bool = False,
    ack_forecast: bool = False, ack_stale_approval: bool = False,
    ...,
) -> ConfirmResult
```

**Flags:**
- `--force`: override all forceable gates (QG-1, QG-9, QG-24 are forceable=True); use when nudge was sent this cycle and owners haven't yet updated ADO.
- `--ack-forecast`: pre-acknowledge ETA forecast warnings that would otherwise require interactive confirmation mid-confirm; use when the forecast shift is understood and the confirm should be non-interactive.

**Transaction steps:**
1. Load bundle, draft state, overrides
2. Build confirm artifacts (snapshot, HTML, MD, manifest)
3. Evaluate quality gates → blocking vs forced results
4. **Archive write** (inside `ArchiveLock`): stage → promote snapshot, HTML, MD, EML, manifest, overrides copy, review copy, narratives copy, vitality record
5. **Claim extraction:** AI-first claim + decision-ask extraction with governed regex fallback → `claims.jsonl`
6. **Workstream association persistence:** append confirmed draft association records into `programs/<prog>/journal/workstream_associations.jsonl` when present
7. **State reset:** overrides → carry forward dimensions; narratives → scaffold; review status → blank
8. **Edit pattern recording:** diff draft vs confirmed narratives → `edit_patterns.jsonl`
9. **Learning distillation:** summarize corrections into editorial rule proposals
10. **Context snapshot write** (§22 E2): `write_context_snapshot()` records Plane 1 file hashes + `context_maturity_level` at `programs/<prog>/archive/<edition>/context_snapshots/issue_NNN.context.json`
11. **Maturity regression check** (§13.2): `load_context_snapshot()` loads prior issue's snapshot; if `prior.context_maturity_level > current`, emits `⚠ Context maturity regression: LX → LY` warning to stderr with `vertex doctor --context` remediation hint

### 11.3 Gather (`src/commands/gather.py`)

```python
def gather_program(
    program_id: str, *, as_of=None, programs_root=None,
    loader=None, include_workiq=False, include_kusto=False, include_icm=False,
    ...,
) -> GatherArtifacts
```

**Steps:**
1. Load program context (program.yaml, workstreams.yaml)
2. Fetch live ADO items
3. Read current week's existing signals (dedup baseline)
4. Build candidate signals from: ADO revisions, freshness findings, WorkIQ (opt-in), Kusto (opt-in), IcM (opt-in)
5. **Echo-chamber guard:** `dedupe_signals(candidates, existing)` → prevents Vertex's own ADO writes from re-ingesting as signals
6. Write new signals via `append_signal()`
7. **Auto-review:** `_is_auto_approved_signal(signal)` → append `approved` to `reviews.jsonl` with `reviewed_by="system"`. ADO, validated Kusto, IcM, manual, vertex/freshness are auto-approved. WorkIQ and unvalidated Kusto require human review. **FR-SG-38 population auto-approval:** `compute_auto_approval_policies(signals, review_states, min_sample=10, ceiling_rate=0.8, floor_rate=0.2)` — when ≥10 signals from a source have been reviewed at ≥80% approval rate (and >20% floor), all remaining PENDING signals from that source are promoted to AUTO_APPROVED in `persistence_stage.py` during this same gather.
8. **Trajectory updates:** for each item, build trajectory point → `append_trajectory_point()`
9. **Plane 1 changelog** (§22 E1): diff loaded Plane 1 bundle against `changelog/plane1_last_seen.json`; append field-level changes to `changelog/plane1_changes.jsonl`; update `plane1_last_seen.json`
10. **Evidence extraction** (`--extract-evidence` flag, ME-02): if enabled, calls `run_evidence_extraction_stage()` from `gather_pipeline/evidence_extraction_stage.py`. Filters transcript signals, groups by workstream lane, invokes `ContentExtractionAgent` per lane, and writes `WorkstreamEvidence` objects to `programs/<prog>/journal/evidence_store.jsonl` via `persist_evidence()`. Records `EvidenceQualityRecord` (per-lane confidence + extraction count) to `evidence_quality.jsonl` (ME-05). Zone C `AgencyBridge` is injected as a callable; default factory is `_AgencyBridge = AgencyBridge` (lazy import).

### 11.4 Triage (`src/commands/triage.py`)

**Readiness score formula:**
```
score = avg(quality_gate_pass_rate, narrative_score, override_score, signal_score, coverage_score)
```
Where:
- `narrative_score = (written / total) × 100`
- `override_score = (set / total) × 100`
- `signal_score = max(0, 100 - min(100, unreviewed × 10))`
- `coverage_score = max(0, 100 - min(100, gaps × 10))`

### 11.5 Other Commands

| Command | Module | Key Behavior |
|---------|--------|-------------|
| `catchup` | `catchup.py` | Explicit session-change sweep using catchup runner/classifier state; same substrate as the session-start banner |
| `brief` | `brief.py` | Private operator brief over catchup, contradictions, claims, decision asks, and staged interventions |
| `next` | `next.py` | Ranked, read-only next-step suggestions for an edition or static goal recipes from `program.yaml -> goals` |
| `calibration` | `calibration.py` | Forecast/claim calibration reports and operator-facing accuracy summaries |
| `reconcile` | `reconcile.py` | Cross-source contradiction review with refreshable contradiction state |
| `freshness` | `freshness.py` | Build freshness report, group by DRI, optional Teams format |
| `override` | `override.py` | Interactive per-dimension risk editor with evidence display |
| `edit` | `edit.py` | Open narrative file in `$EDITOR`; scaffold if missing |
| `evidence` | `evidence.py` | Show attribution lineage for section/claim/work-item |
| `diff` | `diff.py` | Semantic diff via `publish_diff.py`; modes: `last-draft`, `last-confirmed`, `issue-N` |
| `doctor` | `doctor.py` | Config validation, ADO probe, knowledge integrity, archive check, analytics parity/recovery guidance, semantic-index health. `--context`: validates 20 Plane 1 files against 21 cross-file invariants; reports `context_maturity_level` (L0–L4), violation codes (WS-01…KB-03), staleness flags, and ranked context gaps from `_feedback/context_gaps.jsonl`. `--fix-hints`: appends per-invariant remediation strings from `_FIX_HINT_MAP` and per-file staleness guidance. `--source-waivers`: validates each program's `source_waivers.yaml` against `vertex/policies/source_waivers.schema.yaml` (field presence, allowed values, future expiry dates); reports expired/invalid waivers as ❌ with suggested remediation. `--nudge`: runs NQ-1..NQ-10 nudge hygiene checks (state schema, cooldown validity, draft count, oversized-audit, policy compliance, audience-policy file, circuit breaker, deadline clock, state 1.2 fields, legacy `output/<id>_nudge/` path detection). Storage checks (always): **PO-01** workspace layout — 6 states: OK (canonical `publications/` present, no legacy), WARN (legacy `output/` still present post-migration or label mismatch), ERROR (split-brain: both `output/` and `publications/` exist, aborts), INFO (fresh install or partially migrated). |
| `history` | `history.py` | Browse/search confirmed archive plus local semantic retrieval over archive and incident-journal content |
| `index` | `index.py` | Rebuild or optimize the local semantic archive index |
| `review-sections` | `review_sections.py` | Show/set/clear per-section review state |
| `review-full` | `review_full.py` | Two-pane reviewer HTML: newsletter + evidence/vitality/drift/anticipation |
| `vitality` | `vitality.py` | Per-owner/workstream vitality scores with leakage detection |
| `signals` | `signals.py` | List/review/add/link journal signals; human pending and review surfaces preserve signal confidence alongside source/workstream metadata |
| `nudge` | `nudge.py` | Full-hygiene heat-map EML **draft** (`X-Unsent: 1`, never sent) written to `programs/<prog>/nudge/drafts/{run_id}.eml`; data-driven N-section engine from per-edition `full_hygiene` config; `--approve-draft <draft-ref>` persists operator approval for approval-gated audience diffs; `--mark-sent <draft-ref>` promotes draft to `nudge/published_eml/`; `--import-sent <published-ref>` reconstructs lifecycle state for historical sent EMLs; `--list-drafts` lists available drafts; legacy `--stale-a/b/c` honored as a deprecated shim. Exit 0 success, 2 config/auth/EML failure, 3 degraded |
| `watch` | `watch.py` | Foreground polling loop over ready signal sources with cadence-aware default source selection and readiness validation |
| `propose` | `propose.py` | Seed the next issue and generate per-section revision proposals |
| `review-proposals` | `review_proposals.py` | Render read-only proposal review HTML with optional `--section` filtering and `--resolved-only` history mode |
| `apply-proposals` | `apply_proposals.py` | Accept, reject, bulk-accept, modify, undo, or interactively apply section proposals |
| `claims` | `claims.py` | List/resolve tracked claims and decision asks |
| `actions` | `actions.py` | Action register list/review/resolve surface with grouped meeting-close batch summaries, deterministic recurring cross-meeting pattern detection, and reviewed-batch `--apply-ado` handoff |
| `meeting-close` | `meeting_close.py` | Transcript-to-action closure packet with review/apply, HTML, Teams, action-promotion surfaces, and inline `--apply-ado` handoff |
| `ask` | `ask.py` | Advisory natural-language routing over a bounded deterministic command subset |
| `hints` | `hints.py` | Narrative delta hints: list pending hints, accept/reject/modify per-workstream hints with interactive flow; writes `hints.jsonl` to issue narratives directory. **Path fix (commit 9adee32):** `hints.py` uses `bundle.program.id` (not `edition_name`) for the proposal store path — proposals written to `programs/{program_id}/narratives/issue_{n:03d}/proposals.jsonl` (where `program_id` comes from edition YAML's `program_id:` field). |
| `decisions` | `decisions.py` | Decision register management: `list`, `add`, `resolve`, `aging`, `nudge`; `governance show` renders DFD date, escalation state, LT commitment; `governance edit` updates governance YAML interactively |
| `ado status` | `ado.py` | Area path coverage, orphans, coverage gaps, gather stats |
| `ado propose` | `ado.py` | Generate ADO update manifest (comment/field/vitality_nudge/vitality_tag) |
| `ado apply` | `ado.py` | Apply proposal via locked manifest with concurrency guard |
| `ado reconcile` | `ado.py` | Read-only Vertex vs ADO state comparison |
| `gather` | `gather.py` | Evidence ingestion from ADO/WorkIQ/Kusto/IcM plus optional dependency-scout refresh. `--extract-evidence` flag triggers ME-02 evidence extraction stage post-gather. |
| `enrich` | `enrich.py` | **ME-03.** Standalone evidence enrichment command: `vertex enrich --edition <name> [--since <date>] [--dry-run]`. Loads workstream registry, builds per-lane WorkIQ queries, invokes `AgencyBridge`, parses `WorkstreamEvidence` from AI response, and persists via `persist_evidence()`. Dry-run prints output without writing. `--since` restricts the evidence window (default: 7 days). Idempotent: re-running for the same window updates confidence without duplication. |
| `summarize` | `summarize.py` | AI rolling workstream summaries from approved signals |
| `dependencies` | `dependencies.py` | Proposal-only dependency scout review, accept, and dismiss flow |
| `prep` | `prep.py` | Meeting prep brief for satellite editions |
| `owner-pack` | `owner_pack.py` | Per-owner remediation Markdown |
| `readiness` | `readiness.py` | Readiness fetch/report surface over readiness engine and snapshot state |
| `trust` | `trust.py` | Trust calibration and slice reporting over autonomy audit/history |
| `salience` | `salience.py` | Inspect or reset local author-salience state |
| `config` | `config.py` | Allowlisted governed program-config get/set surface plus explicit schema `validate` / `migrate` commands and claim-extractor promotion checks |
| `policy` | `policy.py` | Promote learned signal-approval proposals into active local policy with autonomy-audit trace |
| `audit archive` | `audit.py` | Archive autonomy-audit history by date or configured retention window; rewrites the active journal, writes yearly `autonomy_audit_archive_<year>.jsonl` files, and rebuilds analytics projection |
| `migrate` | `migrate.py` | File→SQLite migration plus `--rebuild-analytics` projection rebuild path |
| `integration show` | `integration.py` | Show UIL registry status for a channel: registration count, last discovery, scope health, per-channel config |
| `integration discover` | `integration.py` | Run UIL discovery for a program's configured channels; writes `channel_registry.sqlite3`; updates `gather_state.json` |
| `integration list` | `integration.py` | List UIL registrations with filtering by status, workstream, channel |
| `integration confirm/suppress/promote/reassign/signal-yield` | `integration.py` | Governance operations on UIL registrations |
| `integration candidates` | `integration.py` | List persisted source-discovery candidates with `--requires-decision`, `--status`, `--workstream`, `--source-type`, `--json`, and privacy-reveal toggles |
| `integration candidate-accept/reject/reassign/candidate-clear-rejection` | `integration.py` | Govern source-candidate lifecycle, multi-intent disambiguation, and reversible rejection windows |
| `integration intent-suppress/retire/intent-clear-suppression/intent-reopen` | `integration.py` | Govern source-intent lifecycle without silently re-opening retired/suppressed declarations |
| `integration seed-id` | `integration.py` | Create an accepted manual durable-ID binding for one source intent and write the corresponding UIL registration |
| `integration explain-source` | `integration.py` | Render a single source's intent state, attempts, candidates, decisions, and next recommended action |
| `integration ref-id` | `integration.py` | Migrate a UIL registration to a new ref_id (e.g. after Teams thread rotation); atomic DELETE+INSERT with cascading bindings and audit record in `registry_feedback` |
| `integration schema-migrate` | `integration.py` | Idempotent schema migration; `--force` re-initializes empty DB |
| `integration prune` | `integration.py` | Prune retired/suppressed registrations and feedback events older than retention window |
| `integration migrate` | `integration.py` | Migrate `m365_registry.yaml` YAML entries into UIL Teams channel registrations (preserves all governance fields) |
| `integration report` | `integration.py` | UIL channel health report (per-scope, delta history, completeness) |
| `integration feedback` | `integration.py` | Show feedback event history for a channel |
| `integration health` | `integration.py` | Channel health summary across all configured UIL channels |
| `kb changelog` | `kb.py` | Git-based people_directory.yaml change history |
| `kb update` | `kb.py` | AI-assisted KB correction with safety model |
| `setup` | `setup.py`, `setup_preview.py` | Conversational AI-assisted onboarding concierge. State machine in `src/core/setup_state.py`; AI assistant in `src/ai/setup_assistant.py`. |
| `onboard` | `onboard.py` | Interactive program/edition scaffold wizard (power-user path) |
| `deck-companion` | `deck_companion.py` | Markdown deck from draft/confirmed state |
| `archive-journals` | `archive_journals.py` | Move old journal partitions to archive |
| `--skip-issue` | `cli.py` (top-level callback) | Record issue as intentionally skipped in archive index; `--reason` required |
| `hypothesis propose/confirm/update/show/challenge/reject/reinstate/invalidate/from-assumption/quickstart` | `hypothesis.py` | Full L1 hypothesis lifecycle management |
| `assertion add/update/list/export/history/add-evidence-url` | `assertion.py` | TelemetryAssertion CRUD and export surface |
| `observation inject` | `observation.py` | PM-injected manual MetricObservation with optional registry validation |
| `reality digest/snooze/pending-review/challenges/dismiss/reopen` | `reality.py` | Reality reconciliation output, snooze, challenge management |
| `bootstrap` | `bootstrap.py` | Seed L1 state from claims, assumptions, milestones, and KPI query assertions |
| `admin db verify/backup/migrate/compact/relocate` | `db.py` | L1 SQLite DB operations |
| `admin auth setup` | `auth.py` | Auth/token setup |
| `admin reconcile` | `admin_reconcile.py` | Administrative state reconciliation |
| `admin metric list/history/validate` | `metric.py` | Metric registry inspection |
| `admin notifications` | `notifications.py` | Notification state management |
| `admin assertion` | `assertion.py` (admin subcommand) | Admin assertion operations |
| `facts export/import/rebuild` | `facts.py` | Program Fact Store backup/export/rebuild: `export` serializes the store to JSON, `import` ingests a JSON dump and re-persists all revisions, `rebuild` re-persists canonical authored-state from program YAML/JSONL (FR-SG-68) |
| `facts parity-check` | `facts.py` | Compare fact-store families against legacy YAML/JSONL sidecars; report parity ratio per family; families in `_PENDING_ZERO_TOLERANCE_FAMILIES` warn when ratio < 1.0; families in `_ZERO_TOLERANCE_FAMILIES` block when any parity gap exists |
| `facts dual-read-log` | `facts.py` | Run N parity-check cycles in a tight loop, appending one JSONL record per cycle to `fact_store_parity_log.jsonl`; mismatched families also written to `fact_store_quarantine.jsonl`; exit 0 iff every cycle passes — operator uses this to fulfill the §22 Step 4 sustained dual-read shadow window requirement |
| `facts pin-snapshot` | `facts.py` | Pin the current fact snapshot to a confirmed issue via `ProgramFactStore.pin_snapshot()` → `fact_snapshot_pins` table; returns a `pin_id` (`pfs_<hex>`) |
| `facts detect-drift` | `facts.py` | List fact revisions recorded after a snapshot pin; exit 0 on no drift, exit 2 on drift (supports CI guard / pre-publish hook) |
| `connectors poll` | `connectors.py` | Poll read-only non-ADO external dependency connectors (SharePoint Lists, GitHub Issues) via `connector_polling.py`; persists `ExternalDependency` snapshots to `external_dependencies.jsonl`; errors logged and skipped, never gates gather (FR-SG-48) |
| `rollback` | `rollback.py` | Restore program stores (risk register, decisions, actions, chronicle, overrides) from a checkpoint snapshot created by `checkpoint_store.create_checkpoint_snapshot()` before fact-layer promotion (FR-SG-49); `--drill` runs a side-effect-free sandbox simulation (copies workspace to `.rollback_sandbox/<id>_<ts>`, applies most-recent checkpoint, verifies replayability, cleans up) and records an `s7a_rollback_drill` proof entry via `record_platform_proof(...)` |

### 11.6 Next And Watch Contracts

**`vertex next` (`src/commands/next.py`):**
- `suggest_next_steps(...)` is a read-only ranking function that resolves the active issue from the latest confirmed archive entry and inspects the current draft manifest plus overrides.
- Edition mode is intentionally bounded to up to 3 deduplicated suggestions, sorted by hard-coded priority bands rather than free-form scoring.
- The command is advisory only: it emits suggested CLI commands and rationales, but does not write files, mutate review state, or call external providers.
- Goal mode loads deterministic recipes from `program.yaml -> goals`; validation fails if the goal or its step schema is missing or malformed.

**`vertex watch` (`src/commands/watch.py`):**
- `watch_program(...)` is a foreground loop, not a background service. It polls, prints cycle summaries, sleeps for `--interval`, and exits on Ctrl+C or test-bounded `max_cycles`.
- Default source resolution is cadence-aware and conservative: `ado` only unless intraday cadence proves an `icm` path is ready, in which case the default becomes `(ado, icm)`.
- Explicit source selection is gated by readiness validation before polling begins. `workiq`, `kusto`, and `icm` each fail fast with actionable readiness errors when auth/config/provider prerequisites are not met.
- The command shares the governed discovery substrate with gather/watch-scan helpers, so new signals, auto-review records, and trajectory updates are written through the same deterministic stores rather than a separate watch-only path.

---

## §12 AI Layer

### 12.1 Safety Pipeline (`src/ai/_pipeline.py`)

```python
def process_generated_text(
    text: str, *, allowed_items: tuple[WorkItem, ...] = (),
) -> ProcessedAIText  # (text, cited_work_item_ids)
```

**4-stage sequential pipeline:**
1. **PII scrub** (`safety/pii_scrubber.py`): strip non-MS emails, phones, SSNs → `scrubbed_text`
2. **Injection detection** (`safety/injection_detector.py`): regex scan for prompt injection phrases, delimiters, base64, data URIs, webhooks → reject with `AIPipelineError`
3. **Causality sanitization** (`safety/causality_sanitizer.py`): rewrite causal claims ("caused by" → "observed with")
4. **Grounding** (`safety/grounding.py`): every sentence must cite an allowed `WorkItem`; uncitable sentences removed

**Mandatory enforcement:** Every `src/ai/` generation path must call `process_generated_text()`. Direct re-implementation of any pipeline stage inline is prohibited. `tests/contracts/test_ai_safety_pipeline.py` enforces this via AST scan of all `src/ai/` modules — any AI output path that bypasses the wrapper fails the contract test. There is no opt-out path.

**Related, additive, not-yet-wired mechanism:** `src/ai/safety/ai_trace_sanitizer.py` + `ai_trace_capture.py` (§9.17.1) sanitize and optionally (opt-in only) durably capture prompt/response text for corpus-building — a different concern from this pipeline's output-shaping, and not a substitute for it. The full AI Safety Boundary (per-feature `SemanticValidator[T]`, `AISchemaGateway`, fail-closed audit) described in `.archive/specs/arch-fix.md` has not been built yet; this section describes what runs in production today.

### 12.2 AIClient (`src/ai/client.py`)

```python
class AIClient:
    TIMEOUT_SECONDS = 20
    MAX_RETRIES = 2
    INPUT_COST_PER_1K_TOKENS = 0.0002
    OUTPUT_COST_PER_1K_TOKENS = 0.00125

    def __init__(self, deployment, temperature, budget_usd, *, endpoint=None, api_key=None)
    def chat(self, system, user, max_tokens=800, *, prompt_version=None) -> str
```

Budget guard: `_spent_usd >= _budget_usd` → raises `BudgetExceeded`. Cost computed per-call from token counts.

Retry: up to 2 retries with exponential backoff (`0.5 × 2^attempt`). Retryable: `RateLimitError`, HTTP 429, 5xx.

### 12.3 Content Generators

| Module | Input | Output | Max Tokens |
|--------|-------|--------|-----------|
| `blurb_generator.py` | Items, evidence, deltas, editorial rules, program context | `WorkstreamBlurb` (≤160 tokens) | 160 |
| `exec_summary_drafter.py` | Dimension risks, deltas, blurbs, signals, drift | Exec summary text (≤150 words) | 500 |
| `summary_generator.py` | Approved signals, drift patterns, prior summary | Rolling summary (<500 words) | 1000 |
| `anticipation_engine.py` | Readers, signals, drift, summaries | ≤5 anticipated questions | 800 |

### 12.4 Review & Learning

- `draft_reviewer.py`: Three passes — data gaps, leadership questions, structural checks → `DraftReviewReport`
- `edit_learner.py`: Captures before/after edit patterns → `edit_patterns.jsonl`
- `learning_distiller.py`: Repeated corrections → editorial rule proposals

### 12.5 Cost Management

- `cost_guard.py`: Per-edition, per-run budget tracking with ceiling. **D-16 retirement status:** `cost_guard.json` is a backward-compatibility projection only — `_write_atomic_json` must be called from exactly one site (`record_actual`). `_write_sqlite_state` is the authoritative write path; `load_run_states` reads the SQLite ledger before falling back to the `cost_guard.json` file. New cost state must go through SQLite, never additional JSON writes.
- `context_budget.py`: Token-budget management; truncates dated updates/comments to fit LLM windows
- `llm_trace.py`: Every AI call → JSONL (tokens, latency, cost, model, caller)

### 12.6 Prompt Templates

11 versioned `.txt` files in `src/ai/prompts/`: `anticipation_question.v1`, `backfill_extractor.v1`, `exec_summary_drafter.v1`, `intent_router.v1`, `learning_distiller.v1`, `onboard_structure_assistant.v1`, `onboard_style_assistant.v1`, `setup_explainer.v1`, `setup_ws_suggest.v1`, `summary_generator.v1`, `workstream_blurb.v1`

### 12.7 Feature Policy (`ai_policy.yaml`)

**`frontier_eligible` kill switch:** `ai_policy.yaml` carries a `frontier_eligible: bool` field per feature. This is an operator kill switch — not metadata. It is enforced in `deployment_fallback.py::resolve_ai_deployments_for_feature` **before** any AI client object is constructed. When `frontier_eligible: false`, the feature receives only a standard-tier deployment; the frontier-model path is completely bypassed. Setting the flag in YAML is sufficient; no code change is required. This lets operators disable frontier-model exposure for a feature without a code deploy.

**`AI_PROPOSAL_TTL_DAYS = 14`:** Constant defined in `ai_proposal_store.py`. Controls the garbage-collection horizon for pending AI proposals. Proposals older than 14 days are surfaced as expired by `vertex doctor --ai-proposals` and are eligible for automatic pruning on the next doctor run with `--fix`. Changing this constant requires a code change + redeploy.

---

> **UIL note:** M365 channel discovery/hydration now uses UIL providers: `TeamsDiscoveryProvider` + `TeamsHydrationProvider` (`src/m365/`) and `IcMDiscoveryProvider` + `IcMHydrationProvider` (`src/m365/`). All channels share `ChannelRegistryStore` (`src/core/channel_registry_store.py`) as the unified registry, replacing the channel-specific `m365_registry.yaml` during the migration window. `vertex integration migrate` moves existing `m365_registry.yaml` artifacts into the UIL store.

### 13.1 Agency Bridge (`src/m365/agency_bridge.py`)

```python
class AgencyBridge:
    TIMEOUT = 30
    PROBE_TIMEOUT = 5

    def probe() -> AgencyCapabilities
    def ask_workiq(question: str) -> dict | None
    def invoke_mcp_tool(server, tool, args) -> dict | None
```

Allowlisted servers and tools via `_STATIC_ALLOWED_TOOLS` frozensets. `shell=False` always.

`invoke_mcp_tool` uses JSON-RPC 2.0 over STDIO (`_invoke_stdio_mcp_tool`): spawns `agency mcp <server>`, sends `initialize` → `notifications/initialized` → `tools/call`, reads response on stdout. The `--tool`/`--args` flag syntax (Agency ≤2025.x) is no longer supported and was removed in Agency 2026.x.

**Server routing:**
- `bluebird`: ADO/code-search MCP. No M365 personal data tools; static allowlist is empty.
- `workiq`: M365 personal data MCP. Confirmed tool: `ask_work_iq` (NL reasoning agent over the user's full M365 graph — Outlook, Calendar, Teams, SharePoint). Typed-fallback tools (uncertain availability, fail-silently if absent): `search_emails`, `search_teams`, `get_meetings`, `get_transcript`. **Note:** Structured calendar-search and Teams-message-search tools are NOT available in WorkIQ; calendar and Teams discovery uses `ask_work_iq` NL exclusively, with manual `seed-id` as the fallback when NL returns no candidates (see §1.5 in remains.md).
- `ado`: Static allowlist: `get_work_items`, `get_revisions`, `get_comments`, `query_wiql`.
- `icm`: Dynamically allowlisted after probe.

WorkIQ routing supports workstream-scoped prompt generation: `gather.py` composes per-workstream WorkIQ questions from `signal_sources.workiq_keywords` / `workiq_exclude_keywords`, with program-level M365 defaults as fallback when a workstream has not authored signal-source metadata.

#### 13.1.1 WorkIQ Retrieval Contract

`M365Config.retrieval` is a typed `WorkIQRetrievalConfig`; it is deliberately separate from the legacy `m365.workiq` query map. Preview enumeration fields are `discovery_mode` (`legacy_nl | structured_json`), `discovery_union_runs` (1–5), and `discovery_lookback_days` (1–90). Rich-evidence fields are `per_thread_extraction` (default `false`), `per_thread_top_k`, `per_thread_one_hop`, `max_calls_per_cycle`, and `max_wall_clock_seconds`. The corresponding optional workstream enumeration fields are `signal_sources.workiq_discovery_mode`, `workiq_discovery_union_runs`, and `workiq_discovery_lookback_days`; a non-null lane value overrides program policy.

For `structured_json`, `_build_workiq_query_plans` creates a `DiscoveryRequest` with a fixed UTC date window and renders it through `build_structured_discovery_question()`. Union repetitions reuse the exact prompt and bypass `AgencyBridge` memoization after the first call. Targeted email/calendar/Teams plans and all `legacy_nl` behavior are unchanged.

`validate_structured_discovery_payload()` is the fail-closed boundary before signal construction. It recovers CLI presentation wrapping only after ordinary JSON parsing fails; rejects terminal controls, malformed envelopes, transient `turn…search…` identities, unsafe URL hosts, malformed or out-of-window timestamps; normalizes timezone-less ISO values to UTC; bounds output; and deduplicates semantic identities. When no durable identifier exists, Stage-A preview signals may use a SHA-256 identity derived from normalized subject, sender, and timestamp. That fallback is not a durable M365 source binding.

Live qualification uses `scripts/ga_s1_spike.py`, with mailbox identity and lane declarations required at runtime. Captures must resolve under an ignored operator directory; raw M365 content is never committed. Promotion from opt-in requires representative multi-program yield and relevance proof plus resolution of pending-signal aging/review policy.

When explicitly enabled, `workiq_retriever.py` enumerates bounded candidates and extracts one evidence record per canonical thread under call and wall-clock budgets. SHA-256 freshness identity uses algorithm version + conversation id + message count + newest message identity; a cache entry becomes skippable only after parse, privacy, and persistence succeed. Failed, quarantined, stale, or merely retrieved entries are retried.

Rich WorkIQ persistence is a composite safety boundary. `sanitize_workiq_evidence()` scrubs email/phone/SSN fields and quarantines credential patterns or sensitivity-marked content without retaining the offending excerpt. `SourceRef` round-trips `canonical_id` and `extraction_method`. The reader keeps the latest revision per `(lane_id, canonical_source_id)`, applies approval per source, and deterministically aggregates only admitted sources. `VerificationState` distinguishes `unverified`, `model_self_attested`, `human_verified`, and `source_verified`; approval promotes admitted evidence to `human_verified`. Zone A owns models, freshness identity, and aggregation; Zone C owns WorkIQ retrieval; command orchestration owns budgets, privacy, persistence, and review-signal creation.

### 13.2 ADO Writer (`src/m365/ado_writer.py`)

```python
class ADOWriter:
    def apply_manifest(self, manifest_path: Path, *, applied_at=None) -> ADOApplyArtifacts
```

**Locked manifest pattern:**
1. `open_locked_proposal_manifest(path)` → `portalocker` file lock for entire apply
2. Check expiry (72-hour default unless overridden by proposal generation config)
3. Loop pending/failed entries:
   - Re-fetch live ADO state → revision conflict check → dispatch by action type
   - `add_comment`: duplicate detection (Vertex header check), POST
   - `add_tag`/`remove_tag`: compute tag delta, PATCH `System.Tags`
   - `set_field`: current_value conflict check, PATCH field
4. Update manifest after each entry (crash recovery)
5. Auto-log each write as `vertex/ado_update` journal signal

### 13.3 Service Clients

| Client | Service | Via |
|--------|---------|-----|
| `GraphMailClient` | Email search | WorkIQ `ask_work_iq` NL primary + `search_emails` typed fallback |
| `GraphCalendarClient` | Calendar search | WorkIQ `ask_work_iq` NL primary + `get_meetings` typed fallback |
| `TeamsReader` | Teams messages | WorkIQ `ask_work_iq` NL only (no typed fallback) |
| `TranscriptReader` | Meeting transcripts | WorkIQ `ask_work_iq` NL primary + `get_transcript` typed fallback |
| `GraphSendClient` | Email send | Graph API `POST /me/sendMail` (requires admin-consented service principal) |
| `M365Enricher` | Orchestrator | Collects mail/calendar/Teams/transcript per workstream |

### 13.4 Autonomous Source Discovery Contract

Autonomous M365 source discovery is implemented as a persisted UIL sidecar over `channel_registry.sqlite3`, not as ephemeral gather heuristics.

- **Author intent vs. durable binding.** Authored `workstreams.yaml -> signal_sources` entries bootstrap `SourceIntent` rows (`source_intents`) for meeting series, Teams chats/channels, and email threads. Durable provider IDs are attached later through `SourceCandidate` rows (`source_candidates`) plus `candidate_intent_matches`, rather than being treated as required authored config from day one.
- **Providers and ranking.** `gather.py` reuses `registry.py` lookup helpers and the Zone C `WorkIQMailDiscovery` / `WorkIQCalendarDiscovery` adapters, with shared normalization and ranking helpers in `m365_discovery_support.py`. Meeting ranking blends title similarity, topic overlap, workstream-owner overlap, recurrence hints, and bounded transcript-title corroboration when available.
- **Attempt ledger.** Every discovery pass records a `DiscoveryAttempt` row in `discovery_attempts` with provider, query/config hashes, result count, duration, expiry, and outcome (`no_candidates`, `candidates_found`, `ambiguous`, `auth_blocked`, `rejected_candidate_suppressed`, `stale_plan`, etc.). This is the source of truth for `doctor --operator-gates` and `integration explain-source`.
- **Safe auto-resolution gate.** The decision to auto-bind is centralized in the Zone A `discovery_resolution.passes_auto_resolution_gate(ref_kind, ctx)` predicate, which gather consults instead of inlining thresholds. The gate never duplicates adapter scoring (the WorkIQ adapters own ranking); it only governs whether a ranked candidate may auto-resolve. Rules: a candidate must be unique and not recently rejected; an exact match or confidence `>= 0.85` (`HARD_GATE`) auto-resolves; confidence below `0.75` (`SOFT_GATE`) never auto-resolves; in the `0.75–0.85` band, per-source-type corroboration is required — meeting series need workstream-owner overlap, Teams chats/channels need ≥2 non-zero yield windows, and email threads need ≥1 non-zero yield window plus subject/thread continuity. When the gate passes, gather writes the UIL `ChannelRegistration` immediately, marks the candidate `accepted`, and moves the intent to `resolved`; all other cases remain pending for PM review through `integration candidates`.
- **Concurrency guard.** Auto-resolution plans are versioned against `source_intents.decision_version`. If PM/operator action changes the intent before gather commits, gather records `stale_plan` and skips the write so human decisions always win.
- **Anti-overwrite posture.** Gather does not re-open retired/suppressed intents and suppresses rediscovery of recently rejected durable refs for 60 days. Manual `seed-id` bindings remain reversible but are never silently replaced by autonomous discovery.

### 13.5 Registry Promotion Contract

The current M365 route-promotion contract is implemented jointly by `src/commands/gather.py`, `src/commands/registry.py`, `src/core/m365_registry_store.py`, and `src/commands/doctor.py`.

- **Discovery refresh:** `gather` updates `M365RegistryArtifact.signal_yield_last_3`, `high_confidence_streak`, and route confidence on every WorkIQ-backed pass, then computes `promotion_candidates` plus blocked-artifact buckets for gather output and `gather_state.json`.
- **Candidate eligibility:** `describe_current_m365_registry_promotion_blockers(...)` is the canonical gate. An artifact is promotable only when it is not already promoted, has `sum(signal_yield_last_3) >= 3`, has the required durable identifier (`series_id` for `meeting_series`, `thread_id` for `teams_channel` / `email_thread`), has no active recent rejection, and either:
  - is PM-confirmed, or
  - satisfies the bounded auto-promotion confidence gate (`_artifact_meets_auto_promotion_confidence_gate(...)`), which uses confidence plus `high_confidence_streak`.
- **Governed write path:** `vertex registry promote` writes the promoted route into `workstreams.yaml` and marks the registry artifact as `promoted_to_workstreams_yaml=True`. PM-confirmed artifacts that fail the signal-yield gate are rejected with a config error instead of being silently promoted.
- **Observability:** `doctor` publishes `M365 Registry Review` and `M365 Registry Promotion` checks using the same blocker categories, ensuring gather, registry, and health-report surfaces stay aligned.
- **Validation evidence:** Promotion-ready, recent-rejection-blocked, missing-ID-blocked, and low-yield-blocked cases are covered across `tests/unit/test_commands_gather.py`, `tests/unit/test_commands_registry.py`, `tests/unit/test_m365_registry_store.py`, and `tests/unit/test_commands_doctor.py`.

### 13.6 REV — Program-Context Intelligence Pipeline

REV is the M365 collaboration arm of Vertex's program-context intelligence. See [PRD §12.7](vertex-prd.md#127-rev--program-context-intelligence-pipeline) for product requirements. This section covers module inventory, port contracts, and implementation signatures.

#### 13.6.1 Module inventory

**Zone A — `src/core/rev/`** (18 modules):

| Module | Purpose |
|---|---|
| `ports.py` | Typed port interfaces (`CandidateEnumerator`, `ContentHydrator`, `ChangeFeed`, `SemanticChunkRetriever`, `EvidenceVerifier`); `PortResult` union (`Success|Unsupported|Forbidden|RateLimited|Incomplete`); `GOutcome` enum (`complete/truncated_by_budget/provider_limited/failed/unsupported`) |
| `entity_types.py` | `EntityType` enum (message, event, chatMessage, listItem, driveItem) |
| `identity.py` | `HydrationLocator` → `CanonicalItemIdentity` → `SourceRouteIdentity`; `ItemToRouteBinder`; `SearchHitLocator`; cache key derivation |
| `query_planner.py` | `RetrievalIntent`; port-agnostic intent compiled by `MessageQueryCompiler`/`EventQueryCompiler`/`TeamsQueryCompiler`/`SharePointQueryCompiler`/`NLQueryCompiler`; versioned provider capability tables; unsupported restrictions recorded not silently dropped |
| `governor.py` | `BudgetLimits` (all dimensions — search_requests total + per-entity, hydrated_bytes, chunk_count, model_tokens, content_safety_requests, monetized_spend_usd, wall_clock_seconds, concurrency_per_provider, fleet_concurrency_cap, quiet_lane_relevance_threshold); `Governor` (check each dimension; `check_quiet_lane()` for early exit); reuses `src/core/retry.py` + `circuit_breaker.py` |
| `run_state.py` | `RunStage` enum (enumerated → locator_resolved → hydration_required → extracted_ephemerally → excerpts_vaulted → candidate_staged → candidate_verified → accepted); durable-vs-ephemeral distinction; crash-revert of ephemeral stages to `hydration_required`; append-only JSONL; `current_state()` derived |
| `sync_state.py` | `SyncStateStore` — keyed by (tenant_id, principal_mailbox, container, api_version); TTL eviction (default 30 days); LRU ceiling (500 entries); invalidation precedence: apiVersion > token expiry > query-hash; persists as `programs/<prog>/rev_gap_lifecycle.json` |
| `normalizer.py` | Fixed-order pipeline: HTML/MIME strip → quoted-reply removal → PII scrub → **pseudonymize display names (W5-3)** → chunk (500-char overlap, stable `chunk_id`, codepoint offsets); `normalize(known_display_names=...)` accepts a list of person display names from email headers → replaces with `PERSON_N` tokens; `NormalizationResult.pseudonym_table: dict[str, str] \| None` carries the token→original mapping; `merge_chunk_evidence()` (dedupe_core_hash, evidence-ref union, contradiction → consistency_fail assertion) |
| `privacy.py` | `scrub_pii()`, `scan_credentials()`, `run_local_checks()` (fail-closed on credential hit; date-protected phone regex; `scrubber_version="scrub.v1"`); **W5-3**: `PseudonymTable` (bidirectional token mapping, stable PERSON_N assignment, `to_dict()` for storage); `build_pseudonym_table_from_display_names(names)` (multi-word names only); `pseudonymize_text(text, table)` (longest-match-first, word-boundary anchored) |
| `prompt_shields.py` | `LocalOnlyPromptShields` — visible degrade (`VERDICT_UNAVAILABLE`) until Azure Prompt Shields wired; chunk-by-chunk scan design documented |
| `pipeline.py` | `run_rev_cycle()` — Zone A orchestrator; claim→ledger-event shaping (`_CLAIM_TO_LEDGER_EVENT`); priority sort (relevance_score descending); quiet-lane early exit; short-circuit metadata-only/no-claim items; `_drive_gap_lifecycle` gap-fill driver (P2-6); `_finalize_source_file` 3-dir atomicity for local-import |
| `health.py` | `RevHealthReport` v3 — `hydration_fallback_count/rate`, `enumeration_completion` distribution, `pending_queue_age_p50/max_seconds`, `legacy_unverified_count`; inbox-staleness + quarantine telemetry; circuit-breaker alert; `build_rev_health_report()`, `render_rev_health_human()` |
| `inbox_rotation.py` | `rotate_processed_dir()` — moves stale/surplus files from `processed/` → `processed/archive/` (>90 days or >500 files); timestamp-based collision handling; surface-agnostic (duck-typed on enumerator `processed_dir()`); auto-fires at cycle end via `_rotate_processed_best_effort` |
| `quality_metrics.py` | Quality floor gate — `compute_quality_metrics()` measures G-xtract-prec (≥80%), G-accept-prec (≥85%), G-reject-rate, per-event-type recall (≥50%, N≥5 guard → `insufficient_sample_for_gate`), Cohen's kappa (≥0.70 on first 20 dual-labeled items); exits 1 on gated failure; P2-11 judge-independence enforcement |
| `rev_cache_store.py` | `RevCacheStore` — atomic tmp+`os.replace` writes; `MAX_ENTRY_BYTES=5MiB`; `schema_version="rev_cache.v1"`; content-addressed filenames; load returns `None` on corrupt/expired/stale-schema; extraction cache (90d TTL + LRU 500) + judge cache keyed by `(message_id, "rev_judge.v1", ground-truth-hash)` |
| `corpus_export.py` | `export_corpus()` — PII-scrubbed backup bundle: `candidates.jsonl` + `triage_decisions.jsonl` + labeled-corpus copy + optional `evidence_vault.jsonl` + `manifest.json`; direct identifiers (sender SMTP, message_id, principal_mailbox, tenant_id, triage_actor) hash-redacted with display names preserved |
| `result.py` | Result union helpers |
| `__init__.py` | Package exports |

**Zone A ledger — `src/core/ledger/` additions**:

| Module | Purpose |
|---|---|
| `rev_evidence.py` | `EvidenceRef(vault_hash, representation_version, start_codepoint, end_codepoint, excerpt_hash, normalized_source_hash)`; `store_admitted_excerpt()` two-stage vault; versioned `.revmeta.json` sidecar; retention-by-reference |
| `verification_assertions.py` | `VerificationAssertion(candidate_id, resulting_event_id, check_type, status, policy_version, evidence_refs, set_by, set_at)`; append-only store; `effective_verification_state()` derived; `is_candidate_verified()`; `human_pass_assertion()`; legacy migration → `legacy_unverified` |
| `gap_lifecycle.py` | `GapStatus` (open/filling/resolved/reopened); `ContextGapRecord` with append-only transition log; `GapLifecycleStore` (JSON at `programs/<prog>/rev_gap_lifecycle.json`); `CoverageMaturity` + `compute_coverage_maturity()` |

`CandidateEvent` (`candidate_store.py`) extended: `evidence_refs: tuple[EvidenceRef, ...] = ()` + `schema_version = "1"`, backward-compatible readers.

**Zone B — `src/ai/rev/`** (6 modules):

| Module | Purpose |
|---|---|
| `extractor.py` | `DeterministicRevExtractor` (regex) + `LLMRevExtractor` (always-frontier, `{"events": [...]}` output, `--extractor llm`); `MATERIAL_EVENT_TYPES`; `_ground_excerpt` offset grounding; extraction cache integration; `cache_hits`/`cache_misses` counters |
| `judge.py` | `judge_extractions()` — LLM-as-judge harness; `JudgementReport` with per-claim verdicts (CORRECT/PARTIAL/HALLUCINATED); `verify_judge_independence()` (judge model ≠ extractor model); judge cache integration |
| `rev_extractor.py` | WI-4.2 router-adoption anchor — canonical feature-module alias for `extractor.py`; AST anchor for `_REQUIRED_COUNT` ratchet |
| `rev_judge.py` | WI-4.2 router-adoption anchor — canonical feature-module alias for `judge.py`; routes through tiered router |
| `verification.py` | `run_layered_verification()` — `check_quote_span()` (normalized excerpt presence + offset validity) + `check_entity_date_value()` (entity/date/value consistency); entailment/groundedness advisory deferred |
| `__init__.py` | Package exports |

**Zone C — `src/m365/rev/`** (15 modules):

| Module | Purpose |
|---|---|
| `graph_client.py` | `RevGraphClient` Protocol + `FakeRevGraphClient` (in-process KQL parser for testing) |
| `enumerators.py` | `MailboxContext`; `CollectionSearchEnumerator` (Phase-1 mail default, Graph `GET /me/messages?$search=…`, native `message.id`); `SearchApiEnumerator`/`SearchHitLocator` (secondary, opaque `hitId` not assumed = resource_id) |
| `hydrator.py` | `MailHydrator` — uniqueBody→full-body→conversation→attachment ladder; ImmutableId via `Prefer` header at hydration GET; privacy gate before normalize; `LiveMailHydrator` operator-gated |
| `change_feeds.py` | `FakeChangeFeed` (fully controllable in-process); `MailDeltaFeed`, `CalendarDeltaFeed`, `SharePointDriveItemDeltaFeed` — live paths return `Unsupported` with RV-S1 spike ref; **deprecated** (local-import replaces all live paths per ADR-008) |
| `calendar_hydrator.py` | `GraphEvent` model (`seriesMasterId` for series identity); `FakeRevCalendarClient`; `CalendarHydrator`; `LiveCalendarHydrator` (RV-S1-CALENDAR gated) |
| `sharepoint_hydrator.py` | `GraphDriveItem` model; `FakeRevSharePointClient`; `SharePointHydrator` (text-type extraction + binary→metadata_only; §5.4 no-registry-route note); `LiveSharePointHydrator` (RV-S1-SHAREPOINT gated) |
| `teams_hydrator.py` | `GraphTeamsMessage` + `TeamsContext`; `FakeRevTeamsClient`; `TeamsHydrator` (chat + channel paths; mandatory body hydration; no ImmutableId; metadata-only fallback); `LiveTeamsHydrator` (RV-S1-TEAMS-CHAT/CHANNEL gated) |
| `local_inbox.py` | `LocalInboxClaimer` — shared Zone C base: 3-dir atomicity (`inbox/` → `claimed/` → `processed/`); FIFO mtime order; portalocker `cycle.lock` guard; network-drive `OSError` fallback; oversized-file quarantine at claim time; crash-loop guard |
| `eml_enumerator.py` | `EmlEnumerator` — implements `CandidateEnumerator`; scans `inbox/` for `.eml` files; FIFO `(mtime, SHA-256(Message-ID)[:8])` ordering; `Message-ID` absence fallback; `claimed_at_startup_count` + `count_quarantine_files()` |
| `eml_hydrator.py` | `EmlHydrator` — implements `ContentHydrator`; MIME walk (text/plain first, text/html via BeautifulSoup fallback); charset norm; quoted-reply strip via `normalizer.py`; 30s per-file timeout; `body_empty` quarantine; Winmail.dat + `application/*` → `attachment_denied.jsonl`; `message/rfc822` depth ≤3; `unique_body_ratio` safer fallback; 10 MB guard |
| `ics_enumerator.py` | `IcsEnumerator` — implements `CandidateEnumerator`; scans `inbox/` for `.ics` files; SHA-256-based UID fallback; FIFO mtime + SHA-256 secondary sort |
| `ics_hydrator.py` | `IcsHydrator` (P3-1) — implements `ContentHydrator`; requires `icalendar>=5.0`; `_select_primary_vevent` (highest SEQUENCE, RECURRENCE-ID skipped); VALARM/VTODO/VJOURNAL silently skipped; cancellation (METHOD:CANCEL + STATUS:CANCELLED) → `metadata_only`; RRULE expansion ≤52 via `dateutil.rrule`; organizer CN only (no `mailto:` — OA-9 privacy); all-day + TZID→UTC; 30s POSIX timeout; 10 MB guard |
| `local_file_enumerator.py` | `LocalFileEnumerator` (P3-5) — implements `CandidateEnumerator`; dual-surface `.docx`+`.pdf` via two sequential `LocalInboxClaimer` instances sharing same `cycle.lock`; SHA-256 content-addressed logical ID; per-cycle quarantine counts |
| `local_file_hydrator.py` | `LocalFileHydrator` (P3-5) — implements `ContentHydrator`; `.docx` via `python-docx>=1.1` (paragraph+table extraction); VBA macro denial (`word/vbaProject.bin` ZIP check → `macro_denied_count`); `.pdf` via `pypdf` (already core dep); `pdf_no_text` quarantine for image-only PDFs; `pdf_encrypted` Unsupported; `pdf_no_text_count` per-cycle counter; 30s POSIX timeout; 10 MB guard |
| `__init__.py` | Package exports |

**Zone D — `src/commands/`**:

| Module | Purpose |
|---|---|
| `rev.py` | `vertex rev run` — wires ports into `RevPipelineDeps`; `--mock-fixture` P1 path; `--eml-inbox` local-import path; live path exits 2 with ADR-008 ref. `vertex rev rotate-processed` — housekeeping (stale/surplus `processed/` → `processed/archive/`). `vertex rev export-corpus` — PII-scrubbed corpus backup bundle |
| `doctor.py` | `--rev-health` / `--rev-program` (FR-PCI-12/§5.13) |
| `ledger.py` | Profile-gated verification gate (`_rev_verification_gate_active` + `_enforce_rev_verification_gate`): `triage approve`/`triage edit` reject unverified candidates (exit 7) — only active under `rev_verified` profile + `verification_gate_enabled`; no-op for `legacy_nl`/no-rev programs |

#### 13.6.2 Zone A capability port contracts

```python
# src/core/rev/ports.py
PortResult = Success[T] | Unsupported | Forbidden | RateLimited | Incomplete

class CandidateEnumerator(Protocol):
    def enumerate(self, intent: RetrievalIntent, ctx: MailboxContext) -> PortResult[list[EnumeratedCandidate]]: ...

class ContentHydrator(Protocol):
    def hydrate(self, locator: HydrationLocator) -> PortResult[HydratedContent]: ...

class ChangeFeed(Protocol):
    def get_changes(self, state: SyncState) -> PortResult[list[ChangeEvent]]: ...

class EvidenceVerifier(Protocol):
    def verify(self, candidate: CandidateEvent, refs: tuple[EvidenceRef, ...]) -> list[VerificationAssertion]: ...
```

All Zone C implementations implement these Zone A port interfaces. The Zone A orchestrator (`pipeline.py`) never imports Zone C directly — it receives implementations via `RevPipelineDeps` (dependency injection from Zone D).

#### 13.6.3 Evidence metadata schema

Stored as versioned `.revmeta.json` sidecar alongside each vaulted excerpt:

```
schema_version, tenant_id_hash, principal_mailbox_container_hash,
canonical_item_id, canonical_route_id, native_etag, native_change_key,
retrieval_timestamp, normalization_version, scrubber_version,
injection_policy_version, prompt_version, extraction_policy_version,
chunking_version, extraction_model, extraction_schema_version,
content_safety_result, content_safety_policy_version,
human_materiality_policy_version, source_classification, sensitivity_label,
retention_class, purge_deadline, resulting_event_id
```

Fixed-order normalization ensures reproducible offsets: PII scrub → credential scan → HTML/MIME strip → chunk → hash. Retention by reference state (not age): unreferenced → short TTL; rejected → 30 days; pending → 14-day grace; accepted-event evidence → ledger-governed (deleted only via `ledger redact` with compliance reason, QG-DM-4).

#### 13.6.4 Implementation status (2026-06-24)

| Item | Phase | Status |
|---|---|---|
| REV-00 / RV-S1-* | P0 | **OPERATOR-GATED** — no live consent; `vertex rev run` without `--mock-fixture` exits 2 |
| REV-01 Foundation | P1 | ✅ `src/core/rev/ports.py`, `entity_types.py`, `query_planner.py`, `governor.py`, `run_state.py` |
| REV-02 Privacy lifecycle | P1 | ✅ `src/core/rev/privacy.py`, `prompt_shields.py`; two-stage ephemeral→persist lifecycle |
| REV-03 Enumerator + Identity | P1 | ✅ `src/m365/rev/enumerators.py`, `graph_client.py`; `HydrationLocator → CanonicalItemIdentity → SourceRouteIdentity` |
| REV-04 Mail Hydrator + chunk | P1 | ✅ `src/m365/rev/hydrator.py`, `src/core/rev/normalizer.py` |
| REV-05 EvidenceRef + VerificationAssertion | P1 | ✅ `src/core/ledger/rev_evidence.py`, `verification_assertions.py`; `CandidateEvent.evidence_refs` + schema_version |
| REV-06 Structured extraction | P1 | ✅ `src/ai/rev/extractor.py` (deterministic); LLM probe deferred to live P0 |
| REV-07 Layered verification | P1/P2 | ✅ `src/ai/rev/verification.py` + `src/commands/ledger.py` profile-gated gate |
| REV-08 E2E + `doctor --rev-health` | P1 gate | ✅ `src/core/rev/pipeline.py`, `src/commands/rev.py`, `src/commands/doctor.py`, `src/core/rev/health.py` |
| REV-09 Governor enforcement | P2 | ✅ quiet-lane early exit + priority ordering; async concurrency pool deferred to live P2 |
| REV-10 ChangeFeed/delta | P2 | ✅ stubs (`sync_state.py`, `change_feeds.py`); live delta deferred to RV-S1 |
| REV-11 Calendar + SharePoint | P2 | ✅ `calendar_hydrator.py`, `sharepoint_hydrator.py`; live paths operator-gated |
| REV-12 Teams | P2 | ✅ `teams_hydrator.py`; live paths RV-S1-TEAMS gated |
| REV-13 Split measurement full | P2 | ✅ `health.py` v2 metrics; multi-program aggregation deferred to live P2 |
| REV-14 Copilot Retrieval | P3 | ⏳ Deferred to RV-S1(f) license confirmation |
| REV-15 NL/A2A fallback | P3 | ⏳ A2A spike-gated; NL fallback active via `AgencyBridge.ask_workiq` |
| REV-16 Coverage maturity | P3 | ✅ `src/core/ledger/gap_lifecycle.py`; active loop deferred (OS-7) |
| REV-17 LLM extractor + judge | P1/P2 | ✅ `src/ai/rev/extractor.py` (`LLMRevExtractor`); `src/ai/rev/judge.py` (`judge_extractions()`); extraction cache 90d+LRU-500; judge cache; judge-independence enforcement; 34 extractor + judge tests |
| REV-18 Quality floor tooling | P2 | ✅ `src/core/rev/quality_metrics.py` (G-xtract-prec/G-accept-prec/kappa gates); `scripts/rev_quality_check.py` (CLI); pre-commit hook in `.githooks/pre-commit`; corpus export `src/core/rev/corpus_export.py`; processed-dir rotation `src/core/rev/inbox_rotation.py`; 37 tests |
| REV-19 Calendar + local file import | P3 | ✅ `src/m365/rev/ics_enumerator.py`+`ics_hydrator.py` (P3-1, `icalendar>=5.0`); `src/m365/rev/local_file_enumerator.py`+`local_file_hydrator.py` (P3-5, `python-docx>=1.1`); 50 tests |
| REV-20 Authority policy | P2 | ✅ ADR-0006 accepted v1 authoritative event set: `deployment.completed`, `milestone.completed`, `commitment.date_set`, `ownership.changed`. `risk.blocking_milestone`, `deployment.rollback`, `deployment.started`, and `incident.severity_changed` remain detected but non-authoritative in v1. |
| REV-21 SoR flip gate | P2 | ✅ `evaluate_family_flip_gate()` plus cycle-integrity accounting and family divergence guard; runs only on complete cycles and requires 5 clean cycles before a family can flip to primary. |
| REV-22 Deliverable/incident Phase 2 scaffolding | P3 | ✅ Models and project/load stubs exist for deliverable and incident entries. They are not v1-authoritative until Phase 2 adds policy, accessors, and promotion evidence. |
| REV-23 ProgramReality read-path overlays (S-8a/c/d) | P2 | ✅ Milestone (S-8a, `MilestoneStage`), commitment (S-8c, `commitment_store.load_commitment_entries`), and workstream/ownership (S-8d, `program_fact_store.load_current_workstreams`) each project from `ProgramReality` when their family SoR mode is non-legacy, with graceful legacy fallback + WARNING on failure. Covers 3 of 4 v1-authoritative families; `deployment.completed` rides the same `workitem.state` family as milestone/ownership. Production authority promotion still awaits S-9e corpus certification. |
| REV-24 Corpus G-floor + faithful typing | P2 | ✅ Deterministic extractor clears G-xtract-prec (86.7% ≥ 80%) and G-accept-prec (100% ≥ 85%) on the preliminary extraction population; status-table "Done" false-positive guard (`_is_status_table_cell`) + R2 faithful event types (`deployment.rollback/started`, `incident.severity_changed`) landed. Corpus is preliminary (single annotator, κ=null); certification (S-9e) is operator-paced. |
| REV-25 Real-data activation proof | P2 | ✅ **2026-07-07.** The activation sentence — *a human-approved fact from a real EML appears, cited and reverse-linkable, in the next real render* — fired for the first time on a real pilot program's data. Five previously-unexercised bugs were found and fixed on first real contact (see §13.6.7). `scripts/verify_activation.py --program <id>` (the executable activation-readiness gate, `.archive/specs/activation.md`) converges to 51/56 checks passing; the remaining 5 are the operator-paced gates in §13.6.5/`specs/backlog.md`, not engineering. |

**Test coverage (2026-07-07):** 737+ REV-focused and platform test files; the 2026-07-07 fixes (§13.6.7) added focused regression coverage for the candidate-store outbox init, the milestone bridge stub fallback, the fact↔record entity-ref join, bridge lineage propagation, the counterfactual-proof harness, and the read-time approval-event-id join — all green with zero regressions across the lineage/reality/bridge/commitment/workstream suites.

#### 13.6.5 Accepted decision gates (ADR-0006)

`governance/decisions/0006-consolidated-human-decision-gates.md` is accepted. The machine-readable guard is `src/core/consolidated_gate_approval.py`; `python scripts/audit_consolidated_decision_gates.py --program <id> --require-accepted` must exit 0 before any ADR-gated mutation/authority work is treated as production-ready.

Accepted technical values:

- `security_profile = pilot-local`
- `automation_scope = automatic_after_deposit`
- NCFL v1 writeable target stores: `assumptions`, `decisions`, `milestones`, `risk_register`, `workstreams`, and `knowledge_doc` (Zone B synthesis path, Amendment A1)
- v1 authoritative REV event types: `deployment.completed`, `milestone.completed`, `commitment.date_set`, `ownership.changed`
- deterministic extractor remains production default until the S-9 corpus proves LLM precision/recall and judge independence
- `source_authority.yaml` remains the source of truth for authority families and `sor_flip`
- NCFL apply reuses beta outbox/idempotency plus a minimal recoverable apply-state journal

**Remaining operator-paced gates — see `specs/backlog.md` for the full, executable-gate-mapped backlog. Summary:**

- **BL-A2 (corpus certification, was S-9e):** **update 2026-07-07 — structurally data-constrained for a single pilot program, not an annotation-labor backlog as originally scoped.** `reachable_document_count=72` for the keystone family measures documents that might mention the family broadly, not real extraction-sourced yield of the specific certifying claim; a direct audit of the candidate store found only 1 real extraction-sourced `milestone.completed` instance (and single-digit counts for the other two accessor families) across the pilot's entire real history — Wilson-CI math confirms ≥25 such instances are needed even with perfect labels, which a single program's real event frequency cannot supply in a practical timeframe. `recommended_v1_authoritative` is accepted as the durable operating tier for single-program deployments; full statistical certification is deferred until fleet rollout (≥3 programs) makes pooled dual-annotation viable. See `specs/backlog.md` §3 for the full analysis.
- **Q7 (production extractor):** no promotion decision possible until BL-A2 certifies — now understood to be a fleet-scale precondition, not a near-term annotation task. The deterministic extractor already clears G-floor (G-xtract-prec 86.7%, G-accept-prec 100%) on preliminary data, so the LLM is not required for the precision floor; the LLM deployment (`gpt-5.4-mini`) is separately confirmed working on real mail as of REV-25.
- **BL-A1 (Azure Content Safety, was S-10a):** **update 2026-07-07 — provisioned and verified live** (a real `AzurePromptShields` call against the configured endpoint returned a genuine `clean` verdict; the `_resolve_shields()` wiring correctly activates it when configured). The remaining `is_clean_cycle()`/AG-3 authority-ladder gate (§13.6.7) is now purely wall-clock-gated on new real inbound mail arriving — all currently-known real documents have already been consumed by prior cycles. Not a config/engineering gap; revisit opportunistically as new real mail is exported.

#### 13.6.6 SharePoint §13.5 route binding (REV-G4a ✅ identity layer; REV-G4b ☐ registry promotion)

**Identity layer (REV-G4a ✅ closed 2026-06-24):** `normalize_site_library()` in `src/core/m365_identifiers.py` (strips scheme+host, URL-decodes, lowercases) resolves `ItemToRouteBinder.bind(DRIVE_ITEM, …)` which previously raised `IdentityResolutionError`. `DRIVE_ITEM` is in `REGISTRY_ROUTE_ENTITY_TYPES` (`src/core/rev/entity_types.py`) with a `_route_for` DRIVE_ITEM case in `src/core/rev/identity.py`. 69 SharePoint + identity contract tests pass.

**Registry promotion (REV-G4b ☐ deferred to Phase 3 / P3-6):** the §13.5 registry (`src/core/m365_registry_store.py`) still accepts only `meeting_series`/`teams_channel`/`email_thread` route types — a SharePoint/`DRIVE_ITEM` promotion branch + `M365RegistryArtifact.site_library` field is not yet added. SharePoint-sourced `CandidateEvent`s enter Workflow C (fact discovery via triage) but cannot promote via §13.5 until P3-5 produces live `DRIVE_ITEM` candidates and P3-6 adds the registry branch. Tracked in `.archive/specs/gaps.md` (REV-G4b).

#### 13.6.7 Activation — real-data proof, hardening contracts, and self-verification (2026-07-07)

REV's engineering scope was believed complete after Waves 1–7 (§13.6.4), but no REV-derived fact had ever been carried end-to-end through approval → fact-store bridge → `ProgramReality` → render on real data. A dedicated activation effort (`.archive/specs/activation.md`, ACTIVATION-1 v1.0→v1.29, now archived — its remaining operator-paced work lives in `specs/backlog.md`) forced that slice and is the authority for this subsection.

**The activation sentence** (the one falsifiable acceptance test for "REV facts are real, not just engineered"): *a fact Vertex detects from a real source EML, after a human approves it, appears — cited and reverse-linkable to that EML — in the next real render, and demonstrably changes what it says.* Proven true for the first time on 2026-07-07: a human operator reviewed a real `milestone.completed` candidate (extracted from a real pilot program's email) against its exact vaulted source excerpt, approved it via `vertex ledger triage approve`, and a counterfactual render diff (render the section with vs. without the fact) showed a non-empty, attributable delta carrying the fact's `source_document_key`. The durable proof artifact format is `output/<program>-ag1-counterfactual.md`, generated by `scripts/verify_activation.py --write-counterfactual-pair --fact-id <id> --counterfactual-diff <path>`.

**Five code gaps closed on first real contact** (none were caught by the existing 700+ test suite, because the exact real-data path had never been exercised; all now have regression coverage):

1. **Candidate-store schema drift.** `triage approve`/`triage batch-approve` (`src/commands/ledger.py`) never called `init_candidate_db()` before writing to the `projection_outbox` table, so any `candidates.db` created before the S-1 outbox migration landed crashed on approval. Fixed by calling `init_candidate_db(db_dir)` (idempotent, `CREATE TABLE IF NOT EXISTS`) at both approval call sites.
2. **Milestone stub `target_date`.** `_bridge_milestone_payload`'s `milestone.completed.v1` handling (`src/core/ledger/fact_bridge.py`) left `target_date: None` on the synthesized stub milestone when no prior `milestone.created.v1` event exists — the norm for REV/email-derived completions, which rarely have a paired formal lifecycle-creation event. The read side (`_milestone_from_fact`, `src/core/program_fact_store.py`) requires a non-null `target_date` and raised, so `ProgramReality.load()` crashed for the **entire program** the moment one real REV milestone bridged. Fixed with a fallback to the completion date.
3. **Fact↔record join used the wrong key.** The join in `ProgramReality.load()` (`fact_by_type_and_ref`, `src/core/program_reality.py`) indexed facts by their prefixed `entity_refs` (e.g. `"MILESTONE:milestone:abc"`) but looked records up by their bare `id` (`"milestone:abc"`) — silently dropping `fact_id`/`lineage` for **every** REV-bridged fact type (milestone, risk, dependency, workstream, decision, assumption), not only milestones. Fixed by also indexing the unprefixed suffix.
4. **`source_document_key` never wired.** `build_bridge_fact_input` (the shared helper behind every bridge appender, `src/core/ledger/fact_bridge.py`) never set `source_document_key` on the resulting `ProgramFactInput`, so no bridge-appended fact of any family could satisfy the reverse-lookup citation contract. Fixed to derive it from the originating event's `source_ref` via `src.core.ledger.source_refs.source_document_key()` (best-effort — never blocks the write on a malformed/unsupported `source_ref`).
5. **Counterfactual-proof harness passed the wrong assessment type.** `src/commands/counterfactual_render.py` (moved from `src/core/` — see below) passed raw `FactAssessment` objects where `_build_deck_milestone_rows` (`src/commands/report_deck.py`) requires computed `MilestoneAssessment` health objects (`assess_milestone_health()`), raising `AttributeError` that was silently caught and returned as an *empty* render both with and without the fact — masking every counterfactual attempt as "no diff" regardless of whether the underlying data was correct. Fixed to compute real `assess_milestone_health()` results per milestone. **Also relocated** `counterfactual_render.py` from `src/core/` to `src/commands/`: it depends on `src/commands/report_deck.py`'s row builders, and `src/core/` must never import from `src/commands/` (a pre-existing zone-boundary violation this session's contract-test sweep caught).

A sixth gap — **`approval_event_id` reverse-lookup** — required a different approach: the REV bridge fires synchronously while persisting the resulting domain event, before the separate `discovery.candidate_approved.v1` audit event exists, so no bridge appender can know the approval event id at *write* time. Closed at *read* time instead: `ProgramReality.load()` now joins every bridge-appended fact's `lineage.domain_event_id` against `CandidateDecisionRecord.resulting_event_id → .approval_event_id` (already recorded by every `ledger triage approve`, historical and new), covering both the generic domain-object assessment list and the separately-assembled `commitments_assessed` (S-2a). This closes the citation reverse-lookup contract fully: source EML **and** approval event, not source EML alone.

**Hardening contracts introduced by the activation effort** (all implemented and covered by `scripts/verify_activation.py`'s self-test):

- **`is_clean_cycle()`** — the contract a REV cycle must meet to count toward the family authority ladder: `cycle_status == complete` ∧ `¬shield_degrade` ∧ `¬extraction_degraded` ∧ no terminal failures ∧ `enumerated ≥ 1` ∧ `candidates_staged ≥ 1` ∧ a real EML was present. An empty `acquisition_complete` cycle does **not** count, closing a gaming vector where a no-op cycle could accumulate toward the 5-cycle flip requirement.
- **Cycle classes** — *publication-valid* (completed; may be `extraction_degraded`) ⊂ *quality-valid* (LLM used, no fallback) ⊂ *authority-valid* (quality-valid ∧ clean). Only authority-valid cycles count toward a family's shadow→primary flip.
- **Composite privacy gate (`_projection_privacy_gate`)** — re-runs `run_local_checks` on every `OPERATOR_CONFIRMED` (ACCEPTED) fact at the bridge chokepoint, fail-closed on a credential hit, in addition to the existing ingest-time check.
- **Entity resolution wiring** — `_stage_candidates` resolves person/owner refs via the program `EntityRegistry` at staging time; orphaned refs are marked `UNRESOLVED` rather than staged as an empty tuple.
- **Cross-source conflict detection wired** — `detect_corroboration_and_conflicts` is invoked by `_run_cross_source_conflict_check` in the REV cycle finalize path, writing `fact.conflict`/`fact.corroboration` with a `counter_source_as_of` timestamp when an EML-derived fact and a store-authored fact share an entity key and disagree.

**Self-verification tool.** `scripts/verify_activation.py --program <id> [--json <path>] [--markdown <path>] [--write-counterfactual-pair --fact-id <id>] [--write-corpus-freeze] [--self-test]` is the executable, commit-SHA-stamped, dirty-worktree-aware source of truth for activation readiness — it regenerates the family/accessor matrix, the `is_clean_cycle()` evaluation, the counterfactual-diff proof, and the corpus certification/freeze state on demand, so activation status can never silently drift from the working tree. As of commit `4814058`: **51/56 checks PASS**; the remaining 5 are `specs/backlog.md`'s BL-A1/BL-A2 (external Azure Content Safety provisioning + human corpus annotation labor) — zero known code gaps.

#### 13.6.8 Newsletter read-path closure (2026-07-08)

The activation effort (§13.6.7) proved REV facts flow end-to-end for the milestone family. A dedicated follow-up spec (`specs/fix-data-flow.md`, v1.0→v1.13, now archived to `.archive/specs/fix-data-flow.md`) closed the same gap for every other family the newsletter can plausibly render, plus five platform-hardening tracks the investigation surfaced along the way. All 13 tracks (A–M) are closed; this subsection is the durable technical record.

**Track A — bridge default-on (`ADR-0011`).** `fact_bridge_enabled` now defaults to `true` at both the dataclass level (`RevRetrievalProfile`, `src/core/models_v2.py`) and config-parsing (`_parse_rev_profile`, `src/core/edition_resolver.py`); an explicit `false` in `program.yaml` still wins. The previously-silent `PASSTHROUGH` disposition branch now logs at `debug`; `vertex doctor --fact-bridge` proactively WARNs when a REV-configured program's bridge is disabled, and a reactive stderr warning fires the moment a bridgeable event is actually silenced. Skipped/failed bridge events append to `programs/<id>/ledger/bridge_failures.jsonl` (`src.core.jsonl_utils.append_jsonl_line`), backing a bridge-failure-backlog doctor check.

**Track B — `risk_stage.py` and dependency onto `ProgramReality`.** `RiskStage` (`src/core/stages/risk_stage.py`) SoR-gates on `resolve_family_sor_mode(program_id, "judgment", ...)` — `judgment` is risk/decision/assumption's shared authority family per `source_authority.yaml`'s `family_map`, not an invented per-fact-type name (a load-bearing correction discovered mid-implementation; see the migration-protocol doc below). `StageContext` gained `risk_assessments`/`risk_lineage` fields (`src/core/pipeline.py`). Dependency's SoR gate lives inside `milestone_stage.py` on the same `resolve_family_sor_mode(program_id, "workitem.state", ...)` milestone already uses — direct investigation found the main newsletter has no standalone dependency section; dependency only feeds `MilestoneStage`'s `build_critical_path(...)`. The platform's first-ever trust-badge rendering markup, `templates/partials/truth_badge.j2` (reusable macros for the 5-level truth vocabulary, `[DISPUTED ⚠]`, and the unconfirmed-sources footer), is implemented as **inline-style `<span style="...">` elements, not CSS classes** — a deliberate, verified deviation from [vertex-ux-spec.md](vertex-ux-spec.md) §3.6b/§3.6c's literal `<span class="...">` prose, matching this codebase's actual Outlook-safe convention (no `<style>` blocks anywhere in the render pipeline). Wired into `health_banner.j2`/`continuity_exec_summary.j2` (risk) and `milestone_rows.j2` (milestone). An empty-set cross-check and a visible render-time fallback banner (not a silent one) cover two new bug classes risk's migration surfaced that milestone's original pattern didn't need. `tests/contracts/test_newsletter_reads_through_program_reality.py` is an AST-based durability guardrail against silent regression to a direct `load_program_facts()` call.

**Track B.5 — `sor_gated_family_load()` helper.** Extracted to `src/core/stages/sor_gated_load.py` and refactored into `risk_stage.py` (the mandatory gate for Track C — at least one family must call it before family migration continues). `docs/contributing/migrate-fact-family.md` is the required protocol document; its most load-bearing section is "find the fact type's real authority family first" (per `source_authority.yaml`'s `family_map`), codifying the judgment/workitem.state correction so it cannot be silently re-forgotten by whoever migrates the next family. The bridge-idempotency assertion is parameterized across 6 of 7 appenders (risk/decision/assumption/milestone/dependency/workstream; commitment's differing signature is left for whoever migrates commitment).

**Track C — per-family disposition (assumption migrated; four families found to have no read path).** `report_lookback.py`'s assumption-lifecycle summary is a genuine main-newsletter (lookback archetype) read path — migrated onto `ProgramReality.assumptions()`, gated on `judgment` (shared with risk), with badges/footer/fallback banner. Direct, non-assumed investigation of the remaining four originally-scoped families found: **action** — `action.item` only feeds the deck's issue-projection logic, no standalone newsletter section; **decision** — no current main-report-HTML reader (deck-only plus diagnostic surfaces); **commitment** — current readers are the dedicated `vertex commitment` CLI/store surface, not the main report; **workstream** — the fact type is already SoR-gated at the fact-store layer independent of this work, but the newsletter's visible "workstream" content is dimension/narrative-driven prose, not a direct per-fact section, so migrating it needs a larger render-model change than a stage-wiring mirror of risk/assumption (deferred, not forced).

**Track D — incremental projection wired as an automatic write hook (`ADR-0010`).** `project_events_incremental_to_sqlite()` now fires from `_persist_event`/`_persist_events` (`src/commands/ledger.py`) on every append, reusing the manual `vertex ledger replay` path's event-loading shape — no separate manual rebuild step. Concurrency mechanism: **WAL mode only** (evaluated first per the mandated order; 25 two-thread concurrent-rebuild trials, 50 hook invocations, zero `database is locked` errors — a file mutex was not needed). Opt-out: `VERTEX_DISABLE_AUTO_PROJECTION_REBUILD=1`. A coupled `operator.field_lock`/`unlock` incremental-fold bug (wrong-entity mutation during incremental, not full, refresh) was found and fixed in `src/core/ledger/program_views.py` as a direct consequence of exercising the incremental path automatically for the first time. `tests/contracts/test_incremental_projection_write_hook.py` covers full-rebuild triggers, concurrent-append-vs-read, and concurrent-append-vs-append. Step 2 (`ProgramReality.load()` reading the on-disk projection instead of re-folding in memory) remains conditionally deferred — see `specs/backlog.md` BL-B2.

**Track E — render-pipeline call-site consolidation.** Of 7 confirmed `ProgramReality.load()` call sites feeding the render pipeline: 3 consolidated (`report_deck.py`'s risk/decision/assumption row-builders now accept an optional pre-loaded `reality: ProgramReality | None`); 1 consolidated conditionally (`report_health.py`'s fallback branch); 2 deliberately kept separate with inline documentation (`report_scorecards.py`'s cross-program dependency BFS — inherently multi-program; `report_lookback.py`'s historical `as_of` load, now correctly threading `as_of=as_of`); 1 resolved via reuse (`render_stage.py`'s former independent risk loader now reuses `ctx.risks` when populated by `RiskStage`). `DeckRiskRow`/`DeckDecisionRow`/`DeckAssumptionRow` (`src/core/deck_renderer.py`) and `HealthSummary`/`AssumptionLifecycleRow` (`src/core/view_models.py`) gained additive `evidence_truth_level`/`evidence_disputed`/`evidence_stale` fields populated from `FactAssessment` instead of discarded. A broader Track J audit later found 9 more `.record`-stripping instances outside the render pipeline proper (`report_ai.py`, `report_lookback.py`, `report_scorecards.py`, `risks.py`, `triage.py`) — each is now annotated with an explicit `# .record strip is intentional` comment recording why that call site's consumer only needs structural fields, not `FactAssessment` metadata (CLI/triage text output, cross-program BFS, CSV export back-compat), rather than being silently left ambiguous.

**Track F — regression tests, six first-contact bugs.** `tests/unit/test_first_contact_regressions.py`: one test per §13.6.7 bug (candidate-store schema drift, milestone stub `target_date`, fact↔record join, `source_document_key` wiring, counterfactual-harness assessment type, `approval_event_id` reverse-lookup) plus one bridge-idempotency test, each with the minimal failing input stated inline.

**Track G — doc reconciliation.** The structured `<!-- spec-posture -->` machine-readable block (parsed by `scripts/check_spec_drift.py` `p12-posture-block`) replaced phrase-matching in [vertex-prd.md](vertex-prd.md) §1.1; adversarial tests cover missing-block, contradiction, and malformed-line cases. `scripts/verify_spec_citations.py` samples and re-resolves `file:line` citations across specs against the current tree — added specifically because this spec's own citations drifted across its 13 revisions and required repeated manual re-verification.

**Track H — `--pipeline-v2` removal.** The CLI flag, its `typer.Option`, and the `pipeline_v2`-vs-default branch removed from `src/commands/report.py`. `generate_report_draft_v2` is now a thin delegating alias (full deletion would have required rewriting ~30 existing fixture-referencing tests, out of scope for housekeeping).

**Track J — AI-narrative reality-substrate sourcing.** The audit (PR-11a) found the AI exec-summary/blurb drafters' only program-narrative input was `bundle.program_context` (`NarrativeProgramContext`, sourced from `narrative_context.yaml`) — a third representation carrying zero `truth_level`/`disputed`/`stale` concept, distinct from both the legacy path and `ProgramReality`. The implementation (PR-11b, `src/commands/reality_context.py`) closes this: `build_reality_assessment_summary()` condenses `ProgramReality`'s risk/decision/assumption/milestone/dependency facts into a token-bounded (`REALITY_CONTEXT_MAX_TOKENS = 900`) summary — disputed facts first, then stale, then low-truth, then a representative sample — rendered as compact per-fact lines (`◆ risk: title [status] [DISPUTED]`) via `reality_context_lines()`. Wired into `report_ai.py`'s exec-summary and workstream-blurb prompt assembly (`ai_context.reality_assessments`). No raw `FactAssessment`/evidence/lineage payload is ever serialized into a prompt — the explicit R18 mitigation this spec's design review required. `tests/unit/test_reality_context_track_j.py` covers the condensation, prioritization, and token-ceiling logic.

**Track K — substrate health monitor.** `vertex doctor --storage` gained: a gather-freshness check (WARNs when the most recent gather run is >24h old or never recorded, via the `run_telemetry.jsonl` sidecar); a per-issue render manifest recording `family_read_paths` (`validation_stage.py`'s `_build_manifest_metadata`, reusing the existing `RunManifest.metadata` field) plus a SoR-vs-manifest consistency check comparing the latest confirmed issue's recorded read paths against what `resolve_family_sor_mode` resolves to right now; the pre-existing fact-store location/schema-version consistency check (detects PS-14's stray-database symptom) plus a loud `CRITICAL` log on `reality_store.py`'s previously-silent path-fallback (the root cause); a path-resolution-determinism contract test. `governance/runbook.md` §9 documents the multi-database cleanup procedure, naming `xpf`'s own known stray `~/.vertex/xpf/vertex.sqlite3` as the first real application (cleanup itself is operator housekeeping — see `specs/backlog.md` BL-B1).

**Track L — fact-schema deserialization safety net.** `run_fact_deserialization_doctor` (`src/commands/doctor_checks/fact_store_flip_checks.py`, `vertex doctor --fact-deserialization`) loads each supported fact family and runs it through the same projector function the report pipeline uses, failing loudly and pointing at `vertex admin fact-store migrate-legacy-state` on a deserialization mismatch. A schema-precondition helper is proven against a synthetic 23-column pre-lineage fixture reproducing PS-14's exact stray-database symptom. Scoped explicitly to persisted-mirror rows, not transient read-time shim facts (a distinction PS-11's investigation established).

**Track M — bridge privacy-gate coverage.** Confirmed, not built: `_maybe_bridge_event_to_fact_store`'s composite privacy gate (`_projection_privacy_gate`, AG-11) already runs once, generically, on every `OPERATOR_CONFIRMED` envelope before dispatch to any family's appender — risk and dependency were already covered by the existing architecture. `tests/unit/test_bridge_privacy_gate_risk_dependency.py` proves this directly for risk/dependency payload shapes.

**New modules:** `src/core/stages/sor_gated_load.py` (Track B.5), `src/commands/reality_context.py` (Track J), `scripts/verify_spec_citations.py` (Track G), `templates/partials/truth_badge.j2` (Track B). **New ADRs:** `governance/decisions/0010-incremental-projection-rebuild-on-write.md` (Track D), `governance/decisions/0011-fact-bridge-default-on.md` (Track A). **Full suite:** 6922 passed, 901 skipped, 0 failed. `scripts/verify_activation.py --program xpf`: unchanged at 51/56 (the 5 remaining are `specs/backlog.md`'s BL-A1/BL-A2, pre-existing human/external gates, not affected by this work).

---

## §14 Quality Gates

### 14.1 Gate Registry

```python
def evaluate_phase_1a_gates(ban_list_violations, verbosity_violations, manifest, expected_snapshot_hash, dimension_risks) -> QualityGateReport
def evaluate_phase_1b_gates(freshness_report) -> QualityGateReport
def evaluate_phase_1c_gates(hygiene_warnings, review_status, review_required, archive_inconsistencies) -> QualityGateReport
def evaluate_continuity_gates(html_content, issue_number) -> QualityGateReport
def evaluate_bridge_gates(continuation_contract, narratives, review_status, bridge_graduated=False) -> QualityGateReport
def combine_gate_reports(*reports) -> QualityGateReport
def evaluate_chart_gates(kusto_sections, chart_enabled=True) -> QualityGateReport
```

| Gate | Check | Forceable | Exit Code |
|------|-------|-----------|-----------|
| **QG-4** | Ban-list violations = 0 | **No** | 3 |
| **QG-5** | Verbosity violations = 0 | **No** | 3 |
| **QG-6** | Manifest exists AND snapshot_hash matches | **No** | 3 |
| **QG-8** | No dimensions with `RiskLevel.UNKNOWN` | **No** | 3 |
| **QG-13** | All active `RiskLevel.HIGH` items have signal or narrative coverage | **No** | 3 |
| **QG-1** | `freshness_report.blocks == 0` | Yes | 2 |
| **QG-2** | Hygiene warnings = 0 | Yes | 2 |
| **QG-3** | `review_status.all_approved` | Yes | 2 |
| **QG-7** | Archive index consistent | Yes | 2 |
| **QG-9** | No overdue target dates on non-terminal items | Yes | 2 |
| **QG-10** | High-risk items with ADO state changes since last confirmed issue have authored narrative coverage | Yes | 2 |
| **QG-11** | No contradictions between current ADO state and prior-issue claim assertions | Yes | 2 |
| **QG-12** | Scorecard dimensions at High risk for ≥3 consecutive confirmed issues have escalation narrative coverage | Yes | 2 |
| **QG-17** | Workstreams with multiple contradiction packets must acknowledge at least one contradicted work item in their authored narrative | Yes | 2 |
| **QG-18** | Rendered email HTML must remain Outlook-safe: no `<style>` blocks, no flex/grid inline layout, table styling stays inline, and inline hex colors stay within the canonical palette | Yes | 2 |
| **QG-14** | High-risk scorecard dimensions have a next-best-action item or explicit override note | Yes | 2 |
| **QG-15** | Open actions in signal journal have both an owner and a due date | Yes | 2 |
| **QG-16** | Milestones with linked risk-register entries have a `TrajectoryStore` entry in the last 14 days | Yes | 2 |
| **QG-19** | Cross-program dependency cascades detected from unresolved dependencies must carry an explicit resolution plan | Yes | 2 |
| **QG-B1** | Prior trusted section roster preserved or explicitly retired via overrides | Yes | 2 |
| **QG-B2** | Prior trusted scorecard composition preserved or explicitly revised via overrides | Yes | 2 |
| **QG-B3** | Seeded narratives differ from trusted baseline or have explicit section approval | Yes | 2 |
| **CG-01–09** | Continuity layout structural checks | No | 2 |
| **QG-20** | Chart freshness advisory: cache age > TTL (warn only) | Yes | 2 |
| **QG-21** | Decoded PNG size ≤ 102400 bytes (100 KiB) per chart | **No** | 3 |
| **QG-22** | Chart publish-blocking freshness: cache too stale when `chart_blocks_publish=true` | **No** | 3 |
| **QG-23** | Exec summary semantic similarity to ADO evidence below 0.82 threshold (soft warn) | Yes | 2 |
| **QG-24** | Metric injection failure — one or more KPI sections failed to render data (soft warn) | Yes | 2 |
| **QG-25** | Email signal yield zero across 3+ consecutive gather cycles for active workstreams (circuit breaker) | Yes | 2 |

**`--force` overrides QG-1, QG-2, QG-3, QG-7, QG-9, QG-10, QG-11, QG-12, QG-14, QG-15, QG-16, QG-17, QG-18, QG-19, QG-23, QG-24, QG-25, QG-B1, QG-B2, and QG-B3. QG-4, QG-5, QG-6, QG-8, and QG-13 are never overridable. Bridge gates (QG-B1, QG-B2, QG-B3) evaluate only when a trusted baseline exists.**

This table predates QG-26 (external-dependency state), QG-27 (truth-level/material-dispute), QG-28 (KPI degradation), and QG-WS5B (AI budget) — see §1.3 for their one-line descriptions and `src/core/quality_gates/` for implementation. **QG-29 is reserved, not implemented**: `src/core/quality_gates/gate_registry.py` holds it for the not-yet-built arch-fix.md AF-3 fail-closed AI audit gate (`.archive/specs/arch-fix.md`; `specs/backlog.md` §7 BL-C3).

### 14.2 Ban-List Validator (`src/core/ban_list_validator.py`)

Single-pass regex/word-boundary scan on all rendered content (Zone A + Zone B output):

| ID Range | Category | Examples |
|----------|----------|---------|
| BF-1..5 | Causality | `\bdue to\b`, `\bcaused by\b`, `\bled to\b`, `\bresulted in\b`, `\bbecause of\b` |
| BF-6..15 | AI-isms | `\bdelve\b`, `\btapestry\b`, `\bfurthermore\b`, `\bcrucial\b`, `\btestament\b`, `\bin conclusion\b`, `\bleverage\b` |
| BF-16..18 | Banned openings | `^This week`, `^As mentioned`, `^It should be noted` |
| BF-19..23 | Weak hedges | `\bsomewhat\b`, `\bperhaps\b`, `\bvarious\b`, `\bnumerous\b`, `\bmany\b` |

Programs extend via `editorial_rules.yaml → banned_phrases[]`.

### 14.3 Verbosity Enforcer (`src/core/verbosity_enforcer.py`)

| Surface | Limit |
|---------|-------|
| Workstream blurb | ≤4 sentences, ≤90 words |
| Exec summary | ≤150 words |
| Exec bullet | ≤25 words |
| Scorecard summary | ≤3 sentences |
| Subject line | ≤80 chars |

Word count: `len(re.findall(r'\b\w+\b', text))`. Sentence split: `re.split(r'(?<=[.!?])\s+', text.strip())`.

---

## §15 Error Handling & Resilience

### 15.1 Exception Hierarchy (`src/core/exceptions.py`)

```
VertexError (base)
├── ConfigError        — invalid configuration
├── AuthError          — authentication failure
├── QueryError         — external query failure
│   └── QueryTimeoutError
├── RenderError        — template rendering failure
├── ConfirmError       — archive promotion failure
└── StateError         — workflow state inconsistency
```

### 15.2 Degradation Strategy

| Boundary | On Failure | Behavior |
|----------|-----------|----------|
| ADO OData | Circuit breaker open | Degrade to last snapshot + STALE banner + exit 3 (unless `--allow-stale`) |
| ADO REST | Retry exhausted | `revisions=[]` + warning; evidence confidence drops |
| OpenAI | Budget exceeded | Omit AI sections, render deterministic output |
| Kusto | Cluster unreachable | Render `reference_url` link; "No data available" |
| Graph | 15s timeout | Enrichment empty; non-fatal |
| WorkIQ | Agency unavailable | Skip WorkIQ signals; warning |
| File lock | Lock held | Refuse write + exit 4; suggest `--force-lock` |
| Override YAML | Parse error | Refuse confirm; report line:col |
| Snapshot | Corruption | Walk backwards through archive until valid; freshness block |

### 15.3 Resilience Patterns

- **Retry:** `src/core/retry.py` — exponential backoff with jitter, `Retry-After` support
- **Circuit breaker:** `src/core/circuit_breaker.py` — file-backed CLOSED→OPEN→HALF_OPEN
- **Locked writes:** All journal/trajectory/proposal writes use `portalocker.LOCK_EX` + `fsync`
- **Atomic staging:** Snapshot writes use staging dir → `os.replace()` → cleanup

Proposal workflow writes follow the same pattern: `apply-proposals` backs up the entire issue narrative directory before AI or accepted-modified writes, uses atomic section replacement via `write_narrative_section(...)`, and archives `ACCEPTED` / `ACCEPTED_MODIFIED` proposal sidecars on confirm.

---

## §16 Observability

### 16.1 Structured Logging (`src/core/observability.py`)

```python
def configure_logging(run_id, level, json_output, stream=None, logger_name="vertex") -> RunLoggerAdapter
```

Two formatters: `StructuredFormatter` (JSON lines) and `HumanFormatter` (one-line-per-stage). `run_id` = `manifest_id` (UUID4) prefixed to every log line.

### 16.2 Run Manifest

Per-issue audit record with: `manifest_id`, `issue_number`, `edition`, timestamps, content hashes (SHA-256 for config/snapshot/HTML/MD), `ado_calls`, `ai_calls`, `ai_cost_usd`, `freshness_summary`, `qg_results`, `git_sha`.

### 16.3 LLM Trace (`src/ai/llm_trace.py`)

Every AI call logged to JSONL: edition, run_id, caller module, model, prompt version, token counts (prompt/completion/total), latency_ms, cost_usd, timestamp.

### 16.4 Cost Guard (`src/ai/cost_guard.py`)

Per-edition, per-run budget tracking. Persists state across calls within a run. Enforces ceiling from `ai.budget_usd_per_run` config.

---

## §17 Testing Strategy

### 17.1 Test Suite (2755 collected tests; latest full execution baseline preserved)

| Category | Location | Purpose |
|----------|---------|---------|
| Unit tests | `tests/unit/` | Per-module correctness |
| Contract tests | `tests/contracts/` | Zone boundary, AI safety pipeline usage, provider/integration protocols, architecture fitness guards (no-growth budgets for known god-modules, broad-exception regression ceilings on critical commands), and INV-2 single-write-path enforcement (`test_inv2_write_confirmed_single_write_path`) |
| Golden file tests | `tests/golden/` | Byte-level output comparison |
| Integration (opt-in) | `@pytest.mark.integration` | Live ADO (`--run-integration`) |

### 17.2 Test Infrastructure

- **Fixtures:** `tests/fixtures/` — sample journal, trajectory, knowledge, edition, program configs, `issue_077.snapshot.json`
- **Cassettes:** `tests/cassettes/` — recorded ADO API responses
- **Shared helpers:** `tests/support/report_test_setup.py` — stages V2 temp workspace with `editions/`, `programs/`, `knowledge/`, `reports/schemas`
- **Golden update:** `--update-golden` flag

### 17.3 Determinism

Clock injection (`datetime(2025,11,10,17,0,0,UTC)`), seeded UUIDs, sort-before-emit. All golden tests are byte-identical comparisons.

### 17.4 Key Contract Tests

| Contract | File | Assertion |
|----------|------|-----------|
| Zone boundary | `test_import_boundaries.py` | AST scan: zero `src.ai` or `src.m365` imports in `src/core/` |
| Journal immutability | Unit tests | Journal files never modified; review decisions in sidecar |
| Single write path | Code review | Only `snapshot_store.write_confirmed()` writes snapshots |
| Signal dedup | `test_signal_dedup.py` | Same fingerprint → deduplicated; different sources → both kept |

### 17.5 Execution

```bash
pytest tests/ -q                    # Full suite (2755 collected tests)
pytest tests/contracts/ -q          # Contract tests
pytest tests/golden/ -q             # Golden file tests
pytest tests/ --run-integration     # Live ADO tests
```

Latest full-suite execution evidence is recorded in `governance/test-evidence.md` (the canonical evidence log); `output/__green_run.txt` is a stale local artifact and must not be cited — see `scripts/check_spec_drift.py` `p9-dead-green-run`. The current suite shape is computed at CI time by `scripts/derive_spec_counts.py` (WS-9 step 2 deliverable); UIL additions remain a tracked evidence entry.

---

## §18 Dependencies & Build

### 18.1 Core Dependencies (`requirements.txt`)

```
jsonschema>=4.23.0    # Config schema validation
portalocker>=3.1.1    # Cross-platform file locking
PyYAML>=6.0.2         # YAML parsing
typer>=0.12.3         # CLI framework
requests>=2.32.3      # HTTP client
Jinja2>=3.1.4         # Template rendering
azure-identity>=1.17.1 # Azure auth
pytest>=8.3.0         # Test framework (dev)
cryptography>=43.0.0  # Encrypted profile storage
keyring>=25.3.0       # OS-backed secret storage
```

Current repo-managed dependency count: 19 packages in `requirements.txt`.

### 18.2 All Dependencies (`requirements.txt`)

All capabilities ship in a single requirements file:
```
openai>=1.30.0        # Azure OpenAI (Zone B)
tiktoken>=0.7.0       # Token counting (Zone B)
azure-kusto-data>=4.3  # Azure Data Explorer (Zone A/C)
matplotlib>=3.8        # Chart generation (Zone A)
msgraph-sdk>=1.15.0    # Microsoft Graph (Zone C)
```

### 18.3 Package Config (`pyproject.toml`)

```toml
[project]
name = "vertex"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["cryptography>=43.0.0", "jsonschema>=4.23.0", "keyring>=25.3.0", "PyYAML>=6.0.2", "typer>=0.12.3"]

[project.scripts]
vx = "cli:app"
vertex = "cli:app"

[tool.setuptools.packages.find]
where = ["."]
include = ["src*"]
```

### 18.4 Runtime Requirements

- **Python:** ≥3.11 (slots=True, structural pattern matching)
- **OS:** Windows (primary), macOS/Linux (supported via portalocker)
- **Git:** Required for `vertex kb changelog`
- **Editor:** `$EDITOR` for `vertex edit`

---

*End of tech spec.*
