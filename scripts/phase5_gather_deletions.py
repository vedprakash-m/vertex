"""
Phase 5: Delete old-path functions from gather.py
Functions to delete:
  - _load_program_items_from_ado
  - _load_saved_query_item_ids
  - _extract_saved_query_wiql
  - _bound_saved_query_wiql
  - _load_freshness_program_items
  - _build_odata_filter
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent

FUNCTIONS_TO_DELETE = {
    "_load_program_items_from_ado",
    "_load_saved_query_item_ids",
    "_extract_saved_query_wiql",
    "_bound_saved_query_wiql",
    "_load_freshness_program_items",
    "_build_odata_filter",
}


def run() -> None:
    src_file = ROOT / "src/commands/gather.py"
    lines = src_file.read_text(encoding="utf-8").splitlines(keepends=True)

    result: list[str] = []
    i = 0
    deleted = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Check for a top-level function def to delete
        if stripped.startswith("def "):
            fn_name = stripped[4:].split("(")[0]
            if fn_name in FUNCTIONS_TO_DELETE:
                deleted.append(fn_name)
                i += 1
                # Consume body until next top-level definition or EOF
                while i < len(lines):
                    nxt = lines[i]
                    if nxt and nxt[0] not in (" ", "\t", "\n", "\r") and (
                        nxt.startswith("def ")
                        or nxt.startswith("class ")
                        or nxt.startswith("@")
                        or nxt.startswith("_")  # module-level assignments like _FOO = ...
                        or nxt[0].isalpha()     # other top-level names
                    ):
                        break
                    i += 1
                # Remove trailing blank lines
                while result and result[-1].strip() == "":
                    result.pop()
                result.append("\n")
                continue

        result.append(line)
        i += 1

    print(f"Deleted functions: {deleted}")
    missing = FUNCTIONS_TO_DELETE - set(deleted)
    if missing:
        print(f"WARNING: Functions not found: {missing}")

    src_file.write_text("".join(result), encoding="utf-8")
    print(f"Written: {src_file}")


if __name__ == "__main__":
    run()
