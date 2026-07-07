"""WI-8.1: Update vertex-tech-spec.md with substrate model, truth levels,
authority family_map, actuation architecture, and new module inventory entries.
"""
import re
from pathlib import Path

TECH_SPEC = Path("specs/vertex-tech-spec.md")

content = TECH_SPEC.read_text(encoding="utf-8")

# 1. Update Zone A module count in §1.1 (228 → 241)
content = content.replace(
    "**Zone A — Deterministic Core** | `src/core/` (228 modules)",
    "**Zone A — Deterministic Core** | `src/core/` (241 modules)",
)

# 2. Update Zone B module count in §1.1 (28 → 30)
content = content.replace(
    "**B — AI Layer** | `src/ai/` (28 modules + prompt assets)",
    "**B — AI Layer** | `src/ai/` (30 modules + prompt assets)",
)

# 3. Add new Reality Substrate category to §1.2 Zone A module inventory
# Insert after the "Signal fidelity & fact layer" row
OLD_ROW = "| Signal fidelity & fact layer | `program_fact_store.py` (also Stores), `chronicle.py`, `checkpoint_store.py`, `source_health.py`, `signal_ranking.py`, `maturity_engine.py`, `conversion_fidelity.py`, `measurement_spine.py`, `entity_resolution.py`, `review_packs.py`, `cold_start_accelerator.py` |"
NEW_ROW = OLD_ROW + """
| **Reality substrate** | `program_reality.py` (ProgramReality G-1 facade — WI-1.1; `load()`, domain accessors, `attention()`, `pending_actuations()`, `to_dict()`, `diff()`), `truth_levels.py` (TruthLevel enum — WI-0.7), `truth_model.py` (TruthContext, `derive_truth_level()` 5-rule ladder, `SourceAuthorityPolicy` loader, `MATERIALITY_PREDICATES`, `get_authority_family()` — WI-3.0), `entity_registry.py` (EntityRegistry with exact/casefold/fuzzy tier, rapidfuzz WRatio, per-scope thresholds — WI-2.0/2.1), `signal_normalizer.py` (`normalize_signal()`, idempotence, `backfill_entity_refs()` — WI-2.2), `entity_alias_emitter.py` (`emit_entity_alias_facts()`, idempotent natural-key dedup — WI-2.3), `fact_schema_registry.py` (`validate_fact_payload()`, 13 registered types — WI-1.5), `commitment_store.py` (CommitmentEntry, SlipRecord, direction, slip_history — WI-2.7), `source_trust.py` (trust ledger, Laplace score update, bootstrap grants, circuit breaker, O-16 bucket classification — WI-3.1), `fact_sor_state.py` (FactSorState — legacy/shadow/primary SoR flip), `privacy_filter.py` (`load_privacy_policy()`, `is_fact_visible()` — classification ceiling), `signal_promotion.py` (signal review state promotion — WI-3.2a), `null_projection.py` (NullProjection — O-15 proof; new app builds against facade without Zone A change — WI-7.4) |"""
content = content.replace(OLD_ROW, NEW_ROW)

# 4. Add tiered_router.py and local_tier.py to Zone B module list
OLD_ZONE_B = "`tiered_router.py`"
if OLD_ZONE_B not in content:
    # Find Zone B module list and add the new modules
    content = content.replace(
        "**Zone B (`src/ai/`):** `_pipeline.py`",
        "**Zone B (`src/ai/`):** `_pipeline.py`"
    )
    # Add after action_extractor or synthesizer
    content = content.replace(
        "`action_extractor.py`, `decision_brief_advisor.py`",
        "`action_extractor.py`, `decision_brief_advisor.py`, `tiered_router.py` (AI tier router: Tier-0 local/deterministic → Tier-1 keyword-graph economy → Tier-2 frontier; `route_through_tiers()`, `flush_tier_decisions_to_jsonl()` — WI-4.1), `local_tier.py` (Tier-1 LocalTierMatcher keyword-graph — WI-4.4)"
    )

