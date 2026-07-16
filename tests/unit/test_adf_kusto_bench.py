"""ADF-W1.6: sequential Kusto benchmark script tests (fake executor -- no
live Kusto access, matching Appendix D's live-state guardrails)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("adf_kusto_bench", REPO_ROOT / "scripts" / "adf_kusto_bench.py")
adf_kusto_bench = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["adf_kusto_bench"] = adf_kusto_bench
_SPEC.loader.exec_module(adf_kusto_bench)


def _write_kpis_yaml(programs_root: Path, program_id: str, query_ids: list[str]) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version: '1.0'", "kpis:"]
    for query_id in query_ids:
        lines.extend(
            [
                f"  - id: {query_id}",
                "    cluster: https://cluster.kusto.windows.net",
                "    database: db",
                f"    kql: '{query_id} | take 1'",
                f"    section: {query_id}",
                "    render_as: table",
                "    confidence: high",
                "    validated: true",
            ]
        )
    (program_dir / "kpis.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_bench_sequential_records_per_query_timing_and_total(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_kpis_yaml(programs_root, "fixture_prog", ["q1", "q2", "q3"])

    def _executor(rendered_query) -> list[dict[str, object]]:
        return [{"id": rendered_query.id}]

    result = adf_kusto_bench.bench_sequential("fixture_prog", executor=_executor, programs_root=programs_root)

    assert result["query_count"] == 3
    assert [row["query_id"] for row in result["queries"]] == ["q1", "q2", "q3"]
    assert all(row["status"] == "ok" for row in result["queries"])
    assert all(row["row_count"] == 1 for row in result["queries"])
    assert result["total_elapsed_seconds"] >= 0.0
    assert result["mode"] == "sequential"


def test_bench_sequential_records_query_failures_without_crashing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_kpis_yaml(programs_root, "fixture_prog", ["q1", "q2"])

    def _executor(rendered_query) -> list[dict[str, object]]:
        if rendered_query.id == "q2":
            raise RuntimeError("simulated failure")
        return [{"id": rendered_query.id}]

    result = adf_kusto_bench.bench_sequential("fixture_prog", executor=_executor, programs_root=programs_root)

    statuses = {row["query_id"]: row["status"] for row in result["queries"]}
    assert statuses == {"q1": "ok", "q2": "error"}
    q2_row = next(row for row in result["queries"] if row["query_id"] == "q2")
    assert "simulated failure" in q2_row["error"]


def test_bench_sequential_on_program_with_no_queries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    result = adf_kusto_bench.bench_sequential(
        "fixture_prog_empty", executor=lambda q: [], programs_root=programs_root
    )
    assert result["query_count"] == 0
    assert result["queries"] == []
    assert result["within_candidate_budget"] is True


def test_write_bench_artifact_and_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_kpis_yaml(programs_root, "fixture_prog", ["q1"])
    result = adf_kusto_bench.bench_sequential(
        "fixture_prog", executor=lambda q: [{"a": 1}], programs_root=programs_root
    )

    output_dir = tmp_path / "baselines"
    path = adf_kusto_bench.write_bench_artifact(result, output_dir=output_dir)

    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["program_id"] == "fixture_prog"
    assert on_disk["schema_version"] == "1"
    assert on_disk["queries"][0]["query_id"] == "q1"


def test_within_candidate_budget_flag(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_kpis_yaml(programs_root, "fixture_prog", ["q1"])
    result = adf_kusto_bench.bench_sequential(
        "fixture_prog", executor=lambda q: [], programs_root=programs_root
    )
    assert result["section_8_3_3_candidate_budget_seconds"] == 180
    assert result["within_candidate_budget"] is True  # a fast fake executor is always well under 180s
