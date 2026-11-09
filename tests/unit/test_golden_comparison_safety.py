from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module_name",
    (
        "tests.golden.test_ai_snapshots",
        "tests.golden.test_base_email_snapshots",
        "tests.golden.test_intent_router_snapshots",
        "tests.golden.test_knowledge_context",
        "tests.golden.test_setup_preview",
    ),
)
def test_snapshot_modules_use_public_baselines(module_name: str) -> None:
    module = import_module(module_name)
    assert module.GOLDEN_DIR == Path(module.__file__).resolve().parent / "public_snapshots"


def test_knowledge_golden_fixture_uses_synthetic_provenance() -> None:
    module = import_module("tests.golden.test_knowledge_context")
    assert module._deck_ref().file_path == "fixtures/synthetic-review.pptx"
    knowledge_ref = module._kb_ref("sha256:fixture", "line:1")
    assert knowledge_ref.original_filename == "synthetic-knowledge.md"
    assert knowledge_ref.origin_path == "fixtures/synthetic-knowledge.md"
    blocker = next(
        revision for revision in module._golden_revisions()
        if revision.claim_id == "01DOMAINBLOCKER"
    )
    assert blocker.value == "synthetic-blocker"


def test_private_remediation_baseline_directory_stays_ignored() -> None:
    module = import_module("tests.golden.test_remediation_artifacts")
    assert module.GOLDEN_DIR == Path(module.__file__).resolve().parent / "snapshots"
    ignore_rules = (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "tests/golden/snapshots/" in ignore_rules


def test_remediation_update_does_not_read_existing_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = import_module("tests.golden.test_remediation_artifacts")
    monkeypatch.setattr(module, "GOLDEN_DIR", tmp_path)

    def forbidden_read(name: str) -> None:
        pytest.fail(f"An explicit update must not read the previous baseline: {name}")

    monkeypatch.setattr(module, "_load_golden", forbidden_read)
    module._compare_with_golden("baseline", "synthetic output", True)
    assert (tmp_path / "baseline.golden").read_text(encoding="utf-8") == "synthetic output"


@pytest.fixture(
    params=(
        "tests.golden.test_ai_snapshots",
        "tests.golden.test_base_email_snapshots",
        "tests.golden.test_intent_router_snapshots",
        "tests.golden.test_knowledge_context",
        "tests.golden.test_ledger_projection",
        "tests.golden.test_remediation_artifacts",
        "tests.golden.test_setup_preview",
        "tests.golden.test_synthesize_snapshots",
    ),
    ids=lambda name: name.rsplit(".", 1)[-1],
)
def comparison(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Callable[[str, bool], None], Path]:
    module = import_module(request.param)
    directory = tmp_path / "baselines"
    ledger = request.param.endswith("test_ledger_projection")
    synthesis = request.param.endswith("test_synthesize_snapshots")
    baseline = (tmp_path if synthesis else directory) / ("baseline.json" if ledger or synthesis else "baseline.golden")
    if synthesis:
        monkeypatch.setattr(module, "SNAPSHOT_PATH", baseline)
    else:
        monkeypatch.setattr(module, "GOLDEN_DIR", directory)

    def compare(actual: str, update: bool) -> None:
        if synthesis:
            module._compare_with_snapshot(actual, update)
        else:
            module._compare_with_golden(baseline if ledger else "baseline", actual, update)

    return compare, baseline


def test_missing_golden_fails_without_writing(
    comparison: tuple[Callable[[str, bool], None], Path],
) -> None:
    compare, baseline = comparison
    parent_existed = baseline.parent.exists()
    try:
        with pytest.raises(AssertionError, match=r"Missing (?:public )?golden baseline"):
            compare("synthetic output", False)
    except pytest.skip.Exception:
        pytest.fail("A missing baseline must fail, not create output and skip.")
    assert not baseline.exists()
    assert baseline.parent.exists() == parent_existed


def test_explicit_update_creates_and_replaces_golden(
    comparison: tuple[Callable[[str, bool], None], Path],
) -> None:
    compare, baseline = comparison
    try:
        compare("first\n", True)
    except pytest.skip.Exception:
        pytest.fail("An explicit update must not skip after creating the baseline.")
    assert baseline.read_text(encoding="utf-8") == "first\n"
    compare("second\n", True)
    assert baseline.read_text(encoding="utf-8") == "second\n"


def test_mismatched_golden_fails_without_replacing(
    comparison: tuple[Callable[[str, bool], None], Path],
) -> None:
    compare, baseline = comparison
    baseline.parent.mkdir(exist_ok=True)
    baseline.write_text("reviewed output\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="does not match (golden file|snapshot)"):
        compare("different output\n", False)
    assert baseline.read_text(encoding="utf-8") == "reviewed output\n"


def test_matching_golden_passes_without_replacing(
    comparison: tuple[Callable[[str, bool], None], Path],
) -> None:
    compare, baseline = comparison
    baseline.parent.mkdir(exist_ok=True)
    baseline.write_text("reviewed output\n", encoding="utf-8")
    compare("reviewed output\n", False)
    assert baseline.read_text(encoding="utf-8") == "reviewed output\n"


def test_synthesis_snapshot_keeps_legacy_newline_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = import_module("tests.golden.test_synthesize_snapshots")
    baseline = tmp_path / "synthesis.json"
    baseline.write_text("reviewed output", encoding="utf-8")
    monkeypatch.setattr(module, "SNAPSHOT_PATH", baseline)
    module._compare_with_snapshot("reviewed output\n", False)
    assert baseline.read_text(encoding="utf-8") == "reviewed output"
