from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_activation import (
    _build_evidence_appendix_checks,
    build_counterfactual_diff_artifact,
    build_corpus_certification_check,
    build_corpus_freeze_check,
    build_corpus_freeze_manifest,
    build_family_matrix,
    clean_cycle_streak,
    counterfactual_render_diff,
    is_clean_cycle,
    write_corpus_freeze_manifest,
    write_counterfactual_diff_artifact,
)


def test_is_clean_cycle_rejects_empty_complete_cycle() -> None:
    cycle = {
        "cycle_status": "complete",
        "shield_degrade": False,
        "extraction_degraded": False,
        "terminal_failures": 0,
        "enumerated": 0,
        "candidates_staged": 0,
        "llm_fallback_count": 0,
    }

    assert is_clean_cycle(cycle, eml_present=True) is False


def test_is_clean_cycle_requires_quality_valid_cycle() -> None:
    cycle = {
        "cycle_status": "complete",
        "shield_degrade": False,
        "extraction_degraded": True,
        "terminal_failures": 0,
        "enumerated": 2,
        "candidates_staged": 1,
        "llm_fallback_count": 0,
    }

    assert is_clean_cycle(cycle, eml_present=True) is False


def test_is_clean_cycle_accepts_authority_valid_cycle() -> None:
    cycle = {
        "cycle_status": "complete",
        "shield_degrade": False,
        "extraction_degraded": False,
        "terminal_failures": 0,
        "enumerated": 2,
        "candidates_staged": 1,
        "llm_fallback_count": 0,
    }

    assert is_clean_cycle(cycle, eml_present=True) is True


def test_clean_cycle_streak_counts_only_trailing_authority_valid_cycles() -> None:
    good = {
        "cycle_status": "complete",
        "shield_degrade": False,
        "extraction_degraded": False,
        "terminal_failures": 0,
        "enumerated": 2,
        "candidates_staged": 1,
        "llm_fallback_count": 0,
    }
    empty = dict(good, enumerated=0, candidates_staged=0)

    assert clean_cycle_streak([good, good], eml_present=True) == 2
    assert clean_cycle_streak([good, empty, good], eml_present=True) == 1
    assert clean_cycle_streak([good, good], eml_present=False) == 0


def test_counterfactual_diff_requires_attributable_added_content() -> None:
    result = counterfactual_render_diff(
        "Milestone GA completed. [source: eml:sha256:abc | approval evt-123]",
        "",
        source_document_key="eml:sha256:abc",
        approval_event_id="evt-123",
    )

    assert result.passed is True
    assert result.source_document_key_present is True
    assert result.approval_event_id_present is True


def test_counterfactual_diff_rejects_delta_without_approval_event_when_required() -> None:
    result = counterfactual_render_diff(
        "Milestone GA completed. [source: eml:sha256:abc]",
        "",
        source_document_key="eml:sha256:abc",
        approval_event_id="evt-123",
    )

    assert result.passed is False
    assert result.source_document_key_present is True
    assert result.approval_event_id_present is False
    assert result.reason == "render changed, but added content does not carry approval_event_id"


def test_counterfactual_diff_rejects_identical_render() -> None:
    result = counterfactual_render_diff("same", "same", source_document_key="eml:sha256:abc")

    assert result.passed is False
    assert result.reason == "render outputs are identical"


def test_counterfactual_diff_rejects_delta_without_source_key() -> None:
    result = counterfactual_render_diff(
        "Milestone GA completed.",
        "",
        source_document_key="eml:sha256:abc",
    )

    assert result.passed is False
    assert result.reason == "render changed, but added content does not carry source_document_key"


def test_counterfactual_diff_artifact_contains_metadata_and_unified_diff() -> None:
    artifact = build_counterfactual_diff_artifact(
        "Milestone GA completed. [source: eml:sha256:abc | approval evt-123]\n",
        "Milestone GA pending.\n",
        source_document_key="eml:sha256:abc",
        approval_event_id="evt-123",
        with_fact_label="with.html",
        without_fact_label="without.html",
        context_lines=0,
    )

    assert "# Activation Counterfactual Diff" in artifact
    assert "passed: `true`" in artifact
    assert "approval_event_id: `evt-123`" in artifact
    assert "approval_event_id_present: `true`" in artifact
    assert "--- without.html" in artifact
    assert "+++ with.html" in artifact
    assert "+Milestone GA completed. [source: eml:sha256:abc | approval evt-123]" in artifact


