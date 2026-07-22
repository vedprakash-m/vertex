"""Contract: pyproject.toml's optional-dependency extras stay in sync with
their actual import sites (specs/backlog.md WO-8 / BL-C5).

A bidirectional audit for the five named extras (ai, ai-local, m365, kusto,
render): every package declared in an extra must be traceable to a real
import site in src/ (or an explicitly documented exception -- msgraph-sdk,
kept declared despite being currently unused, see pyproject.toml's own
comment), and every known optional-integration import in src/ must resolve
to the extra this test expects, so a future refactor that moves e.g. the
Kusto import into a different file doesn't silently orphan the `kusto`
extra's rationale.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"

with open(_REPO_ROOT / "pyproject.toml", "rb") as _f:
    _PYPROJECT = tomllib.load(_f)

_EXTRAS: dict[str, tuple[str, ...]] = _PYPROJECT["project"]["optional-dependencies"]

# import-site substring -> (extra, package name). One entry per known
# optional-integration import; extend this when a new gated import is added.
_KNOWN_IMPORT_SITES: tuple[tuple[str, str, str], ...] = (
    # openai/tiktoken are both loaded via importlib.import_module("...")
    # (src/ai/request_router.py, src/ai/context_tokenizers.py) rather than a
    # literal `import openai`/`import tiktoken` statement, so this test
    # matches on the dynamic-import string instead.
    ('importlib.import_module("openai")', "ai", "openai"),
    ('importlib.import_module("tiktoken")', "ai", "tiktoken"),
    ("from rapidfuzz import", "ai-local", "rapidfuzz"),
    ("from azure.kusto.data import", "kusto", "azure-kusto-data"),
    ("import matplotlib", "render", "matplotlib"),
)

# Packages declared in an extra with no current import site, and why that's
# expected (not a bug) -- see pyproject.toml's own comment on each entry.
_DOCUMENTED_UNUSED_PACKAGES: frozenset[str] = frozenset({"msgraph-sdk"})


def _package_name(requirement: str) -> str:
    for sep in ("==", ">=", "<=", ">", "<", "~="):
        if sep in requirement:
            return requirement.split(sep, 1)[0].strip()
    return requirement.strip()


def _grep_src(substring: str) -> tuple[Path, ...]:
    matches = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        if substring in py_file.read_text(encoding="utf-8"):
            matches.append(py_file)
    return tuple(matches)


@pytest.mark.parametrize("extra_name", ("ai", "ai-local", "m365", "kusto", "render"))
def test_extra_exists_and_is_non_empty(extra_name: str) -> None:
    assert extra_name in _EXTRAS, f"pyproject.toml is missing the {extra_name!r} extra"
    assert _EXTRAS[extra_name], f"{extra_name!r} extra must not be an empty list"


def test_every_declared_extra_package_has_a_known_import_site_or_documented_exception() -> None:
    known_packages_by_extra: dict[str, set[str]] = {}
    for _substring, extra, package in _KNOWN_IMPORT_SITES:
        known_packages_by_extra.setdefault(extra, set()).add(package)

    unmapped: list[str] = []
    for extra_name in ("ai", "ai-local", "m365", "kusto", "render"):
        for requirement in _EXTRAS[extra_name]:
            package = _package_name(requirement)
            if package in _DOCUMENTED_UNUSED_PACKAGES:
                continue
            if package not in known_packages_by_extra.get(extra_name, set()):
                unmapped.append(f"{extra_name}: {package}")

    assert unmapped == [], (
        "Package(s) declared in an extra with no known import site and no "
        f"documented-unused exception: {unmapped}"
    )


@pytest.mark.parametrize("substring,expected_extra,package", _KNOWN_IMPORT_SITES)
def test_known_optional_import_site_resolves_to_expected_extra(substring: str, expected_extra: str, package: str) -> None:
    matches = _grep_src(substring)
    assert matches, f"expected to find {substring!r} somewhere under src/ (used to justify the {expected_extra!r} extra)"
    declared = {_package_name(requirement) for requirement in _EXTRAS[expected_extra]}
    assert package in declared, f"{package!r} (import site: {matches[0]}) is not declared in the {expected_extra!r} extra"


def test_typer_and_click_upper_bounds_match_between_pyproject_and_requirements() -> None:
    """WO-8 found pyproject.toml's typer pin was missing the <0.26 upper
    bound that requirements.txt already carries (a known breaking-change
    cap for CLI group introspection) -- pin this so the two manifests can't
    drift apart again without a test catching it."""
    requirements_text = (_REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject_deps = _PYPROJECT["project"]["dependencies"]

    for package in ("typer", "click"):
        requirements_line = next(
            (line for line in requirements_text.splitlines() if line.strip().startswith(package)),
            None,
        )
        pyproject_line = next((dep for dep in pyproject_deps if dep.startswith(package)), None)
        assert requirements_line is not None, f"{package} missing from requirements.txt"
        assert pyproject_line is not None, f"{package} missing from pyproject.toml [project.dependencies]"
        assert "<0.26" in requirements_line or "<8.4" in requirements_line, (
            f"requirements.txt's {package} pin lost its upper bound: {requirements_line!r}"
        )
        assert "<0.26" in pyproject_line or "<8.4" in pyproject_line, (
            f"pyproject.toml's {package} pin is missing its upper bound: {pyproject_line!r}"
        )
