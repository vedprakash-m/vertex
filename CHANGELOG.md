# Changelog

## 2026-11-08

| Change | Behavior |
|---|---|
| Gather-state nesting bound | Reject gather-state JSON deeper than 1,000 nested object/array levels before decoding; readiness reports a content-free failure. |
| Public-config contract isolation | Restrict slice-ID contract checks to tracked program configurations so ignored local workspace data cannot affect public test results. |
| Strict no-write outbox diagnostics | Leave WAL-mode journals and any journal with SQLite sidecars unread, preserving their bytes and existence; expose unavailable counts and keep rollback-journal counts observable. The bounded implementation passes 521 synthetic affected tests; broader qualification remains open. |
| Bridge-status fixture ordering | Create synthetic baseline archives before locking them, preserving archive hardlock enforcement and existing bridge-status assertions. |
| WorkIQ identity admission | Keep semantic-only discovery as an in-memory preview, keep native thread identity distinct from message identity, reject provisional prefetch snapshots, and exclude detectable legacy provisional records from downstream signal consumers without migrating retained state. |
| WorkIQ prefetch depth and identity validation | Detect excessive nested cached payloads consistently across JSON decoder versions and reject malformed snapshot IDs before path selection; preserve live fallback and existing stored bytes. |
| EML diagnostic privacy | Omit local paths and exception text from affected hydration results and warnings, retaining error categories, fallback and quarantine behavior. Existing records and intended attachment sidecars are unchanged. |
| REV mail source containment | Reject missing or inconsistent EML Message-IDs and invalid mail source times through the existing quarantine path before admission, retaining valid independent items and budget precedence; suppress complete-only gates for recorded rejections. Event chronology and broader source qualification remain open. |
| Direct IcM fallback privacy | Keep authentication and query fallback warnings without exposing program IDs, provider exception text or tracebacks; fallback behavior and exception propagation remain unchanged. |
| IcM payload and no-signal warnings | Omit program IDs from payload and no-signal warnings, and describe empty results without inferring source unavailability; warning conditions and fallback behavior remain unchanged. |
| IcM gather exception privacy | Use fixed caught-failure diagnostics and a safe credential-expiry banner, preserving existing observability classifications and stable same-time replay; other provider diagnostics and historical records are unchanged by the wrapper. |
| UIL IcM provider diagnostic privacy | Omit caught discovery and hydration exception text while preserving failure classification, retryability, incident references and usable signals. |
| UIL IcM runtime diagnostic privacy | Omit exception text caught by the shared discovery and hydration runtime for IcM while retaining failure classification and existing behavior for other channels. |
| UIL ADO provider diagnostic privacy | Omit exception text from five existing discovery and hydration handlers while preserving failure classification, references and partial-success behavior. |
| UIL ADO runtime diagnostic privacy | Omit exception text caught by the shared ADO discovery and hydration runtime while preserving classification, continuation and other-channel behavior. |
| Gather architecture budget | Move existing M365 configured-source helpers into their discovery stage while preserving callbacks and the unchanged gather line ceiling. |
| Kusto gather exception privacy | Use fixed caught-failure diagnostics and a safe credential-expiry banner; provider text no longer determines signal class or creates actions or decisions. Other Kusto diagnostics and historical records remain unchanged. |
| UIL Kusto runtime diagnostic privacy | Omit exception text caught by shared Kusto discovery and hydration runtime handlers while preserving classification and single-failure behavior; provider-returned errors remain unchanged. |
| Kusto auth probe results | Report failed probe targets through the existing doctor failure status and operator gate instead of claiming successful access; successful probes remain unchanged, while caught injected errors follow separate privacy handling. |
| Kusto pre-flight warning privacy | Omit provider text and target identifiers from the probe warning while preserving the separate target-specific gather diagnostic. |
| Kusto doctor exception privacy | Replace caught probe exception text with a fixed failure message and taxonomy pattern while preserving doctor and operator-gate failure behavior. |
| Kusto query-doctor exception privacy | Replace caught lightweight query-probe exception text with a fixed message and taxonomy pattern while preserving query IDs, counts and stopping behavior. |
| ADO doctor exception privacy | Replace caught ADO probe exception text with fixed access guidance and taxonomy patterns while preserving failed-reachability and token-check behavior. |
| Metric-binding probe privacy | Replace caught probe query/value errors with fixed guidance and taxonomy patterns while retaining binding failures, state changes and validator-generated field/schema detail. |
| Metric-binding authentication privacy | Remove selected probe authentication messages and chains from escaping exceptions while retaining failure behavior, expiry metadata and binding state. |
| Metric-binding configuration privacy | Replace selected probe configuration-error text with fixed guidance while retaining validator diagnostics and failed-binding behavior. |
| Metric-binding secondary traceback privacy | Remove original selected-probe error chains before a later failure propagates; preserve sanitized diagnostics and storage-failure behavior. |
| Metric-binding doctor YAML privacy | Replace early malformed-YAML parser details and exception chains with fixed guidance while retaining failure behavior and other diagnostics. |
| Metric-binding doctor KPI-query YAML privacy | Apply the same guidance at KPI-query loading while preserving unrelated diagnostics. |
| Metric-binding doctor registry YAML privacy | Replace registry parser text and chains at the shared binding check while retaining the YAML-error family and unrelated diagnostics. |
| Operator-gates early YAML privacy | Replace parser details at the selected preflight and early composition stages while retaining failure behavior and standalone modes. |
| Read-only archive adapter | Remove the archive adapter's snapshot-write method and restrict its direct writer import; archive reads remain. Other confirmed-snapshot paths remain under review. |
| Archive hardlock containment | Refuse confirmed-issue archive writes when the existing baseline lookup recognizes the destination issue as locked; preserve staged input on refusal. |
| Baseline lock-state integrity | Refuse specified unreadable or malformed lock files before guarded writes while preserving missing and empty-baseline compatibility. |
| Duplicate baseline keys | Reject duplicate YAML mapping keys before guarded writes, using the existing strict policy parser while preserving valid aliases and merges. |
| Baseline lookup integrity | Refuse failed baseline lookups and post-discovery disappearance before guarded writes, preserving initial missing-baseline behavior. |
| Snapshot-writer import fitness | Check absolute and lexically resolved relative writer and wildcard imports by Python syntax; other writer paths remain unqualified. |

