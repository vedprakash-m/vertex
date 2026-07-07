from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_BRANDING_RE = re.compile("KHAB" + "ARI_|khab" + "ari ", re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".j2", ".toml"}
SCAN_ROOTS = (
    REPO_ROOT / "src",
    REPO_ROOT / "templates",
    REPO_ROOT / "docs",
    REPO_ROOT / "editions",
    REPO_ROOT / "tests" / "unit",
    REPO_ROOT / "README.md",
    REPO_ROOT / "cli.py",
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "programs" / "acme" / "program.yaml",
    REPO_ROOT / "programs" / "acme" / "source_contract_gap.md",
)
EXEMPT_PATHS = {
    REPO_ROOT / "specs" / "acme-remature.md",
    REPO_ROOT / "tests" / "contracts" / "test_no_legacy_branding.py",
    REPO_ROOT / "tests" / "unit" / "test_narrative_store.py",
    REPO_ROOT / "tests" / "unit" / "test_quality_gates.py",
}


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        if root.is_file():
            files.append(root)
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if path in EXEMPT_PATHS:
                continue
            files.append(path)
    return sorted(dict.fromkeys(files))


def test_no_legacy_branding_outside_exemptions() -> None:
    offenders: list[str] = []
    for path in _iter_text_files():
        content = path.read_text(encoding="utf-8")
        if LEGACY_BRANDING_RE.search(content):
            offenders.append(str(path.relative_to(REPO_ROOT)).replace("\\", "/"))
    assert offenders == []
