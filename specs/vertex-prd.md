# Vertex — Product Requirements Document

**Version:** 1.0  
**Status:** Reflects implemented state as of 2026-07-08 — the newsletter read-path closure (`specs/fix-data-flow.md`, archived) landed on top of the 2026-07-07 REV activation proof; remaining work is operator/human-paced (`specs/backlog.md`)
**Companion specs:** [vertex-tech-spec.md](vertex-tech-spec.md) (technical specification), [vertex-ux-spec.md](vertex-ux-spec.md) (binding for visual design, layout tokens, rendering rules), `specs/backlog.md` (remaining real-data activation work)  
**Scope split:** This PRD owns product intent, operator workflows, constraints, and acceptance criteria. Exact schemas, module inventories, and implementation signatures live in [vertex-tech-spec.md](vertex-tech-spec.md).  

## Changelog

- Last updated: 2026-07-08 — Incorporated `specs/fix-data-flow.md` (v1.0→v1.13, archived to `.archive/specs/fix-data-flow.md`) into the canonical PRD/tech-spec. All 13 tracks (A–M) closed: the newsletter's risk and dependency read paths now route through `ProgramReality` (Track B), with the platform's first-ever trust-badge rendering markup (`templates/partials/truth_badge.j2`) live for risk, milestone, and assumption facts; `fact_bridge_enabled` now defaults to `true` with a visible doctor warning when it doesn't (Track A, ADR-0011); the incremental event-projection fold is now wired as an automatic post-write hook under WAL-mode concurrency (Track D, ADR-0010); all 7 render-pipeline `ProgramReality.load()` call sites are consolidated or documented (Track E); the AI-generated executive summary and workstream blurbs now draw on a token-budgeted reality-substrate summary (`src/commands/reality_context.py`) instead of the narrative-only `bundle.program_context`, so structured trust badges and AI prose can no longer silently contradict each other (Track J, PR-11b — closed ahead of this spec's own "next priority after Track C" schedule); a substrate health monitor (gather-freshness, bridge backlog, SoR-vs-actual-read-path consistency via a per-issue render manifest, fact-store location/schema-version consistency) is live in `vertex doctor --storage` (Track K). Per-family investigation found action/decision/commitment have no current main-newsletter read path and workstream's newsletter content is narrative-driven, not fact-per-row — reported honestly rather than force-migrated (§1.1 updated). Two operator-paced follow-ups moved to `specs/backlog.md`: the known stray `~/.vertex/xpf/vertex.sqlite3` cleanup (BL-B1) and Track D Step 2's conditional production-observation gate (BL-B2). Full suite: 6922 passed, 901 skipped, 0 failed; `scripts/verify_activation.py --program xpf` unchanged at 51/56.

