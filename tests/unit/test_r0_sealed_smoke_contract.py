from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

import pytest
from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import confirm, report
from src.commands.confirm_stages import archive_transaction
from src.commands.confirm_stages.draft_loaders import ConfirmDraftManifest, load_confirm_draft_manifest
from src.core import archive_store
from src.core.archive_store import read_archive_index, write_confirmed_issue
from src.core.ban_list_validator import find_ban_list_violations
from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.config_loader import EditorialRules, VerbositySettings, load_bundle
from src.core.draft_artifact_store import load_draft_state
from src.core.quality_gates.fact_drift import evaluate_program_fact_drift_from_draft

from src.core.overrides_store import get_overrides_path
from src.core.exceptions import ConfigError, StateError
from src.core.metric_models import MetricQualityState
from src.core.models import (
    ConfirmedDimension,
    EditionType,
    RiskLevel,
    RunEvidenceSnapshot,
    RunManifest,
    Snapshot,
    SnapshotItem,
)
from src.core.manifest_writer import hash_jsonable
from src.core.narrative_store import get_narratives_dir
from src.core.program_verdict import (
    load_latest_program_verdict,
    observe_program_verdict,
    validate_retained_program_verdict,
)
from src.core.reality_store import RealityStore
from tests.support.report_test_setup import (
    disable_kusto_in_report_copy,
    reset_overrides_to_seed_state,
    stage_v2_report_workspace,
)
from tests.support.synthetic_report_source import EDITION_NAME, build_synthetic_report_source
from tests.unit.test_commands_report import _sample_items


REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKOUT_HASH_ROOTS = ("cli.py", "src", "scripts", "templates")
_SKIP_HASH_PARTS = {"__pycache__", ".pytest_cache"}


def _hash_checkout_tree() -> dict[str, str]:
    digests: dict[str, str] = {}
    for entry in _CHECKOUT_HASH_ROOTS:
        target = REPO_ROOT / entry
        files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
        for path in files:
            rel = path.relative_to(REPO_ROOT)
            if any(part in _SKIP_HASH_PARTS or part.startswith(".coverage") for part in rel.parts):
                continue
            digests[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _seed_workspace(tmp_path: Path, *, name: str) -> tuple[Path, Path, Path, Path]:
    source_root = build_synthetic_report_source(tmp_path / f"{name}-source")
    workspace = tmp_path / name
    reports_root = stage_v2_report_workspace(source_root, workspace)
    programs_root = workspace / "programs"
    archive_root = workspace / "archive"
    metrics_dir = workspace / "knowledge" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "example.yaml").write_text(
        (
            "metrics:\n"
            "  - id: acme.cluster_count\n"
            "    title: Cluster Count\n"
            "    unit: count\n"
            "    aggregation: last\n"
            "    retention_days: 90\n"
        ),
        encoding="utf-8",
    )
    milestones_path = programs_root / "acme" / "milestones.yaml"
    milestones = yaml.safe_load(milestones_path.read_text(encoding="utf-8"))
    for milestone in milestones["milestones"]:
        milestone["target_date"] = "2099-12-31"
        milestone["linked_work_item_ids"] = []
    milestones_path.write_text(yaml.safe_dump(milestones), encoding="utf-8")
    disable_kusto_in_report_copy(reports_root)
    reset_overrides_to_seed_state(reports_root)
    return workspace, programs_root, reports_root, archive_root


def _seed_cached_snapshot(
    workspace: Path, *, issue: int = 77, risk_override: str | None = None,
    as_of: datetime | None = None, projector_preview: bool = False,
) -> tuple[report.DraftArtifacts, Path]:
    programs_root = workspace / "programs"
    reports_root = workspace / "reports"
    archive_root = workspace / "archive"
    seeded = report.generate_report_draft(
        EDITION_NAME,
        issue_number=issue,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
        as_of=as_of,
        projector_preview=projector_preview,
        work_item_loader=lambda _bundle, ts: (_sample_items(ts)[:1], 0),
        open_browser=False,
    )
    if risk_override is not None:
        overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=issue)
        payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
        for scorecard in payload["scorecards"].values():
            for dimension in scorecard.values():
                dimension["risk"] = risk_override
        overrides_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return seeded, (workspace / "vertex-db")


