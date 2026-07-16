from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.ai.blurb_generator import BlurbGenerationError, _derive_ai_confidence, generate_workstream_blurb
from src.core.config_loader import EditionVerbosityLimits, EditorialRules, VerbositySettings, VoiceContractSettings
from src.core.evidence_models import EtaRecord, SourceRef, WorkstreamEvidence
from src.core.models import AttributionTier, Comment, Confidence, DeltaKind, EditionType, EvidencePacket, ItemDelta, RiskLevel, WorkItem
from src.core.models_v2 import Signal, WorkstreamEvidenceBundle


class _FakeAIClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.last_system: str | None = None
        self.last_user: str | None = None
        self.last_prompt_version: str | None = None
        self.calls = 0

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del max_tokens
        self.calls += 1
        self.last_system = system
        self.last_user = user
        self.last_prompt_version = prompt_version
        return parser({"text": self.response_text})


class _MalformedPayloadAIClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del system, user, max_tokens, prompt_version
        return parser(self.payload)


def _editorial_rules() -> EditorialRules:
    return EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=(),
        banned_openings=(),
        verbosity=VerbositySettings(
            workstream_blurb_max_sentences=3,
            workstream_blurb_max_words=60,
            exec_bullet_max_words=None,
            exec_max_bullets=None,
            scorecard_summary_max_sentences=None,
        ),
        voice_contract=VoiceContractSettings(
            applies_to_editions=("acme_weekly",),
            program_tokens=("acme", "northwind", "adventure"),
            abstract_phrases=("materially narrower",),
            synthetic_delta_prefixes=("NEW", "CLOSED", "RISK_UP", "RISK_DOWN", "ETA", "OWNER"),
            decision_lead_terms=("blocking", "checkpoint", "conditional", "eta", "gate", "target"),
            static_concrete_terms=("azure core", "schie", "onedeploy", "northwind", "acme"),
            exec_summary_bucket_prefixes=("acme:",),
            objective_preamble_prefixes=("the objective of the acme program is",),
        ),
    )


def _item(work_item_id: int, title: str, *, comment_text: str | None = None) -> WorkItem:
    comments = []
    if comment_text is not None:
        comments.append(
            Comment(
                work_item_id=work_item_id,
                comment_id=1,
                created_by="Operator",
                created_by_email="operator@example.com",
                created_date=datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc),
                text=comment_text,
            )
        )
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state="Active",
        assigned_to="Operator",
        assigned_to_email="operator@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="FY26\\Sprint 20",
        target_date=date(2026, 6, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
        comments=comments,
    )


def _evidence(work_item_id: int, confidence: Confidence) -> EvidencePacket:
    return EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=confidence,
        tier=AttributionTier.TIER1,
        summary_for_reviewer=f"Evidence summary for #{work_item_id}.",
    )


def _delta(work_item_id: int, kind: DeltaKind) -> ItemDelta:
    return ItemDelta(
        work_item_id=work_item_id,
        kind=kind,
        field_changes={},
        old_risk=None,
        new_risk=None,
        old_eta=None,
        new_eta=None,
        evidence=_evidence(work_item_id, Confidence.HIGH),
    )


def test_generate_workstream_blurb_uses_only_confident_items_and_grounds_output(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    items = (
        _item(101, "Cache warmup safeguard"),
        _item(202, "Ignore this low-confidence item"),
    )
    evidence_by_item = {
        101: _evidence(101, Confidence.HIGH),
        202: _evidence(202, Confidence.NONE),
    }

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=items,
        evidence_by_item=evidence_by_item,
        deltas=(_delta(101, DeltaKind.NEW), _delta(202, DeltaKind.NEW)),
        editorial_rules=_editorial_rules(),
    )

    assert result is not None
    assert result.text == "NEW Cache warmup safeguard is ready [#101]."
    assert result.cited_work_item_ids == (101,)
    assert result.prompt_version == "workstream_blurb.v1"
    assert result.ai_confidence == Confidence.HIGH
    assert client.last_prompt_version == "workstream_blurb.v1"
    assert client.last_user is not None and "#101" in client.last_user
    assert client.last_user is not None and "#202" not in client.last_user


