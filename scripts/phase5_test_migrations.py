"""
Phase 5: Migrate test_commands_gather.py from old ADO path monkeypatches to UIL equivalents.

Changes:
1. Replace monkeypatch pairs (_load_live_program_items + _load_freshness_program_items)
   with UIL equivalents (_resolve_uil_channel_binding_for_gather + _load_ado_items_via_uil)
2. Delete 9 old-path test functions that directly call the deleted functions
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

_LIVE_TAG = '"_load_live_program_items"'
_FRESH_TAG = '"_load_freshness_program_items"'


def _extract_items(line: str) -> str:
    """
    Extract the items tuple from a monkeypatch line like:
      '    monkeypatch.setattr(gather, "_load_live_program_items",
           lambda program, workstreams, as_of, **_: ((item,), 3))'
    Returns the items portion, e.g. '(item,)' or '()'.
    """
    # Find the lambda colon
    colon_idx = line.index(":", line.index("lambda"))
    # Everything after the colon is the return expression (plus trailing '))')
    return_part = line[colon_idx + 1:].strip()
    # Strip outer ')' characters from the monkeypatch call wrapping
    # return_part looks like '((item,), 3))' or '((), 0))'
    while return_part.endswith(")") and return_part.count("(") < return_part.count(")"):
        return_part = return_part[:-1].rstrip()
    # Now return_part is like '((item,), 3)' or '((), 0)'
    # Strip outer parens
    inner = return_part.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1].strip()
    # inner is like '(item,), 3' or '(), 0' or '(item,), 2'
    # Find the last top-level comma to split off the call count
    depth = 0
    last_comma = -1
    for i, ch in enumerate(inner):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            last_comma = i
    if last_comma >= 0:
        return inner[:last_comma].strip()
    return inner


FUNCTIONS_TO_DELETE = {
    "test_load_live_program_items_merges_saved_query_ids_without_duplicates",
    "test_load_live_program_items_degrades_when_saved_query_execution_times_out",
    "test_load_freshness_program_items_skips_revision_queries",
    "test_load_live_program_items_applies_slice_contract_saved_query_filters",
    "test_load_live_program_items_chunks_work_item_batch_queries",
    "test_load_live_program_items_chunks_analytics_history_queries",
    "test_bound_saved_query_wiql_injects_date_bound_when_changeddate_only_in_order_by",
    "test_bound_saved_query_wiql_skips_date_bound_when_changeddate_in_where_clause",
    "test_bound_saved_query_wiql_injects_date_bound_when_changeddate_only_in_select",
}


def run() -> None:
    test_file = ROOT / "tests/unit/test_commands_gather.py"
    lines = test_file.read_text(encoding="utf-8").splitlines(keepends=True)

    result: list[str] = []
    i = 0
    pairs_replaced = 0
    fns_deleted = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ---- Check for a function to delete ----
        if stripped.startswith("def "):
            fn_name = stripped[4:].split("(")[0]
            if fn_name in FUNCTIONS_TO_DELETE:
                fns_deleted += 1
                i += 1
                # Skip body: consume until next top-level def/class/decorator or EOF
                while i < len(lines):
                    nxt = lines[i]
                    if nxt and nxt[0] not in (" ", "\t", "\n", "\r") and (
                        nxt.startswith("def ") or nxt.startswith("class ") or nxt.startswith("@")
                    ):
                        break
                    i += 1
                # Trim trailing blank lines added to result
                while result and result[-1].strip() == "":
                    result.pop()
                result.append("\n")
                continue

        # ---- Check for a monkeypatch pair ----
        if _LIVE_TAG in line and "monkeypatch.setattr" in line:
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if _FRESH_TAG in next_line and "monkeypatch.setattr" in next_line:
                indent = line[: len(line) - len(line.lstrip())]
                live_items = _extract_items(line)
                fresh_items = _extract_items(next_line)
                result.append(
                    f'{indent}monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())\n'
                )
                result.append(
                    f'{indent}monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ({live_items}, {fresh_items}, 0))\n'
                )
                pairs_replaced += 1
                i += 2
                continue

        result.append(line)
        i += 1

    print(f"Step 1: Replaced {pairs_replaced} monkeypatch pairs")
    print(f"Step 2: Deleted {fns_deleted} old-path test functions")

    if pairs_replaced == 0 and fns_deleted == 0:
        print("ERROR: Nothing changed — check patterns!")
        sys.exit(1)

    test_file.write_text("".join(result), encoding="utf-8")
    print(f"Written: {test_file}")


if __name__ == "__main__":
    run()
