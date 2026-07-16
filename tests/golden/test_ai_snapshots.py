from __future__ import annotations

import difflib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ai.draft_reviewer import review_draft
from src.ai.blurb_generator import generate_workstream_blurb
from src.ai.exec_summary_drafter import draft_exec_summary
from src.core.archive_store import write_confirmed_issue
from src.core.config_loader import EditorialRules, VerbositySettings
from src.core.config_loader import KustoQuerySettings, KustoSettings
from src.core.config_loader import VoiceContractSettings
from src.core.models import AttributionTier, Comment, Confidence, DeltaKind, DeltaSet, EditionType, EvidencePacket, FreshnessReport, ItemDelta
from src.core.models import ProgramContext, ReportData, ReviewSection, ReviewState, ReviewStatus, RiskLevel, RunManifest, Snapshot, SnapshotItem, WorkItem


GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"
FROZEN_NOW = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
EDITION_NAME = "acme_weekly"


class GoldenFileMismatchError(AssertionError):
    pass


class _FakeAIClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text

    def chat(self, system: str, user: str, max_tokens: int = 800, *, prompt_version: str | None = None) -> str:
        del system, user, max_tokens, prompt_version
        return self.response_text

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del system, user, max_tokens, prompt_version
        return parser({"text": self.response_text})


def _load_golden(name: str) -> str | None:
    golden_path = GOLDEN_DIR / f"{name}.golden"
    if golden_path.exists():
        return golden_path.read_text(encoding="utf-8")
    return None


def _save_golden(name: str, content: str) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / f"{name}.golden").write_text(content, encoding="utf-8")


def _compare_with_golden(name: str, actual: str, update: bool) -> None:
    golden = _load_golden(name)
    if update or golden is None:
        _save_golden(name, actual)
        if golden is None:
            pytest.skip(f"Created new golden file: {name}.golden")
        return

    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.golden",
                tofile="actual",
            )
        )
        raise GoldenFileMismatchError(
            f"Output does not match golden file: {name}.golden\n\nDiff:\n{diff}"
        )


