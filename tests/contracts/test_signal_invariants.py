from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore


REPO_ROOT = Path(__file__).resolve().parents[2]

# INV-SG-10 is enforced per migrated module wave, not as a global flip.
# Add modules here as each wave moves them to load_program_facts().
MIGRATED_FACT_READ_MODULES = frozenset[str](
    {
        "src/commands/actions.py",
        "src/commands/assumptions.py",
        "src/commands/bootstrap.py",
        "src/commands/deck_companion.py",
        "src/commands/dependencies.py",
        "src/commands/decisions.py",
        "src/commands/doctor.py",
        "src/commands/escalate.py",
        "src/commands/fleet.py",
        "src/commands/freshness.py",
        "src/commands/gather.py",
        "src/commands/hypothesis.py",
        "src/commands/meeting_close.py",
        "src/commands/milestones.py",
        "src/commands/onboard.py",
        "src/commands/owner_pack.py",
        "src/commands/prep.py",
        "src/commands/readiness.py",
        "src/commands/report_ai.py",
        "src/commands/report_deck.py",
        "src/commands/report_health.py",
        "src/commands/report_lookback.py",
        "src/commands/report_scorecards.py",
        "src/commands/review_debrief.py",
        "src/commands/review_sections.py",
        "src/commands/review_full.py",
        "src/commands/risks.py",
        "src/commands/status.py",
        "src/commands/synthesize.py",
        "src/commands/summarize.py",
        "src/commands/triage.py",
        "src/commands/confirm.py",
        "src/core/config_loader_v2.py",
        "src/core/edition_resolver.py",
        "src/core/quality_gates/__init__.py",
        "src/core/raid_graph.py",
        "src/core/readiness_engine.py",
        "src/core/stages/action_stage.py",
        "src/core/stages/milestone_stage.py",
        "src/core/stages/render_stage.py",
        "src/core/stages/risk_stage.py",
        "src/core/program_context.py",
    }
)


def test_inv_sg9_program_fact_schema_is_program_scoped_not_issue_scoped(tmp_path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.initialize()

    with sqlite3.connect(store.db_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(program_fact_revisions)").fetchall()
        }

    assert "program_id" in columns
    assert "fact_id" in columns
    assert "issue_number" not in columns
    assert "edition_id" not in columns


def test_inv_sg10_migrated_modules_read_current_state_via_fact_store_only() -> None:
    forbidden_patterns = (
        re.compile(r"\bload_decisions\s*\("),
        re.compile(r"\bload_actions\s*\("),
        re.compile(r"\bload_dependencies\s*\("),
        re.compile(r"\bload_milestones\s*\("),
        re.compile(r"\bload_risk_register\s*\("),
        re.compile(r"\bload_assumptions\s*\("),
        re.compile(r"\bread_action_items\s*\("),
        re.compile(r"\b_parse_workstreams\s*\("),
    )

    # A module satisfies the fact-store read contract if it routes current-state
    # reads through the unified API directly (``load_program_facts(``) OR through
    # the thin ``load_current_*`` convenience wrappers in program_fact_store.py
    # (which are themselves ``project_* ∘ load_program_facts``). The wrappers keep
    # god-module call sites to one line (FR-SG-51) without weakening the contract.
    # WI-1.2: ``ProgramReality.load(`` is the next-generation sanctioned I/O point;
    # modules migrated to it also satisfy this contract.
    fact_read_markers = ("load_program_facts(", "load_current_", "ProgramReality.load(")

    violations: list[str] = []
    for relative_path in MIGRATED_FACT_READ_MODULES:
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        if not any(marker in source for marker in fact_read_markers):
            violations.append(f"{relative_path}: missing fact-store read (load_program_facts/load_current_*)")
            continue
        for pattern in forbidden_patterns:
            if pattern.search(source):
                violations.append(f"{relative_path}: forbidden direct current-state read via {pattern.pattern}")

    assert violations == []


def test_inv_sg11_lower_precedence_write_creates_proposed_revision(tmp_path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(
        ProgramFactInput(
            fact_type="risk",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"risk": "high"},
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
        ),
    )
    result = store.append_fact(
        ProgramFactInput(
            fact_type="risk",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"risk": "low"},
            precedence=FactPrecedence.RAW_TELEMETRY,
        ),
    )

    assert result.action == "proposed_revision"
    assert len(store.snapshot().facts) == 1
    assert len(store.list_proposed_revisions()) == 1


def test_inv_sg12_snapshot_pin_detects_material_drift(tmp_path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "launch gate closed"},
        ),
    )
    pin = store.pin_snapshot(metadata={"issue_number": 78})
    store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "launch gate approved"},
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        ),
    )

    drift = store.detect_drift(pin.snapshot_id)

    assert len(drift) == 1
    assert drift[0].payload["decision"] == "launch gate approved"


