from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROGRAM_ID = "myprogram"


def _load_yaml(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ValueError(f"Expected mapping at top-level in {path}")
    return document


def _sorted_strings(values: list[str]) -> list[str]:
    return sorted(dict.fromkeys(value for value in values if value), key=str.casefold)


def build_gap_markdown(*, repo_root: Path, program_id: str) -> str:
    program_root = repo_root / "programs" / program_id
    slice_contract_path = program_root / "slice_contracts.yaml"
    kpis_path = program_root / "kpis.yaml"
    golden_queries_path = repo_root / "knowledge" / "golden_queries.yaml"
    slices_document = _load_yaml(slice_contract_path)
    kpis_document = _load_yaml(kpis_path)
    golden_document = _load_yaml(golden_queries_path) if golden_queries_path.exists() else {}

    slices = slices_document.get("slices", [])
    if not isinstance(slices, list):
        raise ValueError(f"Expected list at slices in {slice_contract_path}")
    kpis = kpis_document.get("kpis", [])
    if not isinstance(kpis, list):
        raise ValueError(f"Expected list at kpis in {kpis_path}")
    golden_queries = golden_document.get("queries", [])
    if not isinstance(golden_queries, list):
        golden_queries = []

    telemetry_query_ids = {
        str(entry.get("id") or "").strip()
        for entry in (*kpis, *golden_queries)
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    }

    anchored: list[str] = []
    waivered: list[tuple[str, str | None]] = []
    raw_gaps: list[str] = []
    missing_telemetry: list[str] = []

    for raw_slice in slices:
        if not isinstance(raw_slice, dict):
            continue
        slice_id = str(raw_slice.get("id") or "").strip()
        if not slice_id:
            continue
        source_contract = raw_slice.get("source_contract") if isinstance(raw_slice.get("source_contract"), dict) else {}
        ado = source_contract.get("ado") if isinstance(source_contract.get("ado"), dict) else {}
        telemetry = source_contract.get("telemetry") if isinstance(source_contract.get("telemetry"), dict) else {}

        saved_queries = ado.get("saved_queries") if isinstance(ado.get("saved_queries"), list) else []
        explicit_ids = ado.get("explicit_work_item_ids") if isinstance(ado.get("explicit_work_item_ids"), list) else []
        intentional_filter_only = bool(ado.get("intentional_filter_only", False))
        intentional_filter_only_expires_on = str(ado.get("intentional_filter_only_expires_on") or "").strip() or None

        if saved_queries or explicit_ids:
            anchored.append(slice_id)
        elif intentional_filter_only:
            waivered.append((slice_id, intentional_filter_only_expires_on))
        else:
            raw_gaps.append(slice_id)

        telemetry_query_id = str(telemetry.get("query_id") or "").strip()
        if telemetry_query_id and telemetry_query_id not in telemetry_query_ids:
            missing_telemetry.append(f"{slice_id} -> {telemetry_query_id}")

    anchored = _sorted_strings(anchored)
    raw_gaps = _sorted_strings(raw_gaps)
    missing_telemetry = _sorted_strings(missing_telemetry)
    waivered = sorted(waivered, key=lambda entry: entry[0].casefold())

    lines = [
        f"# {program_id.upper()} Source Contract Gap Record",
        "",
        "## Purpose",
        "",
        f"This file is generated from `programs/{program_id}/slice_contracts.yaml` and `programs/{program_id}/kpis.yaml`.",
        "Use `python cli.py doctor --edition myprogram_weekly --ids` as the operator-visible gap surface.",
        "",
        "## Current Summary",
        "",
        f"- Anchored slices: {len(anchored)}",
        f"- Raw ADO anchor gaps: {len(raw_gaps)}",
        f"- Intentional filter-only waivers: {len(waivered)}",
        f"- Missing telemetry query references: {len(missing_telemetry)}",
        "",
        "## Anchored Slices",
        "",
    ]
    if anchored:
        lines.extend(f"- `{slice_id}`" for slice_id in anchored)
    else:
        lines.append("- None")

    lines.extend(("", "## Raw ADO Anchor Gaps", ""))
    if raw_gaps:
        lines.extend(f"- `{slice_id}`" for slice_id in raw_gaps)
    else:
        lines.append("- None")

    lines.extend(("", "## Intentional Filter-Only Waivers", ""))
    if waivered:
        for slice_id, expires_on in waivered:
            expiry_suffix = f" (expires {expires_on})" if expires_on else " (no expiry recorded)"
            lines.append(f"- `{slice_id}`{expiry_suffix}")
    else:
        lines.append("- None")

    lines.extend(("", "## Telemetry Reference Check", ""))
    if missing_telemetry:
        lines.append("The following slice telemetry references do not resolve to a KPI in `programs/myprogram/kpis.yaml`:")
        lines.append("")
        lines.extend(f"- `{entry}`" for entry in missing_telemetry)
    else:
        lines.append("All slice telemetry query references resolve to entries in `programs/myprogram/kpis.yaml`.")

    lines.extend((
        "",
        "## External Correction Path",
        "",
        "One of the following must happen before the remaining raw gap set can be considered fully closed:",
        "",
        "1. Create and validate saved queries for each raw gap slice, then record those IDs in `programs/myprogram/slice_contracts.yaml`.",
        "2. Replace a slice with an explicit curated work-item set where manual ownership is the correct long-term model.",
        "3. Mark the slice as `intentional_filter_only: true` with an `intentional_filter_only_expires_on` date when the temporary waiver is deliberate.",
    ))
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Regenerate programs/<id>/source_contract_gap.md from slice contracts and KPI config.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Repository root containing the programs directory.")
    parser.add_argument("--program", default=DEFAULT_PROGRAM_ID, help="Program id to regenerate.")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    repo_root = args.repo_root.resolve()
    program_id = str(args.program).strip()
    if not program_id:
        raise SystemExit("--program must be non-empty")
    output_path = repo_root / "programs" / program_id / "source_contract_gap.md"
    output_path.write_text(build_gap_markdown(repo_root=repo_root, program_id=program_id), encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
