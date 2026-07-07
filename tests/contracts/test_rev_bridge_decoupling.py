"""W2-12: REV-bridge decoupling contract tests.

These tests enforce that the REV extraction pipeline (src/{core,ai,m365}/rev/)
never writes facts directly.  The only permitted write path is:

  ledger.py approves candidate
    → writes event to event-log  (persist step)
    → calls _maybe_bridge_event_to_fact_store  (bridge step, decoupled)
    → bridge calls ProgramFactStore.append_fact

Any direct write (write_confirmed, ProgramFactStore.append_fact, etc.) from a
REV module would create a second write path that bypasses audit, approval, and
the shadow/review-state gate.

This is a DISTINCT invariant from INV-2 (which guards snapshot_store.write_confirmed
against confirm.py being bypassed).  Do NOT merge these into INV-2.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

_REV_ROOTS = (
    REPO_ROOT / "src" / "core" / "rev",
    REPO_ROOT / "src" / "ai" / "rev",
    REPO_ROOT / "src" / "m365" / "rev",
)

# Symbols that must never be imported by REV modules
_FORBIDDEN_IMPORT_PATTERNS = (
    re.compile(r"from\s+src\.core\.snapshot_store\s+import\s+[^\n]*\bwrite_confirmed\b"),
    re.compile(r"import\s+write_confirmed\b"),
    re.compile(r"from\s+src\.core\.program_fact_store\s+import\s+[^\n]*\bProgramFactStore\b"),
    re.compile(r"from\s+src\.core\.program_fact_store\s+import\s+[^\n]*\bappend_fact\b"),
)

# AST-level method calls that must not appear in REV modules
_FORBIDDEN_METHOD_CALLS = frozenset({"append_fact", "write_confirmed", "write_confirmed_issue"})


def _rev_python_files() -> list[Path]:
    files: list[Path] = []
    for root in _REV_ROOTS:
        if root.exists():
            files.extend(root.rglob("*.py"))
    return files


def test_rev_modules_do_not_import_write_confirmed() -> None:
    """W2-12: No REV module may import write_confirmed from snapshot_store.

    The snapshot confirm path is exclusively for editorial publishing and must
    not be reachable from the extraction/triage pipeline — importing it would
    create an unsupervised write surface that bypasses the archive lock.
    """
    violations: list[str] = []
    write_confirmed_pattern = re.compile(
        r"from\s+src\.core\.snapshot_store\s+import\s+[^\n]*\bwrite_confirmed\b"
    )
    for py_file in _rev_python_files():
        text = py_file.read_text(encoding="utf-8")
        if write_confirmed_pattern.search(text):
            violations.append(str(py_file.relative_to(REPO_ROOT)))
    assert violations == [], (
        "W2-12: REV modules must not import write_confirmed — "
        "found in: " + ", ".join(violations)
    )


def test_rev_modules_do_not_import_program_fact_store_direct() -> None:
    """W2-12: No REV module may import ProgramFactStore or append_fact directly.

    Facts must be written through the bridge (event-log → _maybe_bridge_event_to_fact_store
    → append_bridged_*), not by directly calling ProgramFactStore from the extraction
    or pipeline layer.  Direct access would bypass the review-state gate and the
    per-event audit trail.
    """
    violations: list[str] = []
    direct_store_pattern = re.compile(
        r"from\s+src\.core\.program_fact_store\s+import\s+[^\n]*\b(ProgramFactStore|append_fact)\b"
    )
    for py_file in _rev_python_files():
        text = py_file.read_text(encoding="utf-8")
        if direct_store_pattern.search(text):
            violations.append(str(py_file.relative_to(REPO_ROOT)))
    assert violations == [], (
        "W2-12: REV modules must not import ProgramFactStore or append_fact directly — "
        "found in: " + ", ".join(violations)
    )


def test_rev_modules_do_not_call_forbidden_write_methods_via_ast() -> None:
    """W2-12: AST-level guard: REV modules must not call append_fact/write_confirmed*.

    Regex import bans can be bypassed with aliasing or lazy imports.  This AST scan
    catches call-site violations regardless of how the symbol arrived.
    """
    violations: list[str] = []
    for py_file in _rev_python_files():
        text = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _FORBIDDEN_METHOD_CALLS:
                violations.append(
                    f"{py_file.relative_to(REPO_ROOT)}:{node.lineno} calls {name!r}"
                )
    assert violations == [], (
        "W2-12: REV modules must not call write/append fact methods directly — "
        "found: " + "; ".join(violations)
    )


def test_event_type_registry_is_internally_consistent() -> None:
    """W2-8: Unified event-type registry is self-consistent.

    Every PROJECTABLE entry must name a bridge appender; every
    KNOWN_UNPROJECTEABLE and PASSTHROUGH entry must not.  Authority families
    for PROJECTABLE entries must be non-empty.  No duplicate prefixes.
    """
    from src.core.ledger.event_type_registry import (
        LEDGER_EVENT_REGISTRY,
        EventDisposition,
        lookup_event_spec,
    )
    _VALID_AUTHORITY_FAMILIES = frozenset({
        "workitem.state", "metric", "incident", "judgment", "commitment", "narrative",
    })
    seen_prefixes: set[str] = set()
    errors: list[str] = []

    for spec in LEDGER_EVENT_REGISTRY:
        # No duplicate prefixes
        if spec.prefix in seen_prefixes:
            errors.append(f"duplicate prefix {spec.prefix!r}")
        seen_prefixes.add(spec.prefix)

        if spec.disposition == EventDisposition.PROJECTABLE:
            if not spec.bridge_appender_name:
                errors.append(
                    f"{spec.prefix}: PROJECTABLE entry must have a bridge_appender_name"
                )
            if spec.authority_family not in _VALID_AUTHORITY_FAMILIES:
                errors.append(
                    f"{spec.prefix}: PROJECTABLE entry has invalid authority_family "
                    f"{spec.authority_family!r} (must be one of {sorted(_VALID_AUTHORITY_FAMILIES)})"
                )
        else:
            if spec.bridge_appender_name is not None:
                errors.append(
                    f"{spec.prefix}: non-PROJECTABLE entry must not have bridge_appender_name"
                )

    assert errors == [], "W2-8 registry consistency errors:\n" + "\n".join(errors)

    # Spot-check: lookup works for well-known event types
    assert lookup_event_spec("risk.raised.v1") is not None
    assert lookup_event_spec("deliverable.status_changed.v1") is not None
    assert lookup_event_spec("discovery.candidate_approved.v1") is not None
    assert lookup_event_spec("unicorn.event.that.does.not.exist") is None


def test_bridge_is_called_after_event_persist_in_ledger() -> None:
    """W2-12: In ledger.py the bridge call must come AFTER event persistence.

    The write order is: write_event/write_events_atomic → _maybe_bridge_event_to_fact_store.
    Reversing this order would bridge a fact for an event that hasn't been durably persisted,
    making the fact unreplayable (the event could be lost on crash).

    This test verifies the structural ordering via AST line-number inspection.
    """
    ledger_file = REPO_ROOT / "src" / "commands" / "ledger.py"
    text = ledger_file.read_text(encoding="utf-8")
    tree = ast.parse(text)

    persist_lines: list[int] = []
    bridge_lines: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name in {"write_event", "write_events_atomic", "_persist_event"}:
            persist_lines.append(node.lineno)
        elif name == "_maybe_bridge_event_to_fact_store":
            bridge_lines.append(node.lineno)

    assert persist_lines, "ledger.py must call write_event/write_events_atomic/_persist_event"
    assert bridge_lines, "ledger.py must call _maybe_bridge_event_to_fact_store"

    for bridge_line in bridge_lines:
        earlier_persist = [p for p in persist_lines if p < bridge_line]
        assert earlier_persist, (
            f"W2-12: _maybe_bridge_event_to_fact_store at line {bridge_line} "
            f"has no earlier persist call — bridge must come AFTER event persistence. "
            f"persist_lines={persist_lines}"
        )


# ---------------------------------------------------------------------------
# W2-3: Durable projector — projector_version + bridge lineage contracts
# ---------------------------------------------------------------------------

def test_projector_version_constant_exists_and_is_nonempty() -> None:
    """W2-3: _PROJECTOR_VERSION must be defined and non-empty in program_views.py."""
    from src.core.ledger.program_views import _PROJECTOR_VERSION
    assert isinstance(_PROJECTOR_VERSION, str) and _PROJECTOR_VERSION, (
        "W2-3: _PROJECTOR_VERSION must be a non-empty string in program_views.py"
    )


def test_projection_meta_includes_projector_version(tmp_path: "Path") -> None:
    """W2-3: project_events_to_sqlite must write projector_version into projection_meta."""
    from src.core.ledger.program_views import project_events_to_sqlite, _PROJECTOR_VERSION
    from src.core.ledger.program_views import canonical_projection_dump
    db_path = tmp_path / "test.db"
    project_events_to_sqlite("acme", [], projection_path=db_path, programs_root=tmp_path)
    meta = canonical_projection_dump(db_path)["projection_meta"]
    assert meta, "projection_meta must not be empty after project_events_to_sqlite"
    assert meta[0].get("projector_version") == _PROJECTOR_VERSION, (
        "W2-3: projector_version in projection_meta must match _PROJECTOR_VERSION; "
        f"got {meta[0].get('projector_version')!r}, expected {_PROJECTOR_VERSION!r}"
    )


def test_incremental_rebuild_on_projector_version_mismatch(tmp_path: "Path") -> None:
    """W2-3: project_events_incremental_to_sqlite must force full rebuild on version mismatch.

    When the DB's stored projector_version differs from the current _PROJECTOR_VERSION,
    the incremental path must call the full projector rather than returning stale data.
    The version check must occur before the delta-events shortcut so that even a
    zero-delta call rebuilds when the projector version has changed.
    """
    import sqlite3
    from datetime import datetime, timezone
    from src.core.ledger.program_views import (
        project_events_to_sqlite,
        project_events_incremental_to_sqlite,
        _PROJECTOR_VERSION,
        canonical_projection_dump,
    )
    from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
    from src.core.ledger.source_refs import OperatorAssertionRef

    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    # Use decision.made.v1 — simple payload, no complex fold dependencies.
    event = EventEnvelope(
        event_id="evt-pv-test-001",
        program_id="acme",
        event_type="decision.made.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={
            "decision_id": "dec:d1",
            "title": "Use Python",
            "decision_text": "We will use Python.",
            "decided_by": ["operator"],
        },
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    db_path = tmp_path / "prog.db"
    # Build initial projection with one event so watermark is non-empty.
    project_events_to_sqlite("acme", [event], projection_path=db_path, programs_root=tmp_path)
    meta_initial = canonical_projection_dump(db_path)["projection_meta"]
    assert meta_initial[0]["event_watermark"] == event.event_id, "test setup: watermark must be set"

    # Corrupt: write a stale projector_version directly into the DB.
    stale_version = f"old-{_PROJECTOR_VERSION}"
    _corrupt_conn = sqlite3.connect(str(db_path))
    try:
        _corrupt_conn.execute("UPDATE projection_meta SET projector_version = ?", (stale_version,))
        _corrupt_conn.commit()
    finally:
        _corrupt_conn.close()
    meta_before = canonical_projection_dump(db_path)["projection_meta"]
    assert meta_before[0]["projector_version"] == stale_version

    # Run incremental with the same event (no new delta after watermark).
    # Version mismatch must trigger full rebuild even though delta is empty.
    project_events_incremental_to_sqlite(
        "acme", [event], projection_path=db_path, programs_root=tmp_path
    )
    meta_after = canonical_projection_dump(db_path)["projection_meta"]
    assert meta_after[0]["projector_version"] == _PROJECTOR_VERSION, (
        "W2-3: after incremental call with stale projector_version, DB must be rebuilt "
        f"to current version {_PROJECTOR_VERSION!r}; "
        f"got {meta_after[0]['projector_version']!r}"
    )


def test_bridged_fact_revision_stores_domain_event_id(tmp_path: "Path") -> None:
    """W2-6 / G-lineage: appending a bridged fact must persist domain_event_id on the revision.

    A fact revision with a populated domain_event_id closes the E2E lineage chain:
    EML → candidate → ledger_event (event_id) → fact_revision.domain_event_id → ProgramReality.
    """
    from datetime import datetime, timezone
    from src.core.ledger.fact_bridge import append_bridged_risk_event
    from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
    from src.core.ledger.source_refs import OperatorAssertionRef
    from src.core.program_fact_store import ProgramFactStore

    now = datetime.now(timezone.utc)
    envelope = EventEnvelope(
        event_id="evt-lineage-domain-001",
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={
            "risk_id": "risk:r1",
            "title": "Lineage test risk",
            "severity": "high",
            "probability": "medium",
            "status": "open",
        },
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )
    append_bridged_risk_event(envelope, db_root=tmp_path)
    store = ProgramFactStore("acme", db_root=tmp_path)
    snapshot = store.snapshot()
    risk_facts = [f for f in snapshot.facts if f.fact_type == "risk.entry"]
    assert risk_facts, "W2-6: at least one risk.entry fact must be written by append_bridged_risk_event"
    revision = risk_facts[0]
    assert revision.domain_event_id == envelope.event_id, (
        f"W2-6 / G-lineage: revision.domain_event_id must equal the source ledger event_id; "
        f"got {revision.domain_event_id!r}, expected {envelope.event_id!r}"
    )


def test_bridge_append_idempotent_via_domain_event_id(tmp_path: "Path") -> None:
    """W2-6 / at-least-once: duplicate bridge delivery of same event must be a noop.

    If the bridge appender is called twice with the same event (replay scenario),
    no duplicate fact revision must be created.  The second call must return action='noop'.
    """
    from datetime import datetime, timezone
    from src.core.ledger.fact_bridge import append_bridged_risk_event
    from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
    from src.core.ledger.source_refs import OperatorAssertionRef
    from src.core.program_fact_store import ProgramFactStore

    now = datetime.now(timezone.utc)
    envelope = EventEnvelope(
        event_id="evt-idempotency-001",
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={
            "risk_id": "risk:r2",
            "title": "Idempotency test risk",
            "severity": "high",
            "probability": "medium",
            "status": "open",
        },
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
        prev_event_hash="sha256:prev2",
        content_hash="sha256:content2",
    )
    result1 = append_bridged_risk_event(envelope, db_root=tmp_path)
    result2 = append_bridged_risk_event(envelope, db_root=tmp_path)

    assert result1.action == "created", f"W2-6: first append must create; got {result1.action!r}"
    assert result2.action == "noop", (
        f"W2-6 / at-least-once: second append of same event_id must be noop (idempotency gate); "
        f"got {result2.action!r}"
    )
    store = ProgramFactStore("acme", db_root=tmp_path)
    snapshot = store.snapshot()
    risk_r2_revisions = [
        f for f in snapshot.facts
        if f.fact_type == "risk.entry" and f.domain_event_id == envelope.event_id
    ]
    assert len(risk_r2_revisions) == 1, (
        f"W2-6: exactly 1 revision must exist for risk:r2 after 2 append calls; "
        f"got {len(risk_r2_revisions)}"
    )


def test_bridge_fact_input_includes_event_id_as_source_signal(tmp_path: "Path") -> None:
    """W2-3 / G-lineage: build_bridge_fact_input must include event.event_id in source_signal_ids.

    Without this, a bridged fact revision has no traceability back to the ledger
    event that created it — the lineage chain is broken.
    """
    from datetime import datetime, timezone
    from src.core.ledger.fact_bridge import build_bridge_fact_input
    from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
    from src.core.ledger.source_refs import OperatorAssertionRef

    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    envelope = EventEnvelope(
        event_id="evt-lineage-test-001",
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"risk_id": "risk:r1", "title": "Risk one"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )
    fact_input = build_bridge_fact_input(
        envelope,
        fact_type="risk.entry",
        entity_refs=("RISK:r1",),
        payload={"id": "r1", "title": "Risk one"},
    )
    assert envelope.event_id in fact_input.source_signal_ids, (
        f"W2-3 / G-lineage: event_id {envelope.event_id!r} must appear in "
        f"fact_input.source_signal_ids; got {fact_input.source_signal_ids!r}"
    )


def test_triage_approve_writes_operator_confirmed_event() -> None:
    """W2-1: triage approve (non-edit path) must upgrade event confidence to OPERATOR_CONFIRMED.

    Without this, the bridge stores a PROPOSED fact (AI_EXTRACTED confidence) and the
    operator's approval is not reflected in the review_state of the bridged fact.  The
    edit path already used OPERATOR_CONFIRMED; the plain approve path must match.
    """
    import ast
    import textwrap

    ledger_path = REPO_ROOT / "src" / "commands" / "ledger.py"
    source = ledger_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find all _write_candidate_event / _write_candidate_event_with_lock_override calls
    # that are NOT in _write_candidate_audit_event and check that none use the
    # default confidence (which would inherit ai_extracted from the candidate).
    class ApproveCallVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.bad_calls: list[int] = []  # line numbers without explicit confidence

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            func_name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else ""
            )
            if func_name not in {"_write_candidate_event", "_write_candidate_event_with_lock_override"}:
                self.generic_visit(node)
                return
            # Check if keyword 'confidence' is present
            has_confidence = any(kw.arg == "confidence" for kw in node.keywords)
            if not has_confidence:
                self.bad_calls.append(node.lineno)
            self.generic_visit(node)

    visitor = ApproveCallVisitor()
    visitor.visit(tree)

    # The only call WITHOUT explicit confidence should be inside _write_candidate_event itself
    # (the implementation body), not call-sites in triage approve/batch-approve.
    # We allow at most 1 (the function definition body calls itself recursively in
    # _write_candidate_event_with_lock_override).
    assert len(visitor.bad_calls) <= 1, (
        f"W2-1: found {len(visitor.bad_calls)} _write_candidate_event call(s) without explicit "
        f"confidence= at line(s) {visitor.bad_calls}. Triage approve must pass "
        f"confidence=ConfidenceTier.OPERATOR_CONFIRMED so the bridge stores ACCEPTED facts."
    )


def test_selective_family_replay_bridges_only_requested_family(tmp_path: "Path") -> None:
    """W2-10: _replay_bridge_for_families must only re-bridge events for selected families.

    Writes risk + milestone events to the ledger, replays only 'risk', and
    asserts that milestone facts are NOT written (family isolation).
    Also asserts that the risk fact IS written (idempotency via domain_event_id).
    """
    from datetime import datetime, timezone
    from src.core.ledger.event_log import (
        ConfidenceTier, EventEnvelope, TemporalConfidence, write_events_atomic,
    )
    from src.core.ledger.source_refs import OperatorAssertionRef
    from src.core.program_fact_store import ProgramFactStore
    from src.commands.ledger import _replay_bridge_for_families

    now = datetime.now(timezone.utc)
    prog = "replay-test"

    def _env(event_id: str, event_type: str, payload: dict) -> EventEnvelope:
        return EventEnvelope(
            event_id=event_id,
            program_id=prog,
            event_type=event_type,
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload=payload,
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
            prev_event_hash="sha256:0",
            content_hash="sha256:0",
        )

    risk_env = _env(
        "evt-replay-risk-001", "risk.raised.v1",
        {"risk_id": "risk:r1", "title": "Risk one", "severity": "high",
         "probability": "medium", "status": "open"},
    )
    milestone_env = _env(
        "evt-replay-ms-001", "milestone.completed.v1",
        {"milestone_id": "ms:m1", "completed_on": "2026-06-25"},
    )

    # Write both events to the ledger (programs_root layout)
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / prog
    prog_dir.mkdir(parents=True)
    write_events_atomic((risk_env, milestone_env), programs_root=programs_root)

    # Replay only the 'risk' family — enable bridge via env var
    import os
    old_env = os.environ.get("VERTEX_LEDGER_FACT_BRIDGE")
    try:
        os.environ["VERTEX_LEDGER_FACT_BRIDGE"] = "1"
        counts = _replay_bridge_for_families(
            prog,
            selected_families=frozenset({"risk"}),
            as_of=None,
            knowledge_as_of=None,
            programs_root=programs_root,
        )
    finally:
        if old_env is None:
            os.environ.pop("VERTEX_LEDGER_FACT_BRIDGE", None)
        else:
            os.environ["VERTEX_LEDGER_FACT_BRIDGE"] = old_env

    assert counts.get("risk", 0) == 1, f"W2-10: 1 risk event should be replayed; got {counts}"
    assert counts.get("milestone", 0) == 0, f"W2-10: milestone not selected, must not be replayed"

    # _resolve_bridge_db_root(programs_root) returns programs_root.parent when
    # programs_root.name == "programs" — so db_root == tmp_path.
    db_root = programs_root.parent
    store = ProgramFactStore(prog, db_root=db_root)
    snapshot = store.snapshot()
    fact_types = {f.fact_type for f in snapshot.facts}

    assert "risk.entry" in fact_types, "W2-10: risk.entry fact must be present after selective replay"
    assert "milestone.entry" not in fact_types, (
        "W2-10: milestone.entry must NOT be present — milestone family was not selected"
    )