def test_generate_workstream_blurb_uses_lowest_cited_evidence_confidence(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard and release fallback need attention [#101] [#202].")

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(
            _item(101, "Cache warmup safeguard"),
            _item(202, "Release fallback"),
        ),
        evidence_by_item={
            101: _evidence(101, Confidence.HIGH),
            202: _evidence(202, Confidence.LOW),
        },
        deltas=(_delta(101, DeltaKind.NEW), _delta(202, DeltaKind.NEW)),
        editorial_rules=_editorial_rules(),
    )

    assert result is not None
    assert result.ai_confidence == Confidence.LOW


def test_derive_ai_confidence_rejects_cited_items_missing_from_evidence() -> None:
    evidence_by_item = {101: _evidence(101, Confidence.HIGH)}

    with pytest.raises(BlurbGenerationError, match="202"):
        _derive_ai_confidence(evidence_by_item, (101, 202))


def test_generate_workstream_blurb_returns_none_without_eligible_delta_items(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    items = (_item(101, "Cache warmup safeguard"),)
    evidence_by_item = {101: _evidence(101, Confidence.NONE)}

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=items,
        evidence_by_item=evidence_by_item,
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
    )

    assert result is None


def test_generate_workstream_blurb_rejects_items_missing_evidence_context(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard is ready [#101].")

    with pytest.raises(BlurbGenerationError, match="202"):
        generate_workstream_blurb(
            client=client,
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(
                _item(101, "Cache warmup safeguard"),
                _item(202, "Release fallback"),
            ),
            evidence_by_item={101: _evidence(101, Confidence.HIGH)},
            deltas=(_delta(101, DeltaKind.NEW), _delta(202, DeltaKind.NEW)),
            editorial_rules=_editorial_rules(),
        )


def test_generate_workstream_blurb_rejects_ban_list_violations(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW unlock progress [#101].")

    with pytest.raises(BlurbGenerationError, match="ban-list"):
        generate_workstream_blurb(
            client=client,
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(_item(101, "Unlock progress"),),
            evidence_by_item={101: _evidence(101, Confidence.HIGH)},
            deltas=(_delta(101, DeltaKind.NEW),),
            editorial_rules=_editorial_rules(),
        )


def test_generate_workstream_blurb_rejects_non_delta_lead(tmp_path: Path) -> None:
    client = _FakeAIClient("Cache warmup safeguard is ready [#101].")

    with pytest.raises(BlurbGenerationError, match="lead with a delta token"):
        generate_workstream_blurb(
            client=client,
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(_item(101, "Cache warmup safeguard"),),
            evidence_by_item={101: _evidence(101, Confidence.HIGH)},
            deltas=(_delta(101, DeltaKind.NEW),),
            editorial_rules=_editorial_rules(),
        )


def test_generate_workstream_blurb_rejects_citations_outside_eligible_items(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard is ready [#202].")

    with pytest.raises(BlurbGenerationError, match="202"):
        generate_workstream_blurb(
            client=client,
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(
                _item(101, "Cache warmup safeguard"),
                _item(202, "Ignore this low-confidence item"),
            ),
            evidence_by_item={
                101: _evidence(101, Confidence.HIGH),
                202: _evidence(202, Confidence.NONE),
            },
            deltas=(_delta(101, DeltaKind.NEW), _delta(202, DeltaKind.NEW)),
            editorial_rules=_editorial_rules(),
        )


def test_generate_workstream_blurb_includes_supplemental_context_in_prompt(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        supplemental_context=(
            "Rolling summary [acme]: Deployment remains the gating lane.",
            "Leadership reader Jordan Lee: cares about ramp timeline.",
        ),
    )

    assert result is not None
    assert client.last_user is not None and "Supplemental context:" in client.last_user
    assert client.last_user is not None and "Rolling summary [acme]" in client.last_user
    assert client.last_user is not None and "Leadership reader Jordan Lee" in client.last_user


def test_generate_workstream_blurb_uses_narrative_limit_override(tmp_path: Path) -> None:
    long_text = "NEW " + " ".join(f"word{i}" for i in range(60)) + " [#101]."
    client = _FakeAIClient(long_text)
    editorial_rules = EditorialRules(
        schema_version="1.0",
        stale_warn_days=14,
        stale_block_days=30,
        banned_phrases=(),
        banned_openings=(),
        verbosity=VerbositySettings(
            workstream_blurb_max_sentences=3,
            workstream_blurb_max_words=60,
            exec_bullet_max_words=None,
            exec_max_bullets=None,
            scorecard_summary_max_sentences=None,
            workstream_blurb_max_words_by_edition=EditionVerbosityLimits(narrative=150),
        ),
    )

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=editorial_rules,
        edition_type=EditionType.NARRATIVE,
    )

    assert result is not None
    assert client.last_user is not None and "Keep the blurb within 150 words." in client.last_user


def test_generate_workstream_blurb_runs_safety_pipeline(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard moved due to vendor follow-up from foo@gmail.com.")

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
    )

    assert result is not None
    assert result.text == "NEW Cache warmup safeguard moved after vendor follow-up from [PII-FILTERED-EMAIL] [#101]."


def test_generate_workstream_blurb_rejects_non_object_payload(tmp_path: Path) -> None:
    with pytest.raises(BlurbGenerationError, match="payload must be an object"):
        generate_workstream_blurb(
            client=_MalformedPayloadAIClient([]),
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(_item(101, "Cache warmup safeguard"),),
            evidence_by_item={101: _evidence(101, Confidence.HIGH)},
            deltas=(_delta(101, DeltaKind.NEW),),
            editorial_rules=_editorial_rules(),
        )


def test_generate_workstream_blurb_rejects_non_string_payload_text(tmp_path: Path) -> None:
    with pytest.raises(BlurbGenerationError, match="payload must include text as a string"):
        generate_workstream_blurb(
            client=_MalformedPayloadAIClient({"text": ["bad-text"]}),
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(_item(101, "Cache warmup safeguard"),),
            evidence_by_item={101: _evidence(101, Confidence.HIGH)},
            deltas=(_delta(101, DeltaKind.NEW),),
            editorial_rules=_editorial_rules(),
        )


def test_generate_workstream_blurb_rejects_blank_payload_text(tmp_path: Path) -> None:
    with pytest.raises(BlurbGenerationError, match="payload text must be non-empty"):
        generate_workstream_blurb(
            client=_MalformedPayloadAIClient({"text": "   "}),
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(_item(101, "Cache warmup safeguard"),),
            evidence_by_item={101: _evidence(101, Confidence.HIGH)},
            deltas=(_delta(101, DeltaKind.NEW),),
            editorial_rules=_editorial_rules(),
        )


def test_generate_workstream_blurb_uses_nova_writing_contract_context(tmp_path: Path) -> None:
    client = _FakeAIClient("Deployment Safety remains medium until OneDeploy closes the 05/15 checkpoint [#101].")
    writing_style = type(
        "WritingStyle",
        (),
        {
            "voice": "Lane-first, technically specific, and decision-oriented.",
            "structure": "Lead with the changed lane, then the checkpoint and consequence.",
            "risk_framing": {"stuck": "State blocker, owner, checkpoint, and consequence."},
            "preferred_patterns": ("{lane} stays {risk} until {checkpoint} closes by {date}.",),
        },
    )()
    dependency = type(
        "Dependency",
        (),
        {
            "source": "Deployment safety closure",
            "target": "Acme Ramp P1",
            "impact": "Ramp does not resume cleanly until OneDeploy safety behavior closes.",
        },
    )()
    workstream = type(
        "Workstream",
        (),
        {
            "name": "Deployment",
            "aliases": (),
            "why_it_matters": "Deployment determines whether ramp can resume safely.",
            "current_blocker": "OneDeploy safety behavior still needs 05/15 closure.",
        },
    )()
    owner = type(
        "OwnerProfile",
        (),
        {"areas": ("Deployment",), "style_note": "Name the gating fix and the next checkpoint."},
    )()
    program_context = type(
        "ProgramContext",
        (),
        {
            "program_name": "Adventure + DD on PF",
            "objective": "Produce a weekly readiness record.",
            "current_phase": "Acme Ramp P1",
            "writing_style": writing_style,
            "key_dependency_chain": (dependency,),
            "workstreams": (workstream,),
            "workstream_owners": (owner,),
        },
    )()

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Deployment Safety"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        program_context=program_context,
    )

    assert result is not None
    assert client.last_user is not None
    assert "Mandatory writing voice" in client.last_user
    assert "Why this lane matters" in client.last_user
    assert "Owner style note" in client.last_user


def test_generate_workstream_blurb_rejects_synthetic_delta_token_for_voice_compat(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Deployment Safety is ready [#101].")
    writing_style = type(
        "WritingStyle",
        (),
        {
            "voice": "Lane-first, blocker-first, and decision-oriented.",
            "structure": "Lead with the gating lane.",
            "risk_framing": {},
            "preferred_patterns": (),
        },
    )()
    program_context = type(
        "ProgramContext",
        (),
        {
            "program_name": "Adventure + DD on PF",
            "current_phase": "Acme Ramp P1",
            "writing_style": writing_style,
            "key_dependency_chain": (),
            "workstreams": (),
            "workstream_owners": (),
        },
    )()

    with pytest.raises(BlurbGenerationError, match="synthetic delta token"):
        generate_workstream_blurb(
            client=client,
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(_item(101, "Deployment Safety"),),
            evidence_by_item={101: _evidence(101, Confidence.HIGH)},
            deltas=(_delta(101, DeltaKind.NEW),),
            editorial_rules=_editorial_rules(),
            program_context=program_context,
        )


def _m365_evidence() -> WorkstreamEvidence:
    return WorkstreamEvidence(
        lane_id="deployment",
        synthesized_at=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        risk_level=RiskLevel.BLOCKED,
        etas=(
            EtaRecord(
                label="Gen9 firmware sign-off",
                eta_date=date(2026, 6, 25),
                owner="operator",
                status="open",
                ado_id="37777539",
            ),
        ),
        blocking_items=("ADO:37777539",),
        owners=("operator",),
        source_refs=(
            SourceRef(
                source_type="workiq_email",
                description="BIOS sync email — blocker raised",
                source_date=date(2026, 6, 17),
                author="operator@example.com",
                permalink="https://outlook/x",
            ),
        ),
        raw_excerpts=("Gen9 sign-off blocked on burn-in",),
        confidence=0.82,
        narrative_summary="BIOS AP Gen9 rollout is blocked on burn-in sign-off.",
        stale_after=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )


def _ado_comment_signal(*, text: str) -> Signal:
    return Signal(
        id="ado/comment/101/2026-06-18",
        timestamp=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        source="ado/comment",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(),
        text=text,
        raw_ref="ado:comment:1",
        confidence=Confidence.HIGH,
    )


def test_p4_1_bundle_feeds_m365_evidence_and_ado_comments_into_prompt(tmp_path: Path) -> None:
    """P4-1: a populated WorkstreamEvidenceBundle adds M365 narrative/blocking and
    ADO-comment excerpts to the blurb prompt, and the blurb carries cited_source_refs."""
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        m365_evidence=_m365_evidence(),
        ado_comments=(_ado_comment_signal(text="Sign-off held pending burn-in results from the lab."),),
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        workstream_evidence_bundle=bundle,
    )

    assert result is not None
    # The M365 narrative + blocking item reached the prompt.
    assert client.last_user is not None
    assert "BIOS AP Gen9 rollout is blocked on burn-in sign-off." in client.last_user
    assert "ADO:37777539" in client.last_user
    # The ADO-comment excerpt reached the prompt.
    assert "Sign-off held pending burn-in results from the lab." in client.last_user
    # §19.1: the source refs from the M365 evidence are surfaced on the blurb.
    assert result.cited_source_refs == bundle.m365_evidence.source_refs


def test_p4_1_bundle_none_preserves_ado_only_baseline_and_empty_source_refs(tmp_path: Path) -> None:
    """P4-1 backward compat: no bundle → ADO-only behavior, empty cited_source_refs."""
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
    )

    assert result is not None
    assert result.cited_source_refs == ()
    # No structured-evidence header when there is no bundle.
    assert client.last_user is not None
    assert "Structured lane evidence" not in client.last_user


def test_p4_1_bundle_without_m365_evidence_omits_evidence_block(tmp_path: Path) -> None:
    """A bundle with only ADO-comment signals (no M365 evidence) still emits comments
    but no M365-derived lines; cited_source_refs stays empty."""
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        ado_comments=(_ado_comment_signal(text="Burn-in pending."),),
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        workstream_evidence_bundle=bundle,
    )

    assert result is not None
    assert result.cited_source_refs == ()
    assert client.last_user is not None
    assert "Burn-in pending." in client.last_user
    assert "narrative:" not in client.last_user


def _icm_blocker_signal(*, incident_id: str, severity: int = 2, title: str = "Gen9 burn-in blocked") -> Signal:
    return Signal(
        id=f"icm/incident/abc/{incident_id}",
        timestamp=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        source="icm",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(f"icm:{incident_id}",),
        text=f"[Sev {severity}] {title}",
        raw_ref="icm:abc",
        confidence=Confidence.HIGH,
        metadata={"incident_id": incident_id, "severity": severity, "owning_team": "Acme-Infra"},
    )


def _kusto_metric_signal(*, text: str = "Kusto query acme-bios-rollout: 1 row(s) observed.") -> Signal:
    return Signal(
        id="kusto/acme-bios-rollout/2026-06-18",
        timestamp=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        source="kusto",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(),
        text=text,
        raw_ref="kusto:bios",
        confidence=Confidence.HIGH,
    )


def _ado_sprint_signal() -> Signal:
    return Signal(
        id="ado/sprint/iteration-1",
        timestamp=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(),
        text="Sprint deployment summary",
        raw_ref="ado:sprint:1",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 42",
            "committed_item_count": 10,
            "completed_item_count": 7,
            "completion_pct": 70,
            "open_item_count": 3,
        },
    )


def _reference_update_signal(*, text: str = "eng.ms page updated: https://eng.ms/docs/acme/spec") -> Signal:
    return Signal(
        id="engms/spec",
        timestamp=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        source="engms",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:101",),
        text=text,
        raw_ref="https://eng.ms/docs/acme/spec",
        confidence=Confidence.LOW,
    )


def test_p4_8_icm_blockers_render_as_structured_prompt_context(tmp_path: Path) -> None:
    """P4-8: IcM incidents appear in the blurb prompt as structured blockers with id + owner."""
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        m365_evidence=_m365_evidence(),
        icm_blockers=(_icm_blocker_signal(incident_id="771996570"),),
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )

    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        workstream_evidence_bundle=bundle,
    )

    assert client.last_user is not None
    assert "icm_blocker:" in client.last_user
    assert "IcM:771996570" in client.last_user
    assert "owner=Acme-Infra" in client.last_user


def test_p4_9_kusto_metrics_render_as_quantitative_prompt_context(tmp_path: Path) -> None:
    """P4-9: Kusto metric text reaches the blurb prompt even without M365 evidence."""
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        kusto_metrics=(_kusto_metric_signal(),),
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )

    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        workstream_evidence_bundle=bundle,
    )

    assert client.last_user is not None
    assert "kusto_metric:" in client.last_user
    assert "acme-bios-rollout" in client.last_user