def test_inv_sg13_facts_are_append_only_and_support_as_of_queries(tmp_path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    first = store.append_fact(
        ProgramFactInput(
            fact_type="action",
            scope="workitem:1",
            entity_refs=("WI:1",),
            payload={"status": "open"},
        ),
    )
    second = store.append_fact(
        ProgramFactInput(
            fact_type="action",
            scope="workitem:1",
            entity_refs=("WI:1",),
            payload={"status": "closed"},
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        ),
    )

    with sqlite3.connect(store.db_path) as connection:
        revision_count = connection.execute(
            "SELECT COUNT(*) FROM program_fact_revisions WHERE fact_id = ?",
            (first.revision.fact_id,),
        ).fetchone()[0]
        stored_superseded_at = connection.execute(
            "SELECT superseded_at FROM program_fact_revisions WHERE revision_id = ?",
            (first.revision.revision_id,),
        ).fetchone()[0]

    before = store.snapshot(as_of=first.revision.recorded_at)
    after = store.snapshot(as_of=second.revision.recorded_at)

    assert revision_count == 2
    assert stored_superseded_at is not None
    assert before.facts[0].payload["status"] == "open"
    assert after.facts[0].payload["status"] == "closed"


def test_inv_sg1_fact_promotion_requires_entity_ref_binding(tmp_path) -> None:
    """INV-SG-1: a fact promoted with no entity ref is rejected; a program-level
    fact binds to an explicit ``PROGRAM:`` sentinel ref rather than an empty list."""
    store = ProgramFactStore("acme", db_root=tmp_path)
    with pytest.raises(ValueError):
        store.append_fact(
            ProgramFactInput(
                fact_type="risk",
                scope="program",
                entity_refs=(),
                payload={"risk": "high"},
            ),
        )
    result = store.append_fact(
        ProgramFactInput(
            fact_type="risk",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"risk": "high"},
        ),
    )
    assert result.action == "created"


def test_inv_sg3_program_fact_layer_stays_in_zone_a() -> None:
    """INV-SG-3: the Program Fact layer / SignalClass heuristics live in Zone A
    (``src/core``) with no Zone A -> ``src.ai`` / ``src.m365`` imports."""
    source = (REPO_ROOT / "src/core/program_fact_store.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("src.ai") or node.module.startswith("src.m365"):
                forbidden.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src.ai") or alias.name.startswith("src.m365"):
                    forbidden.append(alias.name)
    assert forbidden == []


# ---------------------------------------------------------------------------
# Forward-declared invariants (INV-SG-2/4/5/6/7/8).
#
# Per specs/signals.md §9, the enforcement for these invariants ships with its
# owning feature in a later, gated phase (P0/P1/P2/P5/P6).  The contract tests
# below are placeholders that name the owning FR/phase so the suite enumerates
# all 13 invariants and each test activates when its feature lands.  A coding
# agent MUST NOT implement the gated enforcement here (the spec's
# "[CODING AGENT STOP after (c)]" gate) before the P-1 HUMAN GATE items clear.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="INV-SG-2 enforced by the publish-time claim-coverage gate "
    "(FR-SG-12 / QG-SG-18); lands in P2/P5 — not yet implemented"
)
def test_inv_sg2_published_claim_requires_provenance() -> None:
    pass


@pytest.mark.skip(
    reason="INV-SG-4 enforced by the SourceContract source-health gate "
    "(FR-SG-06 / QG-SG-01); lands in P0 — not yet implemented"
)
def test_inv_sg4_required_source_failure_blocks_confirm() -> None:
    pass


@pytest.mark.skip(
    reason="INV-SG-5 enforced by evidence-governed risk/action promotion "
    "(FR-SG-42 / QG-SG-07); lands in P2/P7 — not yet implemented"
)
def test_inv_sg5_promoted_risk_requires_evidence_or_unsourced_flag() -> None:
    pass


@pytest.mark.skip(
    reason="INV-SG-6 enforced by the override->derivation feedback loop "
    "(FR-SG-37); lands in P6 — not yet implemented"
)
def test_inv_sg6_recurring_override_surfaces_optimization_proposal() -> None:
    pass


@pytest.mark.skip(
    reason="INV-SG-7 enforced by the M365 ingestion classification firewall "
    "(FR-SG-45); lands in P0/P1 — not yet implemented"
)
def test_inv_sg7_m365_ingestion_outside_allowlist_is_blocked() -> None:
    pass


@pytest.mark.skip(
    reason="INV-SG-8 enforced by learning-loop OptimizationProposal gating "
    "(FR-SG-37/38/39 / QG-SG-14); lands in P6 — not yet implemented"
)
def test_inv_sg8_learned_threshold_change_requires_accepted_proposal() -> None:
    pass
