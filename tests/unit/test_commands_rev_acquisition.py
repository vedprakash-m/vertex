from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from src.commands.rev import app as rev_app
from src.core.ledger.candidate_store import load_pending_candidates
from src.core.ledger.source_refs import SharePointDocRef, WorkIQRef

runner = CliRunner()


class _WorkIQBridge:
    calls: ClassVar[list[str]] = []

    def probe(self):
        return SimpleNamespace(available=True, has_workiq=True, has_workiq_cli=False)

    def ask_workiq(self, question: str, *, use_cache: bool = False, timeout_seconds=None):
        self.calls.append(question)
        return {
            "emails": [
                {
                    "id": "AAMkAD-message-1",
                    "conversationId": "AAQkAD-thread-1",
                    "subject": "Atlas update",
                    "from": "owner@example.com",
                    "receivedDateTime": "2026-11-11T12:00:00+00:00",
                    "webUrl": "https://outlook.office.com/message/1",
                    "bodyPreview": "Atlas deployment completed 2026-11-11.",
                }
            ]
        }

    def last_mcp_error(self):
        return None


def _write_sharepoint_fixture(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    knowledge_dir = programs_root / "demo" / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "pages:\n"
        "  - id: launch-page\n"
        "    title: Launch status\n"
        "    url: https://contoso.sharepoint.com/sites/demo/launch.txt\n"
        "    program_ids: [demo]\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "sharepoint-fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "page_id": "launch-page",
                        "item_id": "drive-item-1",
                        "drive_id": "drive-1",
                        "site_id": "site-1",
                        "etag": "v3",
                        "name": "launch.txt",
                        "mime_type": "text/plain",
                        "last_modified": "2026-11-11T12:30:00Z",
                        "content_text": "Launch deployment completed 2026-11-11.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return fixture


def test_rev_run_workiq_uses_bounded_rev_ports_and_preserves_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _WorkIQBridge()
    monkeypatch.setattr("src.commands.rev.AgencyBridge", lambda: bridge)
    _WorkIQBridge.calls = []

    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--workiq",
            "--subject", "Atlas",
            "--from-date", "2026-11-05",
            "--to-date", "2026-11-11",
            "--programs-root", str(tmp_path / "programs"),
        ],
    )

    assert result.exit_code == 0, result.output
    report, _ = json.JSONDecoder().raw_decode(result.output)
    assert report["enumerated"] == report["hydrated"] == report["processed_successfully"] == 1
    assert report["candidates_staged"] == 1
    assert len(bridge.calls) == 1
    candidate = load_pending_candidates("demo", programs_root=tmp_path / "programs")[0]
    assert candidate.pipeline == "rev_workiq"
    assert isinstance(candidate.source_ref, WorkIQRef)
    assert candidate.source_ref.artifact_id == "AAMkAD-message-1"
    assert candidate.source_ref.artifact_kind == "native_message"
    assert candidate.source_ref.vault_hash
    assert candidate.evidence_refs


def test_rev_run_sharepoint_fixture_preserves_typed_lineage(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    fixture = _write_sharepoint_fixture(tmp_path)

    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--sharepoint-fixture", str(fixture),
            "--programs-root", str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.output
    report, _ = json.JSONDecoder().raw_decode(result.output)
    assert report["enumerated"] == report["hydrated"] == report["processed_successfully"] == 1
    assert report["candidates_staged"] == 1
    candidate = load_pending_candidates("demo", programs_root=programs_root)[0]
    assert candidate.pipeline == "rev_sharepoint"
    assert isinstance(candidate.source_ref, SharePointDocRef)
    assert candidate.source_ref.site == "site-1"
    assert candidate.source_ref.version == "v3"
    assert candidate.source_ref.vault_hash


def test_rev_run_sharepoint_fixture_preserves_explicit_version(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    fixture = _write_sharepoint_fixture(tmp_path)
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["items"][0]["version"] = "7.1"
    payload["items"][0]["acl"] = [
        {"kind": "group", "id": "launch-readers", "roles": ["read"]}
    ]
    fixture.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--sharepoint-fixture", str(fixture),
            "--programs-root", str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.output
    candidate = load_pending_candidates("demo", programs_root=programs_root)[0]
    assert isinstance(candidate.source_ref, SharePointDocRef)
    assert candidate.source_ref.version == "7.1"


@pytest.mark.parametrize(
    "status",
    [{"deleted": True}, {"inaccessible": True}],
)
def test_rev_run_sharepoint_fixture_rejects_non_authoritative_items(
    tmp_path: Path, status: dict[str, bool]
) -> None:
    programs_root = tmp_path / "programs"
    fixture = _write_sharepoint_fixture(tmp_path)
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["items"][0].update(status)
    fixture.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--sharepoint-fixture", str(fixture),
            "--programs-root", str(programs_root),
        ],
    )

    assert result.exit_code == 1, result.output
    report, _ = json.JSONDecoder().raw_decode(result.output)
    assert report["enumerated"] == 1
    assert report["hydrated"] == 0
    assert report["candidates_staged"] == 0
    assert report["processed_successfully"] == 0
    assert load_pending_candidates("demo", programs_root=programs_root) == ()