def test_p4_23_ado_telemetry_summary_reaches_prompt_context(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        ado_signals=(_ado_sprint_signal(),),
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )

    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        workstream_evidence_bundle=bundle,
    )

    assert client.last_user is not None
    assert "ado_telemetry:" in client.last_user
    assert "Sprint 42" in client.last_user
    assert "70% complete" in client.last_user


def test_p4_23_reference_updates_reach_prompt_context(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        reference_signals=(_reference_update_signal(),),
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )

    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        workstream_evidence_bundle=bundle,
    )

    assert client.last_user is not None
    assert "reference_update:" in client.last_user
    assert "eng.ms/docs/acme/spec" in client.last_user


def test_p4_24_and_p4_25_bundle_prompt_includes_trajectory_and_source_freshness(tmp_path: Path) -> None:
    """Temporal and freshness context should reach the blurb prompt from the bundle."""
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        lookback_intelligence=(
            "WI#101 eta_drift (high): Target date slipped 3 times in the last 90 days.",
        ),
        freshness_by_source={
            "icm": datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
            "m365": datetime(2026, 6, 17, 9, 0, tzinfo=timezone.utc),
        },
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )

    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        workstream_evidence_bundle=bundle,
    )

    assert client.last_user is not None
    assert "trajectory: WI#101 eta_drift (high): Target date slipped 3 times in the last 90 days." in client.last_user
    assert "source_freshness: icm=2026-06-18; m365=2026-06-17" in client.last_user


