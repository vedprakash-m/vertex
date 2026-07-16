"""Unit tests for the ADF-W0.10 CPK/SQLite direct-connect scanner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("adf_cpk_audit", REPO_ROOT / "scripts" / "adf_cpk_audit.py")
adf_cpk_audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["adf_cpk_audit"] = adf_cpk_audit
_SPEC.loader.exec_module(adf_cpk_audit)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_finds_direct_connect_outside_sanctioned_wrapper(tmp_path: Path) -> None:
    scan_root = tmp_path / "src"
    _write(
        scan_root / "core" / "some_store.py",
        "import sqlite3\n\n"
        "def read_all(path):\n"
        "    conn = sqlite3.connect(path)\n"
        "    return conn\n",
    )
    sites = adf_cpk_audit.scan_direct_sqlite_connects(scan_root)
    assert len(sites) == 1
    assert sites[0].enclosing_function == "read_all"
    assert sites[0].file.endswith("core/some_store.py") or sites[0].file.endswith("core\\some_store.py")


def test_scan_excludes_db_py_by_filename(tmp_path: Path) -> None:
    scan_root = tmp_path / "src"
    _write(
        scan_root / "core" / "_db.py",
        "import sqlite3\n\n"
        "def open_program_db(path):\n"
        "    return sqlite3.connect(path)\n",
    )
    sites = adf_cpk_audit.scan_direct_sqlite_connects(scan_root)
    assert sites == ()


def test_scan_excludes_sanctioned_function_name_anywhere(tmp_path: Path) -> None:
    scan_root = tmp_path / "src"
    _write(
        scan_root / "core" / "duplicate_wrapper.py",
        "import sqlite3\n\n"
        "def open_program_db_with_retry(path):\n"
        "    return sqlite3.connect(path)\n",
    )
    sites = adf_cpk_audit.scan_direct_sqlite_connects(scan_root)
    assert sites == ()


def test_scan_records_module_level_connect(tmp_path: Path) -> None:
    scan_root = tmp_path / "src"
    _write(scan_root / "commands" / "tool.py", "import sqlite3\n\nconn = sqlite3.connect('x.db')\n")
    sites = adf_cpk_audit.scan_direct_sqlite_connects(scan_root)
    assert len(sites) == 1
    assert sites[0].enclosing_function is None


def test_ordered_prerequisite_list_ranks_ledger_first(tmp_path: Path) -> None:
    scan_root = tmp_path / "src"
    _write(scan_root / "commands" / "tool.py", "import sqlite3\ndef f():\n    sqlite3.connect('x.db')\n")
    _write(scan_root / "core" / "ledger" / "store.py", "import sqlite3\ndef f():\n    sqlite3.connect('x.db')\n")
    sites = adf_cpk_audit.scan_direct_sqlite_connects(scan_root)
    ordered = adf_cpk_audit.ordered_prerequisite_list(sites)
    assert len(ordered) == 2
    assert "ledger" in ordered[0].file


def test_render_report_and_main_write_governance_doc(tmp_path: Path) -> None:
    scan_root = tmp_path / "src"
    _write(scan_root / "core" / "some_store.py", "import sqlite3\ndef f():\n    sqlite3.connect('x.db')\n")
    out_path = tmp_path / "governance" / "decisions" / "adf-cpk-dependencies.md"

    exit_code = adf_cpk_audit.main(["--scan-root", str(scan_root), "--output", str(out_path)])
    assert exit_code == 0
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "ADF-W0.10" in content
    assert "some_store.py" in content
