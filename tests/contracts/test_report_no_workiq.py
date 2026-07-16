"""ADF-W1.5 (Section 8.3.2 / INV-ADF-2): report never performs WorkIQ NL
discovery inline.

Two checks:

1. Static (AST): ``src/commands/report.py`` and
   ``src/commands/report_pipeline/*.py`` must not import any module that can
   issue a live WorkIQ NL call (``src.m365.agency_bridge`` and the
   ``src.m365.workiq_*``/``src.m365.discovery.workiq_pipeline`` family, plus
   ``src.commands.enrich`` -- the orchestrator that wires the live bridge).
   Reading *already-collected* WorkIQ signals (e.g.
   ``src.core.leakage_detector.load_approved_workiq_signals``, a Zone-A
   store read) or evaluating a budget gate over them is explicitly fine --
   only the live-call modules are forbidden.
2. Behavioral: a program still configured with the legacy
   ``workiq_enrich_schedule: pre_report`` must not trigger a live call from
   ``report.py::_maybe_auto_run_workiq_enrich``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Modules that can issue a live WorkIQ NL call. report-path files may not
#: import these (directly or via ``from ... import``).
_FORBIDDEN_LIVE_CALL_MODULES = (
    "src.m365.agency_bridge",
    "src.m365.workiq_ask_support",
    "src.m365.workiq_calendar_discovery",
    "src.m365.workiq_mail_discovery",
    "src.m365.workiq_retriever",
    "src.m365.discovery.workiq_pipeline",
    "src.commands.enrich",
)

_REPORT_PATH_FILES = (
    REPO_ROOT / "src" / "commands" / "report.py",
    *sorted((REPO_ROOT / "src" / "commands" / "report_pipeline").glob("*.py")),
    # The render/validation/milestone stages the report pipeline runs
    # through (Section 8.3.2 covers "report" broadly, not only these two
    # anchor files) -- defense-in-depth, not just the literal Appendix C anchors.
    *sorted((REPO_ROOT / "src" / "core" / "stages").glob("*.py")),
)


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_report_path_never_imports_a_live_workiq_call_site() -> None:
    violations: list[str] = []
    for path in _REPORT_PATH_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        for forbidden in _FORBIDDEN_LIVE_CALL_MODULES:
            hit = {module for module in imported if module == forbidden or module.startswith(forbidden + ".")}
            if hit:
                violations.append(f"{path.relative_to(REPO_ROOT)}: imports {sorted(hit)} (forbidden: {forbidden})")
    assert violations == [], "report-path files must never import a live WorkIQ call site:\n" + "\n".join(violations)


def test_report_path_files_exist_and_were_actually_scanned() -> None:
    # Guards against the scan silently covering zero files (e.g. a rename).
    assert len(_REPORT_PATH_FILES) >= 8
    for path in _REPORT_PATH_FILES:
        assert path.exists(), path


def test_legacy_pre_report_schedule_never_triggers_a_live_call(monkeypatch, tmp_path: Path) -> None:
    from src.commands import report as report_module
    from src.core.models_v2 import M365Config

    monkeypatch.setattr(report_module, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        report_module,
        "resolve_edition",
        lambda edition_name, programs_root=None: SimpleNamespace(
            program=SimpleNamespace(
                id="xpf",
                m365=M365Config(enabled=True, prefer_agency=True, workiq_enrich_schedule="pre_report"),
            )
        ),
    )
    monkeypatch.setattr(
        "src.commands.enrich.enrich_command",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError(f"unexpected live WorkIQ enrich call: {kwargs}")),
    )

    report_module._maybe_auto_run_workiq_enrich(
        edition_name="xpf_weekly",
        dry_run=True,
        offline=False,
        show_progress=False,
    )

    # ADF-W5.8: ensure the enrichment blocker also produced its own alert,
    # not just avoided the forbidden live call.
    from src.core.alerts import read_alerts

    alerts = read_alerts("xpf", programs_root=tmp_path)
    assert len(alerts) == 1
    assert alerts[0].category == "workiq_inline_invocation_attempted"