def test_p4_18_prompt_budget_trims_excessive_context(tmp_path: Path) -> None:
    """P4-18: oversized evidence/supplemental context is bounded before reaching the LLM."""
    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    noisy_comment = " ".join(f"note{i}" for i in range(160))
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        ado_comments=tuple(_ado_comment_signal(text=f"{idx} {noisy_comment}") for idx in range(18)),
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )
    supplemental_context = tuple(f"context {idx} {noisy_comment}" for idx in range(12))

    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        supplemental_context=supplemental_context,
        workstream_evidence_bundle=bundle,
    )

    assert client.last_user is not None
    assert "evidence_context_omitted:" in client.last_user
    assert "supplemental_context_omitted:" in client.last_user


def test_p4_27_bundle_prompt_includes_corroboration_note_and_boosted_confidence(tmp_path: Path) -> None:
    """P4-27: corroborated facts raise transient bundle confidence and reach the prompt."""
    from src.commands.report_ai import _boost_evidence_confidence_from_corroboration

    evidence = _m365_evidence()
    boosted, notes = _boost_evidence_confidence_from_corroboration(
        evidence,
        ado_signals=[],
        ado_comments=[_ado_comment_signal(text="Blocked on lab sign-off and delayed by burn-in.")],
        kusto_metrics=[_kusto_metric_signal(text="Deployment blocked in burn-in queue; regression persists.")],
        icm_blockers=[],
    )

    assert boosted.confidence > evidence.confidence
    assert notes

    client = _FakeAIClient("NEW Cache warmup safeguard is ready.")
    bundle = WorkstreamEvidenceBundle(
        lane_id="deployment",
        m365_evidence=boosted,
        corroboration_notes=notes,
        as_of=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
    )

    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
        workstream_evidence_bundle=bundle,
    )

    assert client.last_user is not None
    assert "corroboration:" in client.last_user
    assert f"confidence={boosted.confidence:.2f}" in client.last_user