- Last updated: 2026-07-07 — Incorporated `specs/activation.md` (ACTIVATION-1 v1.0→v1.29, archived to `.archive/specs/activation.md`) into the canonical PRD. §12.7.8 added: **the REV activation sentence has fired on real data** — a human-approved fact from a real program email now appears, cited and reverse-linkable, in a real render, proven by a counterfactual render diff. Getting there surfaced and fixed five previously-undetected code gaps on first real contact (see [tech-spec §13.6.7](vertex-tech-spec.md#1367-activation--real-data-proof-hardening-contracts-and-self-verification-2026-07-07) for the technical detail). `scripts/verify_activation.py --program <id>` now reports 51/56 checks passing for the pilot program; the remaining 5 are documented as `specs/backlog.md` (Azure Content Safety provisioning; corpus dual-annotation) — the only remaining work is external/human-paced, not engineering.

- Last updated: 2026-06-29 — All model-implementable engineering is complete, including the WS-1 read-path migration. The three v1-authoritative families now have `ProgramReality` read-path overlays (`commitment.date_set`, `ownership.changed`, plus the existing `milestone.completed`); `deployment.completed` rides the same `workitem.state` family. The deterministic extractor independently clears the G-floor (G-xtract-prec 86.7%, G-accept-prec 100%) on the preliminary corpus, so the LLM extractor is not required for the precision floor. What remains is purely operator/human-paced (ADR-0006 Amendment A4.2): (1) **S-9e** — recruit a second annotator for the κ dual-label set (0 of 539 candidates dual-labeled today), grow the extraction population, freeze the train/dev/test split; (2) **Q7** — production-extractor promotion deferred until S-9e certifies; (3) **S-10a** — IT provisioning of Azure Content Safety + `VERTEX_AI_JUDGE_DEPLOYMENT` (cycle-time SLO precondition, not on the authority path). The local-only consolidated implementation spec was archived to `.archive/specs/consolidated.md`; PRD/Tech/UX are the only GitHub-synced specs and the source of truth.

- Last updated: 2026-06-27 — Incorporated the remaining consolidated feature-spec decisions into the canonical PRD and made the three core specs the only GitHub-synced specs. ADR-0006 is accepted: `pilot-local` security profile, `automatic_after_deposit` automation scope, NCFL v1 writeable targets (`assumptions`, `decisions`, `milestones`, `risk_register`, `workstreams`), v1 REV-authoritative event set of 4 (`deployment.completed`, `milestone.completed`, `commitment.date_set`, `ownership.changed`), and deterministic extraction as the production default until the S-9 corpus proves LLM quality. NCFL apply is now a governed L3-style local write workflow with optimistic concurrency, recoverable state journal, canonical `save_*` writers, changelog, and ledger recording. Deliverable and incident authority remain Phase 2; scaffolding may exist, but v1 must not treat them as authoritative.

- Last updated: 2026-06-25 — REV Waves 1–7 coding complete. §12.7 implementation posture updated: normalizer data-loss fix + full PII wiring (W1); acceptance as review-state transition + shadow isolation (defense-in-depth, 11 contract tests) + typed lineage fields on fact schema (`domain_event_id`/`candidate_id`) + unified event-type registry (`event_type_registry.py`) + selective family replay + gap lifecycle `GapMatchCriteria` (W2); `RealityCompletenessVector` 3-area platform-level vector + `vertex doctor --rev-health` (W3); ICS/docs wired into `vertex rev run` (W6-1); SQLite candidate/decision store (`candidate_sqlite_store.py`, WAL mode, auto-migration from JSONL+rotated files) replacing JSONL operational state (W7-1); real incremental projection fold `_incremental_fold` with full-rebuild triggers (W7-2); PII pseudonymization with `PseudonymTable` / display-name header extraction / `PERSON_N` token substitution in `privacy.py` + `normalizer.py` + `eml_hydrator.py` (W5-3). 1455 contract tests pass (up from 1405). Working spec archived to `.archive/specs/still-gaps.md`; remaining work is OPERATOR-gated or WS-1 large-scope deferred.

- Last updated: 2026-06-24 — Incorporated `specs/program-context-intelligence.md` (REV v1.6, archived to `.archive/specs/`) into the canonical M365 integration requirements. §12.7 REV — Program-Context Intelligence Pipeline added: capability-port pipeline (FR-PCI-1..13), `vertex rev run` command, `doctor --rev-health` diagnostic, capability profiles (`legacy_nl | search_hydrate | rev_verified`), acceptance gates (RV-E1/RV-VP1/RV-V1/RV-A1), and implementation status (P1 engineering skeleton implemented; P0 live consent operator-gated). Working spec archived to `.archive/specs/program-context-intelligence.md`.

- Last updated: 2026-06-22 — Incorporated `specs/move-output-newsletter.md` (archived to `.archive/specs/`) into the canonical workspace layout requirements. §5.3 Issue lifecycle: rendered deliverables land in `programs/<id>/publications/<edition_id>/` (renamed from `output/`; operator runs `python scripts/migrate_edition_output.py --all --verify` to rename existing directories). New **PO-01** doctor storage check surfaces workspace layout state (OK / WARN / ERROR-split-brain / INFO-fresh) on every `vertex doctor` run; split-brain errors abort to prevent artifact divergence. Phase 5 cleanup (remove legacy `output/` fallback) triggers when PO-01 is clean for 2 consecutive runs. The `output_dir` field on `ResolvedEditionPaths` is a deprecated alias for `publications_dir`; remove in Phase 5.

- Last updated: 2026-06-22 — Incorporated `specs/nudge-gaps.md` into the canonical nudge requirements and archived the working spec. The nudge contract now reflects schema 2.1 `full_hygiene` ownership, ProgramReality-backed milestone/action-due resolution, audience-policy enforcement before draft creation, lifecycle-v2 cooldown semantics anchored to human send attestation (`--mark-sent`, optional `--sent-at`), state schema 1.2, publication-index schema 1.1 with content-hash + audience manifest, and `event.nudge.*` fact writes through the single sanctioned seam.

- Last updated: 2026-06-21 — WorkIQ structured retrieval (FQ-01). WorkIQ discovery is now rollback-safe by default (`m365.retrieval.discovery_mode: legacy_nl`); qualified programs can opt into bounded structured JSON enumeration (`structured_json`) with strict client-side validation, uncached union repetitions, and semantic-identity fallback. FQ-01 is preview-only (no new privacy exposure; signals still flow through the existing PENDING/approval gate and scrubbed signal path). The richer FQ-02 bundle (per-thread body extraction, composite privacy policy, source-keyed read model, grounding) is deferred and separately gated. Canonical contract in [tech-spec §13.1.1](vertex-tech-spec.md#1311-workiq-retrieval-contract); UX contract in [ux-spec §12.6.4](vertex-ux-spec.md). The working specification was archived locally under `.archive/specs/fix-workiq.md`.

- Last updated: 2026-06-19 — SharePoint as first-class data source. `vertex gather --sharepoint [--lt-deck] [--force-refresh]` wires the existing SharePoint pipeline into the governed gather loop. LT deck signals (native .pptx extraction via `lt_deck_extractor.py` or WorkIQ NL query) produce PENDING `Signal` entries, require explicit approval, and flow into `WorkstreamEvidence` via `sharepoint_evidence_stage.py`. New config: `m365.sharepoint` block (`SharePointConfig`, `SharePointLtDeckConfig`). New data model fields: `EngMsPage.source_subtype/cadence_days`, `WorkstreamSignalSources.sharepoint_paths/engms_paths`, `WorkstreamEvidence.lt_deck_alignment`. New doctor checks: QG-SP-1 through QG-SP-8. Detail spec archived to `.archive/specs/sharepoint.md`.

- Last updated: 2026-06-19 — Consolidated the WorkIQ/M365 newsletter-enrichment spec into the core PRD. `vertex enrich` is now documented as the governed pre-report M365 evidence path: WorkIQ email/Teams evidence is extracted into `WorkstreamEvidence`, review-gated as pending signals, and made available to report/propose AI only after approval. Reference-doc updates (eng.ms / SharePoint-backed knowledge pages), ADO telemetry summaries, feedback-loop context reuse, and rendered section-level source footnotes are now part of the canonical requirements. The working spec `specs/newsletter-workiq.md` is archived locally under `.archive/specs/`.

- Last updated: 2026-06-19 — Evidence extraction pipeline (ME-01 through ME-05) incorporated. §5.3 Issue Lifecycle: `vertex enrich` noted as optional evidence enrichment step after gather. §9.1: §9.1b Evidence Extraction added documenting `--extract-evidence`, `vertex enrich`, and the WorkIQ → WorkstreamEvidence → evidence_store.jsonl pipeline. §19.1: `vertex enrich` success criterion added.

 Evidence pipeline Phases 1-4 complete (BL-19/20/21/22/25/26/27/31/32/40 from commit 4c3838b): `WorkstreamEvidence`/`EtaRecord`/`SourceRef` domain models (`evidence_models.py`), `ContentExtractionAgent` (`content_extractor.py`), `LocalKbReader` (`local_kb_reader.py`), `EvidenceProvenanceRecord` (`evidence_provenance.py`). Edition resolver migrated to programs-tree lookup (`programs/*/editions/<id>.yaml`) with backwards-compat root fallback (GAP-31). BL-09/10/11 doctor checks for empty workstream registry and missing `name` fields. Companion docs updated to remove stale `gaps.md` and `backlog.md` references (both archived).

- Last updated: 2026-06-21 — Incorporated `specs/fix-nudge.md` (v2.3, archived to `.archive/specs/`) into the canonical nudge requirements. §10.2 `nudge` row: clarified the heat-map EML is a generated **draft** (`X-Unsent: 1`, never sent), driven by per-edition `full_hygiene` config (data-driven N-section engine, no hardcoded A/B/C model), with cooldown from `hygiene.cooldown_days` (minimum across matching sections), `nudge_audit.jsonl` audit, and `vertex doctor --nudge` (NQ-1..NQ-9) validation. Command-options table: `--stale-a/b/c` marked as a deprecated shim (`--stale-override` is canonical).
- Last updated: 2026-06-16 — Incorporated Acme onboarding learnings: §9.2 FR-SG-38 auto-approval policy added (≥10 signals at ≥80% approval rate → remaining PENDING auto-approved, `min_sample=10`, `ceiling_rate=0.8`, `floor_rate=0.2`); §10.2 `nudge` row updated to note `FullHygieneRow` covers QG-1/QG-9/QG-24 natively and M365/DPA approval is not required; §10.2 `discover candidates` row updated to add `--source prose_extract`, `--source-dir`, `--wave 1|2|3|4` flags (W1-W4 prose extraction implemented, commit 1eb3c30); §10.2 `confirm` flags updated to include `--ack-forecast`. Spec `specs/acme-onboard.md` archived to `.archive/specs/acme-onboard.md` (rev bw, 97% completion).

- Last updated: 2026-06-12 — Incorporated `specs/data-model.md` (event-sourced program ledger) into core specs and archived the feature spec to `.archive/specs/data-model.md`. §2: Added 5 ledger/knowledge-plane glossary terms (`LedgerEvent`, `CandidateEvent`, `TemporalConfidence`, `ConfidenceTier`, `KnowledgeClaim`). §7: Added §7.4 Ledger & Knowledge Plane Models. §10.2: Added `ledger`, `discover`, and `knowledge` commands. §14.7: Added QG-DM-1..13 quality gates. §18: Added INV-DM-1..6 invariants. All implementation is complete; only OPERATOR-gated work (backfill execution, live discovery validation) remains.

- Last updated: 2026-06-10 — Spec consolidation: re-debt.md and remains.md archived to `.archive/specs/`; operational readiness backlog consolidated into `specs/backlog.md` (local-only); `specs/` folder now contains only canonical committed specs. §2 Glossary expanded with reality substrate terms (TruthLevel, FactAssessment, ProgramReality, FleetReality, RealityConflict, AttentionItem, RealityDelta, ActuationProposal, FactSorState, EntityRegistry). §5.1 module counts updated to current (295/35/26/235). All `remains.md` forward-references updated to `backlog.md`.

- Last updated: 2026-06-15 — re-debt WI-8.1 closeout: §1.1 TPM/EM Operating Thesis added (reality-substrate model, `ProgramReality` facade, G-1 read-contract, truth-level attribution, disposable-projection rule); §4.4 Jobs-displaced Matrix added (cross-ref to re-debt §3.2; 15 grunt-job rows with absorbing capability and phase); §1 implementation posture updated (241 Zone A modules, 30 Zone B modules; reality substrate phases 1–7.4 complete summary; Phase 5 + 7.1/7.2 + 8.2 remaining work noted).

- Last updated: 2026-06-09 — Phase 4/5/6 debt-remediation wave closed (debt.md rev 277–351): persisted-state grounding hardened across all 28 file-backed stores (strict loader contracts, no silent coercion on replay); JSONL append-only rotation live for all 7 high-risk writers (10 MB per-stem cap, bounded `retain=5` window); AI safety pipeline (`process_generated_text`) now mandatory on every AI handoff — no bypass path in `src/ai/`; `frontier_eligible` flag in `ai_policy.yaml` is a real operator kill switch enforced at deployment-resolution time; `AI_PROPOSAL_TTL_DAYS = 14` constant governs proposal GC and `doctor --ai-proposals` reporting. New CLI surfaces: `vertex doctor --source-waivers` (schema validation + expiry check); `vertex rollback --drill [--archetype <name>] [--notes <text>]` (side-effect-free sandbox simulation + `s7a_rollback_drill` proof recording); `vertex facts dual-read-log`, `vertex facts pin-snapshot`, `vertex facts detect-drift`. `ProgramEvent` dataclass is the canonical seam for event-style fact types; `event.issue.skip` is the first migration; `workstream.association` and `baseline.trust_event` fact types now dual-write to the Fact Store. `ProviderRegistry` extended with connector registration (`register_connector` / `resolve_connector` / `connector_types()`). Working docs `debt.md`, `ops-ready.md`, and `prod.md` archived to `.archive/specs/`; remaining work consolidated in `specs/backlog.md` (local-only). Implementation posture: test suite at 4,300+ tests (4,023 unit + contract tests in recent targeted slice).

- Last updated: 2026-06-02 — Folded autonomous M365 source-discovery behavior from `discover.md` into the canonical PRD: authored meeting/chat/email names are discovery intent, gather now auto-resolves unique high-confidence matches into UIL registrations, ambiguous matches stay in the governed candidate queue, and `doctor --operator-gates` now maps discovery debt into six actionable states.

- Last updated: 2026-06-02 — P1 scope decision applied: §1 and §3 now scope Vertex to any Microsoft TPM program within the declared archetypes/exclusions, while broader TPM/EM global + non-ADO ecosystem expansion remains roadmap, not current supported scope.
- Last updated: 2026-06-02 — P7 spec-drift closure: §9.1 updated to describe ADO UIL as default-on while Kusto/Teams/IcM remain env-gated; §2 and §9.1 source-health wording refined so `required: false` roles warn without blocking and required roles remain the bounded confirm gate; fact-store migration text kept aligned with shadow-write foundation landed / SoR flip pending.
- Last updated: 2026-06-01 — §2: Added signals-fidelity glossary terms (`ProgramFactStore`, `ProgramFact`, `SourceContract`, `SourceHealth`, `SignalClass`, `Judgment`, `Chronicle`/`ProgramEvent`, `ExternalDependency`, `ReviewPack`, `ConversionFidelity`, `ETACredibility`, `Checkpoint`); §9.x: Signal Sourcing & Synthesis Fidelity capability (Program Fact Store system-of-record, fail-loud source health); §10.2: `facts`, `connectors`, `rollback` commands; §14.7: QG-SG-01, QG-SG-09, QG-SG-20 gates; §18: INV-SG invariants note. Reflects the implemented (coding-agent) portion of signals.md. Remaining `[OPERATOR]`/`[HUMAN GATE]` work from both `signals.md` and `backlog.md` is consolidated into the new [ops-ready.md](ops-ready.md); both `signals.md` and `backlog.md` are archived to `.archive/specs/`.
- Last updated: 2026-05-30 — §2: Added chart pipeline glossary terms (`ChartPipeline`, `ChartCacheEntry`, `ChartRenderer`); §9.1: Chart pipeline listed as Phase L0 capability; §14.1: QG-20/21/22 added to quality gate framework; §14.7: QG-20, QG-21, QG-22 chart gates added. Reflects charts.md implementation (archived to `.archive/specs/`).
- Last updated: 2026-05-29 — §2: Added `HintKind`, `NarrativeDeltaHint`, `HintProposal`, `GovernanceState`, `DecisionRecord`, `ExecSummaryStalenessFinding`, `PullRequestSummary` glossary terms; §10.2: `hints` command and `decisions governance` subcommand; §14.7: QG-23, QG-24, QG-25 quality gates; §19.1: hint engine and governance functional completeness criteria. Reflects hands-off.md implementation (archived to `.archive/specs/`).
- Last updated: 2026-05-27 — §2: Added `ProgramContext`, `ContextMaturityLevel`, `ContextGap` glossary terms; §4.1: doctor --context/--fix-hints PM need; §10.2: doctor `--context`/`--fix-hints` flags, fleet context health columns; §18: context health invariants note; §18.1: context maturity integration. Reflects program-context-maturity.md implementation (spec archived to `.archive/specs/`).
- Last updated: 2026-05-27 — §9.1: Phase 4a parity gate PASSED (`VERTEX_UIL_ADO=1` ready for default rollout), Phase 5 ADO old-path removal confirmed complete; §12.1: UIL migration spec archived (`.archive/specs/unify.md`), M365 migration ready.
- Last updated: 2026-05-26 — Updated §9.1 with UIL channel abstraction, §10.2 with `vertex integration` commands and `vertex registry` UIL bridge, §12 with UIL architecture note.
- Last updated: 2026-05-23 — Consolidated the former ingestion, feature backlog, and Acme readiness plans into `backlog.md` (now superseded by `.archive/specs/gaps.md`); refreshed implementation status from code walk-through and full-suite validation.

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Glossary](#2-glossary)
3. [Problem Statement](#3-problem-statement)
4. [Personas & Needs](#4-personas--needs)
5. [Architecture Overview](#5-architecture-overview)
6. [Directory Structure](#6-directory-structure)
7. [Data Models](#7-data-models)
8. [Configuration System](#8-configuration-system)
9. [Functional Requirements](#9-functional-requirements)
10. [CLI Reference](#10-cli-reference)
11. [AI Layer](#11-ai-layer)
12. [M365 Integration Layer](#12-m365-integration-layer)
13. [Visual Design & Rendering](#13-visual-design--rendering)
14. [Quality Gates & Editorial Contract](#14-quality-gates--editorial-contract)
15. [Testing Strategy](#15-testing-strategy)
16. [Non-Functional Requirements](#16-non-functional-requirements)
17. [Privacy & Data Sensitivity](#17-privacy--data-sensitivity)
18. [Invariants](#18-invariants)
19. [Success Criteria](#19-success-criteria)
20. [Dependencies & Infrastructure](#20-dependencies--infrastructure)

---

## §1 Executive Summary

Vertex is a config-driven TPM intelligence platform for **Microsoft TPM programs** operating within the declared supported archetypes and exclusions. It maintains a shared program model across workstreams, risks, milestones, dependencies, decisions, and evidence, then projects that model into Outlook-compatible HTML newsletters, daily digests, LT decks, quarterly retrospectives, Teams messages, and related operator surfaces.

The current deterministic kernel pulls live data from Azure DevOps (ADO), computes week-over-week deltas, scores risk across workstreams, and renders the weekly newsletter plus adjacent output surfaces from that shared program model. Weekly publication remains the primary operating loop today, but it is a projection of the program model rather than the system's defining boundary.

**Current supported scope:** Microsoft TPM programs using the declared supported archetypes and exclusions (SD-8 scope decision pending formal enumeration in a 3-row scope matrix). **Roadmap direction:** broader TPM/EM adoption outside Microsoft and additional non-ADO ecosystems (for example Jira and Slack) are intentional future expansion areas, not part of the current supported V-11 bar.

The system operates as a **CLI tool** invoked by an author (the PM), not a service. Every published fact must be source-traceable. The author retains editorial control: Vertex surfaces evidence; the author applies judgment. AI features are opt-in and operate behind a safety pipeline; deterministic output is the default.

### 1.1 TPM/EM Operating Thesis *(adopted from re-debt.md §1.1 — WI-8.1)*

Vertex is the source-of-truth **reality substrate** for technical program management and engineering management. It continuously ingests data from authoritative systems, **validates and reconciles** it (cleaning, entity resolution, cross-source corroboration, conflict adjudication), tracks freshness and provenance for every fact, and powers a growing set of applications that remove manual status chasing, source reconciliation, evidence gathering, stale reporting, repetitive narrative assembly, and routine data entry.

Source systems (ADO, Kusto, IcM, M365) remain authoritative for their *native records*. **Vertex becomes authoritative for cross-source program reality**: the validated, linked, aged, explained picture no single source holds. Every application — report, nudge, risk board, decision brief, ask — is a disposable projection over that model, never a source of truth itself.

`ProgramReality` is the single read facade enforcing this contract (G-1). All projections must read exclusively through `ProgramReality.load()`; the domain accessors return `FactAssessment` tuples with truth level, staleness, disputed flag, and evidence references attached to every fact. The facade is the platform contract; the underlying stores are implementation detail.

Vertex does not replace TPM/EM judgment. Human-confirmed facts outrank machine inference everywhere; machine writes to human-judgment fields never happen; nothing Vertex writes outward executes without approval.

**Key capabilities:**

- **Multi-altitude output.** A single program model produces daily, weekly, deck, lookback, and escalation outputs by changing the edition config — not by duplicating the pipeline.
- **Program memory.** Per-item ADO field trajectories accumulate across issues, enabling ETA drift detection, chronic reassignment alerts, and state oscillation warnings.
- **Multi-source evidence.** ADO, WorkIQ (email/Teams), Kusto, IcM, and SharePoint/LT deck signals feed an append-only journal with an explicit approval workflow before they reach published output.
- **Governed M365 enrichment.** `vertex enrich` turns WorkIQ-derived email/Teams context into `WorkstreamEvidence`, evidence-provenance records, and evidence-quality records; only approved backing signals can influence AI-generated report or proposal text.
- **Reference and telemetry context.** Approved eng.ms / SharePoint-backed reference-doc updates, ADO analytics/sprint telemetry, and approved prior-issue feedback now flow into AI drafting context instead of remaining detached operator-only artifacts.
- **Multi-program support.** Shared knowledge (people, teams, products) lives at the repo root. Program-specific knowledge (workstreams, history, milestones) is isolated per program.
- **ADO write-back.** Vertex can propose and apply bulk updates to ADO work items (comments, tags, fields) with preview, revision-based concurrency guards, and full audit trail.
- **ADO vitality.** A culture-change capability measuring how well ADO items reflect reality (freshness, richness, leakage), with graduated nudge surfaces from author-only triage to newsletter sections to ADO board tags.

**Current implementation posture (2026-07-08):**

- Codebase shape: **321** Zone A core modules, 35 Zone B AI modules, 26 Zone C M365 modules, and 235 command modules. (Zone A count derived at CI time by `scripts/derive_spec_counts.py`; PRD figures are re-derived quarterly.)
- Full test-suite evidence: see `governance/test-evidence.md` (the canonical evidence log; `output/__green_run.txt` is a stale local artifact — see `scripts/check_spec_drift.py` `p9-dead-green-run`). Counts in this section are computed by `scripts/derive_spec_counts.py` (WS-9 step 2).
- Acme Phases 0-2 are code-complete for deterministic operation; remaining issue-078 work is PM/operator execution: approve 20 pending sections, run `review-full`, confirm issue 078 with `--untrusted`, and resolve external ADO/Kusto/M365 data-plane gaps.
- Ingestion is approximately 94% implemented. The remaining gap is primarily live configuration and authenticated validation: ADO PR repository IDs, meeting `series_id` / Teams `thread_id`, Kusto query validation/RBAC, IcM provisioning, L1 metric rollout, and multi-session operational proof.
- AI framework and safety plumbing are present; Acme has `ai.enabled: true`, but AI output remains gated by explicit invocation flags and missing AOAI deployment environment variables.
- The authoritative gap register is `.archive/specs/still-gaps.md` (local-only, archived 2026-06-25 after Wave 1–7 coding complete). It supersedes `.archive/specs/gaps.md`, `backlog.md`, `signals.md`, `ops-ready.md`, `debt.md`, and `prod.md`. All former source plans are archived under `.archive/specs/`. All coding-implementable work is complete; remaining items are OPERATOR/HUMAN gates (see the changelog entries dated 2026-06-29 and 2026-07-07 for the authoritative current status — this section is a snapshot, the changelog is the source of truth for any drift).
- **Reality substrate (Phases 1–7.4 complete):** `ProgramReality` is the single read facade for all projections (G-1 contract). The 5-level truth ladder (GOVERNANCE_LOCKED→RAW_OBSERVED), `disputed` flag, and `provisional_inputs` flag are all wired at read time via `derive_truth_level` + snapshot-indexed conflict and signal sets (GAP-5 complete as of 2026-06-16). Entity registry (exact/fuzzy/casefold), trust ledger, signal normalizer, fact schema registry, commitment store with slip history, and the `vertex ask` named-intent surface (10 intents, zero-frontier O-14) are implemented and contract-tested. `vertex reality export` (§6.12.2) with timeseries replay, per-program cursor manifest, audit log, `sor_flip_boundary` frames, and `NullProjection` O-15 proof are live. The WS-1 read-path migration landed on 2026-06-29: `ProgramReality` read-path overlays exist for the three v1-authoritative families (`commitment.date_set`, `ownership.changed`, plus `milestone.completed`); `deployment.completed` rides the same `workitem.state` family. **The newsletter read-path closure (formerly `specs/fix-data-flow.md`, now archived to `.archive/specs/fix-data-flow.md`) is fully closed as of 2026-07-08**: `risk_stage.py` and the dependency read path inside `milestone_stage.py` now route through `ProgramReality` when a program's SoR mode is non-legacy; the platform's first-ever trust-badge rendering markup (`templates/partials/truth_badge.j2`) now renders for risk, milestone, and assumption facts (correcting [vertex-ux-spec.md](vertex-ux-spec.md)'s 2026-06-15 changelog claim that badges were "already implemented" — direct code investigation found no trust-badge rendering markup existed anywhere until this closure); `assumption` was also migrated (`report_lookback.py`); direct per-family investigation found **action, decision, and commitment have no current main-newsletter read path to migrate**, and **workstream's newsletter content is narrative/dimension-driven prose, not a per-fact section** — reported honestly as a verified finding rather than force-migrated. `fact_bridge_enabled` now defaults to `true` platform-wide (ADR-0011) with a visible `vertex doctor` warning + durable failure backlog when a program's bridge is disabled or failing. The incremental event-projection fold is wired as an automatic post-write hook under WAL-mode concurrency (ADR-0010), removing the need for a manual rebuild step. A substrate health monitor (gather-freshness, bridge backlog, SoR-vs-actual-read-path consistency via a per-issue render manifest, fact-store location/schema-version consistency) is live in `vertex doctor --storage`. The AI-generated executive summary and workstream blurbs now draw on a token-budgeted reality-substrate summary (`src/commands/reality_context.py`) instead of the narrative-only `bundle.program_context`, so AI prose can no longer silently contradict a structured trust badge. Remaining epistemic surfaces: corroboration/conflict E2E proofs (GAP-36), EXPLAIN drill-down (GAP-37), and workstream's larger render-model migration (deferred — no current per-fact section to migrate onto).
- **REV pipeline (Waves 1–7 plus deterministic authority slices complete, 2026-06-27):** Program-Context Intelligence (REV) coding waves W1–W7 are substantially complete. Key deliverables: normalizer data-loss fix + PII wiring (W1); acceptance as review-state transition + shadow isolation + typed lineage + unified event-type registry + selective family replay + gap lifecycle (W2); `RealityCompletenessVector` + `vertex doctor --rev-health` (W3); ICS/docs wired into `rev run` (W6-1); SQLite candidate/decision store replacing JSONL operational state (W7-1); real incremental projection fold via `_incremental_fold` (W7-2); PII pseudonymization with `PseudonymTable` + display-name extraction from email headers (W5-3). ADR-0006 accepts the v1 authority boundary: REV `human_comms` can become secondary input for `workitem.state` and `commitment` after clean-cycle gates, not for `judgment`; v1 authoritative REV event types are `deployment.completed`, `milestone.completed`, `commitment.date_set`, and `ownership.changed`. Remaining: S-9 corpus proof before production LLM extraction, UX/privacy/operator proof cycles, and any Phase 2 deliverable/incident authority work.
- **Remaining work (operator / human gates):** All coding-implementable re-debt Phases 0–8, the newsletter read-path closure, and accepted ADR-0006 deterministic gates are code-complete. Remaining items are OPERATOR gates documented in local-only archives/governance evidence and `specs/backlog.md`: production corpus proof for LLM extraction, live multi-program and multi-cycle operational proof, UX certification, privacy DPA review, the known stray `~/.vertex/xpf/vertex.sqlite3` database cleanup (BL-B1), and Track D Step 2's conditional production-observation gate (BL-B2).

<!--
  spec-posture (machine-readable; parsed by scripts/check_spec_drift.py `p12-posture-block`).
  Each line: `<work-item>: <status> (<date>)`. Statuses: complete | in-progress | deferred | not-started.
  This block is the single source of truth for work-item status referenced from the prose
  posture section above; the prose is a human narrative, this block is the CI-checked contract.
-->
<!-- spec-posture
WS-1: complete (2026-06-29)
GAP-5: complete (2026-06-16)
GAP-36: not-started
GAP-37: not-started
T0-4-trust-badges: complete (2026-07-08)
BL-A1: deferred
BL-A2: deferred
data-flow-v1: complete (2026-07-08)
-->

---

## §2 Glossary

| Term | Definition |
|------|-----------|
| **Altitude** | The abstraction level of an output: `street` (daily), `helicopter` (weekly), `satellite` (LT deck), `escalation` (incident). Controls signal filtering and template selection. |
| **Claim** | A narrative commitment extracted from a confirmed newsletter (e.g., "UD chunking fix expected by June 15"). Tracked across issues for staleness or contradiction against ADO state. |
| **Confidence** | A four-level assessment (`high`, `medium`, `low`, `none`) attached to signals, forecasts, and evidence packets. |
| **Coverage gap** | An active ADO item with no approved signals and no narrative mention in the current journal window. |
| **Decision-ask** | A leadership ask or decision request surfaced in a confirmed newsletter. Tracked until resolved. |
| **ChartCacheEntry** | A pre-scrubbed, timestamped local snapshot of Kusto rows written by the gather phase, scoped to one edition and query, with a configurable TTL. Stored in `chart_cache/` under the edition root. |
| **ChartPipeline** | The gather-time cache, renderer registry, quality gates, and archive lineage system that extends Kusto sections to support PNG image output via `render_as: chart_image` with `chart_config`. |
| **ChartRenderer** | A registered callable that accepts a `KustoQuery` and pre-scrubbed row data and returns PNG bytes plus metadata; identified by a namespaced ID (`namespace::name`). |
| **Delta** | A change between the current ADO state and the last confirmed snapshot. Types: `NEW`, `CLOSED`, `RISK_UP`, `RISK_DOWN`, `ETA_CHANGED`, `OWNER_CHANGED`, `UNCHANGED`. |
| **DRI** | Directly Responsible Individual — the workstream owner who reviews their section before publish. |
| **Edition** | A specific output configuration: program + altitude + audience + template. e.g., `acme_weekly`. Thin YAML declaration pointing to a shared program model. |
| **Entity graph** | The YAML knowledge base: people, teams, products. Shared across programs. |
| **Evidence packet** | Per-item justification bundle: revisions, comments, enrichments, confidence assessment. |
| **Forecast** | A confidence-weighted ETA prediction based on trajectory history. Shows slip probability alongside the ADO TargetDate. |
| **Freshness** | Per-item staleness assessment: days since last meaningful ADO update, overdue ETAs, ghost items. |
| **Gather** | The process of fetching evidence from all sources and appending signals and trajectory points to the program journal. |
| **Journal** | The append-only JSONL signal ledger. One file per ISO week per program. Review decisions stored in a separate sidecar. |
| **Leakage** | An information leakage event: a WorkIQ signal references a work item, but that item has no corresponding ADO update in the same 7-day window. |
| **Lookback** | A quarterly retrospective edition that aggregates confirmed archive data over a configurable window. |
| **Override** | A per-issue, per-dimension risk level set by the author, taking precedence over the derived risk. |
| **Program** | An organizational context (e.g., Acme, Platform) with workstreams, history, milestones, and program-specific knowledge. |
| **Proposal** | A batch manifest of proposed ADO updates generated by `vertex ado propose`. Must be explicitly applied. Expires after a default 72 hours unless overridden by proposal TTL config. |
| **Risk level** | Five-level scale: `High`, `Medium`, `Low`, `Done`, `❓ Needs Input`. The stored enum remains `unknown`; the rendered/operator label is `❓ Needs Input`. |
| **Rolling summary** | AI-generated compressed workstream state (~500 words), regenerated periodically from approved signals. |
| **Scorecard** | A named grid of dimensions, each mapped to a workstream and area-path filter. Cells show risk level, delta, and trend. |
| **Signal** | A single evidence entry in the journal from any source (ADO, WorkIQ, Kusto, IcM, manual). Immutable after write. |
| **Snapshot** | The confirmed state of all work items at the time of `vertex confirm`. |
| **Trajectory** | Per-item ADO field history over time. One JSONL file per work item. Grows only on state change. |
| **Triage** | A prioritized author checklist aggregating all pending work before an editing session. |
| **Vitality** | Composite score (0–100) measuring how well an ADO item reflects reality: freshness, richness, and leakage. |
| **Workstream** | A fluid grouping of work toward a goal, defined by the PM, spanning multiple teams. Not tied to org chart. |
| **Zone** | Architectural isolation boundary. Zone A = deterministic core. Zone B = AI layer. Zone C = M365/external I/O. |
| **ProgramContext** | A compiled, immutable knowledge graph loaded once per operation from the 20 Plane 1 YAML files (`program.yaml`, `workstreams.yaml`, `milestones.yaml`, etc.). Contains `invariant_violations`, `staleness_flags`, and `maturity_level`. Built by `src/core/program_context.py`. |
| **ContextMaturityLevel** | A five-level scale (L0–L4) measuring program-file health: L0 = critical errors, L1 = schema/structure errors, L2 = critical context gaps, L3 = quality degraded, L4 = fully healthy. Computed from invariant violations and staleness. Orthogonal to the program autonomy maturity level. |
| **ContextGap** | A record of a missing or low-quality context field detected at feature-run time (gather, doctor, nudge, etc.). Persisted to `programs/<prog>/_feedback/context_gaps.jsonl` via `src/core/context_gap_store.py` for prioritized remediation. |
| **Plane 1 changelog** | An append-only JSONL log at `programs/<prog>/changelog/plane1_changes.jsonl` that records field-level mutations to the 20 Plane 1 program YAML files across gather cycles (§22 E1 context versioning). |
| **ProgramFactStore** | The program-scoped, bitemporal, append-only system-of-record for synthesized program beliefs (`src/core/program_fact_store.py`). Holds typed facts (Claim, Decision, Action, Risk, MetricObservation, ExternalDependency, ProgramEvent, Milestone, ReviewFinding) keyed by a durable `fact_id` independent of any issue. Newsletters, nudges, the risk registry, action tracking, reviews, and reports are *projections* over this store. Stored in the per-program SQLite DB at `~/.vertex/<program_id>/vertex.sqlite3`. |
| **ProgramFact** | A single durable, source-linked program belief carrying `fact_id`, `program_id`, temporal validity (`recorded_at`, `valid_from`/`valid_until`), precedence, review state, and a `natural_key` for dedup. A change inserts a new revision row and closes the prior row's `superseded_at`; `as_of(t)` time-travel reads the believed state at any past time. |
| **SourceContract** | A per-scorecard-dimension declaration of required vs optional source roles, minimum freshness, and minimum yield (authored in `programs/<prog>/slice_contracts.yaml`). Surfaced in `doctor --channels`; unhealthy required roles block non-forced confirm unless waived (QG-SG-01), while unhealthy optional roles remain warning-only. |
| **SourceHealth** | The runtime evaluation of a SourceContract role as `healthy`, `stale`, `zero_yield`, `auth_failed`, or `unbound`. Transient failures may be waived with owner/date/reason in `programs/<prog>/source_waivers.yaml`; structural `unbound` misconfiguration is non-waivable. |
| **SourceIntent** | A declared meeting/chat/email source Vertex should discover and maintain. It captures PM-authored intent even before a durable `series_id` or `thread_id` exists. |
| **SourceCandidate** | A discovered durable meeting/chat/email identifier with confidence, evidence, lifecycle state, and decision history. |
| **DiscoveryAttempt** | An auditable record of one autonomous discovery pass, including provider, outcome (`no_candidates`, `ambiguous`, `auth_blocked`, `stale_plan`, etc.), count, and expiry window. |
| **SignalClass** | A semantic classification of a signal — `status`, `rca`, `mitigation`, `decision`, `risk`, `dependency` — used to weight synthesis (rca/mitigation/decision rank higher), route decisions to the Decision register, and de-prioritize status-only noise. |
| **Judgment** | A PM override persisted as a typed fact with `owner`, `reason`, `scope`, `provenance`, and `expiry`/`review_date`. Overrides a lower-precedence fact for a bounded window and is itself reviewable; recurring judgments surface as optimization proposals rather than silently mutating derivation. |
| **Chronicle / ProgramEvent** | An append-only log of program-level events (`pause`, `resume`, `dfd_slip`, `pivot`, `commitment`, `approval`, `pm_steering`) feeding exec-summary historical grounding and ETA credibility (`src/core/chronicle.py`). |
| **ExternalDependency** | A tracked cross-team or non-ADO dependency (e.g., a SharePoint List item or GitHub issue) polled read-only via a connector and surfaced in `doctor` with cross-team freshness. |
| **ReviewPack** | A per-DRI bundle of only the claims/signals needing validation for that DRI's workstreams; SME corrections are captured as structured feedback (source-fix / claim-fix / risk-fix / owner-fix / taxonomy-fix) rather than one-off prose edits. |
| **ConversionFidelity** | Per-function (newsletter, nudge, risk, action, review) fraction of required inputs arriving as automatic, source-traceable facts. Persisted to `programs/<prog>/metrics/conversion_fidelity.yaml` and shown in `doctor` so degradation is attributed to the right function. |
| **ETACredibility** | A 0–1 score `max(0, 1 − slip_count·0.15 − slip_magnitude_days/60)` derived from trajectory slip history; surfaced in scorecard tooltip and `doctor`; low credibility on a non-High dimension suggests a risk upgrade. Slip history renders as `~~d1~~ ~~d2~~ d3`. |
| **Checkpoint** | A timestamped snapshot of program stores (risk register, decisions, actions, chronicle, overrides) taken before fact-layer promotion, enabling `vertex rollback --to <checkpoint>` to reverse a bad promotion. |
| **HintKind** | An enum of narrative delta hint categories: `metric_stale`, `hint_stale`, `decision_stale`, `workstream_lead_missing`. Produced by `ado_narrative_hint_engine.py`. |
| **NarrativeDeltaHint** | A single detected gap between ADO evidence and the current narrative. Carries `kind`, `workstream_id`, `severity`, `evidence_refs`, `suggested_text`, `staleness_days`. |
| **HintProposal** | A pending narrative hint awaiting author accept/reject/modify. Persisted to `programs/<prog>/narratives/issue_NNN/hints.jsonl`. |
| **GovernanceState** | DFD date, DFD history, escalation active flag, escalation workstreams, and LT commitment. Persisted to `overrides/<issue>.yaml → governance:`. |
| **DecisionRecord** | A single governance decision with `id`, `workstream`, `type`, `statement`, `source_type`, `source_ref`, `owner`, `status`, `effective_date`, `resolved_date`. Stored in `overrides/<issue>.yaml → decisions[]`. |
| **ExecSummaryStalenessFinding** | A finding from `exec_summary_diff_engine.py` indicating the exec summary has diverged from ADO evidence by more than the similarity threshold (~0.82). Triggers QG-23. |
| **PullRequestSummary** | A pull request record from the ADO Git REST API with 11 fields: `pr_id`, `title`, `status`, `created_by`, `creation_date`, `source_branch`, `target_branch`, `merge_status`, `reviewers`, `repository_id`, `url`. Produced by `ado_pr_client.py`. |
| **TruthLevel** | Five-level belief classification: `GOVERNANCE_LOCKED` (baseline-locked fact), `HUMAN_CONFIRMED` (approved by human in review/confirm loop), `CORROBORATED` (independently confirmed by multiple sources), `SOURCE_VALIDATED` (primary authority source, no contradiction), `RAW_OBSERVED` (ingested but not yet validated). Every `FactAssessment` carries a `TruthLevel`. The truth derivation ladder (5 rules) is in `src/core/truth_model.py`. |
| **FactAssessment** | The read-time wrapper returned by every `ProgramReality` domain accessor. Carries the family's domain view model (`record: ActionItem | RiskEntry | ...`), `fact_id`, `truth_level`, `disputed` (open conflict), `stale`, `provisional_inputs` (INV-16 propagation), and `evidence` (source refs). Applications never receive raw store records; they receive `FactAssessment` tuples. |
| **ProgramReality** | The single read facade (G-1 contract) that all projections must use. `ProgramReality.load(program_id, *, programs_root, as_of, domains)` returns a fully-loaded reality snapshot. Domain accessors: `actions()`, `risks()`, `decisions()`, `dependencies()`, `milestones()`, `assumptions()`, `workstreams()`, `claims()`, `commitments()`. Reality API: `evidence_for()`, `conflicts()`, `stale_facts()`, `attention()`, `pending_actuations()`, `diff()`. Platform contracts: `to_dict()`, `events_since()`, `freshness()`. Implemented in `src/core/program_reality.py`. |
| **FleetReality** | Org-scope, multi-program read facade (`src/core/program_reality.py`). Aggregates per-program `ProgramReality` instances. `FleetReality.load(programs_root)` enables fleet-level `reality status`, cross-program attention, and portfolio health in one call. |
| **RealityConflict** | A detected disagreement between two or more sources about the same fact. Carries `conflict_id`, `entity_id`, `authority_family`, `sources`, `materiality` (material or minor), `resolution` (open/resolved). Material conflicts block rendering (QG-27 forceable). `conflicts(open_only=True)` on `ProgramReality`. |
| **AttentionItem** | A structured triage item from `attention()`. Kinds (closed enum): `DISPUTED_FACT`, `STALE_HIGH_SEVERITY`, `UNANSWERED_DECISION`, `PENDING_ACTUATION`, `CORROBORATED_RISK_AWAITING_REVIEW`, `COMMITMENT_SLIPPED`, `STRUCTURAL_GAP`, `DECISION_OUTCOME_DRIFT`, `OVERRIDE_RECERTIFICATION_DUE`. Carries `priority`, `description`, `action_hint`, `provisional_inputs` flag. `vertex triage --full` is the daily front door for all attention items. |
| **RealityDelta** | What changed between two `ProgramReality` snapshots. Returned by `diff()`. Fields: `added`, `changed`, `retired`, `dispute_opened`, `dispute_resolved`, `non_replayable_families`. Drives "what changed?" prep without manual source checking. |
| **ActuationProposal** | A governed propose→approve→execute→verify record. Three origins: fact-derived (G-11), claim-derived (WI-7.1b), gap-fix (§6.11.3). Every proposal has a TTL, requires explicit human approval (INV-12), and produces a `fact.executed` event or a reverse-proposal on failure. `pending_actuations()` on `ProgramReality`. `vertex actuate review` is the operator surface. |
| **FactSorState** | Per-program, per-family source-of-record mode: `legacy` (reads from artifact stores; fact-store shadow only), `shadow` (dual-read, parity-checked), `primary` (fact-store is authoritative). Accessed via `load_fact_sor_state(program_id)`. Family flip from shadow → primary requires ≥5 clean gather-triage cycles (`fact-store-flip --family`). |
| **NCFL Apply** | Governed context-update write workflow for accepted `ContextUpdateProposal`s. V1 may write only the ADR-0006-approved Plane 1 targets: `assumptions`, `decisions`, `milestones`, `risk_register`, and `workstreams`. Apply requires a fresh hash, optimistic concurrency, canonical `save_*` path, changelog entry, ledger/outbox recording, and repairable journal state. |
| **EntityRegistry** | Program + org-scope canonical entity registry (`src/core/entity_registry.py`). Three-tier resolution: exact match → casefold match → fuzzy match (rapidfuzz WRatio, per-scope thresholds). Aliases learned via `entity_alias_emitter.py`. Enables cross-source entity linking for corroboration and conflict detection. |
| **LedgerEvent** | An append-only, hash-chained record in the program event log. Envelope: ULID `event_id`, `event_type` (one of 52 registered types in 7 categories), bi-temporal `occurred_at`/`recorded_at`, `temporal_confidence`, `confidence` tier, `actor`, typed `payload`, and `source_ref`. Written by `src/core/ledger/event_log.py`; JSONL is authoritative, SQLite index is disposable cache. |
| **CandidateEvent** | A staged discovery output awaiting human triage (accept / edit / reject / skip). Zone B AI extractors and Zone C M365 connectors produce `DiscoveryRunResult` objects; Zone A `discovery_run_recorder.py` is the sole writer of candidate rows. `vertex ledger triage` governs the lifecycle through `pending.jsonl` → `triaged.jsonl`. |
| **TemporalConfidence** | Four-level confidence in an event's `occurred_at` timestamp accuracy: `exact` (known to the minute), `day` (date known, time approximate), `week` (week known, date approximate), `approximate` (only rough period known). Governs projection ordering and conflict resolution. |
| **ConfidenceTier** | Four-level source authority for ledger events: `operator_confirmed > source_authoritative > ai_extracted > inferred`. Maps to `TruthLevel` for the fact bridge (`fact_bridge.py`): `operator_confirmed` → `HUMAN_CONFIRMED`, `source_authoritative` → `SOURCE_VALIDATED`, `ai_extracted` → `CORROBORATED`, `inferred` → `RAW_OBSERVED`. |
| **KnowledgeClaim** | A scope-tagged, predicate-driven knowledge assertion about a program or its context. Stored via `src/core/knowledge/` (claim store + closed predicate registry). Scope hierarchy: `operator > org > portfolio > domain > program`. Surfaced by `vertex knowledge`. Distinct from narrative claims tracked by `claim_tracker.py`. |

---

## §3 Problem Statement

Microsoft TPM programs manage evolving program state that leadership consumes through weekly newsletters, LT decks, daily digests, quarterly retrospectives, and related decision surfaces. The weekly newsletter remains the primary publication surface today, and producing it still requires:

1. Pulling dozens of ADO work items across multiple area paths and work item types.
2. Computing deltas against the prior week's confirmed state.
3. Assigning risk to 10+ scorecard dimensions — each requiring judgment informed by ADO state, freshness, comments, and offline signals.
4. Writing workstream narratives that comply with editorial rules (no causal claims, concise, delta-first).
5. Rendering Outlook-compatible HTML that survives email client rendering quirks.
6. Producing derivative outputs (daily digest, LT deck, Teams messages) from the same data.

Without Vertex, this process takes 4–6 hours per issue and is error-prone: stale data, missed items, inconsistent risk assessments, and manual HTML formatting.

**Structural limits Vertex solves:**

| Problem | How Vertex Solves It |
|---------|---------------------|
| No program memory | Per-item trajectories detect ETA drift, chronic reassignment, and state oscillation across issues. |
| Single output format | Edition system projects daily/weekly/deck/lookback from a shared program model. |
| Evidence blind spots | Journal ingests ADO + WorkIQ + Kusto + IcM with approval workflow. |
| Collaboration context stranded outside the newsletter | `vertex enrich` and approved reference-doc/feedback loops feed governed M365 evidence into newsletter and proposal drafting without bypassing author review. |
| Flat config duplication | Thin edition YAML (~15 lines) inherits from shared program config. Adding a program is `vertex onboard`. |
| No feedback loop to ADO | Write-back pipeline proposes bulk comments/tags/fields with revision-based concurrency checks. |

### 3.1 Non-Goals

| ID | Non-Goal | Reason |
|----|----------|--------|
| NG-1 | **Auto-send emails.** | Author controls send until confidence is established. Vertex produces EML files; author proofreads and sends manually. |
| NG-2 | **Ungoverned orchestration.** | Author-in-the-loop advisory and proposal-staging flows are allowed, but Vertex must not take external action or write to ADO/communications without explicit approval for each application. |
| NG-3 | **Real-time streaming.** | Vertex runs on-demand via CLI. No daemon, no webhook listener, no event-driven architecture. |
| NG-4 | **PowerPoint generation.** | Generate slide-ready Markdown for LT decks. Actual `.pptx` generation is out of scope. |
| NG-5 | **Cloud vector store / semantic dependency.** | No external vector DB or cloud semantic memory service. Local semantic indexing under program storage is permitted when justified by operator volume and privacy constraints. |
| NG-6 | **Multi-author concurrent editing.** | Single-author model. Handoff is manual (transfer overrides + narratives). |

---

## §4 Personas & Needs

### 4.1 Primary: Program Manager (Author)

The author is the single person who owns the editorial output. They use Vertex 1–3 times per week.

| Need | Vertex Feature |
|------|---------------|
| See what changed since the last session before doing deeper triage | Session-start catchup banner plus `vertex catchup` |
| Start with a prioritized local action view instead of checking multiple commands manually | `vertex brief` synthesizes catchup, contradictions, claims, asks, and staged interventions |
| See what changed since last issue at a glance | `vertex triage`, delta engine, What Changed section |
| Set risk overrides efficiently | `vertex override` (interactive), `overrides.yaml` (direct edit) |
| Write narratives with evidence at hand | `vertex edit` opens editor; `vertex evidence` shows attribution |
| Review draft before publish | `vertex report --dry-run` renders HTML and opens browser |
| Get reviewer sign-off per section | `vertex review-sections set`, `vertex review-full` |
| Confirm and archive | `vertex confirm` — quality gates enforce completeness |
| Produce daily/weekly/deck outputs | Edition system — one program, multiple `vertex report --edition` calls |
| Detect stale items and nudge owners | `vertex freshness`, `vertex vitality`, ADO vitality nudge proposals |
| Send weekly hygiene nudges ahead of the newsletter gather | `vertex nudge --program <prog> [--dry-run]` writes a full-hygiene heat-map EML **draft** (`X-Unsent: 1`, never sent) to `programs/<prog>/nudge/drafts/{run_id}.eml`; deadlines and subject urgency resolve through `ProgramReality` when milestone references are present; audience policy (allowed domains, opt-out handling, unresolved-owner behavior, delivery mode, recipient cap) is enforced before a sendable draft is written; `--approve-draft <draft-ref>` records operator audience approval against the draft content hash when approval is required; `--mark-sent <draft-ref> [--sent-at <iso>]` attests the send, promotes the draft to `nudge/published_eml/`, writes publication metadata (content hash + audience manifest), and starts cooldown from attested send time; `--import-sent <published-ref> [--sent-at <iso>]` reconstructs cooldown/publication tracking for already-sent EMLs; `--list-drafts` lists available drafts; sections, sources, stale thresholds, and cooldown are data-driven from per-edition `full_hygiene`; `--stale-a/b/c` retained as a deprecated backwards-compat shim; runs are audited to `nudge/nudge_audit.jsonl`; `vertex doctor --nudge` validates legacy `NQ-*` checks and the `NQD-*` hardening checks |
| Track commitments made in prior issues | `vertex claims`, stale claim detection in triage |
| Search prior confirmed narratives and incident learnings without manual archive grep | `vertex history --semantic` with local semantic index support |
| Prepare for LT meetings | `vertex prep` generates a brief with anticipated questions |
| Close meetings into follow-up actions and draft artifacts | `vertex meeting-close` maps transcript actions into local review, Teams/HTML follow-up, and optional action promotion |
| Diagnose system health | `vertex doctor` validates config, ADO access, knowledge base integrity |
| Diagnose program context health | `vertex doctor --context` validates all 20 Plane 1 program files, reports invariant violations (WS-01…KB-03), staleness flags, and context maturity level (L0–L4); `--fix-hints` appends per-invariant remediation guidance |
| Review and apply section revision proposals without editing every file by hand | `vertex propose`, `vertex review-proposals`, `vertex apply-proposals` |

### 4.2 Secondary: DRI (Section Reviewer)

DRIs review their workstream section in Teams format. They do not use the CLI.

| Need | Vertex Feature |
|------|---------------|
| See only my section, not the full newsletter | Per-section Teams review via `review-sections` |
| Correct data errors | ADO edit link in review format |
| Approve or request changes | Author records DRI feedback via `vertex review-sections set` |

### 4.3 Tertiary: Leadership Reader

Leadership consumes the published newsletter, deck, or quarterly lookback. They have no interaction with the tool.

| Need | Vertex Feature |
|------|---------------|
| 10-second health check | Health Banner + Top 3 Now |
| 60-second scan | What Changed + Scorecards |
| Deep dive when needed | Executive Summary + Workstream sections |
| Track trends across issues | Scorecard trend annotations, vitality trend line |

### 4.4 Jobs-displaced Matrix *(re-debt §3.2 cross-ref — WI-8.1)*

Each row names a TPM/EM grunt job, the Vertex capability that absorbs it, and the implementation phase.

| TPM/EM grunt job | Absorbing capability | Phase |
|---|---|---|
| Weekly report assembly & evidence lookup | report projection + `evidence_for()` + `as_of` deltas | 1, 6 |
| Pre-meeting "what changed?" prep | `diff()` + decision-brief projection | 1 |
| Reconciling structured sources (ADO vs Kusto vs IcM) | corroboration engine + materiality-filtered conflict triage | 3 |
| Reconciling human text vs systems | claim-derived proposals through actuation governance (§6.11.4) — structured-ref-bearing claims only | 7 |
| Chasing DRIs for stale status | freshness ledger + nudge projection + structural-gap attention | 3, 5 |
| Manual ADO updates after meetings | actuation: propose → one-click approve → execute → verify | 7 |
| Creating the obvious missing artifact (mitigation task for uncovered risk) | gap-fix proposals (§6.11.3) | 7 |
| Mirroring uncontested system state into program records | `fact.source_sync` — automatic, mirror fields only, after family's primary flip | 5+ |
| Answering repeated stakeholder questions | `vertex ask` 10 named intents (O-14 zero-frontier) | 7.3 |
| Risk rollups across workstreams | risk-board projection + truth levels | 1, 3 |
| Dependency chasing | dependency board + cross-program entities | 1, 2 |
| Spotting silent slippage (theirs and ours) | directional `commitment.entry` + slip history + ETA-credibility digest | 2, 3 |
| Re-checking whether past decisions still hold | decision-outcome tracking via linked assumptions (§6.2.8) | 3 |
| Noticing what's structurally missing | structural-gap rules in `attention()` (§6.1.3) | 3 |
| Multi-program portfolio review | `FleetReality` + fleet-default `reality status` | 5 |

---

## §5 Architecture Overview

### 5.1 Three-Zone Hybrid

```
┌─────────────────────────────────────────────────────┐
│  Zone A — Deterministic Core (src/core/)             │
│  321 modules. No AI imports. No M365 imports.        │
│  Models, engines, stores, renderers, validators,     │
│  reality substrate. ADO/Kusto data acquisition is    │
│  the controlled external I/O exception.              │
├─────────────────────────────────────────────────────┤
│  Zone B — AI Layer (src/ai/)                         │
│  35 modules + prompt assets.                         │
│  Blurb generation, exec summary, anticipation,       │
│  rolling summaries, draft review, safety pipeline,   │
│  tiered router, local tier.                          │
├─────────────────────────────────────────────────────┤
│  Zone C — M365 Integration (src/m365/)               │
│  26 modules. ADO writer, Agency bridge, Graph mail,  │
│  Graph calendar, Teams reader, transcript reader,    │
│  enricher, discovery adapters, backfill.             │
├─────────────────────────────────────────────────────┤
│  Orchestrator (src/commands/)                        │
│  235 command modules. Wires zones together.          │
│  CLI entry point: cli.py → Typer app → commands.     │
└─────────────────────────────────────────────────────┘
```

**Sacred boundary:** `src/core/` must not import from `src/ai/` or `src/m365/`. Enforced by `tests/contracts/test_import_boundaries.py` (AST-level check on every `src/core/*.py` file).

**Five-Plane Architecture (vocabulary only — not enforced):** The implementation can also be understood through a conceptual Five-Plane model:

| Plane | Purpose | Primary Modules |
|-------|---------|----------------|
| Evidence Plane | Ingest and validate signals from all sources | `journal.py`, `trajectory.py`, `ado_client.py`, `agency_bridge.py` |
| Program Model Plane | Maintain structured state across workstreams, risks, milestones, dependencies, decisions, assumptions | `scorecard_engine.py`, `delta_engine.py`, `forecast_engine.py`, RAID stores |
| Automation Plane | Propose and apply ADO write-backs; signal dedup; vitality nudges | `ado_proposal.py`, `ado_reconcile.py`, `vitality_scorer.py` |
| Collaboration Plane | Reviewer pane, Adaptive Cards, escalation, nudge, prep briefs | `review_full.py`, `escalate.py`, `notify.py`, reviewer templates |
| Governance Plane | Quality gates, audit, manifest, lineage, maturity gating | `quality_gates.py`, `manifest_writer.py`, `lineage.py`, `maturity_check.py` |

This is an organizational vocabulary overlay — the binding architecture is the three-zone model.

**Note on Zone A I/O:** `ado_client.py` and `kusto_client.py` reside in Zone A and make HTTP calls for data acquisition. The zone boundary forbids AI and M365 imports — not all external I/O. ADO/Kusto calls are deterministic data acquisition, not probabilistic AI or M365 integration.

### 5.2 Data Flow

```
┌─────────────── Gather Phase ───────────────────┐
│  ADO OData ──┐                                  │
│  ADO REST ───┤                                  │
│  WorkIQ ─────┤──→ Signal Journal (JSONL)        │
│  Kusto ──────┤     programs/<prog>/journal/     │
│  IcM ────────┘                                  │
│                                                 │
│  ADO Revisions ──→ Trajectories (JSONL)         │
│                     programs/<prog>/trajectories/│
└─────────────────────────────────────────────────┘

┌─────────────── Draft Phase ────────────────────┐
│  Knowledge (YAML) ─────┐                        │
│  Edition Config (YAML) ─┤                        │
│  Journal (JSONL) ───────┤──→ Pipeline ──→ Output │
│  Trajectories (JSONL) ──┤    (Zone A)    (HTML/  │
│  Rolling Summaries (MD) ┤                MD/EML) │
│  Overrides (YAML) ──────┤                        │
│  Narratives (MD) ───────┘                        │
│                                                 │
│  AI Synthesis (Zone B) ←→ Pipeline (optional)   │
└─────────────────────────────────────────────────┘

┌─────────────── Confirm Phase ──────────────────┐
│  Output artifacts ──→ Quality Gates ──→ Archive │
│                       (Zone A)         programs/ │
│                                        <prog>/  │
│                                        archive/ │
└─────────────────────────────────────────────────┘
```

### 5.3 Issue Lifecycle

The editorial workflow follows an eight-stage state machine per issue:

```
1. GATHER    → vertex gather --program <prog> [--cadence daily|weekly] [--workiq] [--kusto] [--icm] [--analytics] [--sprints] [--pipelines] [--extract-evidence]
                 Fetches evidence, appends signals + trajectories. `--extract-evidence` runs ME-02 extraction stage.

1b. ENRICH   → vertex enrich --edition <edition> [--since <YYYY-MM-DD>] [--dry-run]  (optional; recommended when gather's --extract-evidence is insufficient)
                 Queries WorkIQ per workstream lane, extracts WorkstreamEvidence via AI, persists to evidence_store.jsonl.

2. TRIAGE    → vertex triage --edition <edition>
                 Prioritized checklist: blockers, missing overrides,
                 missing narratives, stale claims, coverage gaps.

3. PROPOSE   → vertex propose --edition <edition> [--no-ai|--ai]
                 Seeds the next issue, assembles evidence briefs,
                 and writes `proposals.jsonl` for pending section revisions.

4. REVIEW-PROPOSALS → vertex review-proposals --edition <edition> [--section <id>] [--resolved-only]
                 Read-only proposal review pane for pending current vs proposed text,
                 or resolved-only proposal history once no pending proposals remain.

5. APPLY-PROPOSALS → vertex apply-proposals --edition <edition>
                 Records accept/reject decisions, supports `--interactive`,
                 `--accept-modified`, `--accept-all`, and `--undo`, and updates
                 narratives atomically.

6. DRAFT     → vertex report --edition <edition> --dry-run
                 Renders HTML/EML/Markdown draft. Opens in browser.
                 Author edits narratives (vertex edit), sets overrides
                 (vertex override), reviews sections (review-sections).

7. REVIEW    → vertex review-full --edition <edition>
                 Leadership review pane with evidence, anticipated
                 questions, vitality bars, drift patterns.

8. CONFIRM   → vertex confirm --edition <edition>
                 Quality gates validated. Snapshot archived.
                 Claims extracted. Edit patterns and accepted proposals archived.
```

---

## §6 Directory Structure

```
vertex/
├── knowledge/                      # Shared entity graph (YAML)
│   ├── people_directory.yaml       # Factual: alias, email, title, team, org_chain
│   ├── people_profiles.yaml        # Subjective: comm_style, cares_about (.gitignore'd)
│   ├── people_profiles.example.yaml # Template for people_profiles.yaml
│   ├── teams.yaml                  # Teams → area paths, eng chains
│   ├── products.yaml               # Product taxonomy
│   └── golden_queries.yaml         # KQL query registry
│
├── programs/                       # Per-program data
│   ├── acme/
│   │   ├── program.yaml            # Mission, phases, milestones, leadership readers
│   │   ├── workstreams.yaml        # Workstream definitions + owners
│   │   ├── scorecards.yaml         # Scorecard dimensions → workstream mapping
│   │   ├── kpis.yaml               # Per-workstream KPI query registry
│   │   ├── editorial_rules.yaml    # Banned phrases, verbosity limits
│   │   ├── review.yaml             # Reviewer assignment config
│   │   ├── slice_contracts.yaml    # Per-dimension data source contracts
│   │   ├── template_contract.yaml  # Section ordering rules
│   │   ├── chapter_contract.yaml   # Chapter grouping definitions
│   │   ├── ado_comment_template.md # Customizable ADO comment template
│   │   ├── ado_field_map.yaml      # ADO field mapping (opt-in)
│   │   ├── journal/                # Append-only JSONL signal journal
│   │   │   ├── 2026-W19.jsonl      # Weekly partition (immutable after week)
│   │   │   ├── reviews.jsonl       # Review decisions sidecar
│   │   │   ├── claims.jsonl        # Claim/commitment ledger
│   │   │   ├── edit_patterns.jsonl # Author edit learning audit
│   │   │   ├── kb_edits.jsonl      # KB edit audit trail
│   │   │   └── signal_threads.jsonl # Signal correlation links
│   │   ├── trajectories/           # Per-item ADO field history (JSONL)
│   │   │   ├── 36830830.jsonl
│   │   │   └── 37777351.jsonl
│   │   ├── overrides/              # Per-issue risk overrides (YAML)
│   │   │   └── issue_077.yaml
│   │   ├── narratives/             # Per-issue narrative files (Markdown)
│   │   │   └── issue_077/
│   │   │       ├── .seeding_manifest.json  # Source hashes from trusted-baseline seeding
│   │   │       ├── exec_summary.md
│   │   │       ├── proposals.jsonl         # Pending section revision proposals
│   │   │       └── ws_acme.md
│   │   ├── summaries/              # AI-generated rolling summaries (Markdown)
│   │   ├── knowledge/              # Program-local knowledge (fallback)
│   │   ├── trusted_baseline.yaml   # Weekly continuation baseline, bridge graduation state
│   │   ├── capability_status.yaml  # Kusto/M365/Graph capability activation state for maturity gating
│   │   └── archive/                # Per-edition confirmed artifacts
│   │       └── acme_weekly/
│   │           ├── index.json
│   │           ├── scorecards.json
│   │           ├── vitality.json
│   │           ├── snapshots/
│   │           ├── html/
│   │           ├── md/
│   │           ├── manifests/
│   │           ├── narratives/     # Archived per-issue narrative directories
│   │           └── eml/
│   └── platform/                   # Second program (same structure)
│
├── editions/                       # Thin output declarations (YAML)
│   ├── acme_weekly.yaml             # program: acme, altitude: helicopter, type: detailed
│   ├── acme_nudge.yaml              # program: acme, weekly hygiene email surface (send_day + deadline_offset configurable)
│   ├── acme_daily.yaml              # program: acme, altitude: street, type: condensed
│   ├── acme_lt_deck.yaml            # program: acme, altitude: satellite, type: deck
│   ├── acme_quarterly.yaml          # program: acme, altitude: satellite, type: lookback
│   └── platform_weekly.yaml          # program: platform, type: narrative
│
├── publications/                   # Rendered deliverables + edition runtime state
│   └── acme_weekly/
│       ├── issue_078.html
│       ├── issue_078.eml
│       ├── issue_078.md
│       ├── issue_078.manifest.json
│       ├── issue_078.snapshot.json
│       ├── issue_078.continuation_contract.json  # Continuation inheritance record
│       ├── review_status.yaml
│       └── review/
│
├── reports/schemas/                # JSON Schema files
│   ├── report_config.schema.json
│   ├── signal.schema.json
│   ├── quality_matrix.schema.json
│   └── remediation.schema.json
│
├── src/                            # Source code
│   ├── core/                       # Zone A — 295 modules
│   ├── ai/                         # Zone B — 35 modules + prompt assets
│   ├── m365/                       # Zone C — 26 modules
│   └── commands/                   # Orchestrator — 235 command modules
│
├── templates/                      # Jinja2 templates
│   ├── base.email.j2               # Outlook-compatible HTML shell
│   ├── base.deck.j2                # Markdown deck shell
│   ├── base.reviewer.j2            # Reviewer dashboard HTML
│   ├── base.teams.j2               # Teams message format
│   ├── archetypes/                 # 7 archetype templates
│   │   ├── detailed.j2             # Weekly newsletter
│   │   ├── continuity.j2           # Band scorecard layout
│   │   ├── condensed.j2            # Daily digest
│   │   ├── narrative.j2            # Narrative-driven
│   │   ├── deck.j2                 # LT deck Markdown
│   │   ├── lookback.j2             # Quarterly retrospective
│   │   └── digest.j2               # Digest variant
│   └── partials/                   # 25 reusable partial templates
│
├── tests/                          # Test suite (2755 collected tests)
├── scripts/                        # Utility scripts
├── specs/                          # Specifications
├── cli.py                          # CLI entry point (Typer)
└── pyproject.toml                  # Package metadata
```

### Program Directory Taxonomy

Each `programs/<id>/` root communicates its own structure at a glance via an **authored-vs-machine** tiered layout. The single source of truth is `src/core/program_paths.py` (`ROOT_ENTRIES` + `RUNTIME_ARTIFACTS`); `vertex doctor --storage` (DC-01/DC-02/DC-03) enforces it. Full per-file delete-safety, retention, and operator-editability live in [vertex-tech-spec.md §10.2.3](vertex-tech-spec.md#1023-program-directory-taxonomy); the design rationale is in `specs/declutter.md`.

| Tier | What lives here | Operator-editable? |
|------|-----------------|--------------------|
| **T-1** Authored config | `program.yaml`, `workstreams.yaml`, `scorecards.yaml`, `editorial_rules.yaml`, `review.yaml`, contract files, `editions/`, `knowledge/` | Yes — operator owns it |
| **T-2** Mutable state | `decisions.yaml`, `milestones.yaml`, `trusted_baseline.yaml`, `readiness.yaml`, `risk_register.yaml`, … | Sometimes — operator + platform |
| **T-3** Platform runtime | `runtime/` subdir: `gather_state.json`, `run_telemetry.jsonl`, `channel_registry.sqlite3`, `vertex_analytics.sqlite3`, `m365_registry.yaml`, `readiness_snapshot.yaml`, `dedup_drop_log.jsonl` | No — machine-owned, regenerated by its command |
| **T-4** Append-only logs | `platform_proof_log.yaml` (root), `chronicle.jsonl`, `journal/`, `ledger/`, `trajectories/`, `changelog/` | No — grow-only evidence/audit |
| **T-5** Outputs | `publications/`, `archive/` | No — rendered / confirmed-issue ledger |
| **T-6** Operational | `narratives/`, `summaries/`, `nudge/`, `overrides/`, `checkpoints/`, `metrics/`, `backfill/`, `gold_corpus/` | No (machine); `overrides/` is operator-controlled |
| **T-7** Feedback | `_feedback/` — learned-but-governed policy | No — governed learned state |
| **T-8** Docs/research | `docs/` (one-time human documents), `_spike/` (research scratch) | Yes |

The key safety property: an operator can only break what they own. `runtime/` is platform-only (misfiling a machine file or hand-editing `gather_state.json` is detectable, not just discouraged), and `docs/` is human-only (a platform artifact dropped there is flagged by DC-03). This is the substrate that makes "the operator can only break what they own" a platform-checked invariant — a precondition for the trust/autonomy progression (completion gate V-8).

---

## §7 Data Models

This section names the product-facing entities and responsibilities. Exact typed field definitions, enum spellings, and storage contracts live in [vertex-tech-spec.md](vertex-tech-spec.md).

All value objects use `@dataclass(frozen=True, slots=True)`. Times are UTC `datetime`. IDs are `int` for ADO work items. Enums are `str` enums for JSON portability. No `datetime.utcnow()` — use `datetime.now(timezone.utc)`.

### 7.1 Core Domain Models (`src/core/models.py`)

**Enums:**

| Enum | Values | Usage |
|------|--------|-------|
| `RiskLevel` | HIGH, MEDIUM, LOW, DONE, UNKNOWN | Scorecard dimensions, overrides, quality gates |
| `DeltaKind` | NEW, CLOSED, RISK_UP, RISK_DOWN, ETA_CHANGED, OWNER_CHANGED, UNCHANGED | Change tracking between snapshots |
| `Confidence` | HIGH, MEDIUM, LOW, NONE | Evidence packets, signals, forecasts |
| `EditionType` | DETAILED, FOCUSED, CONDENSED, NARRATIVE, DECK, LOOKBACK | Edition rendering archetype selection |
| `ReviewState` | PENDING, SENT, APPROVED, SKIPPED_NO_DELTA, CHANGES_REQUESTED, REJECTED | Per-section review status |
| `AttributionTier` | TIER1, TIER2, TIER3 | Citation level (inline, section, reviewer) |

**Key types:**

| Type | Purpose | Key Fields |
|------|---------|------------|
| `WorkItem` | ADO work item with revisions and comments | `id`, `title`, `state`, `assigned_to`, `area_path`, `target_date`, `revisions`, `comments`, `fetched_at` |
| `EvidencePacket` | Per-item justification bundle | `work_item_id`, `revisions`, `comments`, `enrichments`, `confidence`, `tier`, `summary_for_reviewer` |
| `ItemDelta` | Single item change record | `work_item_id`, `kind`, `field_changes`, `old_risk`, `new_risk`, `old_eta`, `new_eta`, `evidence` |
| `DeltaSet` | Aggregated deltas for an issue | `issue_number`, `previous_issue_number`, `new_items`, `closed_items`, `risk_changes`, `eta_changes`, `owner_changes`, `unchanged_count` |
| `DimensionRisk` | Scorecard dimension with derived risk | `name`, `risk`, `summary`, `evidence`, `derived_risk`, `override_risk`, `vector_label`, `risk_sparkline`, `trend_label` |
| `Snapshot` | Confirmed point-in-time state | `issue_number`, `generated_at`, `ado_data_as_of`, `edition_type`, `items`, `scorecards` |
| `RunManifest` | Full run audit record | `manifest_id`, `issue_number`, `edition`, `config_hash`, `snapshot_hash`, `html_hash`, `md_hash`, `ado_calls`, `ai_calls`, `ai_cost_usd`, `qg_results` |
| `FreshnessReport` | Per-DRI freshness findings | `issue_number`, `items`, `blocks`, `warns`, `infos` |
| `ReportData` | Full report rendering payload | `issue_number`, `edition`, `generated_at`, `items`, `deltas`, `scorecard`, `exec_summary_text`, `workstream_blurbs`, `freshness`, `review_status`, `manifest_id` |

### 7.2 V2 Models (`src/core/models_v2.py`)

**Knowledge models:**

| Type | Purpose |
|------|---------|
| `PersonDirectory` | Factual person data: alias, email, title, tier, org_chain, team_ids |
| `PersonProfile` | Subjective data: comm_style, cares_about, pet_peeves (`.gitignore`'d) |
| `Team` | Engineering team with area path mappings |
| `Product` | Product taxonomy entry |

**Program models:**

| Type | Purpose |
|------|---------|
| `Program` | Program identity: mission, pillars, dependencies, writing style, leadership readers, runtime defaults (ADO, AI, Kusto, M365) |
| `Workstream` | Fluid work grouping: area paths, owners, leadership sensitivity, status |
| `Scorecard` | Named scorecard with dimensions. Composite key: `(scorecard_name, dimension_name)` |
| `EditionConfig` | Thin output declaration: program_id, altitude, cadence, type, distribution, overrides |

**Signal models:**

| Type | Purpose |
|------|---------|
| `Signal` | Append-only evidence entry: source, entity_refs, text, confidence, metadata |
| `SignalReviewDecision` | Approved/dismissed/deferred decision in `reviews.jsonl` sidecar |
| `SignalUsageMarker` | Records signal-to-confirmed-issue link |
| `SignalThreadLink` | Manual signal correlation link |
| `TrajectoryPoint` | Per-item ADO field snapshot: date, state, assigned_to, target_date, risk_level |

**Typed signal metadata:** `ADOFieldChangeMetadata`, `KustoMetadata`, `WorkIQMetadata`, `IcMMetadata`.

**Vitality models:**

| Type | Purpose |
|------|---------|
| `VitalityScore` | Per-item: freshness_grade, richness_score, leakage_events, composite_score |
| `VitalityAggregate` | Per-owner or per-workstream aggregate |
| `VitalityArchiveEntry` | Confirmed vitality history for trend computation |

**Claim models:**

| Type | Purpose |
|------|---------|
| `ClaimEntry` | Narrative commitment with due_date, entity_refs, owner |
| `DecisionAsk` | Leadership ask with status and resolution |
| `ClaimStatusUpdate` | Status change record (met, contradicted, stale) |

**ADO write-back models:**

| Type | Purpose |
|------|---------|
| `ADOUpdateProposal` | Batch manifest of proposed ADO updates |
| `ADOUpdateEntry` | Single proposed update: work_item_id, action, proposed_value, revision_id |

### 7.3 View Models (`src/core/view_models.py`)

Render-ready dataclasses consumed by Jinja2 templates. These decouple domain logic from presentation.

| Type | Purpose |
|------|---------|
| `HealthSummary` | Banner: overall risk, high/medium/low counts, trajectory, BLUF |
| `Top3Item` | Author-curated top-3-now strip item |
| `WorkstreamData` | Per-workstream render payload: blurb, items, citations, risk |
| `ScorecardData` | Scorecard render data with dimensions and trend annotations |
| `EditionMeta` | Edition metadata for template variables |
| `AdoVitalitySectionData` | Newsletter vitality section data |

### 7.4 Ledger & Knowledge Plane Models (`src/core/ledger/`, `src/core/projections/`, `src/core/protection/`, `src/core/knowledge/`)

The program event ledger and knowledge plane sit above the Program Fact Store and provide longitudinal reality capture, replayable projections, and scope-tagged knowledge assertions. Exact typed schemas and storage contracts live in [vertex-tech-spec.md](vertex-tech-spec.md) §9.17.

**Ledger core models:**

| Type | Purpose |
|------|---------|
| `EventEnvelope` | Typed ledger event with ULID `event_id`, bi-temporal timestamps, hash-chain fields (`content_hash`, `prev_event_hash`), `ConfidenceTier`, `TemporalConfidence`, `actor`, and a typed `payload` validated against the 52-type registry |
| `CandidateEvent` | Staged discovery candidate with `candidate_id`, source extractor attribution, `triage_state` (`pending`/`accepted`/`rejected`/`skipped`/`edited`), and immutable triage audit entry |
| `EventWriteResult` | Result of `write_event()` or `write_events_atomic()`; carries the persisted `envelope` (with assigned event_id) and rotation metadata |
| `EvidenceVaultEntry` | Content-addressed external-origin evidence excerpt stored under `programs/<id>/ledger/evidence/<hh>/<hash>` plus `.meta.json` sidecar |
| `EventRedactionRecord` | Compliance redaction registry entry in `.redactions.jsonl`; preserves `original_envelope_hash` so the hash chain remains verifiable after a §10.8 physical payload scrub |

**Projection and protection models:**

| Type | Purpose |
|------|---------|
| `ProgramProjection` | Deterministic, replayable SQLite snapshot built from the event log via `project_program_events()`; represents current or as-of program state |
| `ProjectionView` | Named view over a projection: `current_state`, `as_of(t)`, `timeline()`, `diff(t1, t2)` |
| `FieldLockEntry` | Operator-asserted field lock event that pins a field value and diverts contradicting writes to the candidate queue |

**Knowledge plane models:**

| Type | Purpose |
|------|---------|
| `KnowledgeClaim` | A predicate-driven assertion with `claim_id`, `scope`, `predicate` (from closed registry), `subject`, `value`, `valid_from`/`valid_until`, `confidence`, and `source_ref` |
| `ClaimCandidate` | A knowledge claim awaiting triage; follows the same a/e/r/s lifecycle as `CandidateEvent` |

---

## §8 Configuration System

This section describes configuration ownership and operator meaning. Exact file schemas, merge rules, and loader behavior live in [vertex-tech-spec.md](vertex-tech-spec.md).

### 8.1 Edition Config

Each edition is a single YAML file in `editions/` with `schema_version: "2.0"`. Typical size: ~15–30 lines.

**Required fields:** `id`, `program_id`, `type`, `altitude`, `cadence`

**Optional fields (inherit from program):** `ado`, `ai`, `kusto`, `m365`, `distribution`, `workstream_filter`, `brand_name`, `scorecard_sort`, `layout_mode`, `cadence_note`

**Edition types and archetype mapping:**

| `type` | `altitude` | Archetype Template | Description |
|--------|-----------|-------------------|-------------|
| `detailed` | `helicopter` | `detailed.j2` or `continuity.j2` | Full weekly newsletter |
| `condensed` | `street` | `condensed.j2` | Daily digest |
| `deck` | `satellite` | `deck.j2` | LT deck Markdown |
| `lookback` | `satellite` | `lookback.j2` | Quarterly retrospective |
| `narrative` | `helicopter` | `narrative.j2` | Narrative-driven (Platform) |
| `focused` | `helicopter` | `detailed.j2` or `continuity.j2` via a focused `template_contract` family | Condensed weekly variant using the same base templates as `detailed` with different ordering and visibility rules |

**Layout modes:** `continuity` (band scorecards with chapters) or `dashboard` (standard section dispatch).

**Config merge:** Edition fields override program defaults field-by-field via `dataclasses.replace()`. Absent edition fields inherit from program.

### 8.2 Program Config

Each program has `programs/<prog>/program.yaml` containing:

- `id`, `name`, `objective`, `mission`, `current_phase`
- `maturity_level` — feature-gating level for governance and automation surfaces
- `communication_plan` — authored cadence plan across editions, including primary operating surface
- `charter` — scope, success criteria, constraints, and stakeholder register
- `raci` — responsibility model used by review, escalation, and owner-facing surfaces
- `storage_backend` — `file` or `sqlite` backing store selection for signal and trajectory persistence
- `pillars` — strategic pillars
- `glossary` — domain-specific terms
- `people` — people references with roles and workstreams
- `leadership_readers` — readers with `cares_about`, `prefers`, `pet_peeves`
- `writing_style` — voice, structure, risk_framing patterns
- `tone_calibration` — overall tone + per-theme overrides
- `ado` — program-level ADO defaults (org, project, area_paths, work_item_types, optional `proposal_ttl_hours`)
- `ai` — program-level AI defaults (enabled, budget, deployments, local semantic index enablement, claim-extractor mode)
- `kusto` — program-level Kusto defaults
- `m365` — program-level M365/WorkIQ defaults, including optional direct IcM incidents URL when app-only IcM access is configured
- `vitality` — culture rollout config with per-surface feature flags
- `catchup` — session-start catchup controls including interval and WorkIQ time budgets
- `salience` — author-attention model controls and decay settings
- `readiness` — readiness-gate enablement and snapshot freshness thresholds
- `scorecard` — scorecard behavior toggles including dependency-risk uplift
- `audit` — autonomy-audit retention and archive warning thresholds
- `gather` — gather execution backend selection

Program-owned runtime metadata also includes:

- `trusted_baseline.yaml` — weekly continuation baseline state, trusted issue history, and bridge graduation marker
- `capability_status.yaml` — machine-readable completion or deferral state for later-wave external dependencies such as M365 activation and Graph app-only auth

### 8.3 Workstream Config

`programs/<prog>/workstreams.yaml` defines fluid workstream groupings:

- `id`, `name`, `aliases`, `description`, `why_it_matters`
- `area_paths` — ADO area path patterns for scoping
- `team_ids` — references to `knowledge/teams.yaml`
- `pm_owner`, `eng_owner`, `alternate` — owner aliases
- `style_note` — how to write about this workstream
- `leadership_sensitivity` — high/medium/low
- `status` — active/dormant/closed
- `ado_saved_query_ids` — saved query GUIDs

### 8.4 Scorecard Config

`programs/<prog>/scorecards.yaml` defines named scorecards with dimensions:

- Each dimension maps to a workstream via `workstream_id`
- Optional `ado_filter` for area-path overrides
- Composite key is `(scorecard_name, dimension_name)` — dimension names can repeat across scorecards

### 8.5 Knowledge Store

Shared knowledge at `knowledge/` (top-level) with program-local fallback at `programs/<prog>/knowledge/`:

| File | Content | Git-tracked | Required |
|------|---------|------------|----------|
| `people_directory.yaml` | Alias, email, title, tier, org_chain, team_ids | Yes | Yes |
| `people_profiles.yaml` | comm_style, cares_about, pet_peeves | No (`.gitignore`'d) | No — optional |
| `teams.yaml` | Team definitions with area paths | Yes | Yes |
| `products.yaml` | Product taxonomy | Yes | Yes |
| `golden_queries.yaml` | KQL query registry (validated flag per query) | Yes | Yes |

**Resolution order:** `knowledge/` (shared) → `programs/<prog>/knowledge/` (fallback).

**Referential integrity rules** (enforced by `vertex doctor --kb`):

1. Every `team_ids` ref in `people_directory.yaml` must exist in `teams.yaml`
2. Every `alias` in `people_profiles.yaml` must exist in `people_directory.yaml`
3. Every `pm_owner`/`eng_owner` in `workstreams.yaml` must exist in `people_directory.yaml`
4. Every `workstream_id` in `scorecards.yaml` must exist in `workstreams.yaml`

### 8.6 Editorial Rules

`programs/<prog>/editorial_rules.yaml` defines:

- `banned_phrases` — phrases forbidden in all rendered content (e.g., "due to", "caused by", "delve")
- `banned_openings` — forbidden sentence starters (e.g., "This week", "As mentioned")
- `verbosity` — word/sentence limits: blurb max words, exec summary max bullets, scorecard summary max sentences
- `stale_warn_days` / `stale_block_days` — freshness thresholds

### 8.7 Contract Files

Three contract files define structural rules:

| File | Purpose |
|------|---------|
| `template_contract.yaml` | Section ordering (mandatory/optional), per-layout-family rules |
| `slice_contracts.yaml` | Per-dimension data source contracts, freshness SLAs, degradation handling |
| `chapter_contract.yaml` | Chapter grouping definitions for continuity layout |

---

## §9 Functional Requirements

### 9.1 Evidence Gathering (`vertex gather`)

**Purpose:** Fetch evidence from external sources and append to the program's signal journal and trajectory store.

**Sources and auto-approval:**

| Source | `--flag` | Signal `src` | Auto-approved? |
|--------|---------|-------------|----------------|
| ADO OData (live items) | always | `ado/odata` | Yes |
| ADO Revisions (field changes) | always | `ado/revision` | Yes |
| WorkIQ Email | `--workiq` | `workiq/email` | No — requires review |
| WorkIQ Teams | `--workiq` | `workiq/teams` | No |
| Kusto (validated query) | `--kusto` | `kusto` | Yes |
| Kusto (unvalidated) | `--kusto` | `kusto` | No |
| IcM Incident | `--icm` | `icm` | Yes |
| Vertex Freshness | always | `vertex/freshness` | Yes |

**Key behaviors:**
- Deduplication uses source-specific fingerprinting (§7.2 signal dedup)
- Trajectories are appended only when key fields change (state, assigned_to, target_date, risk_level, area_path)
- Echo-chamber guard: gather skips revisions authored by Vertex itself or matching the Vertex comment header (`📊 Vertex`)
- Freshness findings are written as `vertex/freshness` signals for temporal tracking
- IcM signals resolve `owning_team` to workstream via `teams.yaml`
- WorkIQ signals require explicit human review before reaching published output
- All signal `text` is PII-scrubbed at gather time (max 500 chars)
- Optional WorkIQ, Kusto, and IcM integrations degrade without blocking the core ADO gather; remediation hints are persisted with the gather result and surfaced through `vertex doctor`, `vertex triage`, `vertex status`, and `vertex fleet`.

**UIL Channel Abstraction (Phases 0–5 complete):**

Vertex has migrated all signal-producing integrations to the Unified Integration Layer (UIL) — a Zone A channel abstraction with a common `DiscoveryProvider` / `HydrationProvider` / `SignalExtractor` protocol. Each UIL channel stores its registry in `programs/<prog>/channel_registry.sqlite3` (managed by `ChannelRegistryStore`). The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated via `VERTEX_UIL_KUSTO=1`, `VERTEX_UIL_TEAMS=1`, and `VERTEX_UIL_ICM=1`.

**Phase 4a parity gate PASSED (2026-05-27):** Two ADO UIL gather cycles completed; UIL=36 items, old-path OData=0 items, miss-set=0. ADO UIL is now the default path. **Phase 5 (ADO old-path removal) complete:** `_build_odata_filter`, `_load_program_items_from_ado`, `_load_freshness_program_items`, and `_load_live_program_items` have been removed from `gather.py`. Note: identically-named functions in `freshness.py` and `reconcile.py` are different functions serving those commands and are not affected. The full UIL migration design is archived at `.archive/specs/unify.md`.

Implemented UIL providers (Zone A, `src/core/`):

| Channel | Discovery | Hydration | Extraction |
|---------|-----------|-----------|------------|
| ADO | `ADODiscoveryProvider` | `ADOHydrationProvider` | `ADOSignalExtractor` |
| Kusto | `KustoDiscoveryProvider` | `KustoHydrationProvider` | `KustoSignalExtractor` |
| Teams | `TeamsDiscoveryProvider` | `TeamsHydrationProvider` | `TeamsSignalExtractor` |
| IcM | `IcMDiscoveryProvider` | `IcMHydrationProvider` | `IcMSignalExtractor` |

The `PROVIDER_REGISTRY` in `channel_wiring.py` maps channel names to their provider trio. `vertex integration discover` runs discovery for all configured channels and writes UIL health metadata to `gather_state.json`. `vertex integration show` displays per-channel registry status without instantiating providers.

### 9.1b Evidence Extraction (`vertex enrich` / `--extract-evidence`)

**Purpose:** Convert approved WorkIQ signals (meeting transcripts, Teams discussions) into structured `WorkstreamEvidence` records — the structured, lane-aligned evidence layer that feeds doctor checks, ETA credibility, and AI grounding.

**Two entry points:**

| Entry Point | When to Use |
|-------------|-------------|
| `vertex gather --program <prog> --extract-evidence` | Inline extraction immediately after gather; evidence is always current-run signals |
| `vertex enrich --edition <edition> [--since <date>]` | Standalone enrichment; re-enriches over a configurable window (default 7 days); idempotent |

**Key behaviors:**
- Filters transcript signals from the journal (`source="transcript"`, min 200 chars); groups by lane via `workstream_registry.yaml`
- Invokes `ContentExtractionAgent` (Zone B) per lane; parses `WorkstreamEvidence` (risk_level, blocking_items, etas, owners, confidence) from AI response
- Persists to `programs/<prog>/journal/evidence_store.jsonl` (last-write-wins per lane); records `EvidenceQualityRecord` to `evidence_quality.jsonl`
- Human corrections via `append_evidence_correction()` feed the edit-learner loop (`journal/evidence_corrections.jsonl`)
- Zone boundary maintained: `AgencyBridge` (Zone C) injected as a callable; extraction runs from the unrestricted `src/commands/gather_pipeline/` zone

**Doctor integration:**
- `check_eta_slippage` and `check_false_done_lanes` accept `evidence_override` to substitute loaded evidence over placeholder confidence=0.0 objects
- `check_evidence_quality_drift` detects per-lane confidence drop or all-zero confidence
- `run_context_doctor()` automatically loads the evidence store and passes it to evidence-aware checks

### 9.2 Signal Review (`vertex signals`)

- `vertex signals --program <prog>` — lists signals without review decisions, preserving review state and signal confidence in human, JSON, and CSV output
- `vertex signals review` — interactive loop: approve / dismiss / defer / skip / quit, preserving signal confidence in the human review prompt
- `vertex signals add "<text>"` — manually add a signal (auto-approved)
- `vertex signals link --signal <id1> --signal <id2>` — create signal thread for correlated signals

**Review sidecar:** `reviews.jsonl` is append-only. Last-write-wins for same `signal_id` and `record_type`. Signals without any review decision appear in the reviewer pane but not in published output.

**FR-SG-38 auto-approval:** When ≥10 signals from a single source have been reviewed and the approval rate is ≥80%, all remaining PENDING signals from that source are promoted to AUTO_APPROVED (floor guard: rate must also be >20%). Applied in `persistence_stage.py` during `vertex gather`. Parameters: `min_sample=10`, `ceiling_rate=0.8`, `floor_rate=0.2`. Once the auto-approval threshold is reached, `vertex signals review` is no longer required for that source in subsequent gathers.

### 9.2a Signal Sourcing & Synthesis Fidelity

**Purpose:** Vertex's quality ceiling is set by signal quality, not prose. This capability raises sourcing and synthesis fidelity along four layers — source identity & health → atomic extraction & classification → a durable program fact layer → cross-workstream synthesis — so that *every* function (newsletter, nudge, risk registry, action tracking, review, report) draws from one fact graph rather than re-deriving state.

**Governing principle — fail loud, never silent.** A required input that is missing, stale, auth-failed, or zero-yield is made visible, attributed, and blocking (or explicitly waived with owner/date/reason) rather than silently absorbed and shifted back onto the PM.

- **Source health (FR-SG-05/06).** Each scorecard dimension declares a `SourceContract` (required/optional source roles, min freshness, min yield) in `slice_contracts.yaml`. `doctor --channels` reports `SourceHealth` per role; unhealthy required roles enforce the bounded source-health gate (QG-SG-01), while unhealthy optional roles remain warning-only. Transient failures are waivable via `source_waivers.yaml`; structural `unbound` misconfiguration is not. Narrative/onboarding programs (no slice contracts) are evaluated against available channels and never hard-blocked.
- **Atomic, classified extraction (FR-SG-08/09/10).** Collaboration content (WorkIQ/Teams/transcript/email) decomposes into individual signals, each carrying non-empty work-item `refs` and a resolved workstream; every stored signal carries a `SignalClass`. Classification weights synthesis, routes decisions, and de-prioritizes status-only noise.
- **Program Fact Store (FR-SG-54..73).** A program-scoped, bitemporal, append-only system-of-record (`program_fact_store.py`) holds typed facts (Claim, Decision, Action, Risk, MetricObservation, ExternalDependency, ProgramEvent, Milestone, ReviewFinding). A write precedence hierarchy — `Active PM Judgment > Confirmed Governance Decision > Verified System Signal > Raw Telemetry` — means a lower-precedence write contradicting a higher-precedence accepted fact is queued as a **Proposed Revision**, never a silent overwrite. `report` pins a `ProgramFactSnapshot` to the draft; `confirm` raises a **State Drift Warning** (QG-SG-20) if live facts have materially diverged. Issue N+1 *inherits* the last accepted fact instead of carrying forward a `❓ Needs input` override (current-truth inheritance). The full storage + API contract is reconciled in [vertex-tech-spec.md](vertex-tech-spec.md) §9.16. *Migration status: the unified read API and shadow-write foundation are landed, with shadow isolation, lineage reverse lookup, per-family SoR mode resolution, divergence detection, and accepted flip-gate policy implemented. The irreversible flip to system-of-record still requires clean-cycle evidence and rollback posture before any family is treated as primary in production.*
- **Synthesis & judgment (FR-SG-19..25).** Per-workstream evidence packets, strategic risk derivation, ETA credibility scoring with slip-history rendering, auto section-scope suppression, and cross-workstream executive summaries extract value from data already present.
- **Closed loops (FR-SG-37..44).** Nudge outcome tracking, evidence-governed risk auto-upsert, per-function ConversionFidelity, QG-failure root-cause learning, signal-approval auto-enforcement, and earned-autonomy maturity advancement — all audited and PM-reversible — turn one-off detections into learning loops.

**Operator-gated dependencies:** live M365 identifier population, Kusto auth/RBAC, IcM enablement, cross-team ADO PAT scope, and the Teams bot / Outlook add-in registration are configuration/access tasks owned by the operator, not the platform.

### 9.3 Draft Generation (`vertex report` / `vertex draft`)

**Purpose:** Render a newsletter draft from current data. `report` and `draft` are permanent aliases.

**Pipeline steps:**

1. **Resolve edition** → program → workstreams → knowledge → editorial rules → overrides → narratives
2. **Fetch ADO items** — OData query with resolved area paths and work item types
3. **Load evidence** — journal signals, review decisions, trajectories, rolling summaries
4. **Compute deltas** — `build_deltas(current_items, previous_snapshot)` → `DeltaSet`
5. **Build scorecards** — `build_scorecard(items, dimensions, prev_confirmed)` → `DimensionRisk[]`
6. **Build evidence packets** — per-item revisions, comments, enrichments → `EvidencePacket`
7. **Build freshness report** — stale/overdue/ghost/placeholder items
8. **Trajectory analysis** — `analyze_trajectories()` → `DriftPattern[]`
9. **ETA forecasting** — `forecast_etas(trajectories, drift_patterns)` → confidence-weighted predictions
10. **Dependency cascades** — detect cross-workstream impact from `key_dependencies`
11. **Scorecard trends** — load archive history, compute direction/consecutive-high counts
12. **Coverage gaps** — active items with no signals and no narrative mention
13. **Vitality scoring** — per-item freshness/richness/leakage composite
14. **Altitude guard** — filter signals/patterns by edition altitude
15. **AI synthesis** (optional) — workstream blurbs, exec summary, rolling summary context
16. **Render** — select archetype template, build `RenderContext`, produce HTML/EML/Markdown
17. **Quality gates** — ban-list, verbosity, risk input, manifest integrity
18. **Write output** — `publications/<edition>/issue_NNN.{html,eml,md,manifest.json,snapshot.json}`

**Key flags:** `--dry-run` (no archive writes), `--diff` (compare to prior dry-run), `--ai-review` (AI advisory), `--no-ai` (suppress AI), `--as-of` (override ADO timestamp), `--format` (stdout output format)

### 9.4 Triage (`vertex triage`)

**Purpose:** Prioritized author checklist showing everything that needs attention before an editing session.

**Output sections:**
- **🔴 Blockers:** Quality gate failures (e.g., `❓ Needs Input` = QG-8 hard block)
- **🟡 Needs Attention:** Unreviewed signals, ETA drift items needing narrative decision, missing narratives, stale claims, stale narratives (narrative file predates latest ETA change)
- **📋 Coverage Gaps:** Active items with no signals or narrative mention
- **📊 ADO Vitality:** Items updated this week, owners with stale items, freshness average
- **🟢 Ready:** Completed narratives, set overrides, passing quality gates
- **Draft readiness score:** Percentage with breakdown

### 9.4a Next-Step Guidance (`vertex next`)

**Purpose:** Provide a bounded, ranked "what should I do next?" operator surface over the active issue instead of requiring the PM to inspect multiple commands manually.

**Operating modes:**
- **Edition mode (`vertex next --edition <edition>`):** Computes up to 3 ranked suggestions from the latest confirmed issue, the current draft manifest, quality-gate state, freshness summary, narrative readiness, and override completeness.
- **Goal mode (`vertex next --goal <name> --program <id>` or `--edition <edition>`):** Prints a static operating recipe from `program.yaml -> goals`, including ordered `vertex ...` steps and optional `success_when` criteria.

**Bounded scope:**
- Advisory only: `vertex next` never mutates repo state, writes to ADO, or advances issue/archive state.
- Ranked output is intentionally capped at 3 suggestions to keep the PM action set focused and deterministic.
- Edition-mode suggestions are limited to concrete control-loop blockers already tracked by Vertex, including stale drafts, QG-8 risk gaps, QG-5 verbosity violations, missing narratives, pending section approvals, freshness blocks, and empty `top_3_now`.
- Goal-mode output is config-authored and deterministic; it does not invent new plans at runtime.

### 9.5 Confirmation (`vertex confirm`)

**Purpose:** Promotes a draft to confirmed status. Archives snapshot, validates all quality gates.

**Behaviors:**
- Only `snapshot_store.write_confirmed()` writes confirmed snapshots (single write path invariant)
- Quality gates must pass (or be force-overridden for forceable gates; hard blocks cannot be forced)
- `❓ Needs Input` is a hard publish-block (QG-8, exit code 3)
- After confirm: claims and decision-asks extracted from narratives (AI-first when enabled, regex fallback available, max 20 per confirm)
- Edit patterns captured (draft vs. confirmed narrative diff → `edit_patterns.jsonl`)
- Signal usage markers appended to `reviews.jsonl`
- Vitality archive entry written to `vitality.json`
- Override and narrative files reset for next issue

### 9.6 Freshness Report (`vertex freshness`)

**Purpose:** Identifies stale work items grouped by DRI. Author-facing only, never published.

**Severity levels:**

| Severity | Condition |
|----------|-----------|
| 🛑 BLOCK | ETA 30+ days overdue AND risk High/Off Track |
| ⚠ WARN | ETA overdue OR stale 14+ days |
| ⏰ INFO | Approaching deadline (≤5 biz days) OR status changed |
| ⚡ BAD FRESH | Updated recently but placeholder/copy-paste content |

**15+ finding rules** (FR-20 through FR-47): ghost items, unowned items, stale items, overdue ETAs, approaching deadlines, placeholder detection, bad fresh detection, etc.

**Freshness-to-signal bridge:** Freshness findings are also written as `vertex/freshness` journal signals during gather, enabling temporal tracking across weeks.

### 9.7 Review Workflow

**Per-section review (`vertex review-sections`):**
- `show` — display review status for all sections
- `set --section <id> --state <state>` — record approval/rejection with note
- `clear` — reset a section's review state

**States:** `pending`, `sent`, `approved`, `skipped_no_delta`, `changes_requested`, `rejected`

**Leadership review (`vertex review-full`):**
- Generates a two-pane HTML: newsletter (left) + evidence/context (right)
- Includes: review status per section, evidence packets, drift patterns, anticipated questions, vitality bars, open claims/asks, coverage gaps
- Opens in browser

### 9.8 Override System (`vertex override`)

**Purpose:** Interactive scorecard dimension risk-level override editor.

**Interactive mode:** Walks through each dimension showing evidence. Options: [L]ow [M]edium [H]igh [D]one [C]lear [S]kip [K]eep.

**Override file:** `programs/<prog>/overrides/issue_NNN.yaml` contains:
- `top_3_now[]` — author-curated (never auto-generated) decision strip items
- `scorecards{}` — per-scorecard → per-dimension → `{risk, note, hide_details, summary}`

**Resolution rule:** Override risk takes precedence over derived risk. Published scorecard shows only the resolved value.

### 9.9 Vitality System (`vertex vitality`)

**Three signals:**

| Signal | What It Measures | Source |
|--------|-----------------|--------|
| **Freshness** | Days since last meaningful ADO update | Trajectory data, ADO revisions/comments |
| **Richness** | Field completeness (0–100 rubric) | ADO item fields |
| **Leakage** | WorkIQ signals with no ADO update in same window | Journal + trajectory |

**Richness rubric (per item, 0–100):**

| Check | Points | Rule |
|-------|--------|------|
| Target Date present | 25 | Present and in the future |
| Recent owner comment | 25 | Comment by `AssignedTo` within 14 days |
| Risk assessment | 15 | Override or custom field exists |
| Description quality | 15 | Non-empty, >50 chars |
| Blocker clarity | 10 | Blocked items mention blocker + owner |
| Next step | 10 | Latest comment contains concrete action |

**Composite score formula:**
- Pre-WorkIQ: `freshness_pct × 0.6 + richness_pct × 0.4`
- Full: `freshness_pct × 0.4 + richness_pct × 0.3 + (1 - leakage_ratio) × 0.3`
- Sparse-data fallback: if `total_workiq_signals < 5`, use pre-WorkIQ formula

**Exclusions:** Terminal items (Resolved/Closed/Removed). Initial items (Proposed/New) < 7 days old. Aliases with `exempt_from_vitality: true` in knowledge or `vitality.exempt_aliases` in program config.

**Five graduated surfaces:**

| Surface | Audience | Intensity | Feature Flag |
|---------|----------|-----------|-------------|
| Triage section | Author only | Low | `vitality.surfaces.triage` |
| Newsletter aggregate | All readers | Low | `vitality.surfaces.newsletter_aggregate` |
| Reviewer-pane per-owner bars | Author only | Low | `vitality.surfaces.reviewer_pane` |
| ADO nudge comments | Item owners | Medium | `vitality.surfaces.ado_nudge_comments` |
| ADO board tags | Item owners (visual) | Medium | `vitality.surfaces.ado_tags` |

**Privacy boundary:** ADO nudge comments never quote WorkIQ content. Use neutral language: "Recent non-ADO activity was detected for this item."

**Nudge cooldown:** Max 1 nudge per item per `cooldown_days` (default 14 days; configurable in `hygiene.cooldown_days`).

**Vitality trend:** Aggregate vitality stored in `vitality.json` on confirm. Trend computed from confirmed archive history.

### 9.10 ADO Write-Back

**Propose → Preview → Apply → Audit pipeline:**

1. `vertex ado propose --type <comment|field|vitality_nudge|vitality_tag>` — generates manifest in `publications/<scope>/ado_proposals/`
2. Author reviews terminal preview
3. `vertex ado apply --proposal <id>` — executes with interactive confirmation or `--yes`
4. Every write logged as `vertex/ado_update` journal signal

**Update types:**

| Type | What It Does |
|------|-------------|
| `comment` | Structured citation comment on work items in a confirmed issue |
| `field` | Update ADO custom fields from Vertex data (opt-in via `ado_field_map.yaml`) |
| `vitality_nudge` | Neutral comment on chronically stale items (composite <40, stale >14d) |
| `vitality_tag` | Add/remove configurable tag (default: `Needs-PM-Review`) on coverage-gap items |

**Safety guards:**
- Proposals expire after a default 72 hours (configurable via `proposal_ttl_hours`)
- `proposal_ttl_hours` is authored under `program.yaml -> ado`
- The shared ADO TTL applies to `comment`, `vitality_nudge`, and `vitality_tag` proposals; `field` proposals may override it in `ado_field_map.yaml`
- Revision-based concurrency check before each write — skip if work item modified since proposal
- Duplicate comment detection — skip if Vertex header already present
- Locked proposal manifest during apply (prevents concurrent apply runs)
- Per-entry result tracking: `applied`, `failed`, `conflict`, `skipped`
- Re-run applies only `pending`/`failed` entries (idempotent)

**ADO status (`vertex ado status`):** Area path coverage, orphaned items, coverage gaps, last gather stats.

**ADO reconciliation (`vertex ado reconcile`):** Read-only diagnostic comparing Vertex overrides/claims against ADO state.

### 9.11 Trajectory Analysis

**Drift pattern detection** (deterministic, Zone A):

| Pattern | Rule | Severity |
|---------|------|----------|
| `eta_drift` | `target_date` changed ≥2 times in 90 days, always later | High if ≥3 slips, Medium if 2 |
| `chronic_reassign` | `assigned_to` changed ≥3 times in 90 days | Medium |
| `state_oscillation` | State cycled Active↔Resolved ≥2 times in 90 days | Medium |
| `stale` | No trajectory point in 90 days AND state is Active | Low |

**ETA forecasting:**
- 0 slips → confidence=high, base slip probability
- 1 slip → confidence=medium, +0.2 slip probability
- 2 slips → confidence=low, +0.4 slip probability
- 3+ slips → confidence=low, slip_probability = max(0.8, base)
- Rendered as "(low confidence — N prior slips, M% miss probability)" alongside ADO TargetDate

**Velocity metrics:** When Kusto is disabled, trajectory-derived Active→Resolved throughput and cycle-time metrics appear as a synthetic section.

### 9.12 Claim Tracking (`vertex claims`)

**Extraction:** During `vertex confirm`, claims and decision-asks are extracted from narratives through an AI-first path when AI is enabled, with governed regex fallback retained for calibration, resilience, and explicit reproducibility runs. Max 20 claims per confirm. Deduplication against existing open claims (same entity_refs + similar due_date within ±7 days).

**Lifecycle:** Claims start as `open`. Status updates (`met`, `contradicted`, `stale`) are appended to `claims.jsonl` as sidecar entries. Claims are never auto-closed — author resolves via `vertex claims resolve`.

**Staleness detection:** During draft/triage, open claims are checked against current trajectories. If claimed ETA has passed or ADO state contradicts the claim, it is flagged.

**Decision-asks:** Separate type tracked alongside claims. Open asks appear in triage and deck editions.

### 9.13 Publish Diff (`vertex diff`)

**Purpose:** Semantic diff between current draft and a reference point.

**Reference modes:** `--since last-confirmed`, `--since last-draft`, `--since issue-N`

**Diff content:** Item deltas (new/closed/risk changes), scorecard risk movements, narrative section diffs, approved signal changes, drift pattern changes.

### 9.14 Coverage Gap Detection

Active ADO items (state ∉ excluded_states, age > 7 days, not in dormant workstreams) with:
- No approved signals in current journal window, AND
- No narrative mention

Reported in triage, draft readiness, and reviewer pane (helicopter altitude). Suppressed at satellite altitude.

### 9.15 Scorecard Trend Analysis

Loads last N confirmed scorecards from archive (default: 4 issues). Per-dimension: current risk, prior risk sequence, trend direction (improving/stable/worsening), consecutive-high count. Rendered as annotations: "⬆ High for 4 consecutive issues", "⬇ Improved from High to Medium".

### 9.16 Dependency Cascade Detection

When a signal or drift pattern fires on a `from_item` in `Program.key_dependencies`, the pipeline surfaces downstream impact in the affected workstream's section. Single-hop only (no transitive closure).

### 9.17 Anticipation Engine

**Layer 1 (Zone A, deterministic):** Detects patterns warranting leadership questions — ETA drift on a reader's topic, risk escalation, stale workstreams, dependency chain impact.

**Layer 2 (Zone B, AI):** Generates natural-language questions with suggested responses using reader profiles.

**Surface:** Reviewer pane and prep brief only. Never in published output.

### 9.18 Meeting Prep (`vertex prep`)

For satellite-altitude editions only. Generates `prep_brief.md` with: latest draft summary, anticipated questions, unresolved drift patterns, recent unincorporated signals, open decision-asks, scorecard trend summary.

### 9.19 KB Management (`vertex kb`)

- `vertex kb changelog --since <week>` — diffs `people_directory.yaml` across git commits
- `vertex kb update "<correction>" [--apply]` — AI-assisted KB correction with safety model: patch preview → schema validation → referential integrity check → explicit apply → audit trail

### 9.20 Owner Packs (`vertex owner-pack`)

Per-owner Markdown remediation packet: their items, high/medium risks, stale items, open decision-asks, matching ADO proposal entries, vitality mini-scorecard (composite score, fresh count, avg richness, leakage).

### 9.21 Deck Companion (`vertex deck-companion`)

Plain Markdown deck from confirmed or draft state. No HTML, no colors. Emoji icons. Includes health rows, top risks, what changed, open/closed asks. Suitable for PowerPoint paste.

### 9.22 Journal Archival (`vertex archive-journals`)

Moves weekly journal partitions older than a given week to `programs/<prog>/journal_archive/`. Archived files remain readable by historical queries (active + archived weekly files scanned together).

### 9.23 Historical Backfill (`vertex backfill`)

Discovers historical newsletters via M365 search and extracts structured data using AI. Modes: auto, offline, m365, hybrid.

`scripts/backfill_archive_to_journal.py` — seeds journal and claims from confirmed archive history.

### 9.24 Onboarding

**`vertex setup`** — Conversational, AI-assisted onboarding for first-time users. Discovers plausible workstreams and scorecard dimensions from live ADO data (or guided manual mode when ADO is unavailable), renders a live HTML preview before writing any files, and supports session save/resume. Falls back to deterministic heuristics if AI is unavailable. Produces a valid, doctor-passing program in under 10 minutes. Additive to `vertex onboard`.

**`vertex onboard`** — Interactive form wizard to scaffold a new program/edition. Generates `editions/<id>.yaml`, `programs/<prog>/program.yaml`, workstreams, scorecards, editorial rules, review config. Supports V2 update-mode merges. Power-user path for explicit, deterministic config generation.

### 9.25 Rolling Summaries (`vertex summarize`)

AI-generated per-workstream summaries from approved signals + drift patterns. Compressed to <500 words. Incremental by default; `--reset` regenerates from raw signals. Staleness warning if >14 days old with no new signals.

### 9.26 DRI Notifications (`vertex notify`)

**Purpose:** Generate or send per-DRI freshness notifications from the current freshness report.

**Channels:**

| Channel | Behavior | Current scope |
|---------|----------|---------------|
| `eml` | Writes manual-send draft `.eml` files per DRI | Default L0-L1 path |
| `adaptive-card` | Writes Adaptive Card JSON; posts to Teams incoming webhook when configured | Informational L0-L1 card path |
| `email` | Sends through delegated Graph mail auth | Explicit opt-in preview path |

**Key behaviors:**
- Recipients come from freshness DRI grouping, not a separate notification roster.
- Content is a per-DRI stale-item list with ADO deep links and neutral language.
- `--dry-run` previews the run without sending or recording confirmation-side notify state.
- Non-dry-run runs record notification state/logs for auditability.
- Manual-send EML remains the primary operational path until broader M365 automation is enabled.

### 9.26a Watch Polling (`vertex watch`)

**Purpose:** Poll already-configured signal sources between full gathers so the PM can monitor intraday or daily changes without turning Vertex into a daemonized service.

**Bounded scope:**
- `vertex watch` is a foreground CLI loop. It polls on an operator-specified interval, emits per-cycle summaries, and stops on Ctrl+C.
- It is read-side signal discovery only. `vertex watch` may append newly discovered signals, auto-review records, and trajectory updates through the same governed gather/watch substrate, but it does not publish drafts, confirm issues, or apply external write-backs.
- Source scope is maturity- and readiness-bound. The default source set is `ado`; `icm` is auto-added only for intraday cadence when the program has a proven ready path. `workiq`, `kusto`, and explicit `icm` polling require corresponding auth/config readiness.
- It is not a replacement for `vertex gather`; it is a lightweight monitoring loop for already-approved, already-wired channels.

### 9.27 L1 Epistemological Engine

The L1 engine provides a typed telemetry substrate for hypothesis-driven program observability. It is a Zone A subsystem stored in a per-program SQLite database at `~/.vertex/<program_id>/vertex.sqlite3`.

**Core lifecycle:**
- **Hypothesis:** A testable belief about program health (`proposed → confirmed → invalidated/rejected`). Confirmed hypotheses drive assertion evaluation.
- **TelemetryAssertion:** A policy rule binding a hypothesis to a metric with a threshold/delivery-date/staleness check and a version-controlled evaluation policy.
- **MetricObservation:** A typed time-series observation (ADO-sourced, Kusto-sourced, or PM-injected `MANUAL`) for a metric. Ingestion runs track provenance.
- **RealityChallenge:** A breach detected by `reconcile_reality()` — threshold failure, missed delivery date, stale metric, or manual override. Lifecycle: `open → snoozed → dismissed/acknowledged`.
- **RealityDigestModel:** Aggregated reconciliation output — challenge list, freshness, snooze state, evaluation results. Cached per program.

**Commands:** `vertex hypothesis`, `vertex assertion`, `vertex observation inject`, `vertex reality digest/snooze/pending-review/challenges/dismiss/reopen`, `vertex bootstrap`, `vertex admin db/auth/reconcile/metric/notifications/assertion`.

**Implementation status:** L1-M0, L1-M1, and L1-M2 are complete. L1-M3 core is substantially complete: CompositeAssertion authoring/evaluation, BETWEEN, dependency cascade, cross-program reality digest rollup, linear forecast assertions, and burn-rate assertions are implemented and covered by focused tests. Remaining L1 work is operational proof, live metric provisioning in the production reality store, and later non-linear/calibration-aware forecast extensions. See `.archive/specs/gaps.md` T0-11 and the V-3 reality loop gate.

---

## §10 CLI Reference

### 10.1 Entry Point

```
vertex <command> [options]
```

Installed entry points: `vertex = "cli:app"` and `vx = "cli:app"` in `pyproject.toml`. Built with Typer.

The generated operator reference lives at [specs/cli-reference.md](specs/cli-reference.md) and is refreshed from the live Typer command tree via `scripts/generate_cli_reference.py`.

### 10.2 Command Summary

| Command | Purpose | Key Flags |
|---------|---------|-----------|
| `catchup` | Session-start or on-demand change sweep | `--program`, `--since`, `--no-scan` |
| `brief` | Private operator brief with staged interventions | `--program`, `--today`, `--approve`, `--dismiss` |
| `next` | Ranked next-step guidance or config-authored goal recipe | `--edition`, `--goal`, `--program` |
| `report` / `draft` | Render newsletter draft | `--edition`, `--dry-run`, `--offline`, `--reseed`, `--no-seed`, `--diff`, `--ai-review`, `--no-ai`, `--as-of`, `--format` |
| `confirm` | Archive confirmed issue | `--edition`, `--dry-run`, `--force`, `--ack-forecast`, `--untrusted`, `--reason` |
| `gather` | Fetch evidence from sources | `--program`, `--cadence`, `--workiq`, `--kusto`, `--icm`, `--analytics`, `--sprints`, `--pipelines`, `--dependency-scout` |
| `calibration` | Forecast and claim-extraction calibration reporting | `report`, `--program`, `--since`, `--format` |
| `reconcile` | Contradiction review across sources | `--edition`, `--refresh`, `--format`, `--dry-run` |
| `freshness` | Freshness report | `--edition`, `--since`, `--teams-format`, `--notify` |
| `triage` | Readiness checklist | `--edition` |
| `readiness` | Readiness snapshot and gate reporting | `--edition`, `--refresh`, `--format` |
| `vitality` | ADO vitality scores | `--program`, `--owner`, `--workstream` |
| `diff` | Semantic diff | `--edition`, `--since` |
| `override` | Interactive risk editor | `--edition`, `--dimension` |
| `edit` | Open narrative in editor | `--edition`, `--section` |
| `evidence` | Show attribution lineage | `--edition`, `--section`, `--claim`, `--ado` |
| `doctor` | System health check | `--edition`, `--fix`, `--channels`, `--kb`, `--analytics-parity`, `--recover-confirm`, `--context`, `--fix-hints`, `--ids`, `--source-waivers` |
| `history` | Browse archive | `--edition`, `--last`, `--issue`, `--diff`, `--search`, `--semantic` |
| `index` | Local semantic index maintenance | `rebuild`, `optimize`, `--edition`, `--if-needed` |
| `review-full` | Leadership review pane | `--edition`, `--open/--no-open` |
| `review-sections show/set/clear/export` | Per-section review | `--edition`, `--section`, `--state` |
| `publish-gate` | Validate gates only | `--edition`, `--force` |
| `manifest` | Show run manifest | `--edition`, `--issue` |
| `deck-companion` | Markdown deck output | `--edition`, `--issue` |
| `meeting-close` | Transcript-to-action closure packet with inline review/apply handoff | `--program`, `--transcript`, `--meeting-id`, `--html`, `--teams`, `--promote-actions`, `--apply-ado` |
| `notify` | DRI freshness notifications | `--edition`, `--issue`, `--channel`, `--dry-run` |
| `ask` | Advisory natural-language command router | freeform request, `--program`, `--edition` |
| `list editions/workstreams/dris` | List configured resources | `--edition` |
| `setup` | Conversational AI-assisted onboarding concierge | `--from-description`, `--demo`, `--preview`, `--auto`, `--manual`, `--resume`, `--update`, `--dry-run` |
| `onboard` | Interactive form wizard (power-user path) | `--edition`, `--update`, `--ai` |
| `backfill` | Historical newsletter extraction | `--edition`, `--source`, `--since` |
| `summarize` | Rolling AI summaries | `--program`, `--reset`, `--workstream` |
| `prep` | Meeting prep brief | `--edition` |
| `owner-pack` | Per-owner remediation packet | `--program`, `--owner` |
| `salience` | Inspect or reset author salience state | `show`, `reset`, `--program` |
| `trust` | Trust and autonomy calibration reporting | `--program`, `--slice`, `--action`, `--format` |
| `archive-journals` | Archive old journal files | `--program`, `--before` |
| `audit` | Journal and workflow audit reporting | `--program`, `--from`, `--to`, `--format` |
| `backup` | Backup snapshot creation and verification | `--to`, `--verify`, `--format` |
| `fleet` | Cross-program fleet health and dependency status; includes context health columns: `context_maturity_level` (L0–L4), `context_invariant_errors` (error count), `context_stale_file_count` | `--format` |
| `status` | Current edition or program status surface | `--edition`, `--format` |
| `escalate` | Rule-based escalation draft generation or send | `--edition`, `--decision-ask`, `--dry-run`, `--channel`, `--format` |
| `nudge` | Full-hygiene heat-map EML **draft** dispatch (`X-Unsent: 1`, never sent) — generates one audience-governed nudge draft from per-edition `full_hygiene` config; EML written to `programs/<prog>/nudge/drafts/{run_id}.eml`; `FullHygieneRow` covers QG-1 (stale_business_days), QG-9 (is_overdue), and QG-24 (has_risk_assessment) natively; milestone-backed deadlines and `[Action DUE …]` subject context resolve via `ProgramReality`; audience policy is enforced before draft creation; `--approve-draft` persists operator approval for approval-gated recipient changes; cooldown is anchored to the attested/imported send record created by `--mark-sent` / `--import-sent`, not to draft generation; nudge state is schema 1.2 and publication index is schema 1.1; run audited to `nudge/nudge_audit.jsonl`; M365/DPA approval is NOT required (nudge is ADO hygiene outreach, not M365 content extraction) | `--program`, `--dry-run`, `--approve-draft`, `--mark-sent`, `--import-sent`, `--sent-at`, `--list-drafts`, `--stale-a`, `--stale-b`, `--stale-c` (deprecated shim) |
| `dependencies` | Infer and review dependency proposals | `--program`, `accept`, `dismiss`, `--format` |
| `watch` | Poll ready signal sources in a foreground monitoring loop | `--program`, `--interval`, `--source`, `--cadence` |
| `synthesize` | Edition-scoped AI synthesis and proposal generation | `--edition`, `--issue`, `--dry-run` |
| `propose` | Generate section revision proposals from seeded narratives + fresh evidence | `--edition`, `--issue`, `--dry-run`, `--offline`, `--ai`, `--no-ai` |
| `review-proposals` | Render read-only proposal review HTML for pending section revisions or resolved proposal history | `--edition`, `--issue`, `--section`, `--resolved-only`, `--open/--no-open` |
| `apply-proposals` | Accept, reject, bulk-accept, undo, or interactively modify/apply section revision proposals | `--edition`, `--issue`, `--accept`, `--accept-modified`, `--reject`, `--accept-all`, `--interactive`, `--undo`, `--yes`, `--dry-run` |
| `actions` | Action register management with grouped meeting-close summaries, recurring cross-meeting pattern surfacing, and reviewed-batch apply handoff | `list`, `review`, `add`, `resolve`, `--program`, `--format`, `--apply-ado` |
| `risks` | Risk register management | `list`, `add`, `review`, `resolve`, `--program`, `--format` |
| `milestones` | Milestone health and authoring workflows | `list`, `assess`, `update`, `--program`, `--format` |
| `decisions` | Decision register management | `list`, `add`, `resolve`, `aging`, `nudge`, `governance show`, `governance edit`, `--program`, `--format` |
| `assumptions` | Assumption register management | `list`, `add`, `validate`, `--program`, `--format` |
| `bridge-status` | Continuation bridge graduation metrics | `--edition`, `--graduate`, `--format` |
| `maturity-check` | L0-L4 maturity precondition reporting | `--edition`, `--format` |
| `config` | Governed program-config inspection, schema validation, migration, and mutation | `get`, `set`, `validate`, `migrate`, `--program`, `--edition`, `--format`, `--dry-run` |
| `policy` | Promote learned local approval policy into active rules | `promote`, `--program`, `--rule`, `--dry-run` |
| `audit archive` | Archive autonomy-audit history by explicit cutoff date or configured retention window | `--program`, `--before`, `--retention`, `--format` |
| `migrate` | Storage migration and analytics rebuild | `--program`, `--to`, `--rebuild-analytics`, `--dry-run` |
| `probe-ado` | ADO connectivity probe | |
| `ado status` | ADO coverage report | `--program` |
| `ado propose` | Generate ADO update manifest | `--program`, `--type`, `--edition`, `--issue` |
| `ado apply` | Apply ADO updates | `--proposal`, `--yes` |
| `ado reconcile` | Vertex vs. ADO state comparison | `--program` |
| `signals` | List pending signals with review-state and confidence visibility | `--program`, `--format` |
| `signals review` | Interactive signal review with confidence-preserving prompts | `--program`, `--reviewer` |
| `signals add` | Manual signal entry | `--program`, `--workstream`, `--ref` |
| `signals link` | Link correlated signals | `--signal`, `--thread` |
| `registry list` | List M365 registry artifacts (bridges to UIL when `channel_registry.sqlite3` present); emits deprecation warning when routing to UIL | `--program`, `--source auto|yaml|uil`, `--format` |
| `registry confirm/reject/promote/reassign` | Governance operations on M365 registry; dual-writes to UIL when `channel_registry.sqlite3` present; emits deprecation warning | `--program`, `--series-id`, `--thread-id` |
| `integration show` | UIL channel registry status | `--program`, `--channel`, `--provider-instance-id` |
| `integration discover` | Run UIL discovery, write registry, update gather_state | `--program`, `--channel`, `--dry-run` |
| `integration list` | List UIL registrations with status/workstream/channel filters | `--program`, `--channel`, `--status`, `--workstream`, `--format` |
| `integration confirm/suppress/promote/reassign/signal-yield` | UIL governance operations | `--program`, `--channel`, `--ref-id`, `--alias`, `--note` |
| `integration candidates` | Review persisted source-discovery candidates with queue filters | `--program`, `--requires-decision`, `--status`, `--workstream`, `--source-type`, `--json` |
| `integration candidate-accept/reject/reassign/candidate-clear-rejection` | Govern source-candidate lifecycle and reversible rejection windows | `--program`, `--pm-alias`, `--reason`, `--workstream`, `--intent-id` |
| `integration intent-suppress/retire/intent-clear-suppression/intent-reopen` | Govern declared source intents without silently editing authored config | `--program`, `--workstream`, `--kind`, `--name`, `--pm-alias`, `--reason` |
| `integration seed-id` | Manually attach a durable meeting/chat/email ID when autonomous discovery is exhausted | `--program`, `--intent-id`, `--ref-id`, `--pm-alias`, `--reason` |
| `integration explain-source` | Explain one source end to end: intent status, attempts, candidates, decisions, and next action | `--program`, `--intent-id`, `--ref-id`, `--ref-kind` |
| `integration schema-migrate` | Idempotent UIL schema migration | `--program`, `--force` |
| `integration prune` | Prune retired/suppressed/expired UIL registrations | `--program`, `--channel`, `--before`, `--dry-run` |
| `integration migrate` | Migrate `m365_registry.yaml` into UIL Teams channel | `--program`, `--dry-run` |
| `integration report` | UIL channel health report with delta history | `--program`, `--channel` |
| `integration feedback` | Show UIL registry feedback event history | `--program`, `--channel` |
| `integration health` | UIL channel health summary across all channels | `--program` |
| `claims` | List open claims/asks | `--program` |
| `claims resolve` | Resolve a claim | `--id`, `--status`, `--note` |
| `kb changelog` | KB change history | `--program`, `--since` |
| `kb update` | AI-assisted KB correction | `--program`, `--apply` |
| `hints` | Narrative delta hints — review, accept, reject, or modify proposed workstream narrative improvements | `--edition`, `--issue`, `--workstream`, `--accept`, `--reject`, `--modify`, `--dry-run` |
| `facts export/import/rebuild` | Program Fact Store backup, export, and rebuild — serialize the fact store to JSON, ingest a JSON dump, or re-persist canonical authored-state from program YAML/JSONL | `--program`, `--path` |
| `facts parity-check` | Compare fact-store families against legacy YAML/JSONL sidecars and report parity ratio per family | `--program` |
| `facts dual-read-log` | Run N parity-check cycles and append one JSONL record per cycle to `fact_store_parity_log.jsonl`; exit 0 only when every cycle passes | `--program`, `--cycles`, `--interval`, `--quarantine/--no-quarantine` |
| `facts pin-snapshot` | Pin the current fact snapshot to a confirmed issue; returns a `pin_id` | `--program`, `--issue-number` |
| `facts detect-drift` | List fact revisions recorded after a pinned snapshot; exit 0 on no drift, exit 2 on drift | `--program`, `--snapshot-id` |
| `connectors poll` | Poll read-only non-ADO external dependency connectors (SharePoint Lists, GitHub Issues) and persist `ExternalDependency` snapshots | `--program`, `--dry-run` |
| `ledger write` | Write a single typed ledger event to the program event log | `--program`, `--event-type`, `--occurred-at`, `--payload`, `--actor` |
| `ledger correct` | Append a correction supersession event for a previously written event | `--program`, `--event-id`, `--field`, `--value`, `--actor` |
| `ledger triage list/approve/edit/reject/skip` | Govern staged discovery candidates through the a/e/r/s lifecycle | `--program`, `--status`, `--batch-id`, `--actor` |
| `ledger status` | Operator dashboard: event counts by type/confidence, pending/active queue sizes, triage metrics, field-lock counts, batch progress, verify status | `--program`, `--format` |
| `ledger history` | Browse ledger event history with filters by type, date, entity, or confidence | `--program`, `--event-type`, `--entity`, `--since`, `--until`, `--format` |
| `ledger replay` | Rebuild the current projection from the authoritative event log | `--program`, `--as-of`, `--dry-run` |
| `ledger verify` | Hash-chain and projection integrity check; redaction-aware | `--program`, `--deep`, `--persist` |
| `ledger export/import` | JSONL export/import for archival, migration, or cross-program replication | `--program`, `--since`, `--output` |
| `ledger gaps` | Surface acknowledged/unacknowledged coverage gaps from discovery runs | `--program`, `--unacknowledged`, `--format` |
| `ledger backfill` | Staged batch ingest from historical artifacts (LT decks, newsletters, Artha KB); enforces QG-DM-9 acceptance criteria | `--program`, `--source`, `--batch-id`, `--dry-run`, `--quarantine-batch` |
| `ledger redact` | Compliance redaction: scrub a specific event payload and register the redaction in `.redactions.jsonl` | `--program`, `--event-id`, `--reason`, `--actor`, `--scrub-field` |
| `ledger redact-vault` | Cascade-redact: delete an evidence vault entry and redact all referencing events | `--program`, `--vault-hash`, `--reason`, `--actor` |
| `discover candidates` | Run discovery pipelines (AI extractors + M365 connectors) and stage output as ledger candidates; `--source prose_extract` targets EML/HTML/PDF newsletters and PPTX LT decks using 4-wave prose extraction (W1=phase/scope/workstream, W2=same, W3=commitment/assumption/dependency/incident, W4=KPI/knowledge) | `--program`, `--source`, `--since`, `--dry-run`, `--connector`, `--source-dir <dir>`, `--wave 1|2|3|4` |
| `knowledge ingest` | Ingest knowledge documents into the content-addressed vault and extract claim candidates | `--program`, `--path`, `--dry-run` |
| `knowledge extract` | AI-assisted extraction of KnowledgeClaims from a vault document | `--program`, `--vault-hash`, `--dry-run` |
| `knowledge triage list/approve/edit/reject/skip` | Govern extracted knowledge claims through the a/e/r/s lifecycle | `--program`, `--status`, `--actor` |
| `knowledge context` | Show the resolved knowledge context for a program at a given time | `--program`, `--as-of`, `--scope`, `--format` |
| `rollback` | Restore program stores from a checkpoint snapshot taken before a fact-layer promotion; `--drill` runs a side-effect-free sandbox simulation and records an `s7a_rollback_drill` proof entry | `--edition`, `--to`, `--drill`, `--archetype`, `--notes`, `--dry-run` |

### 10.3 Top-Level Flags

- **`--skip-issue --reason <text>`** — record next issue as intentionally skipped in the archive index. No draft generated; skip reason archived.

### 10.4 Output Conventions

- **Exit codes:** 0 = clean, 2 = warnings, 3 = blocks, 4 = corruption
- **`--dry-run`** = produce output artifacts but no archive writes, no external sends
- **Terminal output:** one line per stage, quiet by default (`--verbose`/`--debug`)
- **Color:** terminal only; stripped when piped
- **Browser open:** `--dry-run` auto-opens HTML in browser; `--no-open` suppresses

---

## §11 AI Layer

### 11.1 Architecture

Zone B (`src/ai/`) — 16 content modules, 3 safety modules, 9 prompt templates. All AI features are opt-in (`ai.enabled: true` in program config). When disabled, the pipeline produces deterministic output with no AI calls.

### 11.2 Safety Pipeline (`src/ai/_pipeline.py`)

All AI-generated text passes through a mandatory 4-stage pipeline (`process_generated_text`) before reaching any output surface. **Enforcement is mandatory:** every generation path in `src/ai/` must call `process_generated_text`; direct inline safety checks (`scan_text` + `InjectionDetector`) outside this wrapper are forbidden and enforced by contract test.

1. **PII scrubbing** — strip emails (non-MS filtered), phones, SSNs
2. **Injection detection** — regex patterns for prompt injection phrases, delimiters, base64, data URIs, webhooks
3. **Causality sanitization** — rewrite causal claims ("caused by" → "observed with")
4. **Grounding** — every sentence must cite an allowed work item; uncitable sentences removed

### 11.3 Content Generation

| Module | Purpose | Output |
|--------|---------|--------|
| `blurb_generator.py` | Per-workstream newsletter blurbs from deltas/evidence | ≤4 sentences, ≤90 words, delta-first |
| `exec_summary_drafter.py` | Executive summary from ranked changes | ≤150 words (Archetype A) |
| `summary_generator.py` | Rolling workstream summaries from approved signals | <500 words per workstream |
| `anticipation_engine.py` | Predicted leadership questions per reader persona | ≤5 questions with suggested responses |

### 11.4 Review & Learning

| Module | Purpose |
|--------|---------|
| `draft_reviewer.py` | Pre-publish advisory: data gaps, cross-issue flags, structural issues |
| `edit_learner.py` | Captures before/after edit patterns for continuous learning |
| `learning_distiller.py` | Distills repeated corrections into editorial rule proposals |

### 11.5 Routing & Onboarding

| Module | Purpose |
|--------|---------|
| `intent_router.py` | Natural-language → CLI command mapping |
| `onboard_assistant.py` | AI-suggested scorecards, dimensions, writing style during onboard |
| `backfill_extractor.py` | Structured extraction from historical newsletters |

### 11.6 Cost Management

- **Per-run budget:** `ai.budget_usd_per_run` in program/edition config
- **Cost guard:** `CostGuard` tracks spend per edition, enforces ceiling
- **Context budget:** Token-budget management truncates dated updates/comments to fit LLM windows
- **LLM trace:** Every AI call logged to JSONL with tokens, latency, cost

### 11.7 Feature Policy (`ai_policy.yaml`)

`vertex/policies/ai_policy.yaml` carries per-feature configuration for all named AI features:
- **`max_tokens`, `temperature`, `model_tier`** — per-feature generation parameters
- **`frontier_eligible`** — **real operator kill switch** enforced at deployment-resolution time
  (`deployment_fallback.py::resolve_ai_deployments_for_feature`). When `False`, the function
  returns `()` regardless of configured deployments, forcing the feature onto its deterministic
  fallback path before any client is constructed or any token is spent.
- **AI proposal TTL:** `AI_PROPOSAL_TTL_DAYS = 14` constant in `src/core/ai_proposal_store.py`
  governs proposal expiry (GC sets status → `EXPIRED`) and `doctor --ai-proposals` reporting.
  The synthesize call site defaults to this constant; changing to 7d or 21d is a one-line change.

---

## §12 M365 Integration Layer

### 12.1 Architecture

Zone C (`src/m365/`) — 13 modules. External I/O only: writes to journal (via gather), reads from M365 services.

**UIL Migration (Teams channel):** The M365 registry (`m365_registry.yaml`) is being migrated to the UIL `teams` channel. `vertex integration migrate` performs a dry-run or live migration of all `M365RegistryArtifact` entries (preserving confidence, pm_confirmed, promoted, signal_yield, display_name, workstream bindings, and all `M365RoutingFeedbackEvent` history). Phase 6a ran successfully (4 artifacts migrated for Acme). Once a Teams-channel parity gate is passed, `m365_registry.yaml` becomes a read-only backup and Zone C Teams lookup is replaced by UIL registry reads. The migration design is archived at `.archive/specs/unify.md`.

### 12.2 Agency Bridge

`AgencyBridge` wraps a subprocess CLI for MCP tool invocation. Supports:
- ADO tools: `get_work_items`, `get_revisions`
- WorkIQ tools: `ask_work_iq` (NL primary — confirmed present); `search_emails`, `search_teams`, `get_meetings`, `get_transcript` (typed fallbacks — uncertain availability, fail-silently if absent)
- Probe: validates CLI availability and capabilities

For Acme maturity workflows, WorkIQ queries are now generated per workstream from `workstreams.yaml -> signal_sources.workiq_keywords` and `workiq_exclude_keywords`, with program-level M365 prompt defaults retained as a compatibility fallback.

### 12.3 Service Clients

| Client | Service | Via |
|--------|---------|-----|
| `GraphMailClient` | Email search | WorkIQ `ask_work_iq` NL primary + `search_emails` typed fallback |
| `GraphCalendarClient` | Calendar search | WorkIQ `ask_work_iq` NL primary + `get_meetings` typed fallback |
| `TeamsReader` | Teams message search | WorkIQ `ask_work_iq` NL only (no typed fallback) |
| `TranscriptReader` | Meeting transcripts | WorkIQ `ask_work_iq` NL primary + `get_transcript` typed fallback |
| `GraphSendClient` | Send emails | Graph API `POST /me/sendMail` (requires admin-consented service principal) |

Note: `bluebird` (`agency mcp bluebird`) is an ADO/code-search MCP only — it has no access to personal M365 data. All personal data tools (email, calendar, Teams, transcripts) route through `workiq`. In the Microsoft corp tenant, direct Graph API access (`Mail.Read`, `Calendars.Read`, `ChatMessage.Read`) requires tenant-admin pre-consent and is blocked for delegated-auth flows; `workiq.exe` is the sanctioned pre-approved path.

### 12.4 ADO Writer

`ADOWriter` applies update proposals to live ADO work items:
- Reads proposal manifest → locks manifest file → re-fetches live work item state before each write
- Applies comment/tag/field actions
- Skips duplicate Vertex comments
- Updates per-entry status incrementally
- Auto-logs applied writes as `vertex/ado_update` journal signals

### 12.5 Enricher

`M365Enricher` orchestrates mail/calendar/Teams/transcript metadata collection per workstream, mapping findings onto work items as non-ADO evidence for the reviewer pane.

### 12.5.1 WorkIQ Retrieval Requirements

- **FR-WIQ-1 — Safe default:** broad mailbox discovery defaults to `legacy_nl`; structured enumeration is an explicit program or workstream policy and can be rolled back without migration.
- **FR-WIQ-2 — Deterministic requests:** structured discovery uses an absolute date window, a bounded result count, and byte-identical prompts across configured union repetitions.
- **FR-WIQ-3 — Fail-closed intake:** Vertex accepts only bounded records with a usable durable or deterministic preview identity, an ISO-8601 timestamp inside the requested window, no terminal-control content, and either no URL or an allowlisted Outlook URL. Invalid records never become signals.
- **FR-WIQ-4 — Resilient recall:** qualified programs may union one to five uncached repetitions; semantic deduplication prevents repeated messages from multiplying review debt.
- **FR-WIQ-5 — Governed evidence:** structured enumeration creates scrubbed, preview-only Stage-A signals. Body extraction remains review-gated and must not persist rich M365 content without credential scanning, privacy enforcement, source-specific approval, and grounding provenance.
- **FR-WIQ-6 — Promotion gate:** structured discovery becomes a platform default only after representative multi-program yield and relevance validation, an evidence-aging or auto-review policy for pending signals, and completion of the governed body-evidence safeguards.
- **FR-WIQ-7 — Private qualification:** live subjects, senders, permalinks, quotes, mailbox identity, and row-level relevance judgments remain in ignored/restricted operator storage. Repository fixtures must be synthetic or irreversibly sanitized.

### 12.6 M365 Route Promotion & Autonomous Discovery

Vertex treats discovered M365 routes as governed candidates, not auto-trusted configuration.

- `workstreams.yaml -> signal_sources` is discovery intent, not durable source truth. Resolved bindings, candidate evidence, and negative discovery results live in UIL state (`source_intents`, `source_candidates`, `candidate_intent_matches`, `discovery_attempts`) inside `channel_registry.sqlite3`.
- `vertex gather --workiq` continuously attempts durable-ID resolution for declared meeting/chat/email intents through WorkIQ-backed Teams, mail, calendar, and transcript discovery. Unique high-confidence single matches are auto-resolved into UIL registrations immediately; ambiguous or lower-confidence matches remain pending for PM review.
- Auto-resolution is bounded and auditable. Gather suppresses recently rejected candidates for 60 days and records `stale_plan` instead of writing when a PM/operator decision changes an intent's `decision_version` mid-plan.
- `vertex integration candidates`, `candidate-*`, `intent-*`, `seed-id`, and `explain-source` form the governed operator loop for the remainder, so PMs only need to copy opaque IDs when autonomous discovery is exhausted.
- `vertex doctor --operator-gates` classifies remaining discovery debt into six actionable categories: `auto-resolvable`, `pm-decision-required`, `operator-seed-required`, `auth-admin-required`, `source-absent`, and `config-mismatch`.
- `vertex gather --workiq` refreshes `m365_registry.yaml`, updates rolling `signal_yield_last_3`, and surfaces promotion-ready or promotion-blocked artifacts in terminal output and `gather_state.json`.
- `vertex doctor` exposes the same state through `M365 Registry Review` and `M365 Registry Promotion` checks so operators can see whether artifacts are ready for current promotion, blocked by recent rejection, blocked by missing `series_id` / `thread_id`, or blocked by insufficient recent signal yield.
- `vertex registry promote <artifact_id> --program <id>` is the governed promotion path into `workstreams.yaml` signal sources. Promotion is allowed only when the artifact has the required durable identifier, enough recent signal yield, and either PM confirmation or earned high-confidence auto-promotion eligibility.
- High-confidence auto-promotion eligibility is bounded: email threads and chat/channel artifacts must sustain a confidence streak plus sufficient recent signal yield before promotion can proceed without a prior PM confirm event.
- Promotion is auditable and reversible. Feedback actions such as confirm, reject, rename, reassign, and ID attachment are persisted in routing-feedback history and are respected by later promotion decisions.

### 12.7 REV — Program-Context Intelligence Pipeline

REV is Vertex's **M365 collaboration arm of program-context intelligence**. It converts the standing "gather --workiq refresh is ineffective / newsletters need dozens of manual iterations" problem into a governed, measurable pipeline: **Plan → Enumerate → Resolve identity → Hydrate → Vault → Extract → Verify → PENDING triage**.

#### 12.7.1 Purpose and scope

REV re-bases M365 discovery on delegated, deterministic Graph API surfaces so that each refresh enumerates relevant and changed items, hydrates and vaults full content, extracts schema-valid grounded facts, and surfaces a small, high-confidence review queue — while honestly reporting what it could not see. Every fact enters PENDING, carries vault-backed evidence, and only accepted, verified facts reach drafting. The existing approval gate, single-write path, zone boundary, and PII scrubber are unchanged.

**Phase 1 walking skeleton (implemented):** mail only — local `.eml` export import (`EmlEnumerator` enumeration + `EmlHydrator` MIME hydration; 3-directory inbox→claimed→processed atomicity) + prose extraction + two-stage vault + PENDING staging + verification-at-intake + triage → projection. **Graph API delegated scopes (`Mail.Read` etc.) are permanently blocked by Microsoft IT policy** (ADR-008) — there is no consent path; local desktop export is the production ingestion path.

**Phase 2 (implemented):** LLM extractor (`LLMRevExtractor`, `--extractor llm`) + judge harness (`judge_extractions()`); quality-floor gate (G-xtract-prec ≥80%, G-accept-prec ≥85%, per-type recall ≥50%, Cohen's kappa ≥0.70); corpus export (PII-scrubbed backup bundle); extraction + judge caches (90d TTL + LRU-500); pre-commit hook; gap-fill loop driver (`_drive_gap_lifecycle`); processed-dir rotation; local-import health telemetry in `doctor --rev-health`.

**Phase 3 multi-surface (implemented, 2026-06-24):** calendar `.ics` events via `IcsEnumerator`+`IcsHydrator` (`icalendar>=5.0`; SEQUENCE-highest VEVENT wins; RRULE expansion ≤52; cancellation; organizer CN only — no `mailto:` per OA-9 privacy); SharePoint/local docs via `LocalFileEnumerator`+`LocalFileHydrator` (`python-docx>=1.1`; VBA macro denial; `pypdf` PDF text extraction; `pdf_no_text` quarantine). Teams export **BLOCKED** pending enterprise ZIP format feasibility spike (P3-2a).

#### 12.7.2 Functional requirements (FR-PCI-1..13)

| ID | Requirement | Status |
|---|---|---|
| **FR-PCI-1** | Entity-specific Query Planner: context-derived KQL compilers for Message/Event/Teams/SharePoint/NL | ✅ Implemented (`src/core/rev/query_planner.py`) |
| **FR-PCI-2** | CandidateEnumerator: `CollectionSearchEnumerator` (Phase-1 mail default) + `SearchApiEnumerator`/`SearchHitLocator` (secondary) | ✅ Implemented (`src/m365/rev/enumerators.py`) |
| **FR-PCI-3** | Canonical Identity: three-stage `HydrationLocator → CanonicalItemIdentity → SourceRouteIdentity` via `ItemToRouteBinder`; ImmutableId at hydration GET | ✅ Implemented (`src/core/rev/identity.py`) |
| **FR-PCI-4** | ChangeFeed/delta: folder-scoped mail delta, calendarView delta, SharePoint driveItem delta; sync-state TTL/LRU eviction | ✅ Implemented as operator-gated stubs (`src/m365/rev/change_feeds.py`, `src/core/rev/sync_state.py`) |
| **FR-PCI-5** | ContentHydrator + normalize + chunk + cache: mail hydration ladder (`uniqueBody → body → conversation → attachment`); 500-char overlap; stable chunk IDs | ✅ Implemented (`src/m365/rev/hydrator.py`, `src/core/rev/normalizer.py`) |
| **FR-PCI-6** | Two-stage evidence lifecycle: Stage 1 ephemeral (retrieve → Prompt-Shield-scan → privacy gate) → Stage 2 persist (vault admitted excerpts only, `EvidenceRef` tuple) | ✅ Implemented (`src/core/ledger/rev_evidence.py`, `src/core/rev/privacy.py`, `src/core/rev/prompt_shields.py`) |
| **FR-PCI-7** | Chunked Structured Extraction: `DeterministicRevExtractor` (regex event markers + `EvidenceSpan` codepoint spans); LLM probe + `json_object` fallback deferred to live P0 | ✅ Implemented (`src/ai/rev/extractor.py`) |
| **FR-PCI-8** | Layered Verification + append-only `VerificationAssertion`: quote/span + entity/date/value deterministic checks; `triage approve` rejects unless `source_verified` / `human_verified`; effective state derived from assertions (QG-DM-2 preserved) | ✅ Implemented (`src/ai/rev/verification.py`, `src/core/ledger/verification_assertions.py`, `src/commands/ledger.py`) |
| **FR-PCI-9** | Capability-port framework + multi-budget governor + durable run-state machine: per-dimension budgets (search/bytes/chunks/tokens/spend/wall-clock); `enumerated → hydration_required → excerpts_vaulted → candidate_staged → candidate_verified → accepted`; quiet-lane early exit | ✅ Implemented (`src/core/rev/governor.py`, `src/core/rev/run_state.py`, `src/core/rev/pipeline.py`) |
| **FR-PCI-10** | Copilot Retrieval semantic chunks (license-gated) | ⏳ Deferred to RV-S1(f) |
| **FR-PCI-11** | NL/A2A fuzzy fallback: existing `AgencyBridge.ask_workiq` is NL fallback; A2A spike-gated (RV-S1-A2A) | ⏳ A2A deferred; NL fallback active |
| **FR-PCI-12** | Split measurement: `doctor --rev-health` reports G-enum (categorical), G-schema, G-processed, verification distribution, legacy_unverified count, hydration fallback rate, pending queue age | ✅ Implemented (`src/core/rev/health.py`, `src/commands/doctor.py`) |
| **FR-PCI-13** | Coverage maturity + gap lifecycle: `GapStatus` (open/filling/resolved/reopened), `ContextGapRecord` with transition log, `CoverageMaturity`; active gap-fill loop deferred (OS-7) | ✅ Implemented (`src/core/ledger/gap_lifecycle.py`) |

#### 12.7.3 Config — capability profiles

```yaml
m365:
  retrieval:
    profile: legacy_nl | search_hydrate | rev_verified   # default: legacy_nl (backward-compat)
    auth_scope_tier: personal_comms_mail                  # Phase 1: Mail.Read only
    fallback_policy: fail_visible                         # no silent NL degrade
    evidence_policy: excerpt_vaulted                      # required for rev_verified
    budgets:
      max_search_requests_total_per_cycle: 60
      max_hydrated_bytes_per_cycle: 10_485_760
      max_wall_clock_seconds: 600
```

Config rejects unsupported combinations (e.g. `rev_verified + metadata_only`) and surfaces degraded operation prominently. **Default is `legacy_nl`** (no change for existing programs).

#### 12.7.4 CLI surfaces

- **`vertex rev run --program <id> --mailbox <upn> [--eml-inbox <local-path> | --mock-fixture <path>]`** — runs one REV retrieval cycle and stages candidates in PENDING. The production ingestion path is local export import (`--eml-inbox` processes `.eml`/`.ics`/`.docx`/`.pdf` files via the respective Zone C enumerators; 3-dir atomicity inbox→claimed→processed); Graph consent is **permanently blocked by Microsoft IT policy** (ADR-008). `--mock-fixture` exercises the full pipeline with no live data.
- **`vertex doctor --rev-health [--rev-program <id>]`** — reports REV subsystem health: enumeration-completion distribution, run-state counts, verification-assertion distribution, evidence-vault retention state, Prompt-Shields mode, hydration fallback rate, `legacy_unverified` count, inbox staleness, and quarantine telemetry.
- **`vertex rev rotate-processed --program <id>`** — housekeeping: moves stale/surplus files from `processed/` → `processed/archive/` (>90 days mtime or >500 files). Auto-fires best-effort at cycle end.
- **`vertex rev export-corpus --program <id> --output <path> [--include-vault]`** — exports a PII-scrubbed backup bundle (candidates, triage decisions, labeled corpus, optional vault excerpts). Direct identifiers are hash-redacted; display names preserved.

#### 12.7.5 Evidence and verification contracts

- **RV-E1** (no M365 candidate without evidence): every M365+AI-extracted `CandidateEvent` must carry `evidence_refs: tuple[EvidenceRef, ...]` (≥1) and each ref must have a `vault_hash` (QG-DM-8). Backward-compatible readers parse old records without `evidence_refs` as `evidence_refs=()`.
- **RV-VP1** (verification at intake): `triage approve` rejects unless the effective verification state is `source_verified` (non-material) or `human_verified` (material). `project_program_events()` never projects an unverified event (QG-DM-2 preserved). Verification is recorded as append-only `VerificationAssertion` ledger records; effective state is derived, never mutated.
- **RV-V1** (quote/span verifier): `check_quote_span` verifies `canonical_text[start:end] == excerpt_text`; a seeded fabricated fact is blocked before `triage approve`.
- **RV-A1** (PENDING never drafts): existing drafting invariant — candidates pending triage are not projected; `project_program_events()` projects only accepted events.

#### 12.7.6 Three-workflow separation (no shared state)

| Workflow | State type | Store | Promotion gate |
|---|---|---|---|
| **A — Source discovery** | `SourceCandidateStatus` | `m365_registry_store.py` | §13.5 (durable id + yield ≥3 + no rejection) |
| **B — Signal discovery** | `SignalReviewStatus` | `signal_review.py` | FR-SG-38 auto-approval |
| **C — Fact discovery (REV)** | `CandidateDecisionRecord` | `candidate_store.py` + `ledger.py` | `triage approve` (verification precondition) |

FR-SG-38 governs Workflow B only. Workflow C uses ledger triage exclusively. Shared identity (from Workflow A `SourceRouteIdentity`) is read-only input to Workflow C; state machines never merge.

#### 12.7.7 Operator-gated items

- **P0 / RV-S1**: ~~live consent (`Mail.Read` minimum; per-capability gates for Calendar/Teams-Chat/Teams-Channel/SharePoint/Content-Safety/A2A)~~ — **WITHDRAWN (permanently).** All delegated Microsoft Graph API scopes are permanently blocked by Microsoft IT policy for custom Entra app registrations (ADR-008); there is no consent path. The production ingestion path is **local desktop export import** (`.eml` mail via `--eml-inbox`; `.ics` calendar, Teams export, SharePoint docs via local-export Zone C ports in Phase 3). Without an inbox, `vertex rev run` without `--mock-fixture`/`--eml-inbox` prints the local-export reference and exits 2. Azure Content Safety (Prompt Shields) is a separate, non-Graph resource and remains operator-provisionable (OA-2).
- **RV-S2**: 3 live mail-only cycles (G-enum `complete`, G-schema 100%, G-processed ≥95%) — operator-gated (operator drops real `.eml` exports into a local inbox).
- **RV-Q1**: P1 quality floor (labeled mail corpus, G-xtract-prec ≥80%, G-accept-prec ≥85%) — human/operator-gated.

#### 12.7.8 Activation — the activation sentence has fired on real data (2026-07-07)

The operator-felt promise of REV is not "the pipeline runs" — it is **the activation sentence**: *a fact Vertex detects from a real source EML, after a human approves it, appears — cited and reverse-linkable to that EML — in the next real render, and demonstrably changes what it says.* As of 2026-07-07, this is proven true for the first time on a real pilot program's data (see [tech-spec §13.6.7](vertex-tech-spec.md#1367-activation--real-data-proof-hardening-contracts-and-self-verification-2026-07-07) for the exact mechanism and the five previously-undetected engineering gaps this exposed and closed on first real contact).

**The four operator benefits this proves the substrate can deliver:**
1. **Reports assemble themselves from live, validated facts** — the draft cites its sources; "what changed" is computed, not remembered.
2. **Vertex surfaces source disagreement** before the operator finds out in a meeting (cross-source conflict detection is wired into the REV cycle finalize path).
3. **Routine review shrinks over time** — consistently-approved signals flow automatically (tracked longitudinally as fleet scale grows; see `specs/backlog.md` §4 non-goals).
4. **The governance loop closes** — confirmed truth writes back into the Plane-1 stores that compile `ProgramContext` (NCFL apply, §12.9 [if present] / `.archive/specs/activation.md` §6.6).

**What remains** is not engineering — it is external provisioning (an Azure Content Safety resource, for the non-degraded clean-cycle authority ladder) and human annotation labor (a dual-labeled, κ-certified corpus for the keystone family). Both are tracked as an executable-gate-mapped feature spec at `specs/backlog.md`, which is the canonical place to check current status (re-run `scripts/verify_activation.py --program <id>` for a live snapshot).

---

## §13 Visual Design & Rendering

The UX Spec ([vertex-ux-spec.md](vertex-ux-spec.md)) is binding for visual design. Key constraints summarized here.

### 13.1 Email Constraints

- **Max width:** 680px outer table, 640px content area
- **Font stack:** `Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif`
- **Inline CSS only** — no `<style>` blocks, no JavaScript, no media queries
- **`<table>` layout** — `cellpadding="0" cellspacing="0"`, no `<div>`
- **Hex colors only** — no CSS variables, rgb(), hsl()
- **Images:** CID or base64 ≤100KB; prefer Unicode emoji

### 13.2 Color System

**Color = Risk. Always. Only.** Color is never decorative. All non-risk elements are monochrome.

| Risk Level | Background | Foreground | Icon |
|-----------|-----------|-----------|------|
| High | `#FEE2E2` | `#991B1B` | 🔴 |
| Medium | `#FEF3C7` | `#92400E` | 🟡 |
| Low | `#D1FAE5` | `#065F46` | 🟢 |
| Done | `#DBEAFE` | `#1E40AF` | ✅ |
| Unknown | `#F3F4F6` | `#4B5563` | ⚪ |

### 13.3 Typography Scale

| Element | Size | Weight | Color |
|---------|------|--------|-------|
| Title | 20px | 600 | `#111827` |
| Section header (H2) | 16px | 600 | `#111827` |
| Body text | 14px | 400 | `#374151` |
| Table header | 11px | 600 | `#6B7280` |
| Table cell | 13px | 400 | `#111827` |
| Caption/metadata | 11px | 400 | `#9CA3AF` |

**Spacing rhythm:** 8px base unit (32/16/12/8px).

### 13.4 Three-Speed Reading Model

1. **Glance (≤10s):** Health Banner + Nav Bar
2. **Scan (≤60s):** Top 3 Now + What Changed + Scorecards
3. **Deep dive (2–8 min):** Exec Summary + Workstream sections

### 13.5 Assembly Order (Detailed Archetype)

Navigation Bar → Health Banner → Top 3 Now → What Changed → Scorecards → Executive Summary → Workstream Deep Dives → Provenance Footer

### 13.6 Archetype Templates

| Archetype | Template | Layout |
|-----------|---------|--------|
| Detailed weekly | `detailed.j2` or `continuity.j2` | Full assembly with scorecards and deep dives |
| Condensed daily | `condensed.j2` | Compact health + change summary |
| Narrative | `narrative.j2` | Narrative-driven, no scorecards |
| Deck | `deck.j2` | Plain Markdown for slide paste |
| Lookback | `lookback.j2` | Quarterly retrospective with aggregate metrics |

### 13.7 Rendering Pipeline

1. **`html_renderer.py`** — builds `RenderContext` from `ReportData`, dispatches to archetype template
2. **`deck_renderer.py`** — builds `DeckRenderContext`, renders via deck template
3. **`teams_renderer.py`** — builds Teams/Adaptive Card Markdown
4. **`reviewer_renderer.py`** — builds reviewer-pane HTML with evidence/context panels
5. **`eml_writer.py`** — wraps HTML in RFC 2822 .eml file

### 13.8 Key Rendering Rules

- **Delta-first posture:** What Changed before Scorecards; blurbs lead with delta
- **Show-by-exception:** Unchanged dimensions get muted "— no change" badge
- **Dates over issue numbers** in all reader-facing surfaces
- **Top 3 Now is never auto-generated** — author-curated only
- **Verbosity enforcement:** 150 words exec summary (detailed/focused editions), 90 words blurbs
- **Attribution:** Three-tier (inline `🔗`, section-level `Sources:`, reviewer-level full lineage)
- **`❓ Needs Input`:** Gray chip, hard publish-block
- **`⚡ Verify`:** Amber chip for medium-confidence evidence

### 13.9 Jinja2 Template System

**Base templates (4):** `base.email.j2`, `base.deck.j2`, `base.reviewer.j2`, `base.teams.j2`

**Archetypes (7):** Extend base templates, dispatch to partials by section kind.

**Partials (25):** Reusable components — `health_banner.j2`, `scorecard.j2`, `what_changed.j2`, `exec_summary.j2`, `workstream.j2`, `nav_bar.j2`, `risk_chip.j2`, `delta_badge.j2`, `ado_vitality.j2`, `vitality_reviewer.j2`, `reviewer_anticipated_questions.j2`, `provenance_footer.j2`, etc.

**Custom filters:** `risk_label`, `delta_label`, `build_anchor` — provided via `jinja_filters.py`.

---

## §14 Quality Gates & Editorial Contract

This section defines product-level quality-gate intent and publish semantics. Exact gate registry functions and evaluator contracts live in [vertex-tech-spec.md](vertex-tech-spec.md).

### 14.1 Quality Gate Framework

Quality gates are evaluated during `vertex confirm`. Gate phases:

| Phase | Gates | Blocking? |
|-------|-------|-----------|
| Phase 1a | Ban-list violations, verbosity violations, manifest integrity, risk input completeness | Hard block on `❓ Needs Input` (QG-8); warnings on others |
| Phase 1b | Freshness violations, overdue targets, material-change narrative coverage, claim contradictions, chronic high-risk escalation, contradiction-narrative acknowledgment (QG-17), and high-risk signal coverage (QG-13 hard block) | Configurable (QG-13 is never overridable) |
| Phase 1c | Review status, HTML leak detection, and Outlook/email compatibility checks (QG-18) | Configurable |
| Phase 1d | High-risk next-best-action coverage, open actions with owner/due-date coverage, milestone risk-link coverage, and cross-program dependency cascade resolution planning (QG-19) | Advisory (forceable) |
| Chart gates | QG-20 chart freshness advisory, QG-21 decoded PNG size hard gate (100 KiB), QG-22 publish-blocking chart staleness | QG-21/22 are hard blocks; QG-20 is advisory (forceable) |
| Bridge | Prior section roster preserved, scorecard composition preserved, seeded narrative updated | Forceable during bridge period; retire after bridge graduation |
| Continuity | 9 HTML structural checks (CG-01–09) for continuity layout | Forceable |

**Exit codes:** `0` = all passing, `2` = warnings (forceable), `3` = hard blocks (unforceable), `4` = corruption

### 14.7 Quality Gate Registry

| Gate | Check | Hard Block? | Forceable |
|------|-------|-------------|----------|
| QG-4 | Ban-list violations = 0 | Yes | No |
| QG-5 | Verbosity violations = 0 | Yes | No |
| QG-6 | Manifest exists AND snapshot_hash matches | Yes | No |
| QG-8 | No dimensions with `❓ Needs Input` (unknown risk level) | Yes | No |
| QG-13 | All active High-risk items have signal or narrative coverage | Yes | No |
| QG-1 | `freshness_report.blocks == 0` | No | Yes |
| QG-2 | Hygiene warnings = 0 | No | Yes |
| QG-3 | All review sections approved (when `review_required: true`) | No | Yes |
| QG-7 | Archive index consistent | No | Yes |
| QG-9 | No overdue target dates on non-terminal items | No | Yes |
| QG-10 | High-risk/closed items since last issue have authored narrative coverage | No | Yes |
| QG-11 | No contradictions between current ADO state and prior-issue claims | No | Yes |
| QG-12 | No scorecard dimension at High risk for ≥3 consecutive issues without escalation | No | Yes |
| QG-17 | Workstreams with multiple contradiction packets must acknowledge at least one contradicted work item in their authored narrative | No | Yes |
| QG-18 | Rendered email HTML must remain Outlook-safe: no `<style>` blocks, no flex/grid inline layout, table styling stays inline, and inline hex colors stay within the canonical palette | No | Yes |
| QG-14 | High-risk dimensions have a next-best-action item or explicit override note | No | Yes |
| QG-15 | Open actions have both an owner and a due date | No | Yes |
| QG-16 | Milestone risk-linked items have a trajectory entry in the last 14 days | No | Yes |
| QG-19 | Cross-program dependency cascades detected from unresolved dependencies must carry an explicit resolution plan | No | Yes |
| QG-B1 | Prior trusted section roster preserved or explicitly retired via `removed_sections` | No (bridge hard block) | Yes (with `--force`, recorded in manifest) |
| QG-B2 | Prior trusted scorecard composition preserved or explicitly revised via `removed_dimensions` | No (bridge hard block) | Yes (with `--force`, recorded in manifest) |
| QG-B3 | Seeded narrative files differ from trusted-baseline source OR section is explicitly approved via `review-sections set` | No | Yes |
| QG-20 | Chart freshness: TTL not yet exceeded (advisory) | No | Yes |
| QG-21 | Decoded PNG size ≤ 100 KiB per chart section | Yes | No |
| QG-22 | Chart freshness: data is too stale to publish (blocking TTL threshold) | Yes | No |
| QG-23 | Exec summary semantic similarity to ADO evidence below 0.82 threshold (soft warn) | No | Yes |
| QG-24 | Metric injection failure — one or more KPI sections failed to render data (soft warn) | No | Yes |
| QG-25 | Email signal yield zero across 3+ consecutive gather cycles for active workstreams (circuit breaker) | No | Yes |
| QG-26 | External dependency state: critical/blocker dep not in a terminal state ("closed", "fulfilled", "merged") and not fulfilled blocks confirm; vacuous pass when no `external_dependencies.jsonl` (WS-2) | No | Yes |
| QG-27 | Truth-level gate — dual mode: (a) hard block if any material-disputed `fact.conflict` is unresolved; (b) advisory if any fact's truth level is below `SOURCE_VALIDATED` (WI-3.9) | Yes (a: material disputes) / No (b: advisory) | No (a) / Yes (b) |
| QG-28 | KPI query degradation: one or more Kusto/telemetry query is degraded (`is_degraded=True`) in last gather cycle — advisory circuit breaker before publish (WS-1 PB-4) | No | Yes |
| QG-SG-01 | Source-health gate: a required slice-derived ADO/telemetry/decision source role that is unhealthy, stale, or zero-yield blocks confirm unless waived with owner/date/reason | No | Yes |
| QG-SG-09 | Contradiction gate: unresolved cross-source contradictions on an entity's state/risk/ETA must be acknowledged before confirm | No | Yes |
| QG-SG-20 | State Drift Warning: live Program Fact Store materially diverged from the snapshot pinned to the draft at `report` time | Yes | No |
| QG-DM-1 | Ledger hash-chain integrity: `vertex ledger verify` passes (no content-hash mismatches, no broken prev-hash links) | Yes | No |
| QG-DM-2 | Projection determinism: replaying the same event log from any ordering of events produces identical projection output | Yes | No |
| QG-DM-3 | Operator-correction coverage: all supported field types have at least one registered correction event type and supersession logic | No | Yes |
| QG-DM-4 | Hardlock immutability: a projection snapshot locked at confirm time cannot be overwritten or deleted without an explicit `ledger redact` with compliance reason | Yes | No |
| QG-DM-5 | Gap-detection SLA: unacknowledged coverage gaps older than 30 days surface as an advisory | No | Yes |
| QG-DM-6 | Candidate triage latency: candidates in the active queue older than 14 days or with >100 pending items surface as an advisory | No | Yes |
| QG-DM-7 | Conflict budget: open field-lock conflicts exceeding the per-program threshold surface as an advisory listing | No | Yes |
| QG-DM-8 | Source-ref completeness: all ledger events with `external_origin` source refs must carry a `vault_hash` at write time | Yes | No |
| QG-DM-9 | Backfill batch acceptance: a batch may be promoted only if entity-resolution ≥ 90%, spot-check sample is approved, and no unresolved lock conflicts exist | No | Yes |
| QG-DM-10 | Projection freshness: the current projection watermark must be within one gather cycle of the ledger head | No | Yes |
| QG-DM-11 | Knowledge-context determinism: replaying the same claim set produces identical `knowledge_context()` output (golden checkpoint) | Yes | No |
| QG-DM-12 | Self-containment: all external-origin claims and events must have vault-backed evidence; bare external URLs without a vault hash are rejected at write time | Yes | No |
| QG-DM-13 | Claim freshness: knowledge claims older than the configured staleness window surface as an advisory listing | No | Yes |

**Bridge gate behavior:** QG-B1 and QG-B2 are enabled only when `trusted_baseline.yaml` exists. They act as forceable hard blocks during the bridge period (before `bridge_graduated: true` in `trusted_baseline.yaml`) and automatically downgrade to advisory warnings after bridge graduation. QG-B3 is retired after bridge graduation.

### 14.2 Ban-List Enforcement

The editorial ban-list runs on ALL rendered content — Zone A output AND Zone B (AI-generated) output:

**Default banned phrases:** "due to", "caused by", "led to", "resulted in", "because of", "delve", "tapestry", "furthermore", "crucial", "testament", "in conclusion", "leverage"

**Default banned openings:** "This week", "As mentioned", "It should be noted"

Programs can extend via `editorial_rules.yaml`.

### 14.3 Verbosity Enforcement

| Surface | Limit |
|---------|-------|
| Workstream blurb | ≤4 sentences, ≤90 words |
| Exec summary | ≤150 words |
| Exec bullet | ≤25 words |
| Subject line | ≤80 characters |
| Scorecard summary | ≤3 sentences |

### 14.4 Quality Matrix

The `quality_matrix_engine.py` builds per-slice quality checks covering:
- Health checks (data completeness, freshness SLA compliance)
- Continuity assessment (vs. prior issue)
- Kusto telemetry cross-validation (when enabled)

The `remediation_engine.py` generates actionable fix instructions from the quality matrix.

### 14.5 Source Attribution

Every published fact must be source-traceable (INV-8). Three tiers:

| Tier | Mechanism | Surface |
|------|-----------|---------|
| Tier 1 | Inline `🔗` icon per table row → ADO work item URL | Newsletter |
| Tier 2 | Section-level `Sources: #a #b #c` trailer | Newsletter |
| Tier 3 | Full evidence packet with revisions, comments, enrichments | Reviewer pane |

### 14.6 Lineage System

`lineage.py` traces every published statement to source data:
- Per-claim lineage entries with source references
- Lookup by work item ID, section, or claim key
- Accessed via `vertex evidence`

---

## §15 Testing Strategy

### 15.1 Test Suite Structure

2755 collected tests across 4 categories:

| Category | Location | Purpose |
|----------|---------|---------|
| Unit tests | `tests/unit/` | Per-module correctness — engines, stores, renderers, commands |
| Contract tests | `tests/contracts/` | Zone boundary enforcement (AST-level import checks) |
| Golden file tests | `tests/golden/` | Byte-level output comparison against checked-in reference files |
| Integration markers | `@pytest.mark.integration` | Live ADO connectivity tests (opt-in via `--run-integration`) |

### 15.2 Test Infrastructure

- **Fixtures:** `tests/fixtures/` — sample journal, trajectory, knowledge, edition, program configs
- **Cassettes:** `tests/cassettes/` — recorded ADO API responses for deterministic replay
- **Shared helpers:** `tests/support/report_test_setup.py` — stages V2 temp workspace
- **Golden update:** `--update-golden` flag to refresh golden files after reviewed changes

### 15.3 Key Test Contracts

| Contract | Assertion |
|----------|-----------|
| Zone boundary | `src/core/` has zero imports from `src/ai/` or `src/m365/` |
| Journal immutability | Journal files never modified after write; review state in separate sidecar |
| Single write path | Only `snapshot_store.write_confirmed()` writes confirmed snapshots |
| Signal dedup | Same fingerprint → deduplicated; different sources → both kept |
| Altitude guard | Satellite strips low-severity; helicopter includes all approved |

### 15.4 Test Execution

```bash
pytest tests/ -q                            # Full suite (2755 collected tests)
pytest tests/contracts/ -q                  # Contract tests
pytest tests/golden/ -q                     # Golden file tests
pytest tests/ -k "v2 or journal" -q         # V2-specific tests
pytest tests/ --run-integration             # Live ADO tests
```

---

## §16 Non-Functional Requirements

### 16.1 Performance

| Operation | Target | Mechanism |
|-----------|--------|-----------|
| ADO fetch (50 items) | <30 seconds | OData batch + REST batch hydration, configurable timeout |
| Draft render | <5 seconds (cached data) | Jinja2 template rendering, deterministic pipeline |
| Gather (ADO-only) | <60 seconds (100 items) | Batched revision fetching, trajectory dedup |
| Full test suite | <120 seconds | No network I/O in unit tests; cassette replay |

### 16.2 Reliability

- **Retry:** Exponential backoff with jitter on 429/500/502/503/504 responses
- **Circuit breaker:** File-backed (CLOSED → OPEN → HALF_OPEN) for external service calls
- **Locked writes:** Journal and trajectory writes use `portalocker` for atomic append
- **Graceful degradation:** WorkIQ/Kusto/IcM unavailability → warning, not failure

### 16.3 Observability

- **Structured logging:** JSON + human-readable with run_id correlation
- **Run manifest:** Per-issue audit record with content hashes, duration, AI costs, QG results
- **LLM trace:** Every AI call logged to JSONL (tokens, latency, cost, model)
- **Cost guard:** Per-edition AI spend tracking with budget ceiling

### 16.4 Security

- **ADO auth:** PAT (env var) or Azure CLI (`az account get-access-token`)
- **Graph auth:** Admin-consented service principal required for `Mail.Send`; delegated flows (`Mail.Read`, `Calendars.Read`, `ChatMessage.Read`) are blocked in the Microsoft corp tenant without tenant-admin pre-consent. `workiq.exe` is the sanctioned path for M365 data reads.
- **PII scrubbing:** Gather-time filter on all signal text; journal privacy contract test
- **Injection detection:** Regex-based prompt injection scanner on all AI inputs
- **No secrets in code:** All credentials via environment variables or Azure Identity
- **Supply-chain hardening:** `pip-audit --strict` runs in CI on every push (WS-7); HMAC archive-signing primitive (QG-26) guards the confirmed-issue archive against tampering
- **Threat model:** See `governance/threat-model.md` for the STRIDE analysis, trust boundaries, mitigations, and residual risks. See `governance/model-cards.md` for AI model version lifecycle and recertification policy.

### 16.5 Maintainability

- **Python 3.11+** with type hints on all public functions
- **Frozen dataclasses** for all value objects
- **Zone boundary** enforced by AST-level contract test
- **Schema versioning** on all YAML config files (major version = hard failure, minor = warning)
- **19 repo-managed dependencies** in the current root requirements file, covering deterministic core, validation tooling, AI, Kusto, and M365 integrations in a single repo-managed set

---

## §17 Privacy & Data Sensitivity

### 17.1 Data Classification

| Data | Classification | Git-tracked |
|------|---------------|------------|
| `people_directory.yaml` (template) | Internal | Yes (example template only; `knowledge/people_directory.yaml` is `.gitignore`'d) |
| `people_profiles.yaml` | Confidential | No (`.gitignore`'d) |
| Signal journal (JSONL) | Internal | Yes (PII-scrubbed) |
| WorkIQ summaries | Confidential → Internal | Journal `text` only (never raw content) |
| Vitality scores | Internal | Aggregate in archive; per-person ephemeral |
| ADO comment content | Internal | Follows ADO access controls |

### 17.2 Privacy Rules

1. **No raw content in journal.** Signal `text` is always a structured summary, never raw email/message body.
2. **PII redaction at gather-time.** Alias references normalized to `P:<alias>`. Phones/addresses stripped.
3. **Signal text max 500 characters.** Schema-enforced via `signal.schema.json`.
4. **Ban-list on all content.** Editorial ban-list runs on signal text, AI output, and rendered output.
5. **No WorkIQ content in ADO comments.** Nudge comments use neutral language only.
6. **Sensitive profiles are local-only.** `people_profiles.yaml` is `.gitignore`'d.
7. **Per-person vitality is ephemeral.** Archived only if explicitly enabled via `vitality_archive_per_person: true`.

### 17.3 Privacy Enforcement

- `vertex doctor --privacy` — scans journal files for PII violations
- `tests/contracts/test_journal_privacy.py` — validates gather pipeline cannot produce signals with raw content
- `signal.schema.json` — `maxLength: 500` on `text` field
- `governance/privacy-matrix.md` — data-classification matrix, GDPR/CCPA applicability, retention policies, and DPA review checklist (WS-15; DPA review is a `[HUMAN GATE]` in `specs/backlog.md`)

---

## §18 Invariants

These are properties that must hold at all times. Violations are bugs.

| ID | Invariant | Enforcement |
|----|-----------|-------------|
| INV-1 | **Zone boundary:** `src/core/` never imports from `src/ai/` or `src/m365/` | `tests/contracts/test_import_boundaries.py` |
| INV-2 | **Single write path:** Only `snapshot_store.write_confirmed()` writes confirmed snapshots, called only from `confirm.py` | `tests/contracts/test_architecture_fitness.py::test_inv2_write_confirmed_single_write_path` |
| INV-3 | **Journal immutability:** Journal files never modified after write. Review decisions in separate sidecar. | `tests/contracts/test_architecture_fitness.py::test_inv3_journal_append_only_no_write_mode` |
| INV-4 | **Trajectory append-only:** Trajectory points only appended, never overwritten | `tests/contracts/test_architecture_fitness.py::test_inv4_trajectory_append_only_no_write_mode` |
| INV-5 | **Signal review gate:** Published output only includes signals with `approved` decision in `reviews.jsonl` | `tests/contracts/test_signal_review_gate_contract.py` |
| INV-6 | **`❓ Needs Input` = hard publish-block (QG-8)** | `tests/contracts/test_architecture_fitness.py::test_inv6_qg8_needs_input_is_hard_block` |
| INV-7 | **`--dry-run` = no archive writes, no external sends** | `tests/contracts/test_architecture_fitness.py::test_inv7_dry_run_never_writes_to_archive` |
| INV-8 | **Source traceability:** Every published fact cites its origin | `tests/contracts/test_entity_ref_contracts.py` + `tests/unit/test_attribution_engine.py` |
| INV-9 | **Ban-list on ALL rendered content** | `tests/contracts/test_architecture_fitness.py::test_inv9_ban_list_filtering_applied_on_all_rendered_content` |
| INV-10 | **Email max width: 680px outer, 640px content** | `tests/contracts/test_architecture_fitness.py::test_inv10_email_template_width_constraints` |
| INV-11 | **Edition isolation:** An edition config change cannot affect another edition's output | `tests/contracts/test_architecture_fitness.py::test_inv11_edition_resolver_has_no_shared_mutable_state` |
| INV-12 | **ADO write-back requires explicit approval.** No ADO write without `vertex ado apply` | `tests/contracts/test_architecture_fitness.py::test_inv12_no_raw_ado_write_calls_outside_writer` |
| INV-13 | **Echo-chamber guard:** Gather never creates signals for Vertex's own ADO revisions | `tests/contracts/test_architecture_fitness.py::test_inv13_echo_chamber_guards_exist_and_are_called` |
| INV-14 | **Vitality nudge cooldown:** Max 1 nudge per item per 14 days | `tests/contracts/test_architecture_fitness.py::test_inv14_nudge_cooldown_default_is_14_days` |
| INV-15 | **Uniform confidence field:** All Zone A advisory outputs (`ContradictionPacket`, `CatchupEvent`, `ForecastCalibrationModifier`, `InterventionProposal`, `SectionEvidenceBrief`, etc.) carry a `confidence` field from the standard four-level enum (`high/medium/low/none`). | Cross-cutting; enforced by focused advisory-model parity coverage in `tests/unit/test_confidence_parity.py` plus command/store coverage for proposal and brief flows |

**Program Event Ledger invariants (INV-DM):** the event-sourced ledger subsystem carries its own invariant family, enforced by `tests/contracts/test_import_boundaries.py` (AST scan), `tests/golden/test_ledger_projection.py` (determinism), and `tests/unit/test_ledger_*.py` (functional).

| INV-DM-1 | **Ledger append-only:** Event JSONL files are never modified after write, except for the §10.8 compliance redaction path (`ledger redact`) which atomically rewrites a payload with `{"redacted": true}` and registers the original hash in `.redactions.jsonl` | Code review + `verify_event_log` redaction-awareness; `redaction.py` is the sole registered exception in `test_state_reader_authority.py` |
| INV-DM-2 | **ULID monotonicity:** Every `event_id` ULID is strictly greater than the previous within a program | Enforced at write time in `ulid.py`; `verify_event_log` detects ordering violations |
| INV-DM-3 | **Hash chain integrity:** Every event's `prev_event_hash` must equal the envelope hash of the preceding event (genesis sentinel for the first); redacted events use their `original_envelope_hash` from the registry | `verify_event_log` in `event_log.py`; `tests/unit/test_ledger_event_log.py` |
| INV-DM-4 | **Projection determinism:** Replaying the same event log must always produce the same projection, regardless of ingestion order | Golden fixture test `test_qg_dm_2_projection_golden`; shuffle-property test |
| INV-DM-5 | **No wall-clock reads in projection:** `project_program_events()` is a pure function over the event log; no `datetime.now()` calls allowed inside the projection engine | `test_no_wall_clock_in_projection` in `tests/golden/test_ledger_projection.py` |
| INV-DM-6 | **Zone B/C discovery never writes events:** Zone B AI extractors (`src/ai/discovery/`) and Zone C M365 connectors (`src/m365/discovery/`) must never import or call the event write API (`write_event`, `write_events_atomic`, `index_event`, `append_jsonl_line`); only Zone A `discovery_run_recorder.py` writes ledger events | AST scan in `tests/contracts/test_import_boundaries.py::test_inv_dm_6_zone_b_c_discovery_never_calls_event_write_api` |

**Program Fact Store invariants (INV-SG):** the signal-fidelity / fact-layer subsystem carries its own invariant family (INV-SG-1…13), enforced by `tests/contracts/test_signal_invariants.py`. The actively enforced set: INV-SG-1 (a fact promoted with no entity ref is rejected; program-level facts bind a `PROGRAM:` sentinel), INV-SG-3 (the Program Fact layer carries no `src.ai`/`src.m365` imports — Zone A placement), INV-SG-9 (program-scoped identity — no `issue_number`/`edition_id` as a primary key), INV-SG-10 (per-module fact-read allow-list — migrated current-state reads route through `load_program_facts()`), INV-SG-11 (a lower-precedence write contradicting a higher-precedence accepted fact is queued as a Proposed Revision, never a silent overwrite), INV-SG-12 (snapshot-pin drift detection), and INV-SG-13 (bitemporal append-only with real `as_of` time-travel). INV-SG-2/4/5/6/7/8 are forward-declared placeholders tied to gated phases (extraction depth, source-health enforcement breadth, AI drafting, learning loops) and activate when their owning feature lands.

**Context health invariants (INV-C):** `src/core/program_context.py` enforces 21 cross-file invariants across the 20 Plane 1 program files (workstream coverage, milestone completeness, role ownership, ADO binding, KB coverage, etc.), categorized into codes WS-01…KB-03. Violations are classified as `error` (L0–L2 impact) or `warning` (L3 impact). Context maturity level L4 = zero errors and zero critical warnings. Enforced programmatically at `load_program_context()` call time; surfaced by `vertex doctor --context` and projected into the fleet health report.

**Feature binding invariant:** No feature script may contain a hardcoded Microsoft email, `date(20XX,…)` constructor, ADO area path literal, or stub WI ID (9xxxxx range) that appears verbatim in a program file. Sole exception: the program file path itself. Enforced by `tests/contracts/test_feature_binding.py`.

### 18.1 Maturity Levels

`program.yaml -> maturity_level` is the product-level switch that expresses which operating envelope Vertex has earned for a given program. It does not waive deterministic validation, archival, audit, or rollback requirements.

| Level | Meaning | System authority | Human role | Promotion evidence |
|------|---------|------------------|------------|--------------------|
| **L0** | Deterministic compiler | Gather, render, validate, and archive only through deterministic handlers | Drives every step | Baseline mode; `maturity_level` must remain within the governed L0-L4 range |
| **L1** | Advisory detection / reality engine | Surface anomalies, contradictions, and other advisory detections | Review, ignore, or act | Advisory false-positive proxy remains acceptable across 5 scoped sessions |
| **L2** | AI-assisted proposal staging | Draft comments, nudges, briefs, and similar proposals without writing externally | Explicitly approve or reject each proposal | Proposal acceptance rate stays at or above the earned threshold, governance plane is live, and quality gates remain enforced |
| **L3** | Governed bounded automation | Apply explicitly approved writes within a bounded blast radius | Approve bounded batches and retain halt/rollback control | Persisted prior acceptance stays high, every audited write records blast radius, and rollback path is documented |
| **L4** | Scheduled low-risk autonomy | Run specific low-risk action types on schedule under standing policy | Receive audit trail and halt if needed | One action type proves a sustained L3 history and remains contradiction-free across the validation window |

**Operationalization in `vertex maturity-check`:**

- L1 is currently operationalized as a false-positive proxy of `rejected / (accepted + rejected)` over the latest 5 issue-scoped sessions, with `<= 20%` treated as acceptable until a dedicated PM-review ledger exists.
- L2 proposal-staging readiness is operationalized as `>= 70%` accepted outcomes across the latest 10 issue-scoped proposal windows, plus governance-plane and quality-gate checks.
- L3 bounded-write readiness is operationalized from `journal/autonomy_audit.jsonl`: one accepted `l3`/`l4` action type must carry `prior_acceptance_rate >= 90%` plus non-empty `blast_radius` and `rollback_mechanism` metadata.
- L4 scheduled autonomy is operationalized from autonomy-audit history plus contradiction state: one accepted `l3`/`l4` action type must show at least 10 recorded cycles, and current contradiction state must be empty before the standing-policy window is considered earned.

**Approval contract:** L2 and L3 remain author-approved modes. L4 is the first standing-policy mode, but all external writes still flow through deterministic command handlers, emit autonomy-audit entries first, and preserve a documented rollback path.

**Context maturity vs. autonomy maturity:** The `program.yaml → maturity_level` autonomy ladder (L0–L4 above) is distinct from the context maturity level (also L0–L4) computed by `load_program_context()` from Plane 1 file quality. Context maturity measures *how well the program files are authored*; autonomy maturity measures *how much authority Vertex has earned to act*. `vertex doctor --context` reports context maturity; `vertex maturity-check` reports autonomy maturity. Confirm warns if context maturity has regressed since the prior confirmed issue.

---

## §19 Success Criteria

### 19.1 Functional Completeness

Evidence refresh: full execution evidence is recorded in `governance/test-evidence.md` (the canonical evidence log); `output/__green_run.txt` is a stale local artifact and the count there must not be cited — see `scripts/check_spec_drift.py` `p9-dead-green-run`. The current suite shape is computed at CI time by `scripts/derive_spec_counts.py` (WS-9 step 2 deliverable). Command-level and operational-proof criteria below still require their own targeted verification.

| Criterion | Verification |
|-----------|-------------|
| `vertex report --edition acme_weekly --dry-run` produces Issue 077+ from V2 paths | Run command, inspect HTML output |
| `vertex report --edition acme_daily` produces condensed output | Run command, verify street-altitude rendering |
| `vertex report --edition acme_lt_deck` produces deck Markdown | Run command, verify satellite-altitude filtering |
| `vertex report --edition acme_quarterly` produces quarterly lookback | Run command, verify archive aggregation |
| `vertex gather --program acme` writes signals + trajectories | Inspect `programs/acme/journal/` and `trajectories/` |
| `vertex gather --program acme --extract-evidence` populates `evidence_store.jsonl` | Inspect `programs/acme/journal/evidence_store.jsonl`; entries have `confidence > 0` |
| `vertex enrich --edition acme_weekly --dry-run` prints lane evidence without writing | Run command, verify no `evidence_store.jsonl` written; output contains lane names |
| Trajectory analysis detects ≥2 ETA slips as `eta_drift` | Unit test with fixture data |
| `vertex triage` produces readiness checklist with readiness score | Run command, verify all data sources aggregated |
| `vertex confirm` archives snapshot and extracts claims | Run command, verify archive + `claims.jsonl` |
| `vertex nudge --program acme --dry-run` writes full-hygiene EML preview | Run command, inspect `programs/acme/nudge/drafts/` |
| `vertex propose --edition acme_weekly` writes `proposals.jsonl` with pending section revisions | Run command, inspect `programs/acme/narratives/issue_NNN/proposals.jsonl` |
| `vertex review-proposals --edition acme_weekly --resolved-only` renders proposal history after decisions are recorded | Run command after resolving all pending proposals, inspect `programs/acme/publications/acme_weekly/review/proposals_review.html` |
| `vertex apply-proposals --edition acme_weekly --accept-modified <section_id>=<text>` writes reviewed narrative text and preserves accepted proposal history | Apply proposal, inspect narrative file + `proposals.jsonl` |
| `vertex doctor --edition acme_weekly --check-auth` surfaces optional-integration readiness and persisted capability state | Run command, verify ADO/Agency/Kusto readiness plus capability warnings are rendered together |
| `vertex ado propose --type comment` generates proposal manifest | Run command, inspect manifest |
| `vertex ado apply` writes comments with concurrency check | Apply proposal, verify ADO comments + journal signals |
| `vertex vitality` produces per-owner composite scores | Run command, verify freshness/richness/leakage |
| Shared knowledge changes reflected in both Acme and Platform | Edit `knowledge/people_directory.yaml`, verify both programs |
| `vertex hints --edition acme_weekly --issue N` surfaces narrative delta hints and author accept/reject/modify flow | Run command, verify `hints.jsonl` written to issue narratives directory |
| `vertex decisions governance show --program acme` renders DFD date, escalation state, and LT commitment | Run command, verify governance YAML rendered correctly |

### 19.2 Quality Assurance

| Criterion | Verification |
|-----------|-------------|
| Full test suite passes | Latest full execution evidence: see `governance/test-evidence.md`; current suite collection count is computed at CI time by `scripts/derive_spec_counts.py` (do not hardcode) |
| Zone boundary enforced | `tests/contracts/test_import_boundaries.py` passes |
| `vertex doctor --kb` validates knowledge integrity | No structural errors |
| Live dry-run smoke completes | `vertex report --edition acme_weekly --dry-run` succeeds |
| V-11 production readiness | All coding-implementable workstreams (WS-1…WS-25) code-complete; contract tests + unit tests passing (exact counts derived at CI time by `scripts/derive_spec_counts.py`; as of 2026-06-16: ~1,259 contracts + ~4,700+ unit); remaining items are OPERATOR/HUMAN GATE/CALENDAR gates documented in `.archive/specs/gaps.md` (local-only) |

### 19.3 Phase Scope Boundaries

**Phase-1 deliverables:**

- Acme program is fully wired with 14 live Kusto KPIs (10 Acme + 4 DD, including 2 aggregate Active-Incidents tiles visible in the email body), 19/19 slices ADO-anchored, hygiene nudge with per-DRI cadence, ADO fetch timeout honored, and the operator-driven weekly cadence runbook (`docs/runbook.md` §17).
- Kusto probe status: 9 of 11 probe-eligible queries have `validated: true` in `kpis.yaml`. 2 queries deferred by operator decision: `acme-buildout-slo` (RBAC gap on `azcis/azcispub`) and `acme-os-compliance` (unresolved table name in `apdmdata/DeviceManager`). IcM-routed queries (`acme-p0-blocker-count`, `acme-active-incidents-sev02`, `dd-active-incidents`) are intentionally excluded from Kusto probe.

**Phase-2 deferrals:**

- Unattended automation, AOAI narrative generation as default, Graph app-only send, headless/SPN auth, inline rendering of the 2 remaining `render_as: table` KPIs, row-returning Active-Incidents siblings, 4 Repair-Team-SLI-SLO queries, and the dashboard-catalog production tooling (`dashboard_catalog.py`, promotion script) are deferred to later phases tracked in [backlog.md](backlog.md).

---

## §20 Dependencies & Infrastructure

### 20.1 Core Dependencies

```
jsonschema>=4.23.0    # Config schema validation
portalocker>=3.1.1    # Cross-platform file locking for journal/trajectory writes
PyYAML>=6.0.2         # YAML config parsing
typer>=0.12.3         # CLI framework
requests>=2.32.3      # HTTP client for ADO REST API
Jinja2>=3.1.4         # Template rendering
azure-identity>=1.17.1 # Azure auth (CLI + managed identity)
pytest>=8.3.0         # Local validation and test execution
cryptography>=43.0.0  # Encrypted profile storage
keyring>=25.3.0       # OS-backed secret storage for encrypted profiles
```

### 20.2 All Dependencies (`requirements.txt`)

All capabilities are shipped in a single requirements file:
```
openai>=1.30.0        # Azure OpenAI client (AI layer)
tiktoken>=0.7.0       # Token counting for context budget (AI layer)
azure-kusto-data>=4.3  # Azure Data Explorer client (Kusto layer)
matplotlib>=3.8        # Chart generation (Kusto layer)
msgraph-sdk>=1.15.0    # Microsoft Graph API client (M365 layer)
```

### 20.3 External Services

| Service | Auth | Usage |
|---------|------|-------|
| Azure DevOps (ADO) | PAT or Azure CLI | OData queries, REST work item hydration, revision history |
| Azure OpenAI | Azure Identity | Blurb generation, exec summary, anticipation, summaries |
| Azure Data Explorer (Kusto) | Azure Identity | Telemetry queries (golden_queries.yaml) |
| Microsoft Graph | Device-code flow | Email send, M365 enrichment |
| Agency CLI (MCP) | Subprocess | WorkIQ, IcM, Bluebird tools |

### 20.4 Runtime Requirements

- **Python:** ≥3.11
- **OS:** Windows (primary), macOS/Linux (supported via portalocker cross-platform locking)
- **Git:** Required for `vertex kb changelog` (reads commit history)
- **Editor:** `$EDITOR` or system default for `vertex edit`
- **Browser:** For `--dry-run` auto-open and `vertex review-full`

---

*End of PRD.*