# 5. Add §9.17 Reality Substrate section after §9.16
NEW_SECTION = """

### 9.17 Reality Substrate (`src/core/program_reality.py` and related, WI-1.1–WI-7.4)

The **reality substrate** is the G-1 platform contract: all projections read exclusively through `ProgramReality`. It replaces the legacy `project_*` direct calls that each projection used to make.

**`ProgramReality` (§6.1):**
- Single read interface; `load()` is the ONLY disk-touching point.
- Domain accessors: `actions()`, `risks()`, `decisions()`, `dependencies()`, `milestones()`, `assumptions()`, `workstreams()`, `claims()`, `commitments()`, `conflicts()`, `metric_observations()`, `hypotheses()`, `approved_signals()`, `observation_facts()`, `pending_actuations()`.
- Derived accessors: `attention()` (AttentionItem tuples with 9 AttentionKind values), `stale_facts()`, `freshness()`, `evidence_for()`, `diff()`.
- Serialization: `to_dict()` (versioned envelope `reality_schema_version="1"`, privacy-scoped).
- `as_of` replay: `load(as_of=dt)` reconstructs history for any past timestamp.

**Truth levels (§6.3, WI-3.0):**

| Level | Meaning |
|-------|---------|
| `GOVERNANCE_LOCKED` | Locked by governance process (DFD, signed-off) |
| `HUMAN_CONFIRMED` | Human-reviewed and accepted via confirm workflow |
| `CORROBORATED` | Multiple independent sources agree |
| `SOURCE_VALIDATED` | Passed structural validation; single authoritative source |
| `RAW_OBSERVED` | Raw ingestion; unvalidated |

Derived by `derive_truth_level(fact, truth_ctx)` in `src/core/truth_model.py`. The `TruthContext` bundles locked keys, suspended sources, and corroboration events. Management fact families always derive at `HUMAN_CONFIRMED` minimum (Phase-1 static rule).

**Source authority family map (`vertex/policies/source_authority.yaml`):**

Maps each data source to its provenance class and authority family. The `get_authority_family(source_key)` accessor in `truth_model.py` provides the sanctioned lookup. Family keys: `management`, `operational_telemetry`, `human_validated`, `system_corroborated`, `raw_observed`. Corroboration requires sources from distinct families (`is_independent_source(a, b)` checks).

**Entity registry (WI-2.0/2.1):** `EntityRegistry.load(program_id)` builds exact + casefold + fuzzy (rapidfuzz WRatio) tiers. Per-scope thresholds: program=88, org=85. `resolve(name)` returns `CanonicalEntity | None`.

**Trust ledger (WI-3.1):** `vertex/policies/trust_policy.yaml` — Laplace score update, bootstrap grants, circuit breaker thresholds, O-16 bucket classification (high/medium/low/suspended).

**Actuation model (§6.11, WI-7.1/7.2 — pending CP-7 operator gate):**
- `ActuationProposal` dataclass in `program_reality.py`: `proposal_id`, `rule_id`, `adapter`, `operation`, `entity_ref`, `payload`, `proposed_at`, `approved`.
- Approval TTL: 24 hours. Execution-time revalidation: degraded inputs invalidate the proposal (re-queued, not dropped).
- `pending_actuations()` returns proposals; `actuation_engine.py` is the Phase-7 implementation (WI-7.1, ships with `enabled: false`).
- INV-12: no auto-execute tier exists regardless of trust scores.

**External consumer surface (§6.12.2, WI-7.4):**
- `vertex reality export --program <id> [--json] [--timeseries --interval <days> --since <date>]`
- Per-program cursor manifest: `output/<program>/reality_export_cursor.json` (not shared across programs).
- Audit JSONL: `output/<program>/reality_export_audit.jsonl` — every export appended.
- Timeseries: `non_replayable_families` per frame; `sor_flip_boundary=true` on replayability change (v3.2); `max_frames=60` policy cap.

**O-15 proof:** `src/core/null_projection.py` — `NullProjection` builds against the facade without importing from `src.commands.*` or modifying any Zone A module.
"""

# Insert §9.17 before the ## 10 section
content = content.replace(
    "\n## 10.1 Constants",
    NEW_SECTION + "\n\n## 10.1 Constants"
)

# 6. Update changelog at the top
OLD_CHANGELOG_MARKER = "- Last updated: 2026-06-09 — Phase 4/5/6 debt-remediation"
NEW_CHANGELOG_ENTRY = """- Last updated: 2026-06-15 — WI-8.1 update: §1.1 Zone A count 228→241 (13 new reality substrate modules); Zone B count 28→30 (tiered_router + local_tier); §1.2 new "Reality substrate" category (program_reality.py, truth_levels.py, truth_model.py, entity_registry.py, signal_normalizer.py, entity_alias_emitter.py, fact_schema_registry.py, commitment_store.py, source_trust.py, fact_sor_state.py, privacy_filter.py, signal_promotion.py, null_projection.py); §9.17 Reality Substrate section (ProgramReality facade, 5 truth levels, source authority family map, entity registry, trust ledger, actuation model, external consumer surface). **Implementation status: Phases 0–4, 6–7.3/7.4 complete; Phase 5 pending CP-3; Phase 7.1/7.2 pending CP-7 + A-14.**
- Last updated: 2026-06-09 — Phase 4/5/6 debt-remediation"""
content = content.replace(OLD_CHANGELOG_MARKER, NEW_CHANGELOG_ENTRY)

# 7. Update status line
content = content.replace(
    "**Status:** Reflects implemented state as of 2026-06-09",
    "**Status:** Reflects implemented state as of 2026-06-15 (Phases 0–4, 6, 7.3/7.4 implemented; Phase 5 pending CP-3 operator gate; Phase 7.1/7.2 pending CP-7 + A-14 permission check)"
)

TECH_SPEC.write_text(content, encoding="utf-8")
print(f"Updated {TECH_SPEC} ({len(content)} chars)")