def test_p4_8_p4_9_augment_folds_icm_into_blocking_items_and_kusto_into_narrative() -> None:
    """P4-8/P4-9 orchestrator augmentation: IcM IDs land in blocking_items (deduped),
    Kusto text is appended to narrative_summary; None evidence passes through unchanged."""
    from src.commands.report_ai import _augment_evidence_with_quantitative_signals

    evidence = _m365_evidence()  # blocking_items=("ADO:37777539",), narrative ends with "sign-off."
    icm = [_icm_blocker_signal(incident_id="771996570")]
    kusto = [_kusto_metric_signal(text="SCHIE compliance now at 73.4%.")]

    augmented = _augment_evidence_with_quantitative_signals(
        evidence, icm_blockers=icm, kusto_metrics=kusto,
    )

    assert augmented is not None
    assert "IcM:771996570" in augmented.blocking_items
    assert "ADO:37777539" in augmented.blocking_items  # original preserved
    assert augmented.narrative_summary.startswith("BIOS AP Gen9 rollout is blocked on burn-in sign-off.")
    assert "SCHIE compliance now at 73.4%." in augmented.narrative_summary
    # Source refs preserved (cited_source_refs unchanged).
    assert augmented.source_refs == evidence.source_refs

    # IcM id already present is not duplicated.
    again = _augment_evidence_with_quantitative_signals(
        augmented, icm_blockers=icm, kusto_metrics=[],
    )
    assert again.blocking_items.count("IcM:771996570") == 1

    # None evidence passes through unchanged.
    assert _augment_evidence_with_quantitative_signals(
        None, icm_blockers=icm, kusto_metrics=kusto,
    ) is None

    # Evidence with no IcM/Kusto signals is returned unchanged (same object).
    same = _augment_evidence_with_quantitative_signals(
        evidence, icm_blockers=[], kusto_metrics=[],
    )
    assert same is evidence