@pytest.mark.parametrize(
    "options,message",
    [
        (["--workiq", "--subject", "Atlas", "--from-date", "20261105"], "YYYY-MM-DD"),
        (["--workiq", "--subject", "Atlas", "--from-date", "2026-11-12", "--to-date", "2026-11-11"], "must not be after"),
        (["--workiq"], "at least one --subject or --body term"),
    ],
)
def test_rev_run_rejects_invalid_workiq_admission(
    tmp_path: Path, options: list[str], message: str
) -> None:
    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            *options,
            "--programs-root", str(tmp_path / "programs"),
        ],
    )

    assert result.exit_code != 0
    assert message in result.output


def test_rev_run_rejects_multiple_acquisition_sources(tmp_path: Path) -> None:
    mock = tmp_path / "messages.json"
    mock.write_text("[]", encoding="utf-8")

    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--mock-fixture", str(mock),
            "--workiq",
            "--subject", "Atlas",
            "--programs-root", str(tmp_path / "programs"),
        ],
    )

    assert result.exit_code != 0
    assert "Select exactly one REV source" in result.output


def test_rev_run_checkpoint_retains_incomplete_discovery_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _WorkIQBridge()
    original = bridge.ask_workiq

    def capped(question: str, **kwargs):
        payload = original(question, **kwargs)
        payload["emails"] = [
            {**payload["emails"][0], "id": f"message-{index}"}
            for index in range(2)
        ]
        return payload

    bridge.ask_workiq = capped
    monkeypatch.setattr("src.commands.rev.AgencyBridge", lambda: bridge)
    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--workiq",
            "--subject", "Atlas",
            "--limit", "1",
            "--from-date", "2026-11-05",
            "--to-date", "2026-11-11",
            "--programs-root", str(tmp_path / "programs"),
        ],
    )

    assert result.exit_code == 1, result.output
    report, _ = json.JSONDecoder().raw_decode(result.output)
    assert report["stop_reason"] in {"page_cut", "page_cut:window"}
    last_cycle = json.loads(
        (tmp_path / "programs" / "demo" / "_rev" / "last_cycle.json").read_text(encoding="utf-8")
    )
    assert last_cycle["stop_reason"] == report["stop_reason"]
    assert last_cycle["breached_budget"] == "incomplete"
    assert last_cycle["date_parse_failures"] == 0


def test_rev_run_reports_workiq_time_rejections_alongside_valid_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _WorkIQBridge()

    def mixed(question: str, **kwargs):
        payload = _WorkIQBridge().ask_workiq(question, **kwargs)
        invalid = dict(payload["emails"][0])
        invalid["id"] = "invalid-time-message"
        invalid.pop("receivedDateTime")
        payload["emails"].insert(0, invalid)
        return payload

    monkeypatch.setattr(bridge, "ask_workiq", mixed)
    monkeypatch.setattr("src.commands.rev.AgencyBridge", lambda: bridge)

    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--workiq",
            "--subject", "Atlas",
            "--from-date", "2026-11-05",
            "--to-date", "2026-11-11",
            "--programs-root", str(tmp_path / "programs"),
        ],
    )

    assert result.exit_code == 1, result.output
    report, _ = json.JSONDecoder().raw_decode(result.output)
    assert report["enumerated"] == 1
    assert report["candidates_staged"] == 1
    assert report["stop_reason"] == "unqualified_source_time:1"
    last_cycle = json.loads(
        (tmp_path / "programs" / "demo" / "_rev" / "last_cycle.json").read_text(encoding="utf-8")
    )
    assert last_cycle["stop_reason"] == "unqualified_source_time:1"


def test_rev_run_does_not_promote_sharepoint_evidence_without_source_time(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    fixture = _write_sharepoint_fixture(tmp_path)
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["items"][0].pop("last_modified")
    fixture.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(
        rev_app,
        [
            "run",
            "--program", "demo",
            "--mailbox", "tpm@example.com",
            "--sharepoint-fixture", str(fixture),
            "--programs-root", str(programs_root),
        ],
    )

    assert result.exit_code == 1, result.output
    report, _ = json.JSONDecoder().raw_decode(result.output)
    assert report["hydrated"] == 1
    assert report["candidates_staged"] == 0
    assert report["date_parse_failures"] == 1
    assert report["cycle_status"] in {"security_degraded", "extraction_degraded"}
    assert load_pending_candidates("demo", programs_root=programs_root) == ()
    assert list((programs_root / "demo" / "ledger" / "evidence").rglob("*.revmeta.json"))
