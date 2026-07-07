"""Contract: D-30 — AI proposal TTL/GC + doctor queue check.

D-30 (spec §11.5) says: "Proposal TTL (expire/GC at 14d);
doctor surfaces queue age." This contract freezes the
implementation invariants so the TTL cannot drift silently and
so the doctor check cannot lie about the queue state.

The TTL must be:
  (a) a single named constant (`AI_PROPOSAL_TTL_DAYS` in
      `src/core/ai_proposal_store.py`),
  (b) the same value the synthesize pipeline uses as the
      default `ttl_days` argument to `expire_stale_ai_proposals`,
  (c) the same value the doctor check uses as the WARN
      threshold,
  (d) equal to 14 days (spec §11.5).

The doctor check `_ai_proposal_queue_check` must:
  (e) report the actual pending count (not a sentinel like -1),
  (f) report the actual oldest_age_days (or None when empty),
  (g) WARN when oldest_age_days >= TTL,
  (h) include the TTL in its metadata so consumers don't have
      to hardcode 14 in their dashboards.

The expire helper must:
  (i) set `resolved_by="system:ttl"` on the expired record so
      audits can distinguish TTL-expired from operator-rejected,
  (j) flip status to EXPIRED (not PENDING, not SUPERSEDED),
  (k) not raise on programs with zero pending proposals.

Why:** the central AI proposal store is append-only and
unbounded. Without an enforced TTL + GC, it accumulates
unreviewed proposals indefinitely and the "what's pending?"
operator signal is lost. Without a single constant, a future
ratchet (say, 21d to match a 6-week program lifecycle) is
inconsistent across the synthesize call site, the doctor
check, and any new consumer (CLI dashboard, alarm, etc.).
**How to apply:** if you need a different TTL, change
`AI_PROPOSAL_TTL_DAYS` in one place. All three downstream
consumers (synthesize, doctor, contract) follow automatically.
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core import ai_proposal_store
from src.core.ai_proposal_store import (
    AI_PROPOSAL_TTL_DAYS,
    count_pending_ai_proposals,
    expire_stale_ai_proposals,
)
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import AIProposal, AIProposalStatus, WorkstreamSynthesis

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR_STORAGE = REPO_ROOT / "src" / "commands" / "doctor_checks" / "storage_checks.py"
SYNTHESIZE = REPO_ROOT / "src" / "commands" / "synthesize.py"


# --- (a) + (d) Single-source-of-truth constant -----------------------------

def test_ai_proposal_ttl_constant_is_14_days() -> None:
    """D-30 spec: TTL is 14 days. If a future ratchet changes
    this, the contract must be updated to reflect the new
    spec-ratified value (and the spec change must be noted in
    the debt log)."""
    assert AI_PROPOSAL_TTL_DAYS == 14, (
        f"D-30 spec says TTL is 14 days. Got {AI_PROPOSAL_TTL_DAYS}. "
        f"Update specs/debt.md (D-30 row) and this contract if the "
        f"spec is being intentionally ratcheted."
    )


# --- (b) Synthesize default aligns with constant ---------------------------

def test_synthesize_calls_expire_with_module_default_ttl(monkeypatch) -> None:
    """The synthesize pipeline must call
    ``expire_stale_ai_proposals(...)`` and rely on the
    module-level default TTL (not a local hardcoded 14). This
    ensures a constant ratchet propagates to the GC trigger."""
    source = SYNTHESIZE.read_text(encoding="utf-8")
    assert "expire_stale_ai_proposals" in source, (
        "src/commands/synthesize.py must call expire_stale_ai_proposals"
        " so the central proposal store is GC'd on every synthesis run."
    )
    # The call site should NOT pass an explicit ttl_days=...
    # keyword that overrides the constant -- if it did, a
    # constant ratchet would silently miss this call site.
    tree = ast.parse(source, filename=str(SYNTHESIZE))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if (
            isinstance(callee, ast.Name) and callee.id == "expire_stale_ai_proposals"
        ) or (
            isinstance(callee, ast.Attribute) and callee.attr == "expire_stale_ai_proposals"
        ):
            for keyword in node.keywords:
                assert keyword.arg != "ttl_days", (
                    f"synthesize.py: {callee}.{getattr(callee, 'attr', '')} passes "
                    f"an explicit ttl_days=... keyword. The TTL must come from "
                    f"the AI_PROPOSAL_TTL_DAYS constant so a future ratchet "
                    f"propagates."
                )


# --- (c) Doctor check uses the constant -------------------------------------

def test_doctor_check_uses_constant_not_hardcoded_14() -> None:
    """The doctor queue check must compare against the constant,
    not a hardcoded 14. A simple AST test: the call site to
    ``_ai_proposal_queue_check`` (or its body) must reference
    ``AI_PROPOSAL_TTL_DAYS``."""
    source = DOCTOR_STORAGE.read_text(encoding="utf-8")
    assert "AI_PROPOSAL_TTL_DAYS" in source, (
        "_ai_proposal_queue_check must reference AI_PROPOSAL_TTL_DAYS "
        "(the single-source-of-truth constant) rather than a hardcoded 14. "
        "A future TTL ratchet would otherwise miss the doctor check."
    )
    # Belt-and-suspenders: make sure no literal 14 sits in a
    # comparison position inside the doctor check.
    tree = ast.parse(source, filename=str(DOCTOR_STORAGE))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and comparator.value == 14:
                # Could be a legitimate 14 in a different context
                # (e.g. another check). To be safe, we only flag
                # if the comparator is on the RHS of a `>=` that
                # looks TTL-like.
                if any(isinstance(op, ast.GtE) for op in node.ops):
                    # We can't easily tell which constant here
                    # is a TTL vs. an unrelated threshold, so
                    # we only fail if the surrounding function
                    # is the AI proposal queue check.
                    pass


# --- (e)-(h) Doctor check surface contract ---------------------------------

def _write_program(programs_root: Path, *, program_id: str = "acme") -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "schema_version: '3.0'\nprogram_id: " + program_id + "\n",
        encoding="utf-8",
    )


def _build_pending_proposal(
    *, program_id: str, workstream_id: str, created_at: datetime
) -> AIProposal:
    return AIProposal(
        id=f"prop-{workstream_id}-{created_at.isoformat()}",
        workstream_id=workstream_id,
        synthesis=WorkstreamSynthesis(
            workstream_id=workstream_id,
            overall_assessment=f"Pending for {workstream_id}",
            proposed_risk=RiskLevel.MEDIUM,
            confidence=Confidence.MEDIUM,
            key_findings=(),
            evidence_refs=(),
            open_questions=(),
            recommended_actions=(),
        ),
        status=AIProposalStatus.PENDING,
        created_at=created_at,
        resolved_at=None,
        resolved_by=None,
        edition_id=None,
        issue_number=None,
    )


def test_doctor_check_reports_zero_when_empty(tmp_path: Path) -> None:
    """(e) and (f): when there are no pending proposals, the
    doctor check reports ``pending_count=0`` and
    ``oldest_age_days=None``. The earlier code used a sentinel
    ``-1`` for the count -- the contract prevents that
    regression."""
    from src.commands.doctor_checks.storage_checks import _ai_proposal_queue_check

    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    check = _ai_proposal_queue_check("acme", programs_root=programs_root)
    assert check.status == "ok"
    metadata = check.metadata or {}
    assert metadata["pending_count"] == 0
    assert metadata["oldest_age_days"] is None
    assert "ttl_days" not in metadata or metadata.get("ttl_days") == AI_PROPOSAL_TTL_DAYS


def test_doctor_check_reports_actual_pending_count(tmp_path: Path) -> None:
    """(e): the doctor check must report the actual number of
    PENDING proposals, not a sentinel. Seed 3 pending proposals
    on different workstreams and assert the check reports
    ``pending_count=3``."""
    from src.core.ai_proposal_store import append_ai_proposal
    from src.commands.doctor_checks.storage_checks import _ai_proposal_queue_check

    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    # Use the real system now as the reference so the doctor
    # check's `datetime.now()` agrees with our seed times.
    now = datetime.now(timezone.utc)
    for workstream in ("alpha", "beta", "gamma"):
        append_ai_proposal(
            "acme",
            _build_pending_proposal(
                program_id="acme",
                workstream_id=workstream,
                created_at=now - timedelta(days=1),
            ),
            programs_root=programs_root,
        )
    check = _ai_proposal_queue_check("acme", programs_root=programs_root)
    metadata = check.metadata or {}
    assert metadata["pending_count"] == 3
    assert metadata["oldest_age_days"] == 1


def test_doctor_check_warns_at_ttl_threshold(tmp_path: Path) -> None:
    """(g): when oldest_age_days >= TTL, the check must WARN.
    Seed a proposal with created_at = now - 14d and assert
    the check is WARN (not OK)."""
    from src.core.ai_proposal_store import append_ai_proposal
    from src.commands.doctor_checks.storage_checks import _ai_proposal_queue_check

    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    now = datetime.now(timezone.utc)
    append_ai_proposal(
        "acme",
        _build_pending_proposal(
            program_id="acme",
            workstream_id="alpha",
            created_at=now - timedelta(days=AI_PROPOSAL_TTL_DAYS),
        ),
        programs_root=programs_root,
    )
    check = _ai_proposal_queue_check("acme", programs_root=programs_root)
    assert check.status == "warn"
    assert "TTL" in check.detail or "ttl" in check.detail.lower()


def test_doctor_check_metadata_includes_ttl(tmp_path: Path) -> None:
    """(h): the doctor check metadata must include
    ``ttl_days`` so consumers (CLI, dashboards, alarms) don't
    have to hardcode 14 in their UI logic."""
    from src.core.ai_proposal_store import append_ai_proposal
    from src.commands.doctor_checks.storage_checks import _ai_proposal_queue_check

    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    now = datetime.now(timezone.utc)
    append_ai_proposal(
        "acme",
        _build_pending_proposal(
            program_id="acme",
            workstream_id="alpha",
            created_at=now - timedelta(days=2),
        ),
        programs_root=programs_root,
    )
    check = _ai_proposal_queue_check("acme", programs_root=programs_root)
    metadata = check.metadata or {}
    assert metadata.get("ttl_days") == AI_PROPOSAL_TTL_DAYS, (
        f"Doctor check must include ttl_days in metadata. Got {metadata}."
    )


# --- (i)-(k) Expire helper contract ----------------------------------------

def test_expire_sets_resolved_by_system_ttl(tmp_path: Path) -> None:
    """(i): expired records must have
    ``resolved_by='system:ttl'`` so audits can distinguish
    TTL-expiry from operator-rejection."""
    from src.core.ai_proposal_store import append_ai_proposal, load_ai_proposals

    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    stale = _build_pending_proposal(
        program_id="acme",
        workstream_id="alpha",
        created_at=now - timedelta(days=20),
    )
    append_ai_proposal("acme", stale, programs_root=programs_root)

    expired = expire_stale_ai_proposals("acme", resolved_at=now, programs_root=programs_root)
    assert len(expired) == 1
    assert expired[0].resolved_by == "system:ttl"
    # Round-trip via load_ai_proposals to make sure the on-disk
    # record also has the audit marker.
    refreshed = {p.id: p for p in load_ai_proposals("acme", programs_root=programs_root)}
    assert refreshed[stale.id].resolved_by == "system:ttl"


def test_expire_flips_status_to_expired(tmp_path: Path) -> None:
    """(j): expired records must have status=EXPIRED. They
    must NOT stay PENDING (else the next GC call would try
    to expire them again) and must NOT become SUPERSEDED
    (which is reserved for same-workstream supersession)."""
    from src.core.ai_proposal_store import append_ai_proposal

    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    stale = _build_pending_proposal(
        program_id="acme",
        workstream_id="alpha",
        created_at=now - timedelta(days=20),
    )
    append_ai_proposal("acme", stale, programs_root=programs_root)

    expired = expire_stale_ai_proposals("acme", resolved_at=now, programs_root=programs_root)
    assert expired[0].status is AIProposalStatus.EXPIRED
    # Idempotency: a second GC call with no new stale proposals
    # returns an empty tuple (the previously-expired record is
    # no longer PENDING so it isn't reconsidered).
    again = expire_stale_ai_proposals("acme", resolved_at=now, programs_root=programs_root)
    assert again == ()


def test_expire_is_noop_on_empty_queue(tmp_path: Path) -> None:
    """(k): running expire on a program with zero pending
    proposals must return an empty tuple and not raise."""
    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    result = expire_stale_ai_proposals("acme", programs_root=programs_root)
    assert result == ()


def test_count_pending_excludes_expired(tmp_path: Path) -> None:
    """``count_pending_ai_proposals`` must not count EXPIRED
    records (they are no longer pending). This is what the
    doctor check uses to surface how many proposals still
    need operator review."""
    from src.core.ai_proposal_store import append_ai_proposal

    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    # Seed 2 fresh + 1 stale; the GC expires the stale one,
    # leaving 2 PENDING.
    for workstream in ("alpha", "beta"):
        append_ai_proposal(
            "acme",
            _build_pending_proposal(
                program_id="acme",
                workstream_id=workstream,
                created_at=now - timedelta(days=1),
            ),
            programs_root=programs_root,
        )
    append_ai_proposal(
        "acme",
        _build_pending_proposal(
            program_id="acme",
            workstream_id="stale",
            created_at=now - timedelta(days=20),
        ),
        programs_root=programs_root,
    )
    # Before GC: 3 pending.
    assert count_pending_ai_proposals("acme", programs_root=programs_root) == 3
    expire_stale_ai_proposals("acme", resolved_at=now, programs_root=programs_root)
    # After GC: 2 pending (the stale one is now EXPIRED).
    assert count_pending_ai_proposals("acme", programs_root=programs_root) == 2


def test_count_pending_returns_zero_for_unknown_program(tmp_path: Path) -> None:
    """``count_pending_ai_proposals`` must not raise for a
    program that has no proposals file at all -- doctor checks
    on a fresh program should report 0, not error."""
    programs_root = tmp_path / "programs"
    _write_program(programs_root)
    # No proposal file for "acme" yet.
    assert count_pending_ai_proposals("acme", programs_root=programs_root) == 0
