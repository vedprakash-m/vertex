# Changelog

## 2026-11-04

| Change | Behavior |
|---|---|
| WorkIQ CLI transport | Request JSON/non-streaming output, decode the response envelope without altering answer text or references, and reject malformed, empty or provider-error envelopes rather than caching them as evidence. |
| REV incomplete outcomes | Preserve provider-limited and budget-truncated cycle categories through salvaged enumeration, quiet-lane exit and persisted cycle summaries; incomplete hydration cannot become a complete cycle. |
| Bound Kusto signal prose | Direct gather reuses UIL metric formatting and SLO comparison instead of previewing arbitrary row fields for a declared metric; unbound diagnostic tables retain their existing preview. |
| Retained fact capture | Reject mismatched membership, count or revision content before describing retained facts with a pin's digest; reuse the existing fact-store hash without changing stored bytes. |
| Cooperative assessment fence | Exclude covered fact/ledger writers while `ProgramReality` assembles local inputs; ordered file locks permit nested held subsets, refuse expansion/contention and release on exceptions or process exit. Other input stores remain unsealed. |
| Knowledge-integrity input | Reuse the canonical shared-vault summary in assessment instead of sampling another root; wrong-root decoys cannot create false integrity findings, while actual shared-vault corruption remains visible. |
| Shared knowledge fence | Exclude covered claim/candidate/index/vault writers through actual assessment using the same ordered shared-root lock; hold it through each public file/index operation and release on failure, without adding partial-write recovery or sealing other inputs. |
| Retained knowledge scopes | Read assessment claims using its already selected scope tuple instead of sampling configuration again; retain selected shared claims and exclude unrelated scopes without freezing all policy inputs. |
| Persisted authority capture | Include the selected program's SoR-state file in the assessment fence and fence its canonical writer; prevent mixed persisted program/family modes while retaining environment override semantics. |
| Assessment program config reuse | Reuse the parsed program model for committed-gather activation and knowledge scopes on the default path; preserve explicit override loading behavior without claiming a config freeze. |
| Committed gather selection | Resolve hash-verified committed run IDs and the earliest valid legacy cutoff in one scan; fact and signal readers consume the same selection without serializing gather writers. |
| Milestone source diagnostics | Keep unrelated facts when full-load milestone input is invalid/unavailable, retain a safe degraded status and corrective action, and preserve successful absence and targeted-load errors. |
| Incremental projection parity | Route all supported incremental entity families to affected-row folding, compare full canonical output against rebuilds, and advance projector version so old cached projections rebuild. |
| Outbox terminal containment | Reject automatic transitions from terminal rows, including late success; refuse missing or non-dispatchable requested create intents before unrelated backlog dispatch. |
| Retained ADO destination | Refuse backlog creates whose retained organization/project do not match the current client, before any provider request; malformed object shape fails explicitly. |
| Create recovery lookup | Require complete unique intent-marker lookup and exact tag/title readback before adopting an attempted create; failed, empty, ambiguous or malformed evidence requires reconciliation without dispatch or title-only fallback. |
| Timeout test cleanup | Release the synthetic Kusto hang after timeout/isolation assertions so serial validators exit normally; production cancellation behavior is unchanged. |
| Isolated command fixtures | Declare synthetic Agency capabilities and backup encryption status in affected tests instead of probing host CLIs; production probes and explicit unavailable/unencrypted controls are unchanged. |
| Isolated router fixture | Declare invocation mode inside fake-client topic-router tests and restore the prior raw state; preserve disabled-mode, fallback, safety and release assertions without changing production gating. |

