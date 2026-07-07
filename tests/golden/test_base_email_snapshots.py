from __future__ import annotations

import difflib
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"


class GoldenFileMismatchError(AssertionError):
    pass


def _load_golden(name: str) -> str | None:
    golden_path = GOLDEN_DIR / f"{name}.golden"
    if golden_path.exists():
        return golden_path.read_text(encoding="utf-8")
    return None


def _save_golden(name: str, content: str) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / f"{name}.golden").write_text(content, encoding="utf-8")


def _compare_with_golden(name: str, actual: str, update: bool) -> None:
    golden = _load_golden(name)
    if update or golden is None:
        _save_golden(name, actual)
        if golden is None:
            pytest.skip(f"Created new golden file: {name}.golden")
        return

    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.golden",
                tofile="actual",
            )
        )
        raise GoldenFileMismatchError(
            f"Output does not match golden file: {name}.golden\n\nDiff:\n{diff}"
        )


@pytest.fixture
def email_environment(repo_root: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(repo_root / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )


def test_base_email_template_snapshot(email_environment: Environment, update_golden: bool) -> None:
    template = email_environment.get_template("base.email.j2")
    rendered = template.render(
        title="Program Hygiene | Issue 77 | May 12, 2026",
        preheader="Focused edition highlighting readiness deltas.",
        subtitle="~4 min read | Detailed Edition | Data as of May 12 09:00 PT",
        content_html="<p style=\"margin:0 0 12px 0;\">Newsletter body preview.</p>",
        show_footer=True,
        footer_text="Issue 77 | Generated May 12, 2026",
    )
    _compare_with_golden("base_email_template", rendered, update_golden)


def test_base_email_template_outlook_constraints(email_environment: Environment) -> None:
    template = email_environment.get_template("base.email.j2")
    rendered = template.render(
        title="Constraint Check",
        preheader="Constraint preview",
        subtitle="Constraint subtitle",
        content_html="<p style=\"margin:0;\">Constraint body.</p>",
        footer_text="Constraint footer",
    )

    assert '<style' not in rendered.lower()
    assert 'font-family:Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif;' in rendered
    assert 'width="680"' in rendered
    assert 'width="640"' in rendered
    assert 'cellpadding="0" cellspacing="0"' in rendered