Bridge-status fixture ordering: `tests/unit/test_commands_bridge_status.py`. Tests: `tests/unit/test_commands_bridge_status.py`, `tests/unit/test_archive_workspace_context.py`, `tests/unit/test_baseline_lock.py`.
WorkIQ identity admission: `src/commands/gather_workiq_helpers.py`, `src/commands/gather.py`, `src/m365/workiq_ask_support.py`, `src/core/m365_payload_support.py`, `src/commands/gather_pipeline/workiq_prefetch_stage.py`, `src/commands/gather_pipeline/persistence_stage.py`, `src/commands/gather_pipeline/evidence_extraction_stage.py`, `src/commands/gather_record_helpers.py`, `src/commands/report_ai.py`, `src/core/leakage_detector.py`, `src/core/m365_signal_corpus.py`, `specs/vertex-prd.md`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`, `specs/relo.md`. Tests: `tests/unit/test_workiq_observation_integrity.py`, `tests/unit/test_m365_workiq_ask_support.py`, `tests/unit/test_workiq_prefetch_stage.py`, `tests/unit/test_gather_persistence_stage.py`, `tests/unit/test_m365_signal_corpus.py`, `tests/unit/test_leakage_detector.py`, `tests/unit/test_commands_report_ai.py`, `tests/unit/test_me01_provenance_wiring.py`.
WorkIQ prefetch depth and identity validation: `src/commands/gather_pipeline/workiq_prefetch_stage.py`, `src/core/prefetch_store.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_workiq_prefetch_stage.py`, `tests/unit/test_prefetch_store.py`.
EML diagnostic privacy: `src/m365/rev/eml_hydrator.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_eml_enumerator_hydrator.py`, `tests/unit/test_rev_pipeline_local_import.py`.
REV mail source containment: `src/core/rev/pipeline.py`, `src/m365/rev/eml_hydrator.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_rev_pipeline_local_import.py`, `tests/unit/test_eml_enumerator_hydrator.py`, `tests/contracts/test_rev_contracts.py`, `tests/integration/test_rev_e2e.py`.
Direct IcM fallback privacy: `src/commands/gather_pipeline/icm_signal_stage.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_gather_icm_signal_stage.py`.
IcM payload and no-signal warnings: `src/commands/gather_pipeline/icm_signal_stage.py`, `src/commands/gather.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_gather_icm_signal_stage.py`.
IcM gather exception privacy: `src/commands/gather.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_icm_error_privacy.py`.
UIL IcM provider diagnostic privacy: `src/m365/icm_discovery.py`, `src/m365/icm_hydration.py`, `src/core/failure_taxonomy.py`, `src/commands/gather.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_icm_uil_provider_privacy.py`.
UIL IcM runtime diagnostic privacy: `src/commands/gather_pipeline/channel_runtime.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_icm_runtime_error_privacy.py`.
UIL ADO provider diagnostic privacy: `src/core/ado_discovery.py`, `src/core/ado_hydration.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_ado_provider_error_privacy.py`.
UIL ADO runtime diagnostic privacy: `src/commands/gather_pipeline/channel_runtime.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_ado_runtime_error_privacy.py`, `tests/unit/test_icm_runtime_error_privacy.py`, `tests/unit/test_gather_channel_runtime.py`.
Gather architecture budget: `src/commands/gather.py`, `src/commands/gather_pipeline/m365_discovery_stage.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_gather_m365_discovery_helpers.py`, `tests/contracts/test_architecture_fitness.py`.
Kusto gather exception privacy: `src/commands/gather.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_kusto_error_privacy.py`, `tests/unit/test_commands_gather.py`.
UIL Kusto runtime diagnostic privacy: `src/commands/gather_pipeline/channel_runtime.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_kusto_runtime_error_privacy.py`, `tests/unit/test_icm_runtime_error_privacy.py`.
Kusto auth probe results: `src/commands/doctor_checks/auth_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_doctor_kusto_probe_result.py`.
Kusto pre-flight warning privacy: `src/core/kusto_client.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_kusto_probe_log_privacy.py`.
Kusto doctor exception privacy: `src/commands/doctor_checks/auth_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_doctor_kusto_caught_error_privacy.py`, `tests/unit/test_doctor_kusto_probe_result.py`, `tests/unit/test_commands_doctor.py`.
Kusto query-doctor exception privacy: `src/commands/doctor_checks/kusto_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_doctor_kusto_query_error_privacy.py`, `tests/unit/test_commands_doctor.py`.
ADO doctor exception privacy: `src/commands/doctor_checks/doctor_support_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_doctor_ado_caught_error_privacy.py`, `tests/unit/test_commands_doctor.py`.
Metric-binding probe privacy: `src/commands/metric.py`, `src/commands/doctor_checks/metric_binding_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_metric_binding_probe_error_privacy.py`, `tests/unit/test_commands_metric.py`, `tests/unit/test_commands_doctor.py`.
Metric-binding authentication privacy: `src/commands/metric.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_metric_binding_auth_privacy.py`, `tests/unit/test_metric_binding_probe_error_privacy.py`.
Metric-binding configuration privacy: `src/commands/metric.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_metric_binding_config_privacy.py`, `tests/unit/test_metric_binding_probe_error_privacy.py`.
Metric-binding secondary traceback privacy: `src/commands/metric.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_metric_binding_chained_privacy.py`, `tests/unit/test_metric_binding_probe_error_privacy.py`, `tests/unit/test_metric_binding_config_privacy.py`.
Metric-binding doctor YAML privacy: `src/commands/doctor.py`, `src/commands/doctor_checks/metric_binding_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_doctor_metric_binding_yaml_privacy.py`, `tests/unit/test_commands_doctor.py`.
Metric-binding doctor KPI-query YAML privacy: `src/commands/doctor_checks/metric_binding_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_doctor_metric_binding_late_yaml_privacy.py`, `tests/unit/test_kusto_query_loader.py`, `tests/unit/test_commands_doctor.py`.
Metric-binding doctor registry YAML privacy: `src/commands/doctor_checks/metric_binding_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_doctor_metric_registry_yaml_privacy.py`, `tests/unit/test_doctor_metric_binding_late_yaml_privacy.py`.
Operator-gates early YAML privacy: `src/commands/doctor.py`, `src/commands/doctor_checks/operator_gate_composition.py`, `src/commands/doctor_checks/yaml_support.py`, `src/commands/doctor_checks/metric_binding_checks.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_doctor_operator_gate_yaml_privacy.py`, `tests/unit/test_doctor_operator_gate_composition.py`.
Read-only archive adapter: `src/core/file_stores.py`, `src/core/store_protocols.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_store_protocols.py`, `tests/contracts/test_architecture_fitness.py`, `tests/contracts/test_store_compliance.py`.
Archive hardlock containment: `src/core/archive_store.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_archive_workspace_context.py`, `tests/unit/test_confirm_archive_transaction.py`.
Baseline lock-state integrity: `src/core/baseline_lock.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_baseline_lock_integrity.py`, `tests/unit/test_baseline_lock.py`.
Duplicate baseline keys: `src/core/baseline_lock.py`, `src/core/yaml_utils.py`, `src/core/policy_loader.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_baseline_lock_integrity.py`, `tests/unit/test_context_review_policy_loader.py`.
Baseline lookup integrity: `src/core/baseline_lock.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_baseline_lock_integrity.py`, `tests/unit/test_baseline_lock.py`.
Snapshot-writer import fitness: `tests/contracts/test_architecture_fitness.py`, `specs/vertex-tech-spec.md`. Tests: `tests/contracts/test_architecture_fitness.py`.
Gather-state nesting bound: `src/core/gather_state_store.py`, `src/commands/doctor_checks/confirm_readiness_checks.py`, `specs/vertex-tech-spec.md`. Tests: `tests/contracts/test_ws6_confirm_readiness_contract.py`, `tests/unit/test_gather_state_store.py`.
Public-config contract isolation: `tests/contracts/test_program_config_contracts.py`. Tests: `tests/contracts/test_program_config_contracts.py`.

## 2026-11-07

| Change | Behavior |
|---|---|
| Sprint snapshot fallback privacy | Keep the item-scoped fallback warning without exposing program IDs or provider exception text; query behavior and exception propagation remain unchanged. |
| Synthetic fixture paths and startup | Keep context-reference and trace-failure fixtures in pytest temporary paths; disable the nested startup test's cache and bound its wait without changing global pytest configuration. |
| Watch analytics workspace | Load saved analytics WIQL queries from the workspace selected by watch or catchup, preserving shared/local knowledge precedence and the default workspace. |
| Analytics diagnostic replay identity | Classify analytics failure signals before deduplication so same-time replays do not create duplicate entries; preserve earlier records and report each gather failure. |
| Analytics diagnostic privacy | Use safe new gather diagnostics and snapshot retry warnings without provider text or field names; preserve retry behavior and historical records. |
| Analytics-ingestion failure visibility | Mark the analytics ingestion record failed for an existing analytics channel error without copying its provider text into that store. Historical records are unchanged. |
| Sprint-capacity failure visibility | Report failed optional capacity queries through gather's existing error, manifest and ingestion surfaces without discarding valid sprint signals or exposing provider details. Successful empty responses remain distinct from failure at the gather level; watch's structured status remains open. |
| Retained synthesis identity | Reject explicitly mismatched program IDs using existing brief/cockpit read-failure diagnostics, while preserving missing-ID compatibility. Released-content hash enforcement remains unresolved. |
| Incomparable synthesis times | Report a retained-read failure when released candidates mix naive and aware generation times, preserving brief/cockpit diagnostics and comparable legacy behavior without guessing a timezone or rewriting evidence. |
| Matched AI release payloads | Validate matched release-decision payloads before authorizing a run or measuring OM-4. Invalid matching payloads block the release read and make audit coverage unavailable; unrelated or unidentifiable records remain outside this check. |
| AI release-ledger reads | Refuse malformed or foreign-program ledger evidence without repairing it while checking release decisions. Preserve valid and absent ledger behavior; event-chain and all-consumer qualification remain open. |
| Setup preview text containment | Escape dynamic preview text so proposed names and sample values display literally rather than becoming HTML. Remove whitespace-only section indentation while preserving layout, counts and optional browser behavior. |
| CI CLI reference check | Check the tracked generated command reference before tests rather than regenerating it in CI. The spec-drift check reports required reference assets without claiming a content comparison. |
| Confirmation calibration workspace | Keep enabled calibration priors in the resolved program workspace, reject invalid explicit roots without legacy fallback, and retain legacy calls and post-confirm warnings. |
| Scoped ask and ledger admission | Require an edition or program before `ask` startup and routing, resolve edition ownership for startup and AI audit, and refuse template or unresolvable editions. Reject newly written envelopes that the ledger reader cannot parse without changing legacy read-repair behavior. Shared miss-log and suggested-command scope limitations remain. |
| Offline report CLI regression | Exercise an eligible issue and require the JSON gate result and recreated HTML rather than accepting the historical-issue warning as a report. Verify preserved snapshot evidence and both archive roots; production guard behavior is unchanged. |
| Synthetic example and golden safety | Stage the tracked example configuration into temporary roots, isolate ledger golden projections, and use public baselines for synthetic snapshot comparisons. Keep the edition-dependent remediation baseline private. Missing baselines fail without writing; explicit reviewed updates remain supported. Four reviewed synthetic AI/email references, one reauthored knowledge-context reference and two synthetic setup previews are included; the intent-router baseline remains absent and unreviewed. |
| Synthetic rollback and projection isolation | Use temporary program roots for ADO rollback, nudge context-gap and ledger projection/snapshot tests. Extend the existing workspace/network guard; production behavior is unchanged. |
| Synthetic metric and telemetry isolation | Run previously skipped metric command tests with temporary roots, redirect AI test telemetry, and isolate three quality-gate tests. Extend the existing workspace/network guard to reject source writes; production behavior is unchanged. |
| Synthetic next-command isolation | Run previously skipped next-command tests with temporary edition roots and isolated startup hooks, and extend the existing workspace/network guard. Production behavior is unchanged. |
| Numeric fixture privacy | Reauthor REV contract-test phone and SSN examples as reserved or invalid synthetic values, preserving test logic. Existing Git history and other fixture provenance remain unqualified. |
| Synthetic quickstart isolation | Run previously skipped quickstart tests with temporary program and metric roots, an authored metric fixture and isolated startup hooks. Extend the existing workspace/network guard to cover the suite; production behavior is unchanged. |
| Residual fixture privacy | Reauthor remaining address-shaped test identities with reserved synthetic domains while preserving domain relationships and the intentional sanitizer sentinel. Existing Git history and other fixture provenance remain unqualified. |
| Synthetic reality, assertion and ask CLI tests | Run the previously skipped command tests with temporary program and metric roots and isolated startup hooks. Correct the existing alphabetical rollup expectation, verify scoped AI release auditing and guard the ask suite and representative commands against private-workspace and network access. This isolation slice did not itself change production behavior. |
| UAT command | Replace the nonexistent module entry point in the repository instructions with the supported report dry-run command. The default and projector visual checks remain required. |
| Migration dry-run safety | Reject analytics rebuild with `--dry-run` before rebuilding, skip unrelated root startup effects, and refuse corrupt trajectories without repairing them during forward previews. Label counts as previews and disclose that reverse preview may initialize SQLite schema or touch sidecars; migration integrity and recovery remain unqualified. |
| Signal storage fidelity | Preserve gather-run IDs and workstream associations in SQLite, including additional-workstream filters, and retain non-derivable single associations in both backends. Read legacy SQLite schemas without writes and upgrade them on default connections; previously lost values cannot be reconstructed and the committed-run filter remains unsupported. |
| Report section links | Point navigation, scorecard details and known decision-workstream links to rendered sections, retaining unlinked labels when sections are suppressed. Use the same anchor for chapter links and chapter output, preserve custom authored anchors, and leave section selection and layout unchanged. |
| Report dependency and AI context isolation | Select dependency-only legacy shims for dependency-risk traversal and omit the unused legacy action shim from report-signal context. Invalid selected dependency sources now propagate instead of silently removing programs; full reality reads retain non-primary action failure behavior. Stored facts, source authority, archive context and gates remain unchanged. |
| Synthetic fixture privacy | Replace potentially identifying team and person labels in M365 test fixtures with synthetic values and use reserved example domains for synthetic meeting-close, setup, PII-scrubbing, REV and nudge addresses. The live-configuration shape check no longer embeds a channel name; production behavior is unchanged. |
| Local-input fixture privacy | Reauthor inline EML and ICS examples and replace additional test identities with reserved synthetic values while preserving identity and domain distinctions. MIME, calendar, privacy and other tested behavior remains unchanged; remaining fixture provenance is unqualified. |
| Report-health verdict comparison | Retain an observe-only comparison of the report's existing health summary and the unqualified verdict in local manifest metadata. Equal risk values do not establish completeness, correctness or clearance; rendered health and gates are unchanged. |
| Requirement phase mappings | Reject missing, malformed or out-of-range phase labels in declared requirement rows while preserving the existing single, list and ascending range forms. This does not establish phase-to-work consistency or evidence execution. |
| Requirement work-item mappings | Reject declared requirement rows whose work-items cell contains no explicit work-item reference, even if a gate or another column names one. Preserve existing reference validation, prose gates and evidence semantics; orphan-work and evidence-execution checks remain open. |

Modified files: `.github/copilot-instructions.md`, `cli.py`, `src/commands/migrate.py`, `src/core/trajectory.py`, `src/core/journal.py`, `src/core/sqlite_stores.py`, `src/core/html_renderer.py`, `src/core/program_verdict.py`, `src/core/stages/render_stage.py`, `src/core/program_fact_store.py`, `src/core/program_reality.py`, `src/commands/report.py`, `src/commands/report_ai.py`, `src/commands/report_scorecards.py`, `templates/partials/nav_bar.j2`, `templates/partials/scorecard.j2`, `templates/partials/top_3_now.j2`, `templates/partials/workstream.j2`, `templates/partials/workstream_anchor.j2`, `scripts/verify_relo.py`, `scripts/verify_spec_citations.py`, `specs/vertex-prd.md`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`, `tests/contracts/cli_reference_snapshot.md`.
Tests: `tests/conftest.py`, `tests/unit/test_ado_writer.py`, `tests/unit/test_nudge_resolution.py`, `tests/unit/test_ledger_incremental_projection.py`, `tests/unit/test_ledger_projection_broader_entities.py`, `tests/unit/test_ledger_projection_entities.py`, `tests/unit/test_ledger_projection_provenance.py`, `tests/unit/test_ledger_projection_updates.py`, `tests/unit/test_ledger_field_locks.py`, `tests/unit/test_ledger_snapshot_manager.py`, `tests/unit/test_ledger_program_views.py`, `tests/unit/test_commands_metric.py`, `tests/unit/test_ai_client.py`, `tests/unit/test_quality_gates.py`, `tests/unit/test_commands_quickstart.py`, `tests/unit/test_commands_next.py`, `tests/unit/test_commands_reality.py`, `tests/unit/test_commands_assertion.py`, `tests/unit/test_commands_ask.py`, `tests/unit/test_synthetic_fixture_isolation.py`, `tests/unit/test_meeting_close.py`, `tests/unit/test_commands_migrate.py`, `tests/unit/test_trajectory.py`, `tests/unit/test_store_protocols.py`, `tests/unit/test_sqlite_stores_connection.py`, `tests/unit/test_commands_storage.py`, `tests/unit/test_renderers.py`, `tests/unit/test_program_verdict.py`, `tests/unit/test_report_fact_capture.py`, `tests/unit/test_verify_relo.py`, `tests/unit/test_commands_report_ai.py`, `tests/unit/test_commands_report_scorecards.py`, `tests/unit/test_report_dependency_archive_context.py`, `tests/unit/test_reality_archive_context.py`, `tests/unit/test_commands_gather.py`, `tests/unit/test_config_loader.py`, `tests/unit/test_m365_backfill_m365.py`, `tests/unit/test_m365_enricher.py`, `tests/unit/test_m365_graph_mail_client.py`, `tests/unit/test_m365_teams_reader.py`, `tests/unit/test_m365_transcript_reader.py`, `tests/unit/test_m365_workiq_ask_support.py`.
Golden safety tests: `tests/unit/test_golden_comparison_safety.py`, `tests/golden/test_ai_snapshots.py`, `tests/golden/test_base_email_snapshots.py`, `tests/golden/test_intent_router_snapshots.py`, `tests/golden/test_knowledge_context.py`, `tests/golden/test_ledger_projection.py`, `tests/golden/test_remediation_artifacts.py`, `tests/golden/test_setup_preview.py`, `tests/golden/test_synthesize_snapshots.py`.
Report CLI regression: `tests/unit/test_report_workspace_identity.py`.
CI reference guard: `.github/workflows/ci.yml`, `README.md`, `scripts/check_spec_drift.py`, `specs/vertex-prd.md`. Tests: `tests/contracts/test_cli_reference_drift_contract.py`, `tests/unit/test_check_spec_drift.py`.
Confirmation calibration: `src/commands/confirm.py`, `src/commands/confirm_stages/baseline_followthrough.py`, `src/core/calibration_engine.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_confirm_calibration_workspace.py`.
Setup preview: `src/commands/setup_preview.py`, the three canonical specs and `tests/golden/test_setup_preview.py`. Tests: `tests/unit/test_setup_preview_rendering.py`. Baselines: `tests/golden/public_snapshots/setup_preview.golden`, `tests/golden/public_snapshots/setup_preview_demo.golden`.
AI release-ledger reads: `src/core/quality_gates/ai_release_audit.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_ai_release_audit.py`, `tests/unit/test_program_synthesis.py`, `tests/unit/test_commands_brief.py`, `tests/unit/test_cockpit_program_synthesis.py`.
Matched AI release payloads: `src/core/quality_gates/ai_release_audit.py`, `src/core/outcome_metrics.py`, `governance/outcome-metrics.md`, the three canonical specs. Tests: `tests/unit/test_ai_release_audit.py`, `tests/unit/test_ledger_read_containment.py`, `tests/unit/test_program_synthesis.py`, `tests/unit/test_commands_brief.py`, `tests/unit/test_cockpit_program_synthesis.py`.
Incomparable synthesis times: `src/core/program_synthesis.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_program_synthesis.py`, `tests/unit/test_commands_brief.py`, `tests/unit/test_cockpit_program_synthesis.py`.
Retained synthesis identity: `src/core/program_synthesis.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_program_synthesis.py`, `tests/unit/test_commands_brief.py`, `tests/unit/test_cockpit_program_synthesis.py`.
Sprint-capacity visibility: `src/commands/gather.py`, `src/commands/gather_pipeline/ado_snapshot_stage.py`, `src/commands/gather_pipeline/source_ingestion_stage.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_sprint_capacity_visibility.py`.
Sprint snapshot fallback privacy: `src/commands/gather_pipeline/ado_snapshot_stage.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_sprint_capacity_visibility.py`.
Analytics-ingestion visibility: `src/commands/gather_pipeline/source_ingestion_stage.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_analytics_ingestion_visibility.py`, `tests/unit/test_gather_source_ingestion_stage.py`.
Analytics diagnostic privacy: `src/commands/gather.py`, `src/commands/gather_pipeline/ado_snapshot_stage.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_analytics_error_privacy.py`.
Analytics diagnostic replay: `src/commands/gather.py`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`. Tests: `tests/unit/test_analytics_error_privacy.py`.
Watch analytics workspace: `src/commands/watch_scan.py`, `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_watch_analytics_workspace.py`.
Synthetic fixture paths and startup: `specs/vertex-tech-spec.md`. Tests: `tests/unit/test_kb_context_references.py`, `tests/unit/test_synthetic_fixture_isolation.py`, `tests/unit/test_ai_trace_capture.py`.
Scoped ask and envelope changes: `cli.py`, `src/commands/ask.py`, `src/ai/intent_router.py`, `src/core/ledger/event_log.py`, the three canonical specs and `tests/contracts/cli_reference_snapshot.md`. Tests: `tests/unit/test_ask_scope_admission.py`, `tests/unit/test_ledger_envelope_admission.py`, `tests/unit/test_commands_ask.py`, `tests/unit/test_ai_intent_router.py`, `tests/golden/ask_output.txt`.
Reviewed synthetic baselines: `tests/golden/public_snapshots/ai_workstream_blurb.golden`, `tests/golden/public_snapshots/ai_exec_summary.golden`, `tests/golden/public_snapshots/ai_draft_review.golden`, `tests/golden/public_snapshots/base_email_template.golden`, `tests/golden/public_snapshots/knowledge_context_qg_dm_11.golden`.
Residual privacy tests: `tests/contracts/test_alias_learning_triage_hook.py`, `tests/contracts/test_pii_per_channel_regression.py`, `tests/contracts/test_ws17_observability_contract.py`, `tests/unit/test_action_mapper.py`, `tests/unit/test_activation_hardening_v1_16.py`, `tests/unit/test_audience_precedence.py`, `tests/unit/test_audience_scope_recipients.py`, `tests/unit/test_confirm_deserialization.py`, `tests/unit/test_enricher_body_text.py`, `tests/unit/test_integration_types.py`, `tests/unit/test_kb_updates.py`, `tests/unit/test_nudge_config_v21.py`, `tests/unit/test_rev_corpus_export.py`.
Privacy fixtures: `tests/data/nudge/acme.json`, `tests/data/nudge/contoso.json`, `tests/golden/public_snapshots/nudge_acme_full.golden`, `tests/golden/public_snapshots/nudge_contoso_full.golden`.
Privacy tests: `tests/golden/test_nudge_snapshots.py`, `tests/unit/test_commands_nudge_full_hygiene.py`, `tests/unit/test_rev_p2_modules.py`, `tests/unit/test_ai_pii_scrubber.py`, `tests/unit/test_ai_blurb_generator.py`, `tests/unit/test_ai_decision_brief_advisor.py`, `tests/unit/test_ai_draft_reviewer.py`, `tests/unit/test_ai_learning_distiller.py`, `tests/unit/test_ai_pipeline.py`, `tests/unit/test_ai_setup_assistant.py`, `tests/unit/test_commands_setup.py`, `tests/unit/test_summary_generator.py`.
Additional privacy tests: `tests/contracts/test_rev_contracts.py`, `tests/unit/test_activation_gates_v1_24.py`, `tests/unit/test_eml_enumerator_hydrator.py`, `tests/unit/test_ics_enumerator_hydrator.py`, `tests/unit/test_local_file_enumerator_hydrator.py`, `tests/unit/test_rev_cache_store.py`, `tests/unit/test_rev_inbox_rotation.py`, `tests/unit/test_rev_quality_check.py`, `tests/unit/test_bridge_privacy_gate_risk_dependency.py`, `tests/unit/test_privacy_purge.py`.

## 2026-11-06

| Change | Behavior |
|---|---|
| OM-4 audit evidence | Report audit coverage as unavailable when an observed application receipt has an unknown status or missing/invalid run ID, instead of measuring a subset or treating it as no consumption. Prevent partial ratios from rounding to false zero or perfect coverage, and describe recorded consumption and latest terminals. Receipt completeness and authorization timing remain unqualified. |
| Local actuation-journal diagnostics | Replace fabricated cockpit reliability zeros with exact selected-domain pending, uncertain and dead-letter counts, or unavailable values when the journal cannot be assessed. Surface terminal-state warnings without payloads or failure details. Escape literal characters in shared read-only SQLite paths and correct OM-2's obsolete not-wired explanation without promoting the metric. SQLite may create or update a WAL shared-memory sidecar during a read; its acceptability remains unresolved. Provider outcomes, journal integrity and the wider inbox remain unqualified. |
| Post-create association errors | Preserve the applied ADO outcome and disclose expected local association exceptions with a content-free manifest warning, repeated on replay. Unexpected errors propagate after an attempt to persist the applied outcome. Missing or unmatched action identities, partial association and recovery remain unqualified. |
| Action-history containment | Refuse malformed or unreadable selected action journals without repairing them or treating them as empty during fact projection, status updates and work-item association. Actions and meeting-close queue errors use fixed content-free diagnostics; legacy repair defaults and primary-family exclusions remain. Earlier meeting-close effects are not rolled back. |
| Confirmation-readiness gather and archive | Use the resolved edition cadence, report unreadable gather state without exposing its contents, and count confirmed entries in the selected archive index. Unavailable indexes and invalid explicit entry kinds remain unassessed warnings; archive integrity and confirmation policy are unchanged. |
| Confirmation-readiness diagnostics | Select the same latest numeric override issue as the loader, inspect supported scorecard risks, and safely report invalid or unreadable inputs, including missing, blank or control-bearing inspected names. Warn when no visible risks can be assessed; this limited check does not guarantee confirmation or change QG-8. |
| Kusto client result admission | Reject invalid attempt counts before client creation and refuse missing declared row cells rather than returning an empty success or fabricated null. Preserve valid retry behavior, explicit nulls, mixed-access rows and undeclared conversion; provider completeness remains unqualified. |
| Grouped Kusto charts | Render existing chart members inside the Fleet Parity group in detailed/focused and lookback email, and use the standalone chart summary in Teams. Escape grouped messages and correct the grouped metric loop; preserve group order and visibility. |
| Chart email rendering | Render live and cached chart timestamps with the existing UTC formatter and include existing descriptions in chart alt text. Treat Kusto title and message as literal text in the affected HTML; no-message wording and global escaping remain unchanged. |
| Declarative chart truth | Keep plotted series aligned with legends and generated descriptions when configured columns are missing; preserve authored alt text, goals on resized charts, and the final PNG hard gate. Existing skip behavior, size thresholds and chart styles are unchanged. |
| Requirement traceability table scope | Resolve requirement mappings only from the visible declared five-column table, and ignore fenced or commented definition examples and unrelated tables. Missing or malformed headers fail; minimal specifications without the section remain compatible. |
| Traceability identifier suffixes | Reject malformed attached goal-table and requirement-table reference suffixes instead of accepting a valid prefix. Preserve terminal punctuation, documented shorthand and ranges, non-QG gate prose, and the existing read-only and minimal-spec behavior. |
| Goal and objective traceability | Check declared §19.1 goal/objective coverage and resolve its requirement, feature and work-item references, including range members and shorthand. Ignore fenced/commented examples and unrelated sections; preserve read-only lint and minimal-spec behavior. This does not certify executed evidence or complete bidirectional traceability. |
| Projection rebuild safety and workspace binding | Retain the previous projection until same-directory replacement and forward the selected programs root during deep verification and historical diff. Replacement failures remain visible to direct callers and logged by the existing write hook; concurrent access and crash recovery are not qualified. |
| Synthetic REV test mailbox | Use a reserved documentation-domain mailbox in local-import fixtures; production mailbox handling is unchanged. |
| Repeated prefetch payload safety | Reuse an identical committed payload rather than truncating it during refresh. Uncommitted or different payloads retain the existing write and fsync order; comparison errors propagate. No concurrent-writer or historical-recovery guarantee is added. |
| WorkIQ cached-payload depth recovery | Warn without exposing payload or exception details and use the existing live fallback when cached JSON is excessively nested. Preserve direct payload-reader errors, subsequent processing errors and stored bytes. |
| Prefetch selection metadata recovery | Report unreadable or invalid snapshot pointer and manifest metadata with content-free warnings and use the existing WorkIQ fallback and schedule-health missing/info handling. Preserve ordinary absence, valid snapshots and stored bytes. |
| Prefetch snapshot integrity | Reject a selected manifest naming another snapshot and verify exact payload bytes against their content address before decoding. WorkIQ uses its existing live fallback; schedule health does not certify payload integrity. |
| WorkIQ snapshot shape recovery | Reject malformed cached payload and signal-list shapes with a generic warning and the existing live fallback. Preserve valid empty snapshots and the direct payload helper; do not rewrite stored data or claim checksum validation. |
| WorkIQ query-plan budget visibility | Preserve valid partial results and report a distinct pending diagnostic and retryable optional-source degradation when the existing budget leaves a plan unattempted. Cached diagnostics retain the same meaning; invalid marker metadata uses the existing live fallback. Prefetch does not currently supply this budget, and typed-provider calls and post-loop passes do not have a whole-phase hard deadline. |
| WorkIQ catchup diagnostics | Preserve distinct quarantine and unusable-response diagnostics alongside valid observations, with diagnostic-specific IDs and per-day deduplication. Use static WorkIQ summaries instead of fabricated ADO changes; retain pending review, the display cap, ordinary catchup behavior and existing history. |
| WorkIQ malformed-record diagnostics | Count existing malformed-shape, terminal-control and disallowed-mail-URL rejections through gather, cached prefetch and enrichment. Preserve valid partial evidence and zero-yield state for rejected-only results; diagnostics contain no rejected content. Admission, preview policy, limits and budgets are unchanged. |
| Unsupported REV enumeration | Report the selected enumerator's unsupported outcome rather than a successful empty cycle, return a nonzero CLI exit and attempt health checkpoints without copying provider details. Health warns and doctor exits 2; no items scanned does not establish shield availability. Invalid history, including uninterpretable fallback counts, is preserved without repair on this path and reported unavailable when the last-cycle checkpoint succeeds. Source logging, other doctor exits, authority gates and existing streaks remain unchanged. |
| REV-health startup isolation | Skip unrelated root tier-sink registration, scheduled maintenance and catchup for `doctor --rev-health`, including argument errors and help. Parse the actual flag rather than matching option values; other successful doctor routes and REV execution retain their startup behavior. Existing diagnostic-reader recovery is unchanged. |
| REV fact database binding | Read divergence and cross-source conflicts from the established selected fact database, including configured directory/file overrides and existing unconfigured roots. Write detected conflict facts to that same store; do not migrate parallel databases. Startup workspace routing and existing best-effort failure semantics remain unchanged. |
| Public dependency lock | Regenerate `uv.lock` from the existing base dependencies and all six extras, retaining its revision 3 format and respecting the Typer/Click caps. Update the OSV-affected certifi, cryptography, filelock, msgpack, pip, pyjwt and urllib3 versions, and use portalocker 4.4.2 without its former pywin32 dependency. The manifest and CI install path are unchanged; live-provider compatibility remains unqualified. |
| Windows CLI help encoding | Use cp1252-compatible text in five command descriptions so the tested root, group and nested help paths work with redirected Windows output. Other CLI output and encoding policy are unchanged. |
| Tokenizer test isolation | Persist the synthetic compiler manifest under the test's temporary programs root instead of the checkout default. Production workspace selection is unchanged. |
| Channel-timeout test cleanup | Release the synthetic hanging providers after verifying timeout behavior, so the test process can exit promptly. This does not change production timeout or worker-shutdown behavior. |
| Owner-change detection and diff | Detect first assignment when only a name or email is available, and render the canonical owner-change values in publish diff instead of reporting both owners as unassigned. Preserve existing identity matching and legacy field aliases. |
| Blocked-risk change rows | Render changes on Blocked items without a ranking failure, placing them ahead of High items within the same priority category. Keep existing row limits and links. |
| Registry SQLite lifecycle | Close owned channel-registry and source-candidate connections after their existing transactions, including early-return and error paths, and close six associated test fixtures. The existing schema probe retains its `False` result on SQLite errors and logs a generic diagnostic, including for close failures. Preserve strict durability and caller-owned connection handling. SQLite may checkpoint when its last WAL connection closes; performance and multi-host behavior remain unqualified. |
| Next-issue override preservation | Create a missing program-backed next-issue override exclusively; preserve an existing target without validation and warn rather than overwrite it. Keep the legacy flat reset and hardlock behavior. Confirmation output distinguishes preservation, and dry-run does not promise a reset. Later seeding, concurrent editing and post-promotion rollback remain separate. |
| Synthetic next-issue continuation | Extend the isolated onboarding-to-confirmation workflow through next-issue draft, prior-snapshot delta, carried-risk characterization and a forced QG-8 block after one override is cleared. This does not establish override currency or operating qualification. |
| Work-package decision references | Require exact numeric or single-letter-suffixed IDs in the §20 decision table, excluding fenced examples and comments. Keep other checks and minimal specifications without decision-bound cards unchanged; this does not establish decision resolution or readiness. |
| Work-package identifier/class diagnostics | Locate duplicate and unknown card IDs by entry position without echoing unknown IDs, and report unsupported class strings without their values. Preserve declared-ID findings, readiness matching and failure behavior; other diagnostic fields remain outside this correction. |
| Work-package unresolved-path diagnostics | Report the field and entry position for missing, non-file or malformed readiness paths without echoing their values. Preserve path admission, failed checks and unexpected-error behavior; other field-derived findings remain outside this correction. |
| Normal verifier evidence diagnostics | Report specification, work-package, baseline, telemetry and budget read/parse failures without exposing paths, parser excerpts, registry values or exception messages. Preserve existing failed checks, optional evidence selection and exception handling. |
| Selected configuration diagnostics | Report mismatched deployment field names and generic YAML, UTF-8 or file-read failures without exposing configuration values, parser excerpts, exception messages or the selected path. Keep optional selection, valid-variable admission and independent checks unchanged. |
| Work-package repository paths | Reject `AGENT_READY` file references outside the selected repository, including outward links, without echoing their paths. Check lexical containment, then strictly resolve the original path so links and missing intermediate components are not normalized away. Preserve valid in-repository absolute, normalized and linked paths; this read-only check is not a race-free filesystem security boundary. |
| Normal verifier decoding | Report undecodable specification, ledger, baseline or selected telemetry as generic failed checks while continuing independent checks. Preserve inputs, optional evidence selection and unexpected-error propagation; no terminal-state or qualification policy changes. |
| Work-package shape admission | Report malformed card identifiers, classes, dependencies and `AGENT_READY` path lists through the existing failed readiness check instead of crashing or accepting a mapping as a path list. Preserve valid records and other card diagnostics; status, dependency-cycle and path-containment policy remain unchanged. |
| Deployment-fallback budget refusal | Propagate the original `BudgetExceeded` from client construction or either provider method without trying another deployment, including with a custom retry tuple. Claim resolution receives its budget-specific error; confirmation's existing outer fallback and other retryable failures remain unchanged. |
| Confirmation recheck extraction | Move the existing archive-locked recheck into the confirmation transaction stage to retain the unchanged coordinator size ceiling. Keep dependency resolution, validation order, fence selection and errors unchanged; no gate or archive-lock protocol changes. |
| Runner budget-refusal outcome | Keep `BudgetExceeded` non-retryable and identify its cause even when the fallback abstains or is otherwise non-released. Preserve the existing fallback status and other fields; call slots and the separate cost ledger are unchanged. |
| Runner call-slot reservation | Reserve bounded remote and frontier call slots together; a refused limit or precommit SQL failure rolls back both, and fallbacks name the exhausted limit. Keep non-released fallback statuses and the existing released-to-deterministic-fallback mapping. Preserve reservations after uncertain provider outcomes and existing historical counters; commit recovery, cost policy and production integration are unchanged. |
| Runner batch admission | Reject duplicate or invalid per-item result keys and invalid configured batch sizes before processing any item. Keep valid item IDs, request-hash/index fallback, partial failures and existing per-item behavior; no production caller migration or identity authentication. |
| AI reality-summary safeguards | Enforce the existing estimated ceiling on formatted summary bullets, count overlapping trust properties independently, and disclose omitted details or unavailable families. Consider smaller facts after oversized ones; put the trust caution before the workstream prompt cap and flags before fact text. Invalid or insufficient custom budgets fail explicitly. The estimator's heuristic is not a provider-token guarantee, and other prompt context can still be trimmed. |

Modified files: `src/core/_db.py`, `src/core/cockpit_builder.py`, `src/core/cockpit_models.py`, `src/core/ledger/durable_outbox_store.py`, `src/core/outcome_metrics.py`, `governance/outcome-metrics.md`, `src/m365/ado_writer.py`, `src/core/jsonl_utils.py`, `src/core/action_tracker.py`, `src/core/program_fact_store.py`, `src/commands/actions.py`, `src/commands/meeting_close.py`, `src/commands/doctor.py`, `src/commands/doctor_checks/confirm_readiness_checks.py`, `src/commands/doctor_checks/output.py`, `src/core/archive_store.py`, `cli.py`, `uv.lock`, `templates/partials/kusto_section.j2`, `templates/partials/kusto_group_section.j2`, `templates/partials/kusto_chart_body.j2`, `templates/partials/teams_chart_summary.j2`, `templates/archetypes/lookback.j2`, `templates/base.teams.j2`, `templates/partials/exec_summary.j2`, `scripts/verify_relo.py`, `scripts/verify_spec_citations.py`, `src/core/charts/declarative.py`, `src/core/kusto_client.py`, `src/commands/ledger.py`, `src/core/ledger/program_views.py`, `src/ai/deployment_fallback.py`, `src/ai/structured_feature_runner.py`,
`src/m365/workiq_ask_support.py`, `src/m365/workiq_retriever.py`, `src/commands/gather.py`,
`src/commands/gather_auxiliary.py`, `src/commands/gather_pipeline/workiq_prefetch_stage.py`, `src/commands/prefetch.py`,
`src/commands/enrich.py`, `src/core/gather_channel_support.py`, `src/core/catchup_runner.py`,
`src/core/signal_dedup.py`, `src/core/feedback/catchup_classifier.py`, `src/core/prefetch_store.py`, `src/core/schedule_health.py`,
`src/commands/calibration.py`, `src/commands/capture_lt_deck.py`, `src/commands/ingest_update.py`,
`src/commands/privacy.py`, `src/commands/rev.py`, `tests/contracts/cli_reference_snapshot.md`,
`src/commands/confirm.py`, `src/commands/confirm_stages/archive_transaction.py`,
`src/commands/confirm_stages/post_confirm_support.py`, `src/core/overrides_store.py`,
`src/core/rev/pipeline.py`, `src/core/rev/health.py`, `src/core/rev/cycle_history.py`,
`src/commands/doctor_checks/context_checks.py`, `src/core/ledger/fact_bridge.py`,
`src/core/channel_registry_store.py`, `src/core/source_candidate_store.py`, `src/core/delta_engine.py`, `src/core/publish_diff.py`,
`src/core/html_renderer.py`, `src/commands/reality_context.py`, `src/commands/report_ai.py`,
`specs/vertex-prd.md`, `specs/vertex-tech-spec.md`, `specs/vertex-ux-spec.md`.
Tests: `tests/unit/test_ledger_read_containment.py`, `tests/unit/test_cockpit_actuation_outbox.py`, `tests/unit/test_cockpit_view_models.py`, `tests/unit/test_outcome_metrics.py`, `tests/unit/test_relo_trusted_workflow_baseline.py`, `tests/contracts/test_create_intent_idempotency.py`, `tests/unit/test_jsonl_utils.py`, `tests/unit/test_action_tracker.py`, `tests/unit/test_program_fact_store.py`, `tests/unit/test_commands_actions.py`, `tests/unit/test_meeting_close.py`, `tests/contracts/test_ws6_confirm_readiness_contract.py`, `tests/unit/test_archive_store.py`, `tests/unit/test_declarative_chart_builder.py`, `tests/unit/test_kusto_client.py`, `tests/unit/test_kusto_rendering.py`, `tests/unit/test_ledger_incremental_projection.py`, `tests/contracts/test_incremental_projection_write_hook.py`, `tests/unit/test_commands_ledger.py`,
`tests/unit/test_cli_help_encoding.py`, `tests/unit/test_verify_relo.py`, `tests/unit/test_fallback_budget.py`, `tests/unit/test_structured_feature_runner.py`,
`tests/unit/test_m365_workiq_ask_support.py`, `tests/unit/test_workiq_retriever_identity.py`,
`tests/unit/test_workiq_observation_integrity.py`, `tests/unit/test_workiq_prefetch_stage.py`,
`tests/unit/test_prefetch_command.py`, `tests/unit/test_prefetch_store.py`, `tests/unit/test_schedule_health.py`,
`tests/unit/test_doctor_schedule_health_checks.py`, `tests/unit/test_commands_gather.py`,
`tests/unit/test_catchup_runner.py`, `tests/unit/test_catchup_classifier.py`, `tests/unit/test_signal_dedup.py`,
`tests/unit/test_context_tokenizers.py`,
`tests/unit/test_runner_context_identity.py`, `tests/unit/test_channel_execution_policy.py`, `tests/unit/test_confirm_archive_transaction.py`,
`tests/unit/test_confirm_config_binding.py`, `tests/unit/test_commands_confirm.py`,
`tests/unit/test_commands_onboard.py`, `tests/unit/test_overrides_store.py`, `tests/unit/test_reality_context_track_j.py`,
`tests/unit/test_sqlite_stores_connection.py`, `tests/unit/test_channel_registry_store.py`,
`tests/unit/test_fact_pin_workspace_authority.py`, `tests/unit/test_rev_pipeline_local_import.py`,
`tests/unit/test_commands_doctor.py`, `tests/unit/test_commands_diff.py`, `tests/unit/test_renderers.py`.

## 2026-11-05

| Change | Behavior |
|---|---|
| Confirmation evidence fence | Hold the existing cooperative fact-database and selected source-state locks from final recheck through primary archive promotion when those inputs are checked. Contention refuses before checkpoint or archive mutation, even with force; dry-run and lookback selector scope remain unchanged. Same-host subprocess regressions cover both cooperating writers. Other inputs and post-promotion effects remain unsealed, and the independent archive lock has a known acquisition race. |
| Invalid context observations | Refuse unparseable or ungroupable retained gap records with safe diagnostics while preserving independent pending revisions. Ranked doctor, solicitation and reply intake fail without partial history or repair; compatible legacy observations remain readable. |
| Context maintenance isolation | Reject foreign-program gap observations before ranking and keep independent pending proposals visible. Distinguish history failures from unavailable registry-dependent ranking; ranked doctor reports malformed registry input without exposing paths or changing retained state. |
| Ranked context observations | Label grouped context-gap counts as recorded observations, not gather cycles or elapsed time. Preserve ranking and retained history. |
| Optional registry context coverage | Disclose when the selected program's optional registry is absent and its workstream gap checks were not evaluated. Keep health and exit behavior unchanged; existing empty registries still fail with manual population guidance. Ranked history reports an unavailable missing registry without changing its failure status or retained observations. |
| Captured selector identity | Reject multiple modes for one authority family at capture admission and before direct policy comparison. Preserve valid v1 and standalone partial or empty v2 reads, lookback behavior and the existing non-forceable drift gate; no capture authentication or cross-store consistency claim. |
| Storage-doctor SQLite cleanup | Close read-only fact-count and integrity-check connections after their existing transaction contexts, including early-return and database-error paths. Preserve diagnostic results and missing-database behavior; no recovery or operational qualification. |
| Successful empty risk selection | Reuse a completed empty legacy or non-legacy risk selection in health rendering instead of rereading current risks, including when a reality comparison warns. Failure, empty rollback, nonempty selection and render-fallback handling remain unchanged; quality-gate reads remain independent. |
| Nonempty risk rollback read-path accounting | Record `legacy` for the risk-stage manifest path after the existing audited fallback returns authored risks, so doctor can detect a non-legacy selector mismatch. Preserve the captured selector and quality-gate policy; empty rollback and rendering provenance remain open. |
| Lookback assumption fallback disclosure | Retain the existing unconfirmed-source footer for displayed raw assumptions when assessment-row shaping falls back. Do not infer truth for records without assessments; warning copy and Teams disclosure remain open. |
| Milestone rollback read-path accounting | Record `legacy` for milestone and dependency manifest paths after the existing audited fallback returns, so doctor can detect a non-legacy selector mismatch. Keep the captured selector and quality-gate policy unchanged; other family and rendering provenance remains open. |
| Authored milestone rollback | When the existing audited rollback is enabled after an unexpected reality failure, project milestones and dependencies from the selected authored legacy sources rather than merged or stored facts. Preserve legacy-mode and successful reality reads; source-read errors propagate. |
| Authored fact comparison and lookback warnings | Compare empty non-legacy risk/assumption reads and perform audited rollback against authored legacy sources rather than merged facts. Distinguish invalid comparison sources from reality failures, and retain assumption diagnostics in lookback draft warnings even without a section. Preserve legacy reads and quality-gate policy; broader authority and render-fallback work remains open. |
| Mixed-family shim isolation | Skip explicitly primary families' mapped legacy builders before they can fail, quarantine files or add diagnostics during mixed-mode `ProgramReality` loads. Preserve non-primary readers, legacy comparison/cross-checks and existing global-mode behavior; this is not full authority convergence. |
| WorkIQ unstructured source-time quarantine | Quarantine source times that overflow UTC conversion or have no effective offset instead of aborting gather or assuming the host timezone. Preserve valid partial observations and cached diagnostics; structured-discovery and identity policy are unchanged. |
| SQLite setup cleanup | Close connections whose configuration fails before a unit of work is returned, including failed lock/busy retry attempts, and close the related synthetic test fixture. Preserve error visibility, retry and successful transaction behavior; no recovery or multi-host qualification. |
| WorkIQ raw identity fallback | When an invalid higher-priority raw message field would cause quarantine, try later non-URL aliases using the existing native-candidate checks. Preserve selected values and first-choice behavior; the separate provisional-preview attribution gap remains open. |
| Offline report reality context | Defer the AI-only reality summary until an enabled, non-disabled draft needs it, reusing the selected report snapshot. Offline, diff and review readers avoid unnecessary tokenizer access; active and observe-only prompt content and disabled-mode warnings remain unchanged. Other offline paths and tokenizer assets are not qualified. |
| Synthetic candidate workflow regression | Exercise onboarding, context repair, synthetic ADO discovery/hydration, committed gather lineage, draft review, stale-revision refusal, re-review and non-forced confirmation in one isolated workspace. Check the archived work-item link and next-issue review initialization; no live-provider or operating qualification. |
| REV rollback integration coverage | Align the synthetic end-to-end assertion with the existing faithful rollback event type; no production authority or behavior changes. |
| WorkIQ native identity fallback | Resolve conversation, thread and message identifiers independently in priority order, removing unusable grouping fields and normalizing rich-extraction candidates. This can restore or re-key previously omitted candidates without authenticating provider identity. Gather still mislabels some provisional or thread-only observations as native messages; this work remains in progress. |
| WorkIQ pre-admission identity visibility | Preserve count-only diagnostics for examined in-window records lacking both source and preview identity through gather, cached prefetch and rich enrichment. Prevent rejected-only zero-yield updates while preserving existing admission, partial evidence and limits; counts do not establish source completeness. |
| WorkIQ structured-response failure visibility | Distinguish unusable structured responses from valid empty results. Surface generic retryable gather and cached-prefetch diagnostics, and avoid advancing rich-enrichment zero-yield state; do not infer rejected-record counts or source completeness. |
| WorkIQ source-time rejection visibility | Preserve count-only diagnostics for examined missing or invalid timestamps through gather, cached prefetch and rich enrichment. Keep valid partial evidence, distinct quarantine signals and existing admission/limits; rejected-only rich results no longer advance zero-yield state, and affected prefetch snapshots are degraded. Counts do not establish completeness. |
| PDF dependency security floor | Require and lock pypdf 6.21.4 or newer to exclude three OSV findings affecting 6.21.3; no application behavior change. |
| Decision-brief workspace convergence | Honor the selected programs home for pending proposals, signal evidence, AI advice, HTML and pilot comparison/summary records. Reuse shared admission before command/startup effects; preserve reports-only and unset-legacy compatibility, AI-disabled behavior and pilot separation. No new provider, migration or operating authority. |
| Milestone-aware verdict observation | Retain selected milestone records, computed assessments and diagnostics without rereads. Missed/At Risk floor the observation at High/Medium; unresolved overdue targets floor Medium. Preserve deferred/completed assessment exemptions, missing-evidence gaps and no global completion clearance; do not treat heuristic uncertainty as a measured forecast. Published health, gates and actions remain unchanged. |
| Risk-aware verdict observation | Retain RiskStage's selected risks, assessment context, stale-review IDs and diagnostics without rereads. Share cockpit's existing high-score threshold; active strategic high risks floor the observation at High, with linked nonauthoritative asks for missing owner/mitigation ETA. Preserve risk kind in new register-to-fact writes so candidates/hygiene cannot silently reload as strategic. Preserve coverage/trust gaps and refuse ambiguous risk identities; no historical backfill, rendered-health, publication-gate, journal or action authority change. |
| Observe-only program verdict | Ordinary program-backed manifests retain a frozen, versioned verdict and content-bound observation inputs. Known adverse evidence and exact schedule-risk floors remain visible; unqualified capture and unevaluated axes stay explicit gaps with no Low/Done clearance. No rendered-health, publication-gate, AI or actuation authority change. |
| Fleet assessment coverage | Preserve Unknown and blank/missing risk rows in confirmed-issue rollups. Retain known Blocked/High/Medium, prevent green Low/Done subsets, withhold trend for incomplete/missing comparisons and disclose coverage through all five formats. Support Blocked ranking; publication staleness, context diagnostics and persisted history remain unchanged. |
| Scorecard missing-assessment trends | Shared trend direction is explicitly unknown for missing prior or Unknown assessments, preventing false improvement/worsening hints. Report vectors omit unsupported up/down/New High labels; known comparisons, stale diagnostics and observed consecutive-High counts remain, with counts labeled in issues rather than weeks. |
| Scorecard selected coverage | Retain every presented dimension selected by effective composition even without evidence, overrides or prior baseline. Zero matches remains Needs Input and reaches health/QG-8 rather than disappearing behind assessed peers. Preserve explicit hides, linked presentation, current risks and composition; no new schema or authority. |
| Scorecard baseline evidence separation | Keep prior confirmed risk as historical context instead of substituting it for current Unknown. Inherited dimensions remain visible; missing current assessment retains Needs Input and QG-8 blocking, including prior Low/Done. Preserve current known risks, dependency uplift and explicit override precedence; no schema or baseline mutation. |
| Scorecard unknown aggregation | The shared scorecard engine derives High > Medium > Unknown > Low > Done, so unassessed matched work items cannot disappear behind Low/Done peers. Preserve known High/Medium, all matched counts/identities and explicit dimension overrides. Actual dashboard/continuity reports retain Unknown and QG-8; no source normalization, schema, forecast/trust policy or complete ProgramVerdict migration. |
| Report health unknown preservation | Keep unassessed dimensions in totals and prevent an assessed Low/Done subset or empty report from claiming HEALTHY, healthy streak or default no-decisions guidance. HTML/Teams share the existing health boundary and email uses the same healthy eligibility; observed High/Medium and explicit author asks remain visible. Missing current/prior assessments suppress comparative improvement and risk-load/count deltas; the numeric diagnostic is labeled incomplete. No full ProgramVerdict, forecast/trust policy or publish-authority change. |
| Chart-cache definition binding | Gather and report writeback persist one shared SHA-256 definition hash binding source/KQL, rendering configuration and declared admission. Gather refreshes changed or unbound legacy definitions; optional null/missing metadata remains readable, malformed hashes refuse explicitly, invalid writes preserve prior bytes. Stable nested key order avoids needless invalidation; definition equality does not certify original results or enable declared-contract cache bypass. |
| Chart gather cache admission | Forward the selected workspace root and require exact program/query identity with nonnegative age below query TTL for legacy undeclared cache hits. Declared limits/scalar-required queries execute live until original-response cache lineage exists; retention grace is not freshness. Log recoverable cache-load failures and report size-refused writes explicitly without replacing prior bytes or stopping independent valid queries. No cache schema, lineage backfill or provider certification. |
| Kusto consumer admission convergence | Reuse one streaming declared-row admission helper across SDK conversion and injected hydration/report/primary/fallback consumers, direct/KPI/history and live chart gathering. Refused rows cannot advance history/freshness, create observations or overwrite chart cache; independent valid queries continue. Fallback query/auth errors degrade explicitly instead of becoming empty data. Preserve existing scalar/numeric/SLO error priority and legacy empty behavior; no hidden SDK metadata or historical-cache lineage certification. |
| Common Kusto result admission | Refuse SDK-reported partial completion and enforce authored projected-schema, combined-row and decoded UTF-8 byte limits through the shared client, including `no_safety`. Preserve limits and scalar-required declarations across query/report loaders and state reconstruction; changed admission invalidates validation lineage. Actual hydration/report consumers retain failure/degradation, never a successful subset. No invented legacy budgets, full KQL/type/ordering/freshness contract or provider cancellation claim. |
| Shared milestone evidence qualification | Future/target-day milestones without observed linked evidence become Unknown with no confidence or computed completion, and missing required links are coverage gaps rather than a completed subset. Existing report/doctor/status/CLI assessment is shared; report summaries count Unknown and coverage messages retain actual reasons, while status readiness reflects the persisted assessment. Preserve explicit Deferred, past-due and observed-item rules; no unified program-verdict or operational certification. |
| Reply-verification route discovery | Regenerate the missing `context verify-reply-origin` entry in the CLI intent catalogue so discovery matches the existing command and options; no new command or authority. |
| Create missing-review repair | Verified create rerun restores one missing review for an exact retained target signal after review-write failure, without another create or duplicate signal. Preserve every existing review decision and non-create dedup behavior; repeated failures remain explicit, changed remote state cannot repair. No atomic transaction or historical sweep. |
| Retained create binding | Persist original outbox reference with intent/time before dispatch; reuse one key/payload construction and refuse recovery before provider reads on changed proposal/payload/intent/destination, missing row or domain/correlation drift. Preserve legacy reference-free upcast without binding certification; no regenerated attempted identity, retry, queue mutation or authenticated approval claim. |
| Requested create-field readback | Reuse the create patch's sent fields in the existing recovery batch read; refuse missing/malformed/different assignee, description, paths, priority or target date before adoption/audit. Explicit uniqueName/string identity matching, strict other-field equality, unchanged title/tag rules; no extra request/retry/outbox closeout or full reconciliation certification. |
| Verified adoption audit | Reuse the existing signal/review path after exact create-recovery gates; label adopted targets explicitly and retain existing audits under dedup. File/SQLite replay and refusal controls preserve one remote effect and uncertain outbox evidence. No backlog audit, atomic repair or reconciliation closeout certification. |
| Create audit target | Use one resolved applied entry for first-create audit signals and manifest persistence, binding signal/review/receipt identity to the real work item rather than `WI:0`. Distinct creates retain distinct audits; preserve replay, failed/uncertain controls and historical rows without new dedup policy or atomic recovery guarantees. |
| Create HTTP uncertainty | Classify 408/5xx/redirect/nonfinal and malformed success responses as inconclusive rather than definite create failure; persist existing uncertainty/alert with no retry. Disable mutation redirect following, preserve ordinary 4xx/401/429 and read retries. Other writer operations retain failure handling; no generalized outbox/recovery authority. |
| Recovery adoption evidence | Validate exact positive integer candidate/readback IDs and retain optional batch revision in adopted manifests instead of guessed `1`. Malformed row/ID/revision uses safe reconciliation refusal before success events or dispatch. No extra provider read, legacy backfill or uncertain-outbox closeout. |
| Observed create revision | Carry provider-observed revision through the durable acknowledgement, dispatch outcome and first-create manifest instead of hardcoded `1`. Omitted/null revision stays unavailable; malformed supplied revision becomes uncertainty without retry. Preserve positional result compatibility; legacy and recovery-adoption evidence remain unqualified. |
| Durable create acknowledgement | Persist append-once remote-ID/payload/destination evidence atomically with success in the existing outbox database; consume it before completion even if the secondary ledger fails. Real ADO callbacks hash successful responses and classify invalid post-request IDs as uncertain rather than completed or automatically retryable. Preserve legacy rows and local-registry behavior; full terminal receipts/recovery remain incomplete. |
| Outbox transition ordering | Enforce transactional predecessor states: prevent premature actuation completion, redispatch after success and retryable downgrade after dispatch. Preserve local-registry leased completion, terminal/manual-closeout controls and all retained fields on refusal. No new schema, provider writes or receipt/recovery authority. |
| Actuation execution admission | Recheck loaded policy/rule/adapter/operation, approval/gap and current TTL at both CLI and direct dispatch; reject truthy enablement and malformed stored approval/time/TTL instead of treating them as approved/current. Preserve default-deny live writes and write-free dry-run; report correction/review guidance without mutating facts or claiming atomic rate/outbox guarantees. |

Modified files: `pyproject.toml`, `uv.lock`, `src/core/_db.py`, `src/core/actuation_engine.py`, `src/core/program_fact_store.py`, `src/core/program_reality.py`,
`src/core/pipeline.py`, `src/core/stages/validation_stage.py`, `src/commands/doctor_checks/storage_checks.py`,
`src/commands/confirm.py`, `src/commands/confirm_stages/archive_transaction.py`,
`src/commands/doctor_checks/context_checks.py`, `src/core/context_gap_store.py`,
`src/core/context_proposal_review.py`, `src/commands/review_full.py`,
`src/core/stages/sor_gated_load.py`, `src/core/stages/risk_stage.py`, `src/commands/report_lookback.py`,
`docs/contributing/migrate-fact-family.md`,
`cli.py`, `src/commands/decision_brief.py`, `src/commands/decision_brief_pilot.py`,
`src/core/ledger/durable_outbox_store.py`, `src/core/actuation_outbox.py`,
`src/m365/ado_writer.py`,
`src/core/ado_proposal.py`,
`src/core/exceptions.py`,
`src/core/milestone_engine.py`, `src/core/models_v2.py`,
`src/core/stages/milestone_stage.py`, `src/core/stages/render_stage.py`, `src/commands/report_health.py`,
`src/core/scorecard_engine.py`,
`src/core/scorecard_trends.py`, `src/commands/report_pipeline/assemble_stage.py`,
`src/commands/fleet.py`,
`src/core/program_verdict.py`, `src/core/manifest_writer.py`, `src/core/fact_sor_state.py`, `src/commands/report.py`,
`src/core/risk_register_engine.py`, `src/core/cockpit_builder.py`,
`src/commands/report_scorecards.py`,
`src/commands/report_email.py`, `src/core/view_models.py`,
`templates/partials/health_banner.j2`, `templates/partials/top_3_now.j2`, `templates/base.teams.j2`,
`templates/partials/continuity_exec_summary.j2`,
`scripts/verify_spec_citations.py`,
`src/core/kusto_client.py`, `src/core/kusto_result_limits.py`,
`src/core/kusto_query_loader.py`, `src/core/knowledge_store.py`,
`src/core/config_loader.py`, `src/core/config_loader_v2.py`, `src/core/edition_resolver.py`,
`src/core/kusto_hydration.py`, `src/core/kusto_rendering.py`, `src/core/metric_binding_validator.py`,
`src/core/chart_cache_store.py`,
`src/commands/gather.py`, `src/commands/chart_gather.py`,
`src/commands/gather_auxiliary.py`, `src/commands/gather_workiq_helpers.py`,
`src/commands/enrich.py`, `src/commands/prefetch.py`, `src/commands/report_ai.py`,
`src/commands/gather_pipeline/workiq_prefetch_stage.py`,
`src/m365/workiq_ask_support.py`, `src/m365/workiq_retriever.py`,
`src/commands/gather_pipeline/kusto_query_helpers.py`, `src/commands/gather_pipeline/ado_kpi_stage.py`,
`vertex/intent_routes.yaml`,
`src/commands/actuate.py`, `src/commands/doctor_checks/refactor_status.py`,
`specs/vertex-prd.md`, `specs/vertex-tech-spec.md`,
`specs/vertex-ux-spec.md`.
Tests: `tests/contracts/test_wi72_actuation_execute.py`,
`tests/unit/test_confirm_archive_evidence_recheck.py`, `tests/unit/test_confirm_archive_evidence_recheck_process.py`,
`tests/unit/test_confirm_archive_transaction.py`,
`tests/unit/test_classify_workstream_id_consumers.py`,
`tests/unit/test_sqlite_stores_connection.py`, `tests/unit/test_program_fact_store.py`,
`tests/unit/test_sor_gated_load.py`, `tests/contracts/test_ws_risk_sor_flip.py`,
`tests/contracts/test_ws_assumption_sor_flip.py`, `tests/unit/test_reality_pinned_facts.py`,
`tests/unit/test_report_reality_reuse.py`, `tests/unit/test_stage_fact_archive_context.py`,
`tests/unit/test_commands_decision_brief.py`, `tests/unit/test_commands_decision_brief_pilot.py`,
`tests/unit/test_durable_outbox_store.py`, `tests/unit/test_actuation_outbox.py`,
`tests/unit/test_ado_writer.py`, `tests/contracts/test_outbox_create_fault_matrix.py`,
`tests/contracts/test_create_intent_idempotency.py`.
WorkIQ rejection and cached-prefetch controls: `tests/unit/test_m365_workiq_ask_support.py`,
`tests/unit/test_workiq_retriever_identity.py`, `tests/unit/test_workiq_observation_integrity.py`,
`tests/unit/test_workiq_prefetch_stage.py`, `tests/unit/test_prefetch_command.py`.
REV end-to-end coverage: `tests/integration/test_rev_e2e.py`.
Synthetic candidate workflow: `tests/unit/test_commands_onboard.py`.
Context-doctor optional registry controls: `tests/unit/test_doctor_context_checks.py`.
Context-maintenance scope and failure controls: `tests/unit/test_context_proposal_review.py`,
`tests/unit/test_commands_triage.py`, `tests/unit/test_cockpit_build_explain_compare.py`,
`tests/unit/test_commands_review_full.py`, `tests/unit/test_context_gap_store.py`,
`tests/unit/test_commands_context_solicitation.py`.
Storage-doctor connection and context controls: `tests/unit/test_doctor_storage_checks.py`,
`tests/unit/test_doctor_storage_context.py`.
Report reality-context and offline controls: `tests/unit/test_commands_report_ai.py`,
`tests/unit/test_pipeline_stages.py`, `tests/unit/test_commands_report.py`,
`tests/unit/test_commands_diff.py`, `tests/unit/test_commands_review_full.py`,
`tests/unit/test_report_health_failure_containment.py`.
Additional transport tests: `tests/contracts/test_actuation_transport.py`.
Milestone/discovery tests: `tests/unit/test_milestone_no_evidence.py`,
`tests/unit/test_milestone_engine.py`, `tests/unit/test_commands_milestones.py`,
`tests/contracts/test_intent_routes_catalog.py`.
Kusto admission/propagation tests: `tests/unit/test_kusto_client.py`,
`tests/unit/test_kusto_query_loader.py`, `tests/unit/test_config_loader.py`.
Injected-consumer/history/chart tests: `tests/unit/test_gather_kusto_query_helpers.py`,
`tests/unit/test_kusto_rendering.py`, `tests/unit/test_kusto_metric_interpretation.py`,
`tests/unit/test_chart_gather.py`.
Chart-cache controls: `tests/unit/test_chart_cache_store.py`.
Report-health regression and actual output controls: `tests/unit/test_commands_report.py`.
Scorecard aggregation matrix: `tests/unit/test_scorecard_engine.py`.
Missing-comparison and narrative controls: `tests/unit/test_scorecard_trends.py`,
`tests/unit/test_risk_trend_containment.py`, `tests/unit/test_ado_narrative_hint_engine.py`.
Fleet coverage, actual formats and CLI controls: `tests/unit/test_commands_fleet.py`.
Verdict invariants and actual output capture: `tests/unit/test_program_verdict.py`,
`tests/unit/test_report_fact_capture.py`.
Capture admission and confirmation controls: `tests/unit/test_run_evidence_contracts.py`,
`tests/unit/test_confirm_manifest_capture.py`, `tests/unit/test_confirm_archive_evidence_recheck.py`,
`tests/unit/test_confirm_archive_evidence_recheck_process.py`.
Risk-kind round-trip and shared cockpit threshold controls:
`tests/unit/test_risk_register_engine.py`, `tests/unit/test_cockpit_view_models.py`.
Integration corrections: `tests/contracts/test_architecture_fitness.py`,
`tests/contracts/test_ws17_rate_limit_retry_contract.py`,
`tests/unit/test_commands_status.py`, `tests/unit/test_commands_triage.py`,
`tests/unit/test_report_workspace_identity.py`,
`tests/golden/test_report_characterization.py`,
`tests/golden/public_snapshots/report_draft_issue_001.golden`,
`tests/golden/public_snapshots/report_draft_issue_001_condensed.golden`,
`tests/golden/public_snapshots/report_draft_issue_001_kusto.golden`,
`tests/golden/public_snapshots/report_draft_issue_001_narrative.golden`,
`tests/unit/test_ai_program_synthesizer.py`.

## 2026-11-04

| Change | Behavior |
|---|---|
| Shared ADO risk policy | Unify hydration with report/gather/freshness and child/history normalization: terminal DONE, recognized active-item authored assessment before tag/state heuristics, and unsupported values UNKNOWN instead of implicit LOW. Preserve dimension overrides and strategic risk proposals. |
| Projection input binding | Rebuild stale caches when event history, bitemporal filters or gap acknowledgements change; preserve safe incremental appends, clear obsolete event-owned shadows, and keep lock controls out of entity provenance. Old schema/projector caches rebuild, including absent stamp columns. |
| Reply-origin verification | Record an explicit authorized native-OS-principal check against exact reply bytes/proposal digest without approving or applying the change; independent acceptance rejects missing/stale/revoked verification, and review/diff surfaces label current origin separately from intake history. |
| Compiler rejection containment | Discard rejected program-synthesis context before cache/provider access instead of rebuilding an ad-hoc prompt; actual pilot comparisons skip unreleased candidates and preserve earlier released history. |
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

Modified files: `src/core/context_gap_reply_import.py`, `src/core/ncfl_models.py`,
`src/core/ado_enrichment.py`, `src/core/ado_hydration.py`,
`src/commands/freshness.py`,
`src/core/ncfl_proposal_store.py`, `src/core/operator_identity.py`,
`src/core/config_loader.py`, `src/core/context_proposal_review.py`,
`src/core/manual_registry_diff.py`, `src/commands/context.py`, `cli.py`,
`specs/vertex-prd.md`, `specs/vertex-ux-spec.md`,
`src/ai/program_synthesizer.py`,
`src/m365/agency_bridge.py`, `src/core/rev/governor.py`,
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
Tests: `tests/unit/test_context_gap_reply_import.py`,
`tests/unit/test_ado_enrichment.py`, `tests/unit/test_ado_hydration.py`,
`tests/unit/test_ado_enrichment_commands.py`,
`tests/unit/test_commands_context_reply_import.py`,
`tests/unit/test_context_reply_startup_cli.py`, `tests/unit/test_config_loader.py`,
`tests/contracts/cli_reference_snapshot.md`,
`tests/unit/test_ai_program_synthesizer.py`,
`tests/unit/test_commands_program_synthesizer_pilot.py`,
`tests/unit/test_m365_agency_bridge.py`,
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
`tests/contracts/test_incremental_projection_write_hook.py`,
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
