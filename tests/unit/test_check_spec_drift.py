from __future__ import annotations

import datetime
import importlib.util
import json
from pathlib import Path
import sys
import textwrap

import pytest


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "check_spec_drift.py"
    spec = importlib.util.spec_from_file_location("check_spec_drift", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_file(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _build_good_repo(root: Path) -> None:
    _write_file(
        root,
        "specs/vertex-prd.md",
        """
        Vertex is for Microsoft TPM programs.
        Current supported scope: Microsoft TPM programs using the declared supported archetypes and exclusions.
        Roadmap direction: broader TPM/EM adoption outside Microsoft is not part of the current supported V-11 bar.
        The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated.
        Migration status: the unified read API and shadow-write foundation are landed and the irreversible flip to system-of-record remains pending.

        <!-- spec-posture
          WS-1: complete (2026-06-29)
        -->
        """,
    )
    _write_file(
        root,
        "specs/vertex-tech-spec.md",
        """
        Current product scope: any Microsoft TPM program within the declared supported archetypes/exclusions.
        The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated.
        F2 Shadow write: the shadow-write foundation is landed; confirm-time shadow writes remain pending.
        programs/<program>/kpis.yaml is the authoritative KPI-to-chapter binding surface via each KustoQuery.chapter value.
        """,
    )
    _write_file(
        root,
        "specs/vertex-ux-spec.md",
        """
        Scope: currently supported Microsoft TPM-program archetypes only; broader TPM/EM/global/non-ADO is roadmap, not current scope.
        **`vertex facts export/import/rebuild`** - *(planned richer command-run output; help text remains minimal)*
        **`vertex connectors poll`** - *(planned richer command-run output; help text remains minimal)*
        **`vertex rollback`** - *(planned richer command-run output; help text remains minimal)*
        `vertex propose` accepts an optional `--steering`.
        It is not yet wired into `report`.
        """,
    )
    _write_file(
        root,
        "src/core/uil_channel_flags.py",
        """
        def uil_ado_enabled() -> bool:
            return True

        def uil_channel_enabled(channel: str) -> bool:
            return False
        """,
    )
    _write_file(
        root,
        "src/commands/gather.py",
        "",
    )
    _write_file(
        root,
        "src/commands/report.py",
        """
        from __future__ import annotations

        def render_report() -> None:
            return None
        """,
    )
    _write_file(
        root,
        "src/core/models_v2.py",
        """
        class KustoQuery:
            chapter: str | None = None
        """,
    )
    _write_file(
        root,
        "src/core/html_renderer.py",
        """
        from __future__ import annotations

        def render(name: str) -> str:
            if name.endswith("digest.j2"):
                pass  # deprecated: digest.j2 will be removed after 2026-12-31.
            return name
        """,
    )
    _write_file(
        root,
        "scripts/program_onboard_progress.py",
        """
        from __future__ import annotations

        import re


        def _parse_gap_count(text: str):
            match = re.search(r"→ (\\d+) live gaps", text)
            return int(match.group(1)) if match else None


        def _parse_completion_snapshot(text: str):
            return {"Overall": "65%"}


        def _parse_phase_snapshot(text: str):
            return {"Phase 4": "100%", "Phase 9": "20%"}
        """,
    )
    _write_file(
        root,
        "scripts/program_onboard_operator_brief.py",
        """
        from __future__ import annotations


        def build_payload():
            return {
                "live_gap_count": 20,
                "phase_snapshot": {"Phase 3": "46%", "Phase 9": "20%"},
                "operator_action_sheet": [
                    {"step": "0", "action": "Weekly nudge cadence", "success_output": "cadence"},
                    {"step": "1", "action": "Close QG-24", "success_output": "mapping"},
                    {"step": "2", "action": "Resolve WS-15", "success_output": "approval"},
                    {"step": "3", "action": "Recover IDs", "success_output": "series ids"},
                    {"step": "4", "action": "Decide IcM", "success_output": "decision"},
                    {"step": "5", "action": "Resume refresh", "success_output": "resume"},
                ],
                "templates": [
                    {"title": "QG-24 ADO field request / mapping decision", "content": "x"},
                    {"title": "WS-15 scoped DPA / privacy approval", "content": "x"},
                    {"title": "GAP-002 / GAP-003 M365 identifier recovery", "content": "x"},
                    {"title": "GAP-008 / GAP-018 / GAP-020 IcM activate-vs-defer", "content": "x"},
                ],
            }


        def build_next_action(payload):
            return {
                "step": "0",
                "action": "Weekly nudge cadence",
                "success_output": "cadence",
                "suggested_template": None,
                "template_content": None,
            }
        """,
    )
    _write_file(
        root,
        "scripts/program_onboard_clean_confirm_brief.py",
        """
        from __future__ import annotations


        def build_payload():
            return {
                "active_review_backlog": {
                    "issue_number": 131,
                    "pending_count": 16,
                    "pending_sections": [],
                    "written_narrative_count": 0,
                },
                "latest_hygiene_blockers": {
                    "issue_number": 130,
                    "blocker_count": 8,
                    "blockers": [],
                },
            }
        """,
    )
    _write_file(
        root,
        "scripts/program_onboard_execution_dashboard.py",
        """
        from __future__ import annotations


        def build_payload():
            return {
                "next_external_action": {"step": "1", "action": "Close QG-24", "success_output": "ok"},
                "qg24_packet": {"title": "QG-24 ADO field request / mapping decision"},
                "review_backlog": {"issue_number": 131, "pending_count": 16},
                "owner_hygiene_backlog": {"issue_number": 130, "owner_count": 9, "owners": ["A", "B"]},
                "owner_outreach_pack": {"issue_number": 130, "owner_count": 9},
            }
        """,
    )
    _write_file(
        root,
        "specs/acme-onboard.md",
        """
        ### Current Completion Snapshot

        | Area | Completion | Status read |
        |------|------------|-------------|
        | Foundation / governance (`0`, `7`, `8`, `14`, `18`-`20`) | 82% | ok |
        | Corpus inventory + knowledge plane (`1`, `2`, `3`, `11`-`13`) | 76% | ok |
        | Active source integration (`3A`-`3E`) | 46% | ok |
        | Historical backfill (`3.5`, `4`, `5`) | 74% | ok |
        | Program model refresh + readiness (`6`, `6.0`, `7`) | 71% | ok |
        | L1 / AI / actuation / multi-altitude (`8`, `9`, `10`, `15`-`17`) | 24% | ok |
        | Overall | 68% | ok |

        ### Phase Snapshot

        | Phase | Completion | Why |
        |-------|------------|-----|
        | Phase 0 | 45% | ok |
        | Phase 1 | 67% | ok |
        | Phase 2 | 85% | ok |
        | Phase 3 | 46% | ok |
        | Phase 3.5 | 90% | ok |
        | Phase 4 | 100% | ok |
        | Phase 5 | 58% | ok |
        | Phase 6.0 | 100% | ok |
        | Phase 6 | 55% | ok |
        | Phase 7 | 85% | ok |
        | Phase 8 | 25% | ok |
        | Phase 9 | 20% | ok |
        | Phase 10 | 30% | ok |

        Canonical gap count: 23 rows, of which GAP-019 is retracted and GAP-022 is documented/closed → 21 live gaps.
        """,
    )


def test_run_checks_passes_against_repo() -> None:
    module = _load_module()
    results = module.run_checks(repo_root=Path(__file__).resolve().parents[2])
    assert results
    assert all(result.status == "pass" for result in results)


def test_run_checks_flags_stale_section_catalog_claim(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_file(
        tmp_path,
        "specs/vertex-tech-spec.md",
        """
        Current product scope: any Microsoft TPM program within the declared supported archetypes/exclusions.
        The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated.
        F2 Shadow write: the shadow-write foundation is landed; confirm-time shadow writes remain pending.
        src/core/section_catalog.py is the authoritative KPI section registry.
        """,
    )

    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p7-section-catalog"].status == "fail"
    assert "vertex-tech-spec.md still contains the deleted section_catalog authority claim" in by_id["p7-section-catalog"].detail


def test_main_json_exits_nonzero_for_report_steering_drift(tmp_path: Path, capsys) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_file(
        tmp_path,
        "specs/vertex-ux-spec.md",
        """
        Scope: currently supported Microsoft TPM-program archetypes only; broader TPM/EM/global/non-ADO is roadmap, not current scope.
        **`vertex facts export/import/rebuild`** - *(planned richer command-run output; help text remains minimal)*
        **`vertex connectors poll`** - *(planned richer command-run output; help text remains minimal)*
        **`vertex rollback`** - *(planned richer command-run output; help text remains minimal)*
        `vertex report --dry-run` accepts an optional `--steering`.
        """,
    )

    exit_code = module.main(["--repo-root", str(tmp_path), "--format", "json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    assert payload["overall"] == "fail"
    assert any(result["check_id"] == "p7-steering-surface" for result in payload["results"])


# ---------------------------------------------------------------------------
# Adversarial tests (fix-data-flow.md Track G / PR-2 item 4): each of these
# feeds a check the exact contradiction it exists to catch and asserts it
# actually fails. A check that always returns "pass" would satisfy every
# other test in this file without these — see the spec's own meta-lesson
# that a checker's negative-space behavior needs its own test, not just
# confirmation it passes against good input.
# ---------------------------------------------------------------------------


def test_check_fact_store_shadow_write_fails_when_prd_omits_migration_status(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_file(
        tmp_path,
        "specs/vertex-prd.md",
        """
        Vertex is for Microsoft TPM programs.
        Current supported scope: Microsoft TPM programs using the declared supported archetypes and exclusions.
        Roadmap direction: broader TPM/EM adoption outside Microsoft is not part of the current supported V-11 bar.
        The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated.

        <!-- spec-posture
          WS-1: complete (2026-06-29)
        -->
        """,
    )

    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p7-fact-store"].status == "fail"
    assert "does not describe shadow-write-landed" in by_id["p7-fact-store"].detail


def test_check_posture_block_fails_when_block_missing(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_file(
        tmp_path,
        "specs/vertex-prd.md",
        """
        Vertex is for Microsoft TPM programs.
        Current supported scope: Microsoft TPM programs using the declared supported archetypes and exclusions.
        Roadmap direction: broader TPM/EM adoption outside Microsoft is not part of the current supported V-11 bar.
        The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated.
        Migration status: the unified read API and shadow-write foundation are landed and the irreversible flip to system-of-record remains pending.
        """,
    )

    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12-posture-block"].status == "fail"
    assert "no `<!-- spec-posture" in by_id["p12-posture-block"].detail


def test_check_posture_block_fails_on_ws1_contradiction(tmp_path: Path) -> None:
    """The exact contradiction this check exists to catch (spec §6.7 PS-5):
    the changelog says WS-1 is complete, but the posture block still says
    deferred."""
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_file(
        tmp_path,
        "specs/vertex-prd.md",
        """
        Vertex is for Microsoft TPM programs.
        Current supported scope: Microsoft TPM programs using the declared supported archetypes and exclusions.
        Roadmap direction: broader TPM/EM adoption outside Microsoft is not part of the current supported V-11 bar.
        The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated.
        Migration status: the unified read API and shadow-write foundation are landed and the irreversible flip to system-of-record remains pending.

        <!-- spec-posture
          WS-1: deferred
        -->
        """,
    )

    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12-posture-block"].status == "fail"
    assert "contradiction this check exists to catch" in by_id["p12-posture-block"].detail


def test_check_posture_block_fails_on_malformed_line(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_file(
        tmp_path,
        "specs/vertex-prd.md",
        """
        Vertex is for Microsoft TPM programs.
        Current supported scope: Microsoft TPM programs using the declared supported archetypes and exclusions.
        Roadmap direction: broader TPM/EM adoption outside Microsoft is not part of the current supported V-11 bar.
        The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated.
        Migration status: the unified read API and shadow-write foundation are landed and the irreversible flip to system-of-record remains pending.

        <!-- spec-posture
          WS-1: complete (2026-06-29)
          this line is not a valid posture entry
        -->
        """,
    )

    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12-posture-block"].status == "fail"
    assert "unparseable posture line" in by_id["p12-posture-block"].detail


def _write_posture_prd(root: Path, posture_lines: str) -> None:
    _write_file(
        root,
        "specs/vertex-prd.md",
        f"""
        Vertex is for Microsoft TPM programs.
        Current supported scope: Microsoft TPM programs using the declared supported archetypes and exclusions.
        Roadmap direction: broader TPM/EM adoption outside Microsoft is not part of the current supported V-11 bar.
        The ADO UIL gather path is now default-on; Kusto, Teams, and IcM remain env-gated.
        Migration status: the unified read API and shadow-write foundation are landed and the irreversible flip to system-of-record remains pending.

        <!-- spec-posture
          WS-1: complete (2026-06-29)
        {posture_lines}
        -->
        """,
    )


def test_check_posture_backlog_reconciliation_passes_when_bklg_absent(tmp_path: Path) -> None:
    """specs/bklg.md is a tracked-but-derived file; a checkout that hasn't
    synced it yet must not fail this check."""
    module = _load_module()
    _build_good_repo(tmp_path)
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12b-posture-backlog-reconciliation"].status == "pass"


def test_check_posture_backlog_reconciliation_passes_with_matching_heading_and_lifecycle(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_posture_prd(tmp_path, "  BL-X1: in-progress (2026-07-22)")
    _write_file(
        tmp_path,
        "specs/bklg.md",
        """
        | Item | § | Lifecycle | Pri | Accountable | Next action |
        |---|---|---|---|---|---|
        | BL-X1 | §1 | `actionable` | P1 | Someone | Do the thing |

        ### BL-X1 — Some work item
        Prose.
        """,
    )
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12b-posture-backlog-reconciliation"].status == "pass"


def test_check_posture_backlog_reconciliation_fails_on_missing_heading(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_posture_prd(tmp_path, "  BL-X1: in-progress (2026-07-22)")
    _write_file(
        tmp_path,
        "specs/bklg.md",
        """
        | Item | § | Lifecycle | Pri | Accountable | Next action |
        |---|---|---|---|---|---|
        | BL-X1 | §1 | `actionable` | P1 | Someone | Do the thing |
        """,
    )
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12b-posture-backlog-reconciliation"].status == "fail"
    assert "no `### BL-X1` heading" in by_id["p12b-posture-backlog-reconciliation"].detail


def test_check_posture_backlog_reconciliation_passes_with_no_backlog_row_annotation(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_posture_prd(tmp_path, "  BL-X1: in-progress (2026-07-22) [no-backlog-row: tracked under BL-X2]")
    _write_file(
        tmp_path,
        "specs/bklg.md",
        """
        | Item | § | Lifecycle | Pri | Accountable | Next action |
        |---|---|---|---|---|---|
        """,
    )
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12b-posture-backlog-reconciliation"].status == "pass"


def test_check_posture_backlog_reconciliation_fails_on_status_contradiction(tmp_path: Path) -> None:
    """The exact contradiction BL-K1 step 5 exists to catch: bklg.md's own
    Status-at-a-glance table says an item is `done`, but the posture block
    still declares it `deferred`."""
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_posture_prd(tmp_path, "  BL-X1: deferred (2026-07-22)")
    _write_file(
        tmp_path,
        "specs/bklg.md",
        """
        | Item | § | Lifecycle | Pri | Accountable | Next action |
        |---|---|---|---|---|---|
        | BL-X1 | §1 | `done` | — | — | Shipped |

        ### BL-X1 — Some work item
        Prose.
        """,
    )
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12b-posture-backlog-reconciliation"].status == "fail"
    assert "is `done`" in by_id["p12b-posture-backlog-reconciliation"].detail
    assert "posture declares it 'deferred'" in by_id["p12b-posture-backlog-reconciliation"].detail


def test_check_posture_backlog_reconciliation_fails_when_open_item_missing_from_posture(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_posture_prd(tmp_path, "")
    _write_file(
        tmp_path,
        "specs/bklg.md",
        """
        | Item | § | Lifecycle | Pri | Accountable | Next action |
        |---|---|---|---|---|---|
        | BL-X1 | §1 | `actionable` | P1 | Someone | Do the thing |

        ### BL-X1 — Some work item
        Prose.
        """,
    )
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12b-posture-backlog-reconciliation"].status == "fail"
    assert "no entry in the PRD's spec-posture block" in by_id["p12b-posture-backlog-reconciliation"].detail


def test_check_backlog_table_heading_parity_passes_when_bklg_absent(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12c-backlog-table-heading-parity"].status == "pass"


def test_check_backlog_table_heading_parity_fails_on_heading_with_no_row(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_file(
        tmp_path,
        "specs/bklg.md",
        """
        | Item | § | Lifecycle | Pri | Accountable | Next action |
        |---|---|---|---|---|---|

        ### BL-X1 — Some work item with no table row
        Prose.
        """,
    )
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12c-backlog-table-heading-parity"].status == "fail"
    assert "heading(s) with no Status-at-a-glance table row: BL-X1" in by_id["p12c-backlog-table-heading-parity"].detail


def test_check_backlog_table_heading_parity_fails_on_row_with_no_heading(tmp_path: Path) -> None:
    module = _load_module()
    _build_good_repo(tmp_path)
    _write_file(
        tmp_path,
        "specs/bklg.md",
        """
        | Item | § | Lifecycle | Pri | Accountable | Next action |
        |---|---|---|---|---|---|
        | BL-X1 | §1 | `actionable` | P1 | Someone | Orphan row, no heading |
        """,
    )
    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p12c-backlog-table-heading-parity"].status == "fail"
    assert "row(s) with no matching ### heading: BL-X1" in by_id["p12c-backlog-table-heading-parity"].detail


def test_check_digest_sunset_fails_after_sunset_when_shim_still_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the date-gated sunset enforcement actually fires: if
    `digest.j2`'s deprecation shim is still present after the stated
    2026-12-31 sunset, the check must fail, not silently pass forever."""
    module = _load_module()
    _build_good_repo(tmp_path)

    class _FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2027, 1, 15)

    monkeypatch.setattr(datetime, "date", _FrozenDate)

    results = module.run_checks(repo_root=tmp_path)
    by_id = {result.check_id: result for result in results}
    assert by_id["p13-digest-sunset"].status == "fail"
    assert "still present after sunset" in by_id["p13-digest-sunset"].detail