def test_ai_workstream_blurb_snapshot(update_golden: bool, tmp_path: Path) -> None:
    result = generate_workstream_blurb(
        client=_FakeAIClient("NEW Cache warmup safeguard is ready."),
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
        deltas=(
            _delta(101, DeltaKind.NEW),
            _delta(202, DeltaKind.NEW),
        ),
        editorial_rules=_editorial_rules(),
    )

    assert result is not None
    actual = json.dumps(
        {
            "cited_work_item_ids": list(result.cited_work_item_ids),
            "prompt_version": result.prompt_version,
            "text": result.text,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    _compare_with_golden("ai_workstream_blurb", actual, update_golden)


def test_ai_exec_summary_snapshot(update_golden: bool, tmp_path: Path) -> None:
    result = draft_exec_summary(
        client=_FakeAIClient(
            "Risk rose for Cache warmup safeguard. Deployment blocker entered scope. Repairs incident closed after mitigation."
        ),
        program_id="acme",
        programs_root=tmp_path,
        items=(
            _item(101, "Cache warmup safeguard", risk_level=RiskLevel.HIGH),
            _item(102, "Deployment blocker", risk_level=RiskLevel.HIGH),
            _item(103, "Repairs incident", risk_level=RiskLevel.HIGH),
            _item(104, "Owner follow-up", risk_level=RiskLevel.MEDIUM),
        ),
        deltas=SimpleNamespace(
            risk_changes=(_delta(101, DeltaKind.RISK_UP, old_risk=RiskLevel.MEDIUM, new_risk=RiskLevel.HIGH),),
            new_items=(_delta(102, DeltaKind.NEW, new_risk=RiskLevel.HIGH),),
            closed_items=(_delta(103, DeltaKind.CLOSED, old_risk=RiskLevel.HIGH, new_risk=RiskLevel.HIGH),),
            eta_changes=(_delta(104, DeltaKind.ETA_CHANGED, old_eta=date(2026, 5, 10), new_eta=date(2026, 5, 20)),),
            owner_changes=(),
        ),
        editorial_rules=_editorial_rules(),
    )

    assert result is not None
    actual = json.dumps(
        {
            "cited_work_item_ids": list(result.cited_work_item_ids),
            "prompt_version": result.prompt_version,
            "text": result.text,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    _compare_with_golden("ai_exec_summary", actual, update_golden)


def test_ai_draft_review_snapshot(update_golden: bool, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    prior_as_of = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    current_as_of = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
    current_item = _item(301, "Cache warmup safeguard", risk_level=RiskLevel.HIGH)

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_review_snapshot(issue_number=1, as_of=prior_as_of, items=(_review_snapshot_item(current_item, risk=RiskLevel.HIGH),)),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\nCache warmup safeguard remained high risk.\n",
        manifest=_review_manifest(issue_number=1, as_of=prior_as_of),
        archive_root=archive_root,
    )

    report = ReportData(
        issue_number=2,
        edition=EditionType.DETAILED,
        generated_at=current_as_of,
        ado_data_as_of=current_as_of,
        program=ProgramContext(
            program_name="Acme",
            mission="Track deployment execution",
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        ),
        items=(current_item,),
        deltas=DeltaSet(
            issue_number=2,
            previous_issue_number=1,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=1,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="Cache warmup safeguard still lacks a quantified resolution path.",
        workstream_blurbs={"deployment": "Deployment remains elevated and needs a tighter narrative for leaders." * 4},
        freshness=FreshnessReport(issue_number=2, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=ReviewStatus(
            issue_number=2,
            sections=(
                ReviewSection(section_id="exec_summary", state=ReviewState.PENDING, reviewer=None, note=None, updated_at=None),
            ),
        ),
        manifest_id="review-manifest",
    )

    leadership_reader = type(
        "LeadershipReader",
        (),
        {
            "name": "Executive Reader",
            "role": "PM Lead",
            "cares_about": ("accuracy", "exec summary quality"),
            "prefers": "Lead with wins + deltas.",
            "pet_peeves": ("verbosity",),
        },
    )()
    richer_program_context = type(
        "ProgramContextWithLeadership",
        (),
        {"leadership_readers": (leadership_reader,)},
    )()

    review_report, info_messages = review_draft(
        report=report,
        draft_markdown="# Draft\nCache warmup safeguard still lacks a quantified resolution path.\n",
        program_context=richer_program_context,
        editorial_rules=_editorial_rules(),
        kusto_settings=_review_kusto_settings(),
        edition_name=EDITION_NAME,
        archive_root=archive_root,
    )

    actual = json.dumps(
        {
            "counts": {
                "cross_issue_flags": review_report.cross_issue_flags,
                "data_gaps": review_report.data_gaps,
                "leadership_questions": review_report.leadership_questions,
                "structural_notes": review_report.structural_notes,
            },
            "info_messages": list(info_messages),
            "suggestions": [
                {
                    "action": suggestion.action,
                    "category": suggestion.category,
                    "confidence": suggestion.confidence.value,
                    "reader_name": suggestion.reader_name,
                    "section_id": suggestion.section_id,
                    "suggestion_text": suggestion.suggestion_text,
                }
                for suggestion in review_report.suggestions
            ],
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    _compare_with_golden("ai_draft_review", actual, update_golden)


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
            abstract_phrases=("materially narrower", "broader program blocker"),
            synthetic_delta_prefixes=("NEW", "CLOSED", "RISK_UP", "RISK_DOWN", "ETA", "OWNER"),
            decision_lead_terms=("blocking", "checkpoint", "conditional", "eta", "gate", "target"),
            static_concrete_terms=("azure core", "schie", "northwind", "acme"),
            exec_summary_bucket_prefixes=("acme:",),
            objective_preamble_prefixes=("the objective of the acme program is", "northwind clusters live within azure"),
        ),
    )


def _item(work_item_id: int, title: str, *, risk_level: RiskLevel = RiskLevel.MEDIUM, comment_text: str | None = None) -> WorkItem:
    comments = []
    if comment_text is not None:
        comments.append(
            Comment(
                work_item_id=work_item_id,
                comment_id=1,
                created_by="Operator",
                created_by_email="operator@example.com",
                created_date=FROZEN_NOW,
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
        risk_level=risk_level,
        tags=[],
        custom_fields={},
        comments=comments,
        fetched_at=FROZEN_NOW,
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


def _delta(
    work_item_id: int,
    kind: DeltaKind,
    *,
    old_risk: RiskLevel | None = None,
    new_risk: RiskLevel | None = None,
    old_eta: date | None = None,
    new_eta: date | None = None,
) -> ItemDelta:
    return ItemDelta(
        work_item_id=work_item_id,
        kind=kind,
        field_changes={},
        old_risk=old_risk,
        new_risk=new_risk,
        old_eta=old_eta,
        new_eta=new_eta,
        evidence=_evidence(work_item_id, Confidence.HIGH),
    )


def _review_kusto_settings() -> KustoSettings:
    return KustoSettings(
        enabled=True,
        queries=(
            KustoQuerySettings(
                id="velocity-p50",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Velocity",
                section="Deployment Velocity",
                render_as="chart_image",
                confidence="high",
                kusto_section_validates_slice=False,
                caveats=(),
                reference_url="https://adventure.kusto.windows.net",
            ),
        ),
    )


def _review_snapshot(issue_number: int, as_of: datetime, items: tuple[SnapshotItem, ...]) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=as_of,
        ado_data_as_of=as_of,
        edition_type=EditionType.DETAILED,
        items=items,
        scorecards=(),
    )


def _review_snapshot_item(item: WorkItem, *, risk: RiskLevel) -> SnapshotItem:
    return SnapshotItem(
        id=item.id,
        type=item.type,
        title=item.title,
        state=item.state,
        assigned_to=item.assigned_to,
        area_path=item.area_path,
        target_date=item.target_date,
        risk_level=risk,
        tags=list(item.tags),
    )


def _review_manifest(issue_number: int, as_of: datetime) -> RunManifest:
    return RunManifest(
        manifest_id=f"review-{issue_number}",
        issue_number=issue_number,
        edition=EDITION_NAME,
        started_at=as_of,
        ended_at=as_of,
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha=None,
    )
