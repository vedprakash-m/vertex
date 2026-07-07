"""End-to-end autonomous source discovery (specs/discover.md §16.1).

These tests exercise the full discovery runtime as a single composed flow,
rather than the per-stage slices covered elsewhere:

1. discovery -> PM accept -> ChannelRegistration written -> hydrate -> signal yield
2. manual seed-id  -> ChannelRegistration written -> hydrate -> signal yield

The email channel is used because its hydrate -> extract path is fully
deterministic (no transcript scoring), which keeps the end-to-end assertions
focused on the discovery control plane rather than provider heuristics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands import integration
from src.commands import integration_discovery
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.discovery_intent import (
    SourceCandidate,
    SourceCandidateStatus,
    SourceIntentStatus,
    SourceRefKind,
    build_source_candidate_id,
)
from src.core.email_signal_extractor import EmailSignalExtractor
from src.core.integration_types import ChannelConfig, RunContext
from src.core.models_v2 import (
    EmailThreadSource,
    Program,
    Workstream,
    WorkstreamSignalSources,
)
from src.core.source_candidate_store import SourceCandidateStore, candidate_evidence_json
from src.m365.email_hydration import EmailHydrationConfig, EmailHydrationProvider
from src.m365.graph_mail_client import MailRecord, MailSearchPage


runner = CliRunner()

_AS_OF = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_THREAD_ID = "thread-launch-123"


class _FakeMailClient:
    def __init__(self, page: MailSearchPage) -> None:
        self.page = page
        self.queries: list[str] = []

    def search_emails(
        self,
        *,
        query: str,
        limit: int = 25,
        cursor: str | None = None,
        timeout_seconds: int | None = None,
    ) -> MailSearchPage:
        del limit, cursor, timeout_seconds
        self.queries.append(query)
        return self.page


def _seed_email_workstream(programs_root: Path) -> SourceCandidateStore:
    (programs_root / "demo" / "runtime").mkdir(parents=True)
    store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.ws",
                name="Launch",
                signal_sources=WorkstreamSignalSources(
                    email_threads=(
                        # No durable thread_id yet -> the intent must be resolved
                        # via discovery/seed before it can bind to a mailbox thread.
                        EmailThreadSource(display_name="Launch Thread", thread_id="", work_item_ids=(42,)),
                    ),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=_AS_OF,
    )
    return store


def _patch_integration_program(monkeypatch) -> None:
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        integration,
        "_load_program",
        lambda program_id, root: Program(schema_version="3.0", id="demo", name="Demo"),
    )
    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_config",
        lambda program, channel, programs_root: ChannelConfig(
            channel=channel,
            enabled=True,
            discovery_threshold_hours=24,
            ttl_days=30,
            extra={"instance_id": "default"},
        ),
    )


def _hydrate_and_extract(registration):
    """Drive the registration through hydrate -> extract and return signals."""
    client = _FakeMailClient(
        MailSearchPage(
            records=(
                MailRecord(
                    source_id="mail-1",
                    subject="Launch Thread",
                    sender="owner@example.com",
                    recipients=("team@example.com",),
                    received_at="2026-06-02T10:00:00Z",
                    web_url="https://outlook.office.com/mail/mail-1",
                    preview="Decision: proceed once WI:1234 closes.",
                    thread_id=_THREAD_ID,
                ),
            ),
            next_cursor=None,
            source="workiq",
        )
    )
    hydration = EmailHydrationProvider(client).hydrate(
        (registration,),
        datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        "demo",
        EmailHydrationConfig(),
        run_ctx=RunContext(),
    )
    assert hydration.hydrated_ref_ids == ((_THREAD_ID, "email_thread"),)
    return EmailSignalExtractor().extract(resources=hydration.resources, program_id="demo").signals


def test_autonomous_discovery_accept_then_hydrate_yields_signal(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    store = _seed_email_workstream(programs_root)
    intent = store.list_intents(workstream_id="demo.ws")[0]

    # Simulate a discovery pass that persisted a pending, unaccepted candidate.
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="email",
            provider_instance_id="default",
            ref_kind=SourceRefKind.EMAIL_THREAD,
            ref_id=_THREAD_ID,
        ),
        program_id="demo",
        channel="email",
        provider_instance_id="default",
        ref_id=_THREAD_ID,
        ref_kind=SourceRefKind.EMAIL_THREAD,
        display_name="Launch Thread",
        confidence=0.91,
        source_provider="graph_mail",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Launch Thread"]}),
        first_discovered_at=_AS_OF,
        last_seen_at=_AS_OF,
    )
    store.upsert_candidate(candidate, pii_prescrubbed=True)
    store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.91)
    _patch_integration_program(monkeypatch)

    result = runner.invoke(
        app,
        [
            "integration",
            "candidate-accept",
            candidate.candidate_id,
            "--program",
            "demo",
            "--intent-id",
            intent.intent_id,
            "--pm-alias",
            "pm@test",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert store.get_candidate(candidate.candidate_id).status == SourceCandidateStatus.ACCEPTED
    assert store.get_intent(intent.intent_id).status == SourceIntentStatus.RESOLVED

    registrations = ChannelRegistryStore(
        programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo"
    ).active_registrations("email")
    assert {reg.ref_id for reg in registrations} == {_THREAD_ID}

    signals = _hydrate_and_extract(registrations[0])
    assert len(signals) == 1
    assert signals[0].source == "workiq/email"
    assert signals[0].thread_id == _THREAD_ID
    assert "WI:1234" in signals[0].entity_refs


def test_autonomous_discovery_seed_id_then_hydrate_yields_signal(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    store = _seed_email_workstream(programs_root)
    intent = store.list_intents(workstream_id="demo.ws")[0]
    _patch_integration_program(monkeypatch)

    result = runner.invoke(
        app,
        [
            "integration",
            "seed-id",
            "--program",
            "demo",
            "--intent-id",
            intent.intent_id,
            "--ref-id",
            _THREAD_ID,
            "--pm-alias",
            "pm@test",
            "--reason",
            "validated from mailbox",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert store.get_intent(intent.intent_id).status == SourceIntentStatus.RESOLVED
    candidate = store.get_candidate_by_ref(ref_id=_THREAD_ID, ref_kind=SourceRefKind.EMAIL_THREAD)
    assert candidate is not None
    assert candidate.status == SourceCandidateStatus.ACCEPTED

    registrations = ChannelRegistryStore(
        programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo"
    ).active_registrations("email")
    assert {reg.ref_id for reg in registrations} == {_THREAD_ID}

    signals = _hydrate_and_extract(registrations[0])
    assert len(signals) == 1
    assert signals[0].source == "workiq/email"
    assert signals[0].thread_id == _THREAD_ID
    assert "WI:1234" in signals[0].entity_refs