Modified files: `src/m365/agency_bridge.py`, `src/core/rev/governor.py`,
`src/core/rev/pipeline.py`, `src/commands/gather_pipeline/kusto_query_helpers.py`,
`src/core/kusto_hydration.py`, `src/core/kusto_signal_extractor.py`,
`src/core/manifest_writer.py`, `src/core/program_fact_store.py`,
`src/core/ledger/durable_outbox_store.py`, `src/core/actuation_outbox.py`,
`src/m365/ado_writer.py`,
`src/core/workspace_lease.py`, `src/core/program_reality.py`,
`src/core/fact_sor_state.py`, `src/core/models_v2.py`,
`src/core/edition_resolver.py`,
`src/core/ledger/event_log.py`, `src/core/ledger/event_index.py`,
`src/core/ledger/candidate_sqlite_store.py`,
`src/core/ledger/program_views.py`,
`src/core/knowledge_claim_store.py`, `src/core/knowledge_candidate_store.py`,
`src/core/knowledge_index.py`, `src/core/knowledge/vault.py`,
`src/commands/ask_intents.py`,
`src/core/gather_run_manifest.py`, `src/core/journal.py`,
`scripts/verify_spec_citations.py`,
`specs/vertex-tech-spec.md`.
Tests: `tests/unit/test_m365_agency_bridge.py`,
`tests/contracts/test_agency_contract.py`, `tests/contracts/test_rev_contracts.py`,
`tests/unit/test_rev_pipeline_local_import.py`,
`tests/unit/test_gather_kusto_query_helpers.py`,
`tests/unit/test_kusto_metric_interpretation.py`.
Capture regressions: `tests/unit/test_run_evidence_contracts.py`,
`tests/unit/test_pinned_fact_projection.py`.
Capture fencing: `tests/unit/test_program_reality_capture_fence.py`,
`tests/unit/test_reality_archive_context.py`.
Knowledge input scope: `tests/unit/test_reality_knowledge_input_scope.py`,
`tests/contracts/test_program_reality_authority.py`.
Program config reuse: `tests/unit/test_edition_resolver_ai.py`.
Shared knowledge fencing: `tests/unit/test_knowledge_capture_fence.py`.
Persisted authority fencing:
`tests/unit/test_reality_authority_capture_fence.py`.
Committed-gather selection: `tests/unit/test_gather_run_manifest.py`.
Incremental projection parity: `tests/unit/test_ledger_incremental_projection.py`,
`tests/golden/ledger/program_golden_projection.json`,
`tests/golden/ledger/program_golden_bitemporal_slices.json`.
Milestone-source diagnostics: `tests/unit/test_program_fact_store.py`,
`tests/contracts/test_wi73_ask_intents.py`.
Outbox regressions: `tests/unit/test_durable_outbox_store.py`,
`tests/unit/test_actuation_outbox.py`,
`tests/contracts/test_outbox_create_fault_matrix.py`.
Destination, recovery and cleanup regressions:
`tests/contracts/test_create_intent_idempotency.py`,
`tests/unit/test_kusto_parallel_contract.py`.
Command fixture isolation: `tests/unit/test_commands_watch.py`,
`tests/unit/test_commands_registry.py`, `tests/unit/test_commands_db.py`.
Router invocation isolation: `tests/unit/test_ai_m365_topic_router.py`.
Existing MCP preference, CLI fallback controls, timeouts and raw process
diagnostics are retained. This does not qualify source identity, completeness
or production operation.

## 2026-11-03

| Change | Behavior |
|---|---|
| Draft evidence | Capture effective source selectors; refuse ordinary confirmation of unbound or selector-drifted drafts, including forced confirmation. Historical reads remain available. |
| Metric lineage | Preserve observed units and semantic-definition fingerprints across persistence, compaction and histories. Incompatible or unknown trends cannot imply recovery. |
| Context inventory | Expose metadata-only authored review-rule coverage without changing review thresholds or publication policy. |
| Publication privacy | Ignore raw source-document and credential-container exports; make the model-card example explicitly synthetic. |

Modified components: report/confirm capture and stages; metric projection, store,
reconciliation, evaluation and history commands; program inventory; publication
ignore rules and model-card guidance; canonical requirements, technical and UX
specifications; citation verification.

Tests: capture/report/confirm and archive-locked drift; metric projection,
store/correction/compaction, reconciliation, evaluation and histories; inventory
classification/containment; publication ignore contracts.

The generated CLI reference now reflects optional command-group invocation.
Existing draft-validation and additive-schema helpers were relocated to retain
the unchanged command/store size ceilings.

These are bounded implementation changes, not operational qualification,
private-data migration, complete context coverage or whole-run atomicity.