def test_generate_workstream_blurb_records_released_terminal_on_success(tmp_path: Path) -> None:
    # ADF-W5.1/P7: blurb_generator's AISchemaGateway migration must record a
    # durable QG-29 "released" terminal for a successful generation, same as
    # risk_proposal_generator's release-audit contract.
    from src.core.ledger.event_log import read_events

    client = _FakeAIClient("NEW Cache warmup safeguard is ready [#101].")

    result = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
    )

    assert result is not None
    events = read_events("acme", programs_root=tmp_path)
    release_decisions = [event for event in events if event.event_type == "ai.release_decision.v1"]
    assert release_decisions
    assert release_decisions[-1].payload["terminal"] == "released"


def test_generate_workstream_blurb_repeat_identical_request_hits_the_cache(tmp_path: Path) -> None:
    # ADF-W5.1/P7: identical program/items/evidence/deltas/editorial_rules
    # should be served from the AI result cache on the second call.
    client = _FakeAIClient("NEW Cache warmup safeguard is ready [#101].")
    items = (_item(101, "Cache warmup safeguard"),)
    evidence_by_item = {101: _evidence(101, Confidence.HIGH)}
    deltas = (_delta(101, DeltaKind.NEW),)

    first = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=items,
        evidence_by_item=evidence_by_item,
        deltas=deltas,
        editorial_rules=_editorial_rules(),
    )
    second = generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=items,
        evidence_by_item=evidence_by_item,
        deltas=deltas,
        editorial_rules=_editorial_rules(),
    )

    assert first is not None
    assert second is not None
    assert client.calls == 1
    assert second.text == first.text


