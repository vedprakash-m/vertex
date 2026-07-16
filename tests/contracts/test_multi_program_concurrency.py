"""ADF-W5.9: Multi-program concurrency contract tests.

Spec reference: specs/arch-data-fix.md Section 15.2 / 11.6a's ADF-W5.9 row --
the one gap the item's own status row explicitly left deferred after
checkpoint/restore, cockpit-history retention, and time-based retention for
all five raw artifact types were closed: "multi-program concurrency tests
(exercising XPF+Armada simultaneously against shared mechanisms like
workspace leases)."

``tests/contracts/test_fleet_isolation.py`` already proves per-program
isolation with *sequential* calls (program A writes, then program B writes,
then both are read back). That is necessary but not sufficient: a fleet
operator runs `vertex gather --edition xpf_weekly` and
`vertex gather --edition armada_weekly` from the same host (or a shared
network workspace) at genuinely overlapping times, so the real regression
risk is a bug that only manifests under actual concurrent execution --
e.g. a module-level global accidentally shared across programs, a lock
keyed on the wrong scope, or a lost write under real SQLite/file lock
contention. These tests use real ``threading``/``ThreadPoolExecutor``
workers (not mocks or sequential calls) hammering two distinct programs
(program ids matching the fleet's real ``xpf``/``armada`` editions)
simultaneously against every mechanism ADF-W5.9 touches: the workspace
lease, the fact-store ledger, checkpoint/restore, and the alert store.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from src.core.alerts import append_or_suppress_alert, read_alerts
from src.core.checkpoint_store import create_checkpoint_snapshot, list_checkpoints
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
from src.core.ledger.fact_bridge import append_bridged_milestone_event
from src.core.ledger.source_refs import EmailRef
from src.core.program_fact_store import ProgramFactStore, project_milestones
from src.core.workspace_lease import (
    LeaseHeldByAnotherOwner,
    acquire_lease,
    read_lease_state,
)

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_PROGRAM_A = "xpf"
_PROGRAM_B = "armada"


def test_concurrent_lease_acquisition_never_cross_blocks_between_programs(tmp_path: Path) -> None:
    """Real concurrent acquisition attempts against TWO different programs,
    interleaved on the wire (not run one program to completion before the
    other starts). Each program must still serialize to exactly one winner
    among its own contenders (same guarantee as the existing single-program
    test), AND no attempt on one program may ever be blocked by the other
    program's holder -- that would mean the lease key silently collapsed
    across programs (e.g. a forgotten ``program_id`` in the DB path)."""
    programs_root = tmp_path / "programs"
    winners: dict[str, list[str]] = {_PROGRAM_A: [], _PROGRAM_B: []}
    losers: dict[str, list[LeaseHeldByAnotherOwner]] = {_PROGRAM_A: [], _PROGRAM_B: []}
    lock = threading.Lock()
    contenders_per_program = 6
    barrier = threading.Barrier(contenders_per_program * 2)

    def _attempt(program_id: str, owner: str) -> None:
        barrier.wait(timeout=10)  # force genuinely overlapping start times
        try:
            acquire_lease(program_id, owner, ttl_seconds=300, programs_root=programs_root)
            with lock:
                winners[program_id].append(owner)
        except LeaseHeldByAnotherOwner as exc:
            with lock:
                losers[program_id].append(exc)

    threads = []
    for i in range(contenders_per_program):
        threads.append(threading.Thread(target=_attempt, args=(_PROGRAM_A, f"xpf-host-{i}")))
        threads.append(threading.Thread(target=_attempt, args=(_PROGRAM_B, f"armada-host-{i}")))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    # Intra-program contention still serializes correctly to one winner.
    assert len(winners[_PROGRAM_A]) == 1, f"expected 1 xpf winner, got {winners[_PROGRAM_A]}"
    assert len(winners[_PROGRAM_B]) == 1, f"expected 1 armada winner, got {winners[_PROGRAM_B]}"
    assert len(losers[_PROGRAM_A]) == contenders_per_program - 1
    assert len(losers[_PROGRAM_B]) == contenders_per_program - 1

    # Cross-program isolation: an xpf attempt is never blocked by an armada
    # holder (or vice versa) -- proves the lease is genuinely keyed per program.
    for exc in losers[_PROGRAM_A]:
        assert exc.holder.startswith("xpf-host-"), f"xpf lease blocked by non-xpf holder: {exc.holder!r}"
    for exc in losers[_PROGRAM_B]:
        assert exc.holder.startswith("armada-host-"), f"armada lease blocked by non-armada holder: {exc.holder!r}"

    state_a = read_lease_state(_PROGRAM_A, programs_root=programs_root)
    state_b = read_lease_state(_PROGRAM_B, programs_root=programs_root)
    assert state_a is not None and state_a.owner in winners[_PROGRAM_A]
    assert state_b is not None and state_b.owner in winners[_PROGRAM_B]
    assert state_a.owner != state_b.owner


def _milestone_event(program_id: str, event_id: str, milestone_id: str) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        program_id=program_id,
        event_type="milestone.completed.v1",
        occurred_at=_NOW,
        recorded_at=_NOW,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="rev-mail",
        payload={"milestone_id": milestone_id, "completed_on": "2026-05-30"},
        source_ref=EmailRef(
            subject="Milestone complete",
            sent_at=_NOW,
            sender="pm@example.com",
            message_id=f"{event_id}@example.com",
            vault_hash=f"sha256:vault-{event_id}",
        ),
        prev_event_hash="sha256:prev",
        content_hash=f"sha256:content-{event_id}",
    )


def test_concurrent_fact_store_writes_stress_across_two_programs(tmp_path: Path) -> None:
    """``test_fleet_isolation.py::test_bridge_appender_isolates_facts_between_two_concurrent_programs``
    proves isolation with two SEQUENTIAL calls. This stress-tests the same
    appender under real overlapping thread execution against a SHARED
    ``db_root`` (mirroring one fact-store backend serving the whole fleet):
    many threads writing distinct facts to program A interleaved with many
    threads writing distinct facts to program B, verifying (a) no write is
    lost under real lock contention and (b) no fact ever crosses programs."""
    db_root = tmp_path / "vertex-db"
    writes_per_program = 15
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _write(program_id: str, i: int) -> None:
        try:
            append_bridged_milestone_event(
                _milestone_event(program_id, f"evt-{program_id}-{i}", f"milestone:{program_id}-{i}"),
                db_root=db_root,
            )
        except BaseException as exc:  # noqa: BLE001 -- captured for assertion below, not swallowed
            with lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for i in range(writes_per_program):
            futures.append(pool.submit(_write, _PROGRAM_A, i))
            futures.append(pool.submit(_write, _PROGRAM_B, i))
        for future in futures:
            future.result(timeout=30)

    assert errors == [], f"concurrent fact-store writes raised: {errors}"

    snapshot_a = ProgramFactStore(_PROGRAM_A, db_root=db_root).snapshot()
    snapshot_b = ProgramFactStore(_PROGRAM_B, db_root=db_root).snapshot()

    assert len(snapshot_a.facts) == writes_per_program, (
        f"expected {writes_per_program} facts for {_PROGRAM_A}, got {len(snapshot_a.facts)} "
        "-- a write was lost under concurrency"
    )
    assert len(snapshot_b.facts) == writes_per_program, (
        f"expected {writes_per_program} facts for {_PROGRAM_B}, got {len(snapshot_b.facts)} "
        "-- a write was lost under concurrency"
    )
    assert all(fact.program_id == _PROGRAM_A for fact in snapshot_a.facts), (
        "fleet isolation broken under concurrency: xpf snapshot contains a non-xpf fact"
    )
    assert all(fact.program_id == _PROGRAM_B for fact in snapshot_b.facts), (
        "fleet isolation broken under concurrency: armada snapshot contains a non-armada fact"
    )

    ids_a = {m.id for m in project_milestones(snapshot_a)}
    ids_b = {m.id for m in project_milestones(snapshot_b)}
    assert len(ids_a) == writes_per_program
    assert len(ids_b) == writes_per_program
    assert ids_a.isdisjoint(ids_b), f"milestone IDs leaked across programs: {ids_a & ids_b}"


def test_concurrent_checkpoint_creation_across_two_programs_does_not_cross_contaminate(tmp_path: Path) -> None:
    """ADF-W5.9's own checkpoint/restore extension (Section 15.2) must stay
    correct when two programs are checkpointed at genuinely overlapping
    times, not just when run one after another. Seeds each program with a
    distinguishable marker in ``risk_register.yaml`` and asserts every
    checkpoint created under concurrent pressure carries only its own
    program's content."""
    programs_root = tmp_path / "programs"
    markers = {_PROGRAM_A: "xpf-marker", _PROGRAM_B: "armada-marker"}
    for program_id, marker in markers.items():
        program_dir = programs_root / program_id
        program_dir.mkdir(parents=True, exist_ok=True)
        (program_dir / "risk_register.yaml").write_text(f"program: {marker}\nrisks: []\n", encoding="utf-8")

    errors: list[BaseException] = []
    lock = threading.Lock()
    checkpoints_per_program = 5

    def _checkpoint(program_id: str, issue_number: int) -> None:
        try:
            create_checkpoint_snapshot(program_id, issue_number, programs_root=programs_root)
        except BaseException as exc:  # noqa: BLE001 -- captured for assertion below, not swallowed
            with lock:
                errors.append(exc)

    threads = []
    for i in range(checkpoints_per_program):
        # Distinct issue_number per thread within a program avoids a same-second
        # timestamp collision reusing one directory -- not the cross-program
        # concern under test, but keeps the assertion below exact rather than "at least".
        threads.append(threading.Thread(target=_checkpoint, args=(_PROGRAM_A, i + 1)))
        threads.append(threading.Thread(target=_checkpoint, args=(_PROGRAM_B, i + 1)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == [], f"concurrent checkpoint creation raised: {errors}"

    checkpoints_a = list_checkpoints(_PROGRAM_A, programs_root=programs_root)
    checkpoints_b = list_checkpoints(_PROGRAM_B, programs_root=programs_root)
    assert len(checkpoints_a) == checkpoints_per_program
    assert len(checkpoints_b) == checkpoints_per_program

    for checkpoint_dir in checkpoints_a:
        assert str(checkpoint_dir).startswith(str(programs_root / _PROGRAM_A)), (
            f"xpf checkpoint escaped its program directory: {checkpoint_dir}"
        )
        content = (checkpoint_dir / "risk_register.yaml").read_text(encoding="utf-8")
        assert markers[_PROGRAM_A] in content
        assert markers[_PROGRAM_B] not in content, "armada content leaked into an xpf checkpoint"

    for checkpoint_dir in checkpoints_b:
        assert str(checkpoint_dir).startswith(str(programs_root / _PROGRAM_B)), (
            f"armada checkpoint escaped its program directory: {checkpoint_dir}"
        )
        content = (checkpoint_dir / "risk_register.yaml").read_text(encoding="utf-8")
        assert markers[_PROGRAM_B] in content
        assert markers[_PROGRAM_A] not in content, "xpf content leaked into an armada checkpoint"


def test_concurrent_alert_emission_across_two_programs_stays_isolated(tmp_path: Path) -> None:
    """Hammers ``append_or_suppress_alert`` (Section 8.2.5, ADF-W5.8's own
    mechanism, exercised by ADF-W5.9's checkpoint/retention alerting too)
    for two programs simultaneously using the SAME category/entity_type/
    entity_id string on both -- the identity tuple is
    ``(program_id, category, entity_type, entity_id)``, so this deliberately
    stresses whether ``program_id`` is genuinely part of every alert's
    identity/storage path under real concurrent write pressure, not just
    when read back sequentially.

    Real production usage serializes writes WITHIN one program: Section
    8.2.5/WS-17's design is explicit that "the surfactant is the operator's
    CLI session, not a daemon" -- one session appends to one program's
    ``alerts.jsonl`` at a time. The genuine multi-program concurrency risk
    this item is about is two programs' single-writer sessions overlapping
    in wall-clock time (e.g. `gather --edition xpf_weekly` and
    `gather --edition armada_weekly` running at once), not many unrelated
    threads racing to append to the SAME program's file with zero ordering
    -- a scenario WS-17 never claims to support and that portalocker's
    Windows ``msvcrt`` backend has a bounded-retry limit under (observed
    directly: 10 unserialized threads hammering one file intermittently hit
    a raw ``PermissionError`` past that limit, unrelated to cross-program
    isolation). One single-worker executor per program serializes each
    program's own emissions in submission order while both executors run
    genuinely concurrently against each other."""
    programs_root = tmp_path / "programs"
    emissions_per_program = 10

    def _emit(program_id: str, i: int) -> None:
        append_or_suppress_alert(
            program_id=program_id,
            category="lineage_regression",
            entity_type="cockpit_snapshot",
            entity_id="shared-entity-id",  # deliberately identical across both programs
            severity="warn",
            message=f"regression observed from {program_id} run {i}",
            next_command="vertex doctor",
            programs_root=programs_root,
            cooldown_minutes=0,
        )

    with (
        ThreadPoolExecutor(max_workers=1) as pool_a,
        ThreadPoolExecutor(max_workers=1) as pool_b,
    ):
        futures = []
        for i in range(emissions_per_program):
            futures.append(pool_a.submit(_emit, _PROGRAM_A, i))
            futures.append(pool_b.submit(_emit, _PROGRAM_B, i))
        for future in futures:
            future.result(timeout=30)  # propagates any real write failure

    alerts_a = read_alerts(_PROGRAM_A, programs_root=programs_root, include_resolved=True)
    alerts_b = read_alerts(_PROGRAM_B, programs_root=programs_root, include_resolved=True)

    # All emissions share one alert_id per program (same category/entity_type/
    # entity_id), so read_alerts collapses to exactly one current-state record
    # per program; occurrence_count must reflect all emissions_per_program
    # calls since each program's own writes are serialized (no TOCTOU race).
    assert len(alerts_a) == 1
    assert len(alerts_b) == 1
    assert alerts_a[0].occurrence_count == emissions_per_program
    assert alerts_b[0].occurrence_count == emissions_per_program
    assert all(alert.program_id == _PROGRAM_A for alert in alerts_a), (
        "fleet isolation broken under concurrency: xpf alert store contains a non-xpf record"
    )
    assert all(alert.program_id == _PROGRAM_B for alert in alerts_b), (
        "fleet isolation broken under concurrency: armada alert store contains a non-armada record"
    )
    assert all(f"from {_PROGRAM_A} run" in alert.message for alert in alerts_a)
    assert all(f"from {_PROGRAM_B} run" in alert.message for alert in alerts_b)