def test_write_counterfactual_diff_artifact_requires_complete_render_pair(tmp_path: Path) -> None:
    out = tmp_path / "diff.md"

    try:
        write_counterfactual_diff_artifact(
            output_path=out,
            with_fact_path=None,
            without_fact_path=tmp_path / "without.html",
            source_document_key="eml:sha256:abc",
        )
    except ValueError as error:
        assert "--counterfactual-diff requires" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_write_counterfactual_diff_artifact_writes_file(tmp_path: Path) -> None:
    with_fact = tmp_path / "with.html"
    without_fact = tmp_path / "without.html"
    out = tmp_path / "diff.md"
    with_fact.write_text("Done [source: eml:sha256:abc | approval evt-123]\n", encoding="utf-8")
    without_fact.write_text("Pending\n", encoding="utf-8")

    write_counterfactual_diff_artifact(
        output_path=out,
        with_fact_path=with_fact,
        without_fact_path=without_fact,
        source_document_key="eml:sha256:abc",
        approval_event_id="evt-123",
    )

    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "source_document_key: `eml:sha256:abc`" in text
    assert "approval_event_id: `evt-123`" in text
    assert "+Done [source: eml:sha256:abc | approval evt-123]" in text


def test_corpus_certification_fails_without_keystone_dual_labels_or_ci_floor() -> None:
    labels = [
        {"expected_event_type": "artifact.published.v1", "label": "accept"}
        for _ in range(30)
    ]
    quality = {
        "kappa": None,
        "kappa_n": 0,
        "g_xtract_prec": 0.87,
        "g_xtract_prec_ci": (0.66, 0.95),
        "g_accept_prec": 1.0,
        "g_accept_prec_ci": (0.82, 1.0),
        "failures": [],
    }

    check = build_corpus_certification_check(
        labels=labels,
        quality_metrics=quality,
        keystone_ledger_event_type="milestone.completed.v1",
    )

    assert check.status == "fail"
    assert check.details["keystone_label_count"] == 0
    assert any("keystone_labels" in failure for failure in check.details["activation_failures"])
    assert any("kappa_n" in failure for failure in check.details["activation_failures"])
    assert any("g_xtract_prec_ci_low" in failure for failure in check.details["activation_failures"])


def test_corpus_certification_passes_with_keystone_dual_labels_kappa_and_ci_floor() -> None:
    labels = [
        {
            "expected_event_type": "milestone.completed.v1",
            "label": "accept",
            "second_label": "accept",
        }
        for _ in range(30)
    ]
    quality = {
        "kappa": 0.82,
        "kappa_n": 20,
        "g_xtract_prec": 0.94,
        "g_xtract_prec_ci": (0.84, 0.98),
        "g_accept_prec": 0.97,
        "g_accept_prec_ci": (0.88, 0.99),
        "failures": [],
    }

    check = build_corpus_certification_check(
        labels=labels,
        quality_metrics=quality,
        keystone_ledger_event_type="milestone.completed.v1",
    )

    assert check.status == "pass"
    assert check.details["keystone_label_count"] == 30
    assert check.details["keystone_dual_labeled_count"] == 30


def test_corpus_freeze_check_fails_when_manifest_is_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    quality_root = programs_root / "xpf" / "_quality"
    quality_root.mkdir(parents=True)
    (quality_root / "rev_labeled_corpus.jsonl").write_text(
        json.dumps({"candidate_id": "c1", "expected_event_type": "milestone.completed.v1", "label": "accept"}) + "\n",
        encoding="utf-8",
    )
    (quality_root / "corpus_manifest.jsonl").write_text(
        json.dumps({"filename": "one.eml", "dominant_strata": ["milestone"]}) + "\n",
        encoding="utf-8",
    )

    check = build_corpus_freeze_check(program="xpf", programs_root=programs_root, repo_root=tmp_path)

    assert check.status == "fail"
    assert check.summary == "corpus freeze manifest is missing"
    assert check.details["expected"]["files"]["rev_labeled_corpus.jsonl"]["rows"] == 1


