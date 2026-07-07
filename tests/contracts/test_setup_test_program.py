"""WS-13 PB-34: bootstrap test program contract."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.setup_test_program import main as bootstrap_main


def _make_template(templates_root: Path, name: str) -> None:
    tdir = templates_root / "_templates" / name
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "program.yaml").write_text("schema_version: '3.0'\nid: TEMPLATE\n", encoding="utf-8")
    (tdir / "edition.yaml").write_text("schema_version: '2.0'\n", encoding="utf-8")
    (tdir / "subdir").mkdir()
    (tdir / "subdir" / "notes.md").write_text("# notes\n", encoding="utf-8")


def test_bootstrap_copies_template_to_program(tmp_path: Path) -> None:
    templates = tmp_path / "programs"
    _make_template(templates, "example_tpm")
    rc = bootstrap_main(
        [
            "--template", "example_tpm",
            "--program", "acme",
            "--programs-root", str(templates),
        ]
    )
    assert rc == 0
    assert (templates / "acme" / "program.yaml").exists()
    assert (templates / "acme" / "edition.yaml").exists()
    assert (templates / "acme" / "subdir" / "notes.md").exists()
    assert (templates / "acme" / ".bootstrapped").exists()


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    templates = tmp_path / "programs"
    _make_template(templates, "example_tpm")
    assert bootstrap_main([
        "--template", "example_tpm", "--program", "acme",
        "--programs-root", str(templates),
    ]) == 0
    # Second run with the marker present should short-circuit (rc=0, no copy).
    assert bootstrap_main([
        "--template", "example_tpm", "--program", "acme",
        "--programs-root", str(templates),
    ]) == 0


def test_bootstrap_refuses_overwrite_without_force(tmp_path: Path) -> None:
    templates = tmp_path / "programs"
    _make_template(templates, "example_tpm")
    target = templates / "acme"
    target.mkdir(parents=True)
    (target / "preexisting.txt").write_text("x", encoding="utf-8")
    rc = bootstrap_main([
        "--template", "example_tpm", "--program", "acme",
        "--programs-root", str(templates),
    ])
    assert rc == 2
    assert (target / "preexisting.txt").exists()
    # The marker was NOT written (we refused).
    assert not (target / ".bootstrapped").exists()


def test_bootstrap_force_overwrites(tmp_path: Path) -> None:
    templates = tmp_path / "programs"
    _make_template(templates, "example_tpm")
    target = templates / "acme"
    target.mkdir(parents=True)
    (target / "preexisting.txt").write_text("x", encoding="utf-8")
    rc = bootstrap_main([
        "--template", "example_tpm", "--program", "acme",
        "--programs-root", str(templates),
        "--force",
    ])
    assert rc == 0
    assert (target / "program.yaml").exists()
    assert (target / ".bootstrapped").exists()
    assert not (target / "preexisting.txt").exists()