def _invoke_cli_subprocess(
    workspace: Path,
    unrelated_cwd: Path,
    cli_args: list[str],
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["VERTEX_PROGRAMS_ROOT"] = str(workspace / "programs")
    env["VERTEX_DB_PATH"] = str(workspace / "vertex-db")
    env["VERTEX_NO_DOTENV"] = "1"
    runner_code = (
        "import json, pathlib, socket, sys\n"
        f"repo_root = pathlib.Path({str(REPO_ROOT)!r}).resolve()\n"
        "def guard_audit(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
        "        raise PermissionError('network blocked in R0 smoke')\n"
        "    if event == 'open' and args and isinstance(args[0], (str, bytes, pathlib.Path)):\n"
        "        mode = args[1] if len(args) > 1 else 'r'\n"
        "        if isinstance(mode, str) and any(c in mode for c in 'wax+'):\n"
        "            p = pathlib.Path(args[0]).resolve()\n"
        "            if p == repo_root or repo_root in p.parents:\n"
        "                raise PermissionError(f'checkout write blocked: {p}')\n"
        "sys.addaudithook(guard_audit)\n"
        "try:\n"
        "    socket.socket().connect(('127.0.0.1', 9))\n"
        "except PermissionError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('socket guard control failed')\n"
        "try:\n"
        "    open(repo_root / 'cli.py', 'a', encoding='utf-8')\n"
        "except PermissionError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('checkout write guard control failed')\n"
        "import cli\n"
        f"sys.argv = ['vertex', *{cli_args!r}]\n"
        "try:\n"
        "    cli.app()\n"
        "except SystemExit as exc:\n"
        "    raise SystemExit(exc.code if isinstance(exc.code, int) else 0)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", runner_code],
        cwd=str(unrelated_cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _normalize_html(html: str, workspace: Path) -> str:
    normalized = html.replace(str(workspace), "<WORKSPACE>").replace(workspace.as_posix(), "<WORKSPACE>")
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", "<ISO_TS>", normalized)
    return re.sub(r"[0-9a-f]{32,64}", "<HEX_ID>", normalized)


def test_state_a_missing_risk_blocks_qg8_unforceably_and_leaves_checkout_and_archive_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    checkout_before = _hash_checkout_tree()
    workspace, programs_root, reports_root, archive_root = _seed_workspace(tmp_path, name="state_a")
    monkeypatch.setenv("VERTEX_DB_PATH", str(workspace / "vertex-db"))

    # Due compaction sentinel in DB dir: dry-run report must NOT run scheduled compaction
    store = RealityStore("acme", db_root=workspace / "vertex-db")
    store.initialize()
    sentinel = store.db_path.parent / ".compact_last_run"
    stale_sentinel = "2020-01-01T00:00:00+00:00"
    sentinel.write_text(stale_sentinel, encoding="utf-8")

    _seed_cached_snapshot(workspace, issue=77, risk_override=None)
    assert not any(archive_root.rglob("*"))

    report_res = _invoke_cli_subprocess(
        workspace,
        unrelated_cwd,
        ["report", "--edition", EDITION_NAME, "--issue", "77", "--offline", "--no-ai", "--no-seed", "--dry-run", "--stdout"],
    )
    assert report_res.returncode == 3, report_res.stderr + report_res.stdout
    payload = json.loads(report_res.stdout)
    assert payload["qg_results"]["QG-8"] is False
    assert not list(workspace.rglob("*.projector.html"))
    incompatible = CliRunner().invoke(
        app, ["report", "--edition", EDITION_NAME, "--offline", "--no-ai", "--no-seed",
              "--dry-run", "--edition-type", "deck", "--projector-preview"],
        env={"VERTEX_PROGRAMS_ROOT": str(programs_root)},
    )
    assert incompatible.exit_code == 2
    assert "--projector-preview requires a newsletter --dry-run" in incompatible.output
    assert sentinel.read_text(encoding="utf-8") == stale_sentinel

    # --offline --as-of is rejected cleanly
    conflict_res = _invoke_cli_subprocess(
        workspace,
        unrelated_cwd,
        ["report", "--edition", EDITION_NAME, "--issue", "77", "--offline", "--as-of", "2026-11-19", "--dry-run"],
    )
    assert conflict_res.returncode != 0

    # confirm --force still blocks hard on QG-8 (exit 3, archive untouched)
    confirm_result = confirm.confirm_issue(
        EDITION_NAME,
        77,
        dry_run=True,
        force=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
    )
    assert confirm_result.exit_code == 3
    assert confirm_result.manifest.qg_results.get("QG-8") is False
    assert confirm_result.failures
    assert not any(archive_root.rglob("*"))
    assert _hash_checkout_tree() == checkout_before


@pytest.mark.parametrize(
    ("case", "milestones", "warnings", "expected_gap"),
    [
        ("empty_but_complete", (), (), "milestone_inputs_unqualified"),
        ("missing", None, ("source_missing",), "milestones_unevaluated"),
        ("failed", None, (f"source_status:{MetricQualityState.QUERY_FAILED.value}",), "milestone_input_degraded"),
        ("partial", (), (f"source_status:{MetricQualityState.PARTIAL.value}",), "milestone_input_degraded"),
        ("stale", (), (f"source_status:{MetricQualityState.STALE_SOURCE.value}",), "milestone_input_degraded"),
        ("unsupported", None, ("unsupported_provider_capability",), "milestone_input_degraded"),
    ],
)
def test_provider_double_degraded_matrix_never_lowers_risk_or_fabricates_timestamps(
    case: str,
    milestones: object,
    warnings: tuple[str, ...],
    expected_gap: str,
) -> None:
    now = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
    snapshot = Snapshot(
        issue_number=1,
        generated_at=now,
        ado_data_as_of=now,
        edition_type=EditionType.DETAILED,
        items=(SnapshotItem(1, "Task", "Synthetic", "Active", None, "Synthetic", now.date(), RiskLevel.HIGH, []),),
        scorecards=(ConfirmedDimension("Synthetic", "Lane-0", RiskLevel.HIGH, None, 1, ""),),
    )
    capture = RunEvidenceSnapshot(
        program_id="synthetic",
        fact_snapshot_id="pin",
        fact_content_sha256="a" * 64,
        as_of=now,
        valid_at=now,
        gather_run_id=None,
        gather_run_hash=None,
        source_scopes=(),
        binding_declarations=(),
        fetched_item_ids=(1,),
        report_snapshot_hash=hash_jsonable(snapshot),
        config_hash="sha256:" + "b" * 64,
        sha256="",
        effective_family_modes=(("judgment", "primary"),),
    )
    cap_payload = asdict(capture)
    cap_payload.pop("sha256")
    capture = replace(capture, sha256=hash_jsonable(cap_payload))
    observed = observe_program_verdict(
        capture,
        snapshot,
        (),
        milestones=milestones,  # type: ignore[arg-type]
        milestone_warnings=warnings,
    )
    assert observed.verdict.as_of == now
    assert observed.verdict.health not in {RiskLevel.LOW, RiskLevel.DONE}, case
    assert expected_gap in {gap.code for gap in observed.verdict.evidence_gaps}, case


def test_state_b_outlook_html_determinism_ban_list_canary_and_claim_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    normalized_htmls: list[str] = []
    snapshot_as_of = datetime.now(timezone.utc)

    for copy_name in ("copy_one", "copy_two"):
        ws, programs_root, reports_root, archive_root = _seed_workspace(tmp_path, name=copy_name)
        monkeypatch.setenv("VERTEX_DB_PATH", str(ws / "vertex-db"))
        _seed_cached_snapshot(
            ws, issue=77, risk_override="low", as_of=snapshot_as_of, projector_preview=True,
        )
        narrative_dir = get_narratives_dir(EDITION_NAME, 77, reports_root, programs_root=programs_root)
        narrative_text = "Material claim: deployment lane cleared integration gate (WI:900001).\n"
        (narrative_dir / "exec_summary.md").write_text(narrative_text, encoding="utf-8")

        res = _invoke_cli_subprocess(
            ws,
            unrelated_cwd,
            ["report", "--edition", EDITION_NAME, "--issue", "77", "--offline", "--no-ai", "--no-seed", "--dry-run", "--stdout", "--projector-preview"],
        )
        assert res.returncode in {0, 3}, res.stderr + res.stdout
        payload = json.loads(res.stdout)
        assert payload["qg_results"]["QG-8"] is True
        html_path = Path(payload["paths"]["html"])
        html = html_path.read_text(encoding="utf-8")
        assert "680px" in html or 'width="680"' in html
        assert "640px" in html or 'width="640"' in html
        assert any(font in html for font in ("Segoe UI", "Calibri", "Arial"))
        assert "WI:900001" in html
        assert "Decision W" not in html
        projector = html_path.with_suffix(".projector.html").read_text(encoding="utf-8")
        assert "Decision W" in projector
        assert "WI:900001" in projector
        editorial_rules = EditorialRules(
            schema_version="1.0",
            stale_warn_days=7,
            stale_block_days=14,
            banned_phrases=(),
            banned_openings=(),
            verbosity=VerbositySettings(None, None, None, None, None),
        )
        assert find_ban_list_violations({"html": narrative_text}, editorial_rules) == ()
        normalized_htmls.append(_normalize_html(html, ws))

        confirm_preview = confirm.confirm_issue(
            EDITION_NAME,
            77,
            dry_run=True,
            force=True,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
        )
        assert confirm_preview.exit_code == 0, confirm_preview.failures
        assert not any(archive_root.rglob("*"))

    assert normalized_htmls[0] == normalized_htmls[1]

    canary_rules = EditorialRules(
        schema_version="1.0",
        stale_warn_days=7,
        stale_block_days=14,
        banned_phrases=(),
        banned_openings=(),
        verbosity=VerbositySettings(None, None, None, None, None),
    )
    canary_violations = find_ban_list_violations(
        {"exec_summary": "Outcome stalled because of synergy_canary blockers."},
        canary_rules,
        program_banned_phrases=("synergy_canary",),
    )
    assert any(v.phrase == "synergy_canary" for v in canary_violations)


def test_state_c_pinned_verdict_parity_and_archive_fence_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, programs_root, reports_root, archive_root = _seed_workspace(tmp_path, name="state_c")
    monkeypatch.setenv("VERTEX_DB_PATH", str(workspace / "vertex-db"))
    t1 = datetime(2040, 5, 1, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(days=7)

    issue2_cap_sha256 = ""
    # Confirm two dated issues in primary edition + secondary edition lookback baseline
    for issue_num, issued_at, risk in ((1, t1, RiskLevel.MEDIUM), (2, t2, RiskLevel.HIGH)):
        snap = Snapshot(
            issue_number=issue_num,
            generated_at=issued_at,
            ado_data_as_of=issued_at,
            edition_type=EditionType.DETAILED,
            items=(SnapshotItem(1, "Task", "Synthetic", "Active", None, "Synthetic", issued_at.date(), risk, []),),
            scorecards=(ConfirmedDimension("Synthetic", "Lane-0", risk, None, 1, ""),),
        )
        cap = RunEvidenceSnapshot(
            program_id="acme",
            fact_snapshot_id=f"capture-{issue_num}",
            fact_content_sha256="a" * 64,
            as_of=issued_at,
            valid_at=issued_at,
            gather_run_id=None,
            gather_run_hash=None,
            source_scopes=(),
            binding_declarations=(),
            fetched_item_ids=(1,),
            report_snapshot_hash=hash_jsonable(snap),
            config_hash="sha256:" + "b" * 64,
            sha256="",
            effective_family_modes=(("judgment", "primary"),),
        )
        cap_payload = asdict(cap)
        cap_payload.pop("sha256")
        cap = replace(cap, sha256=hash_jsonable(cap_payload))
        if issue_num == 2:
            issue2_cap_sha256 = cap.sha256
        obs = observe_program_verdict(cap, snap, ())
        assert validate_retained_program_verdict(
            asdict(obs), capture=cap, report_snapshot_hash=cap.report_snapshot_hash
        ) == obs.inputs_sha256
        pub_dir = programs_root / "acme" / "publications" / EDITION_NAME / f"issue_{issue_num:03d}"
        pub_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = pub_dir / f"issue_{issue_num:03d}.manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "edition": EDITION_NAME,
                    "issue_number": issue_num,
                    "started_at": issued_at.isoformat(),
                    "metadata": {"program_verdict_observation": asdict(obs)},
                },
                default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v),
            ),
            encoding="utf-8",
        )
        confirmed_paths = write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_num,
            snapshot=snap,
            html_body=f"<html><body>Issue {issue_num}</body></html>",
            markdown_body=f"# Issue {issue_num}",
            manifest=RunManifest(
                manifest_id=f"m-{issue_num}",
                issue_number=issue_num,
                edition=EDITION_NAME,
                started_at=issued_at,
                ended_at=issued_at,
                config_hash="sha256:" + "b" * 64,
                snapshot_hash="sha256:" + "a" * 64,
                html_hash="sha256:" + "c" * 64,
                md_hash="sha256:" + "d" * 64,
                ado_calls=0,
                ai_calls=0,
                ai_cost_usd=0.0,
                freshness_summary={},
                qg_results={"QG-8": True},
                git_sha=None,
                metadata={"program_verdict_observation": asdict(obs)},
            ),
            archive_root=archive_root,
        )

    # Unconfirmed issue 3 draft with LOW risk must NOT override retained issue 2 HIGH verdict in cockpit
    _seed_cached_snapshot(workspace, issue=3, risk_override="low")
    retained = load_latest_program_verdict("acme", programs_root=programs_root)
    assert retained is not None
    assert retained.snapshot_id == issue2_cap_sha256
    assert retained.manifest_path == confirmed_paths.manifest_path
    assert retained.as_of == t2
    assert retained.health is RiskLevel.HIGH

    cockpit = build_cockpit_snapshot(
        "acme",
        programs_root=programs_root,
        edition_id=EDITION_NAME,
        now=t2,
    )
    assert cockpit.program_summary.overall_risk == "red"

    # Transaction fence rejection leaves confirmed archive index intact vs passing control
    index_before = confirmed_paths.index_path.read_bytes()
    snapshot_before = confirmed_paths.snapshot_path.read_bytes()
    snapshots_before = len(read_archive_index(EDITION_NAME, archive_root=archive_root).issues)
    assert snapshots_before == 2
    draft3_state = load_draft_state(EDITION_NAME, 3, programs_root=programs_root) or {}
    draft3_manifest = load_confirm_draft_manifest(
        EDITION_NAME, 3, draft_state=draft3_state, programs_root=programs_root
    )
    assert draft3_manifest.program_verdict_observation is not None
    assert draft3_manifest.program_verdict_observation["verdict"]["program_id"] == "acme"
    common_kwargs = dict(
        edition_name=EDITION_NAME,
        issue_number=3,
        reports_root=reports_root,
        programs_root=programs_root,
        program_id="acme",
        draft_state=draft3_state,
        db_root=workspace / "vertex-db",
        effective_family_modes=None,
        load_bundle_fn=lambda ed, reports_root, programs_root: load_bundle(
            ed, reports_root=reports_root, programs_root=programs_root
        ),
        hash_jsonable_fn=hash_jsonable,
        evaluate_fact_drift_fn=evaluate_program_fact_drift_from_draft,
        validate_pinned_gather_run_fn=lambda *_args, **_kwargs: (),
    )
    # Passing control under unchanged config
    archive_transaction.revalidate_evidence(
        draft_manifest=draft3_manifest,
        **common_kwargs,
    )
    # Drifted config fence rejects atomically
    with pytest.raises((ConfigError, StateError), match="configuration changed"):
        archive_transaction.revalidate_evidence(
            draft_manifest=replace(draft3_manifest, config_hash="sha256:" + "0" * 64),
            **common_kwargs,
        )
    assert confirmed_paths.index_path.read_bytes() == index_before
    assert confirmed_paths.snapshot_path.read_bytes() == snapshot_before
    assert len(read_archive_index(EDITION_NAME, archive_root=archive_root).issues) == 2