def test_corpus_freeze_check_passes_when_hashes_rows_and_commit_match(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    quality_root = programs_root / "xpf" / "_quality"
    quality_root.mkdir(parents=True)
    (quality_root / "rev_labeled_corpus.jsonl").write_text(
        json.dumps({"candidate_id": "c1", "expected_event_type": "milestone.completed.v1", "label": "accept"}) + "\n",
        encoding="utf-8",
    )
    (quality_root / "corpus_manifest.jsonl").write_text(
        json.dumps({"filename": "one.eml", "dominant_strata": ["milestone"]}) + "\n",
        encoding="utf-8",
    )

    freeze_path = write_corpus_freeze_manifest(program="xpf", programs_root=programs_root, repo_root=tmp_path)
    manifest = build_corpus_freeze_manifest(program="xpf", programs_root=programs_root, repo_root=tmp_path)
    check = build_corpus_freeze_check(program="xpf", programs_root=programs_root, repo_root=tmp_path)

    assert freeze_path == quality_root / "corpus_freeze.json"
    assert check.status == "pass"
    assert manifest["files"]["rev_labeled_corpus.jsonl"]["rows"] == 1
    assert manifest["files"]["rev_labeled_corpus.jsonl"]["sha256"]


def test_corpus_freeze_check_detects_row_and_hash_drift(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    quality_root = programs_root / "xpf" / "_quality"
    quality_root.mkdir(parents=True)
    (quality_root / "rev_labeled_corpus.jsonl").write_text(
        json.dumps({"candidate_id": "c1", "expected_event_type": "milestone.completed.v1", "label": "accept"}) + "\n",
        encoding="utf-8",
    )
    (quality_root / "corpus_manifest.jsonl").write_text(
        json.dumps({"filename": "one.eml", "dominant_strata": ["milestone"]}) + "\n",
        encoding="utf-8",
    )
    write_corpus_freeze_manifest(program="xpf", programs_root=programs_root, repo_root=tmp_path)
    (quality_root / "rev_labeled_corpus.jsonl").write_text(
        json.dumps({"candidate_id": "c1", "expected_event_type": "milestone.completed.v1", "label": "accept"}) + "\n"
        + json.dumps({"candidate_id": "c2", "expected_event_type": "milestone.completed.v1", "label": "reject"}) + "\n",
        encoding="utf-8",
    )

    check = build_corpus_freeze_check(program="xpf", programs_root=programs_root, repo_root=tmp_path)

    assert check.status == "fail"
    assert "rev_labeled_corpus.jsonl.rows mismatch" in check.details["failures"]
    assert "rev_labeled_corpus.jsonl.sha256 mismatch" in check.details["failures"]


def test_family_matrix_surfaces_shared_milestones_accessor(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    quality_root = programs_root / "xpf" / "_quality"
    quality_root.mkdir(parents=True)
    (quality_root / "rev_labeled_corpus.jsonl").write_text(
        json.dumps({"expected_event_type": "milestone.completed.v1", "label": "accept"}) + "\n",
        encoding="utf-8",
    )
    (quality_root / "corpus_manifest.jsonl").write_text(
        json.dumps({"dominant_strata": ["milestone"], "filename": "update.eml"}) + "\n",
        encoding="utf-8",
    )

    matrix = build_family_matrix(program="xpf", programs_root=programs_root)
    by_claim = {row.claim_event_type: row for row in matrix}

    assert by_claim["milestone.completed"].accessor == "milestones()"
    assert by_claim["deployment.completed"].accessor == "milestones()"
    assert by_claim["milestone.completed"].labeled_count == 1


def test_evidence_checks_include_p5_rollback_and_lineage_contracts(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    programs_root = tmp_path / "programs"
    (repo_root / "src" / "commands").mkdir(parents=True)
    (repo_root / "scripts").mkdir(parents=True)
    (repo_root / "src" / "core" / "rev").mkdir(parents=True)
    (repo_root / "src" / "core" / "ledger").mkdir(parents=True)
    (repo_root / "src" / "core" / "stages").mkdir(parents=True)
    (repo_root / "src" / "core").mkdir(parents=True, exist_ok=True)
    (repo_root / "templates" / "partials").mkdir(parents=True)
    (repo_root / ".archive" / "specs").mkdir(parents=True)
    (repo_root / "tests" / "contracts").mkdir(parents=True)
    (repo_root / "tests" / "unit").mkdir(parents=True)
    (repo_root / "tests").mkdir(exist_ok=True)
    quality_root = programs_root / "xpf" / "_quality"
    rev_root = programs_root / "xpf" / "_rev"
    armada_root = programs_root / "armada"
    quality_root.mkdir(parents=True)
    rev_root.mkdir(parents=True)
    armada_root.mkdir(parents=True)

    (repo_root / "src" / "commands" / "report.py").write_text("ProgramReality\n", encoding="utf-8")
    (repo_root / "scripts" / "verify_activation.py").write_text(
        "def write_counterfactual_diff_artifact(): pass\n"
        "--counterfactual-diff\n"
        "--approval-event-id\n"
        "difflib.unified_diff\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "commands" / "ledger.py").write_text(
        '@triage_app.command("edit")\n'
        '@triage_app.command("revoke")\n'
        '"operator.correction.v1"\n'
        '"discovery.candidate_revoked.v1"\n',
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "models_v2.py").write_text("fact_bridge_enabled: bool = False\n", encoding="utf-8")
    (repo_root / "src" / "core" / "view_models.py").write_text(
        "source_document_key: str | None = None\napproval_event_id: str | None = None\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "truth_model.py").write_text(
        "def detect_corroboration_and_conflicts():\n    return 'fact.conflict'\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "program_reality.py").write_text(
        "disputed_natural_keys\n"
        "def conflicts():\n    return ()\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "rev" / "privacy.py").write_text(
        "class PseudonymTable: pass\n"
        "def pseudonymize_text(): pass\n"
        "def run_local_checks(): pass\n"
        "def scan_credentials(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "rev" / "normalizer.py").write_text("pseudonym_table\n", encoding="utf-8")
    (repo_root / "src" / "core" / "rev" / "entity_binding_gate.py").write_text(
        "PRECISION_FLOOR = 0.95\n"
        "COVERAGE_FLOOR = 0.80\n"
        "def evaluate_binding(): pass\n"
        "def binding_record_from_entity_refs(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "rev" / "pipeline.py").write_text(
        "cycle_status\nextraction_degraded\nterminal_failures\nsource_unreachable\n"
        "extraction_degraded: bool = False\n"
        '"extraction_degraded": report.extraction_degraded\n'
        '"extraction_degraded": report.extraction_degraded\n'
        "provenance_admit\n"
        # v1.24 AG-15 wiring markers: entity resolution must be wired into staging.
        "_resolve_candidate_entities\n"
        "EntityRegistry\n"
        # v1.25 AG-9 wiring markers: conflict detector invoked in cycle finalize.
        "_run_cross_source_conflict_check\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "ledger" / "fact_bridge.py").write_text(
        # v1.29: conflict detection moved out of rev/pipeline.py (W2-12 —
        # REV modules must never import ProgramFactStore/append_fact directly)
        # into fact_bridge.py, the sanctioned bridge location.
        "def run_cross_source_conflict_detection():\n    return 'fact.conflict'\n"
        "detect_corroboration_and_conflicts\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "operator_identity.py").write_text(
        "def capture_operator_identity(): pass\nprincipal\nmachine\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "rev").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "core" / "ledger").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "ai" / "rev").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "commands").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "core" / "rev" / "provenance_gate.py").write_text(
        "def evaluate_sender(): pass\ndef load_allowlist(): pass\nforge-EML\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "ado_schema_drift.py").write_text(
        "ADO_REQUIRED_FIELDS\nclass SchemaDriftError\ndef inspect_contract_drift\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "ado_hydration.py").write_text("assert_row_shape\n", encoding="utf-8")
    (repo_root / "src" / "core" / "ledger" / "triage_telemetry.py").write_text(
        "def record_triage_decision_telemetry(): pass\n"
        "time_to_triage_seconds\n"
        "def summarize_triage_telemetry(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "ledger" / "candidate_store.py").write_text(
        "record_triage_decision_telemetry\n"
        "prompt_version: str | None = None\n"
        "extraction_rationale: str | None = None\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "ledger" / "source_refs.py").write_text(
        "principal: str | None = None\nmachine: str | None = None\nthread_id: str | None = None\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "activation_slo.py").write_text(
        "class ActivationSloBudget: pass\n"
        "class ActivationSloSample: pass\n"
        "def evaluate_activation_slo(): pass\n"
        "rev_wall_clock_seconds_per_100_eml\n"
        "cost_usd_per_100_eml\n"
        "class TimeMotionSample: pass\n"
        "def evaluate_time_motion_roi(): pass\n"
        "manual_export_seconds\n"
        "manual_typing_seconds\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "activation_benefit.py").write_text(
        "class BenefitTrendSample: pass\n"
        "auto_approved_signal_rate\n"
        "operator_review_seconds\n"
        "def evaluate_longitudinal_benefit(): pass\n"
        "class SustainingQualitySample: pass\n"
        "def evaluate_corpus_rollback(): pass\n"
        "kappa_floor\n"
        "precision_ci_low_floor\n"
        "recall_ci_low_floor\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "activation_fleet.py").write_text(
        "class AccessorRolloutPlan: pass\n"
        "def build_accessor_rollout_plan(): pass\n"
        "class FleetProgramSample: pass\n"
        "def evaluate_fleet_soak(): pass\n"
        "quiet_lane_count\n"
        "growth_mb_per_program\n"
        "cost_usd_per_program\n"
        "fleet_concurrency_cap\n"
        "def evaluate_data_residency(): pass\n"
        "def evaluate_operator_clarity(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "activation_readiness.py").write_text(
        "class BindingReleaseReadiness: pass\n"
        "def evaluate_raw_data_feasibility(): pass\n"
        "def evaluate_annotation_staffing(): pass\n"
        "def evaluate_pilot_degrade_exception(): pass\n"
        "def evaluate_base_schema_cross_program(): pass\n"
        "def evaluate_binding_release_readiness(): pass\n"
        "def evaluate_cross_source_reconciliation(): pass\n"
        "def evaluate_explain_drilldown(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "program_fact_store.py").write_text(
        "source_document_key: str | None = None\n"
        "approval_event_id: str | None = None\n"
        "def as_redacted(): pass\n"
        "def as_retention_expired(): pass\n"
        "class FactLineageUnavailable: pass\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "ncfl_apply.py").write_text(
        "def apply_proposal(): pass\n"
        "def apply_proposals_batch(): pass\n"
        "def _write_journal(): pass\n"
        "needs_repair\n"
        "current_value_hash\n"
        "canonical save_*\n"
        "save_milestones\n"
        "save_workstreams_document\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "ncfl_apply_policy.py").write_text(
        "NCFL_APPLY_TRANSITIONS\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "ncfl_store_policy.py").write_text(
        "knowledge_doc\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "fact_sor_state.py").write_text(
        "def evaluate_family_flip_gate(): pass\n"
        "fact_store_family_cycles.yaml\n"
        "rolled_back_to_shadow\n",
        encoding="utf-8",
    )
    (repo_root / "vertex" / "policies").mkdir(parents=True, exist_ok=True)
    (repo_root / "vertex" / "policies" / "source_authority.yaml").write_text(
        "sor_flip:\n"
        "  defaults:\n"
        "    clean_cycles_to_flip: 5\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "ai" / "rev" / "extractor.py").write_text(
        "prompt_version: str =\nextraction_rationale: str | None = None\n"
        "_injection_fence\nuntrusted-email-\nsecrets.token_hex\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "commands" / "ledger.py").write_text(
        '@triage_app.command("edit")\n@triage_app.command("approve")\n@triage_app.command("revoke")\n'
        '@triage_app.command("batch-reject")\n'
        "operator.correction.v1\ndiscovery.candidate_revoked.v1\n"
        "capture_operator_identity\n"
        '"extraction_rationale": candidate.extraction_rationale\n'
        '"source_document_key": candidate.source_document_key\n'
        "why:\n"
        # v1.24 AG-11 wiring markers: projection privacy gate on ACCEPTED facts.
        "def _projection_privacy_gate(): pass\n"
        "run_local_checks\n"
        "OPERATOR_CONFIRMED\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "commands" / "report_deck.py").write_text(
        "from src.core.program_reality import ProgramReality\n"
        "milestone_lineage\n"
        "source_document_key=lineage.get('source_document_key')\n"
        "approval_event_id=lineage.get('approval_event_id')\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "deck_renderer.py").write_text(
        "class DeckMilestoneRow:\n"
        "    source_document_key: str | None = None\n"
        "    approval_event_id: str | None = None\n",
        encoding="utf-8",
    )
    (repo_root / "templates" / "archetypes").mkdir(parents=True, exist_ok=True)
    (repo_root / "templates" / "archetypes" / "deck.j2").write_text(
        "{{ row.source_document_key }} {{ row.approval_event_id }}\n",
        encoding="utf-8",
    )
    (repo_root / "specs").mkdir(parents=True, exist_ok=True)
    (repo_root / "specs" / "vertex-prd.md").write_text(
        "automation scope is automatic_after_deposit\n",
        encoding="utf-8",
    )
    (repo_root / "specs" / "vertex-ux-spec.md").write_text(
        "Accessibility\n",
        encoding="utf-8",
    )
    (repo_root / "governance" / "decisions").mkdir(parents=True, exist_ok=True)
    (repo_root / "governance" / "decisions" / "0007-activation-automation-honesty.md").write_text(
        "manual EML export\n"
        "automatic_after_deposit\n"
        "Graph API roadmap\n",
        encoding="utf-8",
    )
    (repo_root / "governance" / "privacy-matrix.md").write_text(
        "| source | classification | retention |\n"
        "| kusto | confidential | 1 year |\n",
        encoding="utf-8",
    )
    (repo_root / "governance" / "data-classification.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (repo_root / "governance" / "activation-operator-clarity-review.md").write_text(
        "EXPLAIN-min\n"
        "disputed\n"
        "Downgraded / legacy fallback banner\n"
        "Accessibility notes\n",
        encoding="utf-8",
    )
    (repo_root / "governance" / "activation-raw-data-feasibility.md").write_text(
        "29\n"
        "30\n"
        "Acquisition owner\n"
        "Acquisition path\n"
        "do not mark `P-1-RAW-DATA` green\n",
        encoding="utf-8",
    )
    (repo_root / "governance" / "activation-annotation-plan.md").write_text(
        "Second annotator: activation-secondary-labeler\n"
        "Adjudicator: activation-adjudicator\n"
        "20 dual-labeled\n"
        "κ >= 0.70\n"
        "g_xtract_prec_ci_low\n",
        encoding="utf-8",
    )
    (repo_root / "governance" / "decisions" / "0008-activation-pilot-degrade-exception.md").write_text(
        "Owner: activation-tpm\n"
        "Expires on: 2026-07-31\n"
        "proof_only: true\n"
        "blocks_authority_cycles: true\n",
        encoding="utf-8",
    )
    (repo_root / "governance" / "activation-vision-reconciliation-plan.md").write_text(
        "ado\n"
        "eml\n"
        "kusto\n"
        "icm\n"
        "Materiality policy\n"
        "as_of\n"
        "disputed\n"
        "operator adjudication queue\n"
        "source excerpt\n"
        "counter-source context\n"
        "source_document_key\n"
        "approval_event_id\n"
        "accept, edit, reject, revoke, or defer\n"
        "accessibility review\n",
        encoding="utf-8",
    )
    (repo_root / "governance" / "activation-release-checklist.md").write_text(
        "branch-local\n"
        "canonical\n"
        "dirty_worktree must be false\n"
        "--self-test\n"
        "P-1-RAW-DATA\n"
        "AG-1-COUNTERFACTUAL-DIFF\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "contracts" / "test_activation_operator_workflow_contract.py").write_text(
        "export EML\n"
        "gather REV\n"
        "triage edit approve\n"
        "report revoke re-report\n",
        encoding="utf-8",
    )
    (repo_root / "governance").mkdir(parents=True, exist_ok=True)
    (repo_root / "governance" / "runbook.md").write_text(
        "operator correction protocol\n"
        "corpus rollback\n"
        "Auto-demote\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "core" / "stages" / "milestone_stage.py").write_text(
        "VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK\n"
        "degraded to legacy milestone source via audited rollback flag\n",
        encoding="utf-8",
    )
    (repo_root / "templates" / "partials" / "milestone_rows.j2").write_text("{{ row.source_document_key }}\n", encoding="utf-8")
    (repo_root / ".archive" / "specs" / "consolidated.md").write_text("**Version:** 2.25\n", encoding="utf-8")
    (repo_root / "tests" / "test_placeholder.py").write_text("def test_placeholder(): pass\n", encoding="utf-8")
    (repo_root / "tests" / "contracts" / "test_rev_contracts.py").write_text(
        "pseudonym_table\n"
        "run_local_checks\n"
        "def test_only_one_record_written_on_duplicate(): pass\n"
        "is rejected\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "contracts" / "test_conflict_engine.py").write_text("fact.conflict\n", encoding="utf-8")
    (repo_root / "tests" / "contracts" / "test_s6_entity_binding_gate.py").write_text("evaluate_binding\n", encoding="utf-8")
    (repo_root / "tests" / "contracts" / "test_s5c_flip_gate.py").write_text(
        "def test_flip_occurs_after_threshold_clean_cycles(): pass\n"
        "def test_primary_family_rolls_back_on_divergence(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "contracts" / "test_s3c_lineage_reverse_lookup.py").write_text(
        "def test_reverse_lookup_source_document_key_present(): pass\n"
        "def test_access_denied_unavailability(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "contracts" / "test_rev_bridge_decoupling.py").write_text(
        "canonical_projection_dump\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "contracts" / "test_ncfl_apply_policy.py").write_text(
        "def test_apply_state_machine_matches_recoverable_spec(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "contracts" / "test_ncfl_store_policy.py").write_text(
        "knowledge_doc\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "contracts" / "test_fleet_isolation.py").write_text(
        "Fleet isolation\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "unit" / "test_reality_facade_extensions.py").write_text(
        "FleetReality\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "unit" / "test_ncfl_flow.py").write_text(
        "extract_proposals\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "unit" / "test_commands_ledger.py").write_text(
        "discovery.candidate_rejected.v1\n"
        'projection["proj_milestone"] == []\n'
        "deep_projection_match\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "unit" / "test_rev_pipeline_local_import.py").write_text(
        "extraction_degraded\n"
        "def test_second_cycle_does_not_reprocess_processed_file(): pass\n"
        "quarantine\n"
        "claimed_at_startup_count\n"
        "crash_loop\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "unit" / "test_activation_hardening_v1_16.py").write_text(
        "def test_accessor_rollout_plan_counts_shared_milestones_accessor_once(): pass\n"
        "def test_longitudinal_benefit_passes_when_review_time_shrinks(): pass\n"
        "def test_corpus_rollback_triggers_on_sustaining_quality_drop(): pass\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "unit" / "test_verify_activation.py").write_text(
        "def test_family_matrix_surfaces_shared_milestones_accessor(): pass\n",
        encoding="utf-8",
    )
    (quality_root / "rev_labeled_corpus.jsonl").write_text("", encoding="utf-8")
    (quality_root / "rev_quality_metrics.json").write_text("{}", encoding="utf-8")
    (rev_root / "last_cycle.json").write_text(json.dumps({"cycle_status": "complete"}), encoding="utf-8")
    (programs_root / "xpf" / "platform_proof_log.yaml").write_text(
        "schema_version: '1.0'\n"
        "proofs:\n"
        "- proof_id: s7a_rollback_drill\n"
        "  status: passed\n"
        "  recorded_at: '2026-06-30T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    (programs_root / "xpf" / "trusted_baseline.yaml").write_text(
        "schema_version: '1.0'\n"
        "history:\n"
        "- action: rollback_drill_passed\n"
        "  at: '2026-06-30T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    (programs_root / "xpf" / "program.yaml").write_text("id: xpf\n", encoding="utf-8")
    (programs_root / "xpf" / "workstream_registry.yaml").write_text(
        "schema_version: '1.0'\n"
        "workstreams:\n"
        "- id: xpf\n"
        "  name: XPF\n",
        encoding="utf-8",
    )
    (armada_root / "program.yaml").write_text("id: armada\n", encoding="utf-8")
    (armada_root / "workstream_registry.yaml").write_text(
        "schema_version: '1.0'\n"
        "workstreams:\n"
        "- id: armada\n"
        "  name: Armada\n",
        encoding="utf-8",
    )

    checks = _build_evidence_appendix_checks(program="xpf", programs_root=programs_root, repo_root=repo_root)
    by_id = {check.check_id: check for check in checks}

    assert by_id["P5-AUDITED-ROLLBACK-FLAG"].status == "pass"
    assert by_id["P5-MILESTONE-LINEAGE-RENDER"].status == "pass"
    assert by_id["P6-COUNTERFACTUAL-DIFF-ARTIFACT"].status == "pass"
    assert by_id["P2-CORPUS-FREEZE-MANIFEST"].status == "fail"
    assert by_id["PS-22-TRIAGE-REVOKE"].status == "pass"
    assert by_id["AG-9-CONFLICT-SCAFFOLD"].status == "pass"
    # v1.25 (AG-9): detector is now WIRED into the REV cycle finalize path
    # (was v1.24's honest FAIL; the wiring is real now).
    assert by_id["AG-9-CONFLICT-WIRED"].status == "pass"
    assert by_id["AG-9-CONFLICT-WIRED"].details["conflict_wired"] is True
    assert by_id["AG-11-PRIVACY-SCAFFOLD"].status == "pass"
    # v1.24 (AG-11 honesty): the scaffold now requires the projection gate to be
    # wired into the bridge chokepoint, not just the privacy utilities to exist.
    assert by_id["AG-11-PRIVACY-SCAFFOLD"].details["projection_gate_wired"] is True
    assert by_id["AG-12-DEGRADATION-SCAFFOLD"].status == "pass"
    assert by_id["AG-12-DEGRADATION-SCAFFOLD"].details["source_unreachable"] is True
    # §6.14.3 (v1.16): extraction_degraded must be a real persisted boolean field.
    assert by_id["AG-12-DEGRADATION-SCAFFOLD"].details["extraction_degraded_field"] is True
    assert by_id["AG-12-DEGRADATION-SCAFFOLD"].details["extraction_degraded_persisted"] is True
    assert by_id["AG-15-ENTITY-BINDING-SCAFFOLD"].status == "pass"
    # v1.24 (AG-15 honesty): the scaffold now requires candidate entity_resolution
    # to be wired into staging, not just the S-6 gate to exist.
    assert by_id["AG-15-ENTITY-BINDING-SCAFFOLD"].details["resolution_wired"] is True
    assert by_id["P2-QUALITY-METRICS-ARTIFACT"].status == "pass"
    assert by_id["P2-DENOMINATOR-PLAN"].status == "pass"
    assert by_id["P2-DENOMINATOR-PLAN"].details["plan"]
    # v1.16 hardening scaffolds.
    assert by_id["AG-17-OPERATOR-IDENTITY-SCAFFOLD"].status == "pass"
    assert by_id["AG-17-PROVENANCE-SCAFFOLD"].status == "pass"
    assert by_id["AG-17-INJECTION-FENCE-SCAFFOLD"].status == "pass"
    assert by_id["AG-13-TRIAGE-TELEMETRY-SCAFFOLD"].status == "pass"
    assert by_id["AG-14-SLO-SCAFFOLD"].status == "pass"
    assert by_id["AG-20-TIME-MOTION-ROI-SCAFFOLD"].status == "pass"
    assert by_id["AG-7-AUTOMATION-HONESTY-ADR"].status == "pass"
    assert by_id["O-21-EXPLAIN-MIN-SCAFFOLD"].status == "pass"
    assert by_id["AG-19-MULTI-ALTITUDE-SCAFFOLD"].status == "pass"
    assert by_id["P7-OPERATOR-WORKFLOW-E2E-CONTRACT"].status == "pass"
    assert by_id["AG-1B-ROBUSTNESS-SCAFFOLD"].status == "pass"
    assert by_id["AG-5-NCFL-APPLY-SCAFFOLD"].status == "pass"
    assert by_id["AG-6-REVERSE-LOOKUP-SCAFFOLD"].status == "pass"
    assert by_id["AG-16-LONGITUDINAL-BENEFIT-SCAFFOLD"].status == "pass"
    assert by_id["P15-CORPUS-ROLLBACK-SCAFFOLD"].status == "pass"
    assert by_id["AG-4-ACCESSOR-LADDER-SCAFFOLD"].status == "pass"
    assert by_id["AG-8-AG14-FLEET-SOAK-SCAFFOLD"].status == "pass"
    assert by_id["P1-DATA-RESIDENCY-SCAFFOLD"].status == "pass"
    assert by_id["P13-OPERATOR-CLARITY-SCAFFOLD"].status == "pass"
    assert by_id["P-1-RAW-DATA-FEASIBILITY-PLAN"].status == "pass"
    assert by_id["P2-ANNOTATION-STAFFING-PLAN"].status == "pass"
    assert by_id["RK-1-PILOT-DEGRADE-ADR"].status == "pass"
    assert by_id["P11-BASE-SCHEMA-CROSS-PROGRAM"].status == "pass"
    assert by_id["P15-KUSTO-ICM-RECONCILIATION-SCAFFOLD"].status == "pass"
    assert by_id["GAP-36-GAP-37-EXPLAIN-DRILLDOWN-SCAFFOLD"].status == "pass"
    assert by_id["P0-BINDING-RELEASE-CHECKLIST-SCAFFOLD"].status == "pass"
    assert by_id["O-21-LINEAGE-RICHNESS-SCAFFOLD"].status == "pass"
    assert by_id["O-16-ADO-SCHEMA-DRIFT-SCAFFOLD"].status == "pass"
    assert by_id["AG-10-BATCH-REJECT-SCAFFOLD"].status == "pass"
    assert by_id["P9-AUTHORITY-FLIP-SCAFFOLD"].status == "pass"
    assert by_id["AG-18-ROLLBACK-DRILL-EVIDENCE"].status == "pass"
    assert by_id["O-17-OPERATOR-RUNBOOK"].status == "pass"