def test_generate_workstream_blurb_different_items_do_not_hit_the_cache(tmp_path: Path) -> None:
    client = _FakeAIClient("NEW Cache warmup safeguard is ready [#101].")

    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "Cache warmup safeguard"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
    )
    generate_workstream_blurb(
        client=client,
        program_id="acme",
        programs_root=tmp_path,
        workstream_name="Deployment",
        items=(_item(101, "A totally different work item"),),
        evidence_by_item={101: _evidence(101, Confidence.HIGH)},
        deltas=(_delta(101, DeltaKind.NEW),),
        editorial_rules=_editorial_rules(),
    )

    assert client.calls == 2


def test_generate_workstream_blurb_oversized_request_is_discarded_before_calling_the_provider(
    tmp_path: Path,
) -> None:
    # ADF-W5.1/P7: AISchemaGateway bounds must reject an oversized request
    # payload before ever invoking the frontier provider.
    client = _FakeAIClient("NEW Cache warmup safeguard is ready [#101].")

    with pytest.raises(BlurbGenerationError, match="AISchemaGateway rejected the outbound request"):
        generate_workstream_blurb(
            client=client,
            program_id="acme",
            programs_root=tmp_path,
            workstream_name="Deployment",
            items=(_item(101, "Cache warmup safeguard"),),
            evidence_by_item={101: _evidence(101, Confidence.HIGH)},
            deltas=(_delta(101, DeltaKind.NEW),),
            editorial_rules=_editorial_rules(),
            supplemental_context=("x" * 200_001,),
        )

    assert client.calls == 0
