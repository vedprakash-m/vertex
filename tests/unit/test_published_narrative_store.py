from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.commands.published_baseline import run_published_baseline_import
from src.core.eml_writer import build_eml_bytes
from src.core.narrative_store import get_narratives_dir, load_narratives, seed_narratives_from_prior
from src.core.published_narrative_store import load_published_narratives, prepare_published_narratives, write_published_narratives


EDITION_NAME = "acme_weekly"
SECTION_ID = "acme-adventure-xio-100-ramp-readiness-deployment-velocity"


def test_prepare_published_narratives_extracts_exec_summary_and_sections(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    (edition_root / "html").mkdir(parents=True, exist_ok=True)
    (edition_root / "eml").mkdir(parents=True, exist_ok=True)
    generated_html_path = edition_root / "html" / "issue_077.html"
    published_eml_path = edition_root / "eml" / "issue_077.published.eml"
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 77,
                        "generated_at": "2026-05-15T19:53:04+00:00",
                        "kind": "confirmed",
                        "html_path": str(generated_html_path),
                        "metadata": {"published_eml_path": str(published_eml_path)},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    generated_html_path.write_text(
        """
<html><body>
<a id="exec-summary"></a>
<table><tr><td style="padding:10px 0 8px 0">Executive Summary</td></tr><tr><td style="padding:0 0 12px 0">Generated summary.</td></tr></table>
<a id="acme-adventure-xio-100-ramp-readiness-deployment-velocity"></a>
<table>
  <tr><td style="font-size:16px; line-height:1.4; font-weight:600; color:#111827; vertical-align:middle;">Deployment Velocity</td></tr>
  <tr><td style="padding:0 0 12px 0; font-size:14px; line-height:1.6; color:#374151;">Generated section.</td></tr>
</table>
</body></html>
""".strip(),
        encoding="utf-8",
    )
    published_html = f"""
<html><body>
<a href="#{SECTION_ID}">Depl Vel</a>
<table>
  <tr><td><div class="elementToProof" style="line-height: 1.4; font-size: 12px;"><span style="font-weight: 600;">Executive Summary</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(17, 24, 39);"><p>Published summary paragraph.</p></td></tr>
</table>
<table>
  <tr><td style="line-height: 1.4; vertical-align: middle; color: rgb(17, 24, 39);"><div class="elementToProof" style="line-height: 1.4; font-size: 16px;"><span style="font-weight: 600;">Deployment Velocity</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(55, 65, 81);"><p>Published velocity paragraph.</p><p>Second sentence with <a href="https://example.com/item">reference</a>.</p></td></tr>
</table>
</body></html>
""".strip()
    published_eml_path.write_bytes(
        build_eml_bytes(
            to=("acme_newsletter@example.com",),
            cc=(),
            subject="Program Hygiene | Issue 77 | 2026-05-15",
            html_body=published_html,
            text_body="Published summary paragraph.\n\nPublished velocity paragraph.",
            from_display_name="Vertex Maintainer",
            from_email="maintainer@example.com",
            generated_at=datetime(2026, 5, 15, 19, 53, 4, tzinfo=timezone.utc),
            mark_as_draft=False,
        )
    )

    prepared = prepare_published_narratives(EDITION_NAME, 77, archive_root=archive_root)
    files = {file.filename: file.content for file in prepared.files}

    assert files["exec_summary.md"] == "Published summary paragraph."
    assert files[f"ws_{SECTION_ID}.md"] == "Published velocity paragraph.\n\nSecond sentence with [reference](https://example.com/item)."


def test_prepare_published_narratives_preserves_basic_bold_and_italic_formatting(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    (edition_root / "html").mkdir(parents=True, exist_ok=True)
    (edition_root / "eml").mkdir(parents=True, exist_ok=True)
    generated_html_path = edition_root / "html" / "issue_077.html"
    published_eml_path = edition_root / "eml" / "issue_077.published.eml"
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 77,
                        "generated_at": "2026-05-15T19:53:04+00:00",
                        "kind": "confirmed",
                        "html_path": str(generated_html_path),
                        "metadata": {"published_eml_path": str(published_eml_path)},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    generated_html_path.write_text(
        f"""
<html><body>
<a id="exec-summary"></a>
<table><tr><td style="padding:10px 0 8px 0">Executive Summary</td></tr><tr><td style="padding:0 0 12px 0">Generated summary.</td></tr></table>
<a id="{SECTION_ID}"></a>
<table>
  <tr><td style="font-size:16px; line-height:1.4; font-weight:600; color:#111827; vertical-align:middle;">Deployment Velocity</td></tr>
  <tr><td style="padding:0 0 12px 0; font-size:14px; line-height:1.6; color:#374151;">Generated section.</td></tr>
</table>
</body></html>
""".strip(),
        encoding="utf-8",
    )
    published_html = f"""
<html><body>
<a href="#{SECTION_ID}">Depl Vel</a>
<table>
  <tr><td><div class="elementToProof" style="line-height: 1.4; font-size: 12px;"><span style="font-weight: 600;">Executive Summary</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(17, 24, 39);"><p>Proposal is to <strong>resume ramp on June 1</strong> with <em>close monitoring</em>.</p></td></tr>
</table>
<table>
  <tr><td style="line-height: 1.4; vertical-align: middle; color: rgb(17, 24, 39);"><div class="elementToProof" style="line-height: 1.4; font-size: 16px;"><span style="font-weight: 600;">Deployment Velocity</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(55, 65, 81);"><p><strong>Velocity</strong> improved with <em>follow-through</em>.</p></td></tr>
</table>
</body></html>
""".strip()
    published_eml_path.write_bytes(
        build_eml_bytes(
            to=("acme_newsletter@example.com",),
            cc=(),
            subject="Program Hygiene | Issue 77 | 2026-05-15",
            html_body=published_html,
            text_body="Proposal is to resume ramp on June 1 with close monitoring.\n\nVelocity improved with follow-through.",
            from_display_name="Vertex Maintainer",
            from_email="maintainer@example.com",
            generated_at=datetime(2026, 5, 15, 19, 53, 4, tzinfo=timezone.utc),
            mark_as_draft=False,
        )
    )

    prepared = prepare_published_narratives(EDITION_NAME, 77, archive_root=archive_root)
    files = {file.filename: file.content for file in prepared.files}

    assert files["exec_summary.md"] == "Proposal is to __resume ramp on June 1__ with _close monitoring_."
    assert files[f"ws_{SECTION_ID}.md"] == "__Velocity__ improved with _follow-through_."


def test_prepare_published_narratives_finds_headings_even_when_link_order_differs_from_body(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    (edition_root / "html").mkdir(parents=True, exist_ok=True)
    (edition_root / "eml").mkdir(parents=True, exist_ok=True)
    generated_html_path = edition_root / "html" / "issue_077.html"
    published_eml_path = edition_root / "eml" / "issue_077.published.eml"
    section_a = "acme-adventure-xio-100-ramp-readiness-deployment-velocity"
    section_b = "contoso-pilot-readiness-buildout"
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 77,
                        "generated_at": "2026-05-15T19:53:04+00:00",
                        "kind": "confirmed",
                        "html_path": str(generated_html_path),
                        "metadata": {"published_eml_path": str(published_eml_path)},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    generated_html_path.write_text(
        f"""
<html><body>
<a id="{section_a}"></a>
<table>
  <tr><td style="font-size:16px; line-height:1.4; font-weight:600; color:#111827; vertical-align:middle;">Deployment Velocity</td></tr>
  <tr><td style="padding:0 0 12px 0; font-size:14px; line-height:1.6; color:#374151;">Generated A.</td></tr>
</table>
<a id="{section_b}"></a>
<table>
  <tr><td style="font-size:16px; line-height:1.4; font-weight:600; color:#111827; vertical-align:middle;">Buildout</td></tr>
  <tr><td style="padding:0 0 12px 0; font-size:14px; line-height:1.6; color:#374151;">Generated B.</td></tr>
</table>
</body></html>
""".strip(),
        encoding="utf-8",
    )
    published_html = f"""
<html><body>
<a href="#{section_b}">Buildout</a>
<a href="#{section_a}">Deployment Velocity</a>
<table>
  <tr><td style="line-height: 1.4; vertical-align: middle; color: rgb(17, 24, 39);"><div class="elementToProof" style="line-height: 1.4; font-size: 16px;"><span style="font-weight: 600;">Deployment Velocity</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(55, 65, 81);"><p>Published A.</p></td></tr>
</table>
<table>
  <tr><td style="line-height: 1.4; vertical-align: middle; color: rgb(17, 24, 39);"><div class="elementToProof" style="line-height: 1.4; font-size: 16px;"><span style="font-weight: 600;">Buildout</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(55, 65, 81);"><p>Published B.</p></td></tr>
</table>
</body></html>
""".strip()
    published_eml_path.write_bytes(
        build_eml_bytes(
            to=("acme_newsletter@example.com",),
            cc=(),
            subject="Program Hygiene | Issue 77 | 2026-05-15",
            html_body=published_html,
            text_body="Published A.\n\nPublished B.",
            from_display_name="Vertex Maintainer",
            from_email="maintainer@example.com",
            generated_at=datetime(2026, 5, 15, 19, 53, 4, tzinfo=timezone.utc),
            mark_as_draft=False,
        )
    )

    prepared = prepare_published_narratives(EDITION_NAME, 77, archive_root=archive_root)
    files = {file.filename: file.content for file in prepared.files}

    assert files[f"ws_{section_a}.md"] == "Published A."
    assert files[f"ws_{section_b}.md"] == "Published B."


def test_seed_narratives_from_prior_prefers_published_bundle_with_archive_fallback(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    archive_dir = archive_root / EDITION_NAME / "narratives" / "issue_001"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "exec_summary.md").write_text("Generated summary.\n", encoding="utf-8")
    (archive_dir / f"ws_{SECTION_ID}.md").write_text("Generated section.\n", encoding="utf-8")
    published_dir = archive_root / EDITION_NAME / "published_narratives" / "issue_001"
    published_dir.mkdir(parents=True, exist_ok=True)
    (published_dir / "exec_summary.md").write_text("Published summary.\n", encoding="utf-8")

    state = seed_narratives_from_prior(
        EDITION_NAME,
        target_issue_number=2,
        source_issue_number=1,
        valid_filenames={"exec_summary.md", f"ws_{SECTION_ID}.md"},
        removed_section_ids=set(),
        reports_root=reports_root,
        archive_root=archive_root,
    )

    narratives = load_narratives(EDITION_NAME, 2, reports_root=reports_root)

    assert state is not None
    assert state.source_path == "published_archive_preferred"
    assert "Published summary." in narratives["exec_summary.md"]
    assert "Generated section." in narratives[f"ws_{SECTION_ID}.md"]


def test_run_published_baseline_import_only_applies_to_unchanged_target_files(tmp_path: Path) -> None:
    repo_root = tmp_path
    reports_root = repo_root / "reports"
    archive_root = repo_root / "archive"
    edition_root = archive_root / EDITION_NAME
    (edition_root / "html").mkdir(parents=True, exist_ok=True)
    (edition_root / "eml").mkdir(parents=True, exist_ok=True)
    (edition_root / "narratives" / "issue_077").mkdir(parents=True, exist_ok=True)
    generated_html_path = edition_root / "html" / "issue_077.html"
    published_eml_path = edition_root / "eml" / "issue_077.published.eml"
    (edition_root / "narratives" / "issue_077" / "exec_summary.md").write_text("Generated summary.\n", encoding="utf-8")
    (edition_root / "narratives" / "issue_077" / f"ws_{SECTION_ID}.md").write_text("Generated section.\n", encoding="utf-8")
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 77,
                        "generated_at": "2026-05-15T19:53:04+00:00",
                        "kind": "confirmed",
                        "html_path": str(generated_html_path),
                        "metadata": {"published_eml_path": str(published_eml_path)},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    generated_html_path.write_text(
        """
<html><body>
<a id="exec-summary"></a>
<table><tr><td style="padding:10px 0 8px 0">Executive Summary</td></tr><tr><td style="padding:0 0 12px 0">Generated summary.</td></tr></table>
<a id="acme-adventure-xio-100-ramp-readiness-deployment-velocity"></a>
<table>
  <tr><td style="font-size:16px; line-height:1.4; font-weight:600; color:#111827; vertical-align:middle;">Deployment Velocity</td></tr>
  <tr><td style="padding:0 0 12px 0; font-size:14px; line-height:1.6; color:#374151;">Generated section.</td></tr>
</table>
</body></html>
""".strip(),
        encoding="utf-8",
    )
    published_html = f"""
<html><body>
<a href="#{SECTION_ID}">Depl Vel</a>
<table>
  <tr><td><div class="elementToProof" style="line-height: 1.4; font-size: 12px;"><span style="font-weight: 600;">Executive Summary</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(17, 24, 39);"><p>Published summary paragraph.</p></td></tr>
</table>
<table>
  <tr><td style="line-height: 1.4; vertical-align: middle; color: rgb(17, 24, 39);"><div class="elementToProof" style="line-height: 1.4; font-size: 16px;"><span style="font-weight: 600;">Deployment Velocity</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(55, 65, 81);"><p>Published velocity paragraph.</p></td></tr>
</table>
</body></html>
""".strip()
    published_eml_path.write_bytes(
        build_eml_bytes(
            to=("acme_newsletter@example.com",),
            cc=(),
            subject="Program Hygiene | Issue 77 | 2026-05-15",
            html_body=published_html,
            text_body="Published summary paragraph.\n\nPublished velocity paragraph.",
            from_display_name="Vertex Maintainer",
            from_email="maintainer@example.com",
            generated_at=datetime(2026, 5, 15, 19, 53, 4, tzinfo=timezone.utc),
            mark_as_draft=False,
        )
    )

    target_dir = get_narratives_dir(EDITION_NAME, 78, reports_root=reports_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "exec_summary.md").write_text("Generated summary.\n", encoding="utf-8")
    (target_dir / f"ws_{SECTION_ID}.md").write_text("Author edited section.\n", encoding="utf-8")

    result = run_published_baseline_import(
        edition_name=EDITION_NAME,
        issue_number=77,
        published_eml_path=None,
        target_issue_number=78,
        write=True,
        reports_root=reports_root,
        archive_root=archive_root,
    )

    imported = load_published_narratives(EDITION_NAME, 77, archive_root=archive_root)
    assert result.bundle_dir is not None
    assert imported["exec_summary.md"] == "Published summary paragraph.\n"
    assert result.apply_result is not None
    assert result.apply_result.applied_files == ("exec_summary.md",)
    assert result.apply_result.skipped_files == (f"ws_{SECTION_ID}.md",)
    assert "Published summary paragraph." in (target_dir / "exec_summary.md").read_text(encoding="utf-8")
    assert (target_dir / f"ws_{SECTION_ID}.md").read_text(encoding="utf-8") == "Author edited section.\n"


def test_run_published_baseline_import_updates_archive_metadata_and_applies_archive_fallback(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    (edition_root / "html").mkdir(parents=True, exist_ok=True)
    (edition_root / "eml").mkdir(parents=True, exist_ok=True)
    (edition_root / "narratives" / "issue_077").mkdir(parents=True, exist_ok=True)
    generated_html_path = edition_root / "html" / "issue_077.html"
    published_eml_path = edition_root / "eml" / "issue_077.published.eml"
    fallback_section_id = "contoso-pilot-readiness-buildout"
    (edition_root / "narratives" / "issue_077" / "exec_summary.md").write_text("Generated summary.\n", encoding="utf-8")
    (edition_root / "narratives" / "issue_077" / f"ws_{SECTION_ID}.md").write_text("Generated section.\n", encoding="utf-8")
    (edition_root / "narratives" / "issue_077" / f"ws_{fallback_section_id}.md").write_text("Generated fallback section.\n", encoding="utf-8")
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 77,
                        "generated_at": "2026-05-15T19:53:04+00:00",
                        "kind": "confirmed",
                        "html_path": str(generated_html_path),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    generated_html_path.write_text(
        f"""
<html><body>
<a id="exec-summary"></a>
<table><tr><td style="padding:10px 0 8px 0">Executive Summary</td></tr><tr><td style="padding:0 0 12px 0">Generated summary.</td></tr></table>
<a id="{SECTION_ID}"></a>
<table>
  <tr><td style="font-size:16px; line-height:1.4; font-weight:600; color:#111827; vertical-align:middle;">Deployment Velocity</td></tr>
  <tr><td style="padding:0 0 12px 0; font-size:14px; line-height:1.6; color:#374151;">Generated section.</td></tr>
</table>
<a id="{fallback_section_id}"></a>
<table>
  <tr><td style="font-size:16px; line-height:1.4; font-weight:600; color:#111827; vertical-align:middle;">Buildout</td></tr>
  <tr><td style="padding:0 0 12px 0; font-size:14px; line-height:1.6; color:#374151;">Generated fallback section.</td></tr>
</table>
</body></html>
""".strip(),
        encoding="utf-8",
    )
    published_html = f"""
<html><body>
<a href="#{SECTION_ID}">Depl Vel</a>
<table>
  <tr><td><div class="elementToProof" style="line-height: 1.4; font-size: 12px;"><span style="font-weight: 600;">Executive Summary</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(17, 24, 39);"><p>Published summary paragraph.</p></td></tr>
</table>
<table>
  <tr><td style="line-height: 1.4; vertical-align: middle; color: rgb(17, 24, 39);"><div class="elementToProof" style="line-height: 1.4; font-size: 16px;"><span style="font-weight: 600;">Deployment Velocity</span></div></td></tr>
  <tr><td style="line-height: 1.6; padding-bottom: 12px; color: rgb(55, 65, 81);"><p>Published velocity paragraph.</p></td></tr>
</table>
</body></html>
""".strip()
    published_eml_path.write_bytes(
        build_eml_bytes(
            to=("acme_newsletter@example.com",),
            cc=(),
            subject="Program Hygiene | Issue 77 | 2026-05-15",
            html_body=published_html,
            text_body="Published summary paragraph.\n\nPublished velocity paragraph.",
            from_display_name="Vertex Maintainer",
            from_email="maintainer@example.com",
            generated_at=datetime(2026, 5, 15, 19, 53, 4, tzinfo=timezone.utc),
            mark_as_draft=False,
        )
    )

    target_dir = get_narratives_dir(EDITION_NAME, 78, reports_root=reports_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "exec_summary.md").write_text("Generated summary.\n", encoding="utf-8")
    (target_dir / f"ws_{SECTION_ID}.md").write_text("Generated section.\n", encoding="utf-8")
    (target_dir / f"ws_{fallback_section_id}.md").write_text("Generated fallback section.\n", encoding="utf-8")

    result = run_published_baseline_import(
        edition_name=EDITION_NAME,
        issue_number=77,
        published_eml_path=published_eml_path,
        target_issue_number=78,
        write=True,
        reports_root=reports_root,
        archive_root=archive_root,
    )

    index_payload = json.loads((edition_root / "index.json").read_text(encoding="utf-8"))
    seeding_manifest = json.loads((target_dir / ".seeding_manifest.json").read_text(encoding="utf-8"))

    assert result.apply_result is not None
    assert result.apply_result.applied_files == (
        "exec_summary.md",
        f"ws_{SECTION_ID}.md",
        f"ws_{fallback_section_id}.md",
    )
    assert result.apply_result.skipped_files == ()
    assert index_payload["issues"][0]["metadata"]["published_eml_path"] == str(published_eml_path)
    assert seeding_manifest["source_path"] == "published_archive_preferred"
    assert seeding_manifest["files"][f"ws_{SECTION_ID}.md"]["source_path"] == "published_archive"
    assert seeding_manifest["files"][f"ws_{fallback_section_id}.md"]["source_path"] == "archive"
    assert "Published velocity paragraph." in (target_dir / f"ws_{SECTION_ID}.md").read_text(encoding="utf-8")
    assert "Generated fallback section." in (target_dir / f"ws_{fallback_section_id}.md").read_text(encoding="utf-8")