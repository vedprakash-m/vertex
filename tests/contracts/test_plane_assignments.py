from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_MODULES: tuple[tuple[str, str, Path], ...] = (
    ("Evidence", "Zone A", REPO_ROOT / "src" / "core" / "journal.py"),
    ("Evidence", "Zone A", REPO_ROOT / "src" / "core" / "trajectory.py"),
    ("Evidence", "Zone A", REPO_ROOT / "src" / "core" / "signal_dedup.py"),
    ("Evidence", "Zone A", REPO_ROOT / "src" / "core" / "ado_client.py"),
    ("Evidence", "Zone A", REPO_ROOT / "src" / "core" / "kusto_client.py"),
    ("Evidence", "Zone C", REPO_ROOT / "src" / "m365" / "agency_bridge.py"),
    ("Evidence", "Zone C", REPO_ROOT / "src" / "m365" / "enricher.py"),
    ("Evidence", "Zone C", REPO_ROOT / "src" / "m365" / "teams_reader.py"),
    ("Evidence", "Zone C", REPO_ROOT / "src" / "m365" / "transcript_reader.py"),
    ("Evidence", "Orchestrator", REPO_ROOT / "src" / "commands" / "gather.py"),
    ("Evidence", "Orchestrator", REPO_ROOT / "src" / "commands" / "signals.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "models.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "models_v2.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "milestone_engine.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "risk_register_engine.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "action_tracker.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "dependency_graph.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "delta_engine.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "scorecard_engine.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "freshness_engine.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "forecast_engine.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "vitality_scorer.py"),
    ("Program Model", "Zone A", REPO_ROOT / "src" / "core" / "trajectory_analyzer.py"),
    ("Program Model", "Orchestrator", REPO_ROOT / "src" / "commands" / "triage.py"),
    ("Program Model", "Orchestrator", REPO_ROOT / "src" / "commands" / "override.py"),
    ("Program Model", "Orchestrator", REPO_ROOT / "src" / "commands" / "milestones.py"),
    ("Program Model", "Orchestrator", REPO_ROOT / "src" / "commands" / "risks.py"),
    ("Program Model", "Orchestrator", REPO_ROOT / "src" / "commands" / "actions.py"),
    ("Automation", "Zone A", REPO_ROOT / "src" / "core" / "ado_proposal.py"),
    ("Automation", "Zone A", REPO_ROOT / "src" / "core" / "action_extractor_basic.py"),
    ("Automation", "Zone B", REPO_ROOT / "src" / "ai" / "blurb_generator.py"),
    ("Automation", "Zone B", REPO_ROOT / "src" / "ai" / "exec_summary_drafter.py"),
    ("Automation", "Zone B", REPO_ROOT / "src" / "ai" / "anticipation_engine.py"),
    ("Automation", "Zone B", REPO_ROOT / "src" / "ai" / "action_extractor.py"),
    ("Automation", "Zone B", REPO_ROOT / "src" / "ai" / "synthesizer.py"),
    ("Automation", "Zone C", REPO_ROOT / "src" / "m365" / "ado_writer.py"),
    ("Automation", "Orchestrator", REPO_ROOT / "src" / "commands" / "report.py"),
    ("Automation", "Orchestrator", REPO_ROOT / "src" / "commands" / "confirm.py"),
    ("Automation", "Orchestrator", REPO_ROOT / "src" / "commands" / "escalate.py"),
    ("Automation", "Orchestrator", REPO_ROOT / "src" / "commands" / "nudge.py"),
    ("Collaboration", "Zone A", REPO_ROOT / "src" / "core" / "html_renderer.py"),
    ("Collaboration", "Zone A", REPO_ROOT / "src" / "core" / "deck_renderer.py"),
    ("Collaboration", "Zone A", REPO_ROOT / "src" / "core" / "teams_renderer.py"),
    ("Collaboration", "Zone A", REPO_ROOT / "src" / "core" / "reviewer_renderer.py"),
    ("Collaboration", "Zone A", REPO_ROOT / "src" / "core" / "eml_writer.py"),
    ("Collaboration", "Zone C", REPO_ROOT / "src" / "m365" / "adaptive_card_renderer.py"),
    ("Collaboration", "Zone C", REPO_ROOT / "src" / "m365" / "graph_send_client.py"),
    ("Collaboration", "Orchestrator", REPO_ROOT / "src" / "commands" / "review_sections.py"),
    ("Collaboration", "Orchestrator", REPO_ROOT / "src" / "commands" / "review_full.py"),
    ("Collaboration", "Orchestrator", REPO_ROOT / "src" / "commands" / "notify.py"),
    ("Collaboration", "Orchestrator", REPO_ROOT / "src" / "commands" / "fleet.py"),
    ("Governance", "Zone A", REPO_ROOT / "src" / "core" / "quality_gates" / "__init__.py"),
    ("Governance", "Zone A", REPO_ROOT / "src" / "core" / "ban_list_validator.py"),
    ("Governance", "Zone A", REPO_ROOT / "src" / "core" / "verbosity_enforcer.py"),
    ("Governance", "Zone A", REPO_ROOT / "src" / "core" / "lineage.py"),
    ("Governance", "Zone A", REPO_ROOT / "src" / "core" / "observability.py"),
    ("Governance", "Zone B", REPO_ROOT / "src" / "ai" / "_pipeline.py"),
    ("Governance", "Orchestrator", REPO_ROOT / "src" / "commands" / "doctor.py"),
    ("Governance", "Orchestrator", REPO_ROOT / "src" / "commands" / "audit.py"),
    ("Governance", "Orchestrator", REPO_ROOT / "src" / "commands" / "publish_gate.py"),
)

REQUIRED_DIRECTORIES: tuple[tuple[str, str, Path], ...] = (
    ("Governance", "Zone B", REPO_ROOT / "src" / "ai" / "safety"),
)


def test_plane_assignment_modules_exist_in_expected_zones() -> None:
    missing: list[str] = []
    for plane, zone, path in REQUIRED_MODULES:
        if not path.is_file():
            missing.append(f"{plane} {zone}: missing module {path.relative_to(REPO_ROOT)}")
    assert missing == []


def test_plane_assignment_directories_exist_in_expected_zones() -> None:
    missing: list[str] = []
    for plane, zone, path in REQUIRED_DIRECTORIES:
        if not path.is_dir():
            missing.append(f"{plane} {zone}: missing directory {path.relative_to(REPO_ROOT)}")
            continue
        if not any(path.rglob("*.py")):
            missing.append(f"{plane} {zone}: directory {path.relative_to(REPO_ROOT)} has no Python modules")
    assert missing == []