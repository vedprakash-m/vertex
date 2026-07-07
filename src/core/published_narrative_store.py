from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from html import escape, unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from src.core.archive_store import read_archive_index
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root


PUBLISHED_NARRATIVES_DIRNAME = "published_narratives"
PUBLISHED_NARRATIVE_MANIFEST_FILENAME = "manifest.json"
_SECTION_LINK_RE = re.compile(r'href="#([a-z0-9\-]+)"', re.IGNORECASE)
_SECTION_ANCHOR_RE = re.compile(r'<a\s+id="([^"]+)"></a>', re.IGNORECASE)
_GENERATED_TITLE_RE = re.compile(r'font-size:16px;[^>]*>(?P<title>.*?)</td>', re.IGNORECASE | re.DOTALL)
_BODY_CELL_PATTERNS = (
    re.compile(r'<td[^>]*padding-bottom:\s*12px[^>]*>(?P<body>.*?)</td>\s*</tr>', re.IGNORECASE | re.DOTALL),
    re.compile(
        r'<td[^>]*padding:\s*0(?:px)?\s+0(?:px)?\s+12px\s+0(?:px)?[^>]*>(?P<body>.*?)</td>\s*</tr>',
        re.IGNORECASE | re.DOTALL,
    ),
)
_SCRIPT_STYLE_RE = re.compile(r'<(script|style)\b.*?</\1>', re.IGNORECASE | re.DOTALL)
_AUXILIARY_BLOCK_RE = re.compile(
    r'<div\b[^>]*>.*?(ADO Findings|Downstream Dependency Impact).*?</div>',
    re.IGNORECASE | re.DOTALL,
)
_NARRATIVE_WARNING_RE = re.compile(r'<p\b[^>]*>[^<]*Narrative missing[^<]*</p>', re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True, slots=True)
class PublishedNarrativeFile:
    filename: str
    section_id: str
    title: str
    content: str
    source_hash: str


@dataclass(frozen=True, slots=True)
class PreparedPublishedNarratives:
    edition: str
    issue_number: int
    published_eml_path: Path
    generated_html_path: Path
    files: tuple[PublishedNarrativeFile, ...]
    warnings: tuple[str, ...]


def get_published_narratives_dir(
    edition: str,
    issue_number: int,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> Path:
    return get_archive_root(edition, archive_root) / PUBLISHED_NARRATIVES_DIRNAME / f"issue_{issue_number:03d}"


def load_published_narratives(
    edition: str,
    issue_number: int,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> dict[str, str]:
    directory = get_published_narratives_dir(edition, issue_number, archive_root=archive_root)
    if not directory.exists():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.md"))
    }


def prepare_published_narratives(
    edition: str,
    issue_number: int,
    *,
    published_eml_path: Path | None = None,
    generated_html_path: Path | None = None,
    archive_root: Path = ARCHIVE_ROOT,
) -> PreparedPublishedNarratives:
    resolved_eml_path = published_eml_path or _resolve_published_eml_path(edition, issue_number, archive_root=archive_root)
    if resolved_eml_path is None or not resolved_eml_path.exists():
        raise FileNotFoundError(f"Published EML not found for {edition} issue {issue_number:03d}.")

    resolved_generated_html_path = generated_html_path or _resolve_generated_html_path(
        edition,
        issue_number,
        archive_root=archive_root,
    )
    if resolved_generated_html_path is None or not resolved_generated_html_path.exists():
        raise FileNotFoundError(f"Generated HTML not found for {edition} issue {issue_number:03d}.")

    generated_html = resolved_generated_html_path.read_text(encoding="utf-8")
    published_html = _extract_eml_part(resolved_eml_path, "text/html")
    if published_html is None:
        raise ValueError(f"Published EML {resolved_eml_path} does not contain an HTML body.")

    warnings: list[str] = []
    section_ids = _extract_section_ids_from_published_html(published_html)
    if not section_ids:
        raise ValueError(f"Published EML {resolved_eml_path} does not contain any section links.")

    title_map = _extract_generated_title_map(generated_html, section_ids)
    files: list[PublishedNarrativeFile] = []

    exec_summary_body, exec_summary_end = _extract_exec_summary_body(published_html)
    if exec_summary_body is None:
        warnings.append("Executive Summary could not be extracted from the published HTML.")
    else:
        exec_summary_content = _html_fragment_to_markdown(_strip_auxiliary_blocks(exec_summary_body))
        if exec_summary_content:
            files.append(
                PublishedNarrativeFile(
                    filename="exec_summary.md",
                    section_id="exec_summary",
                    title="Executive Summary",
                    content=exec_summary_content,
                    source_hash=_hash_text(exec_summary_content),
                )
            )
        else:
            warnings.append("Executive Summary was found in the published HTML but rendered empty after normalization.")

    heading_positions: list[tuple[str, str, int]] = []
    for section_id in section_ids:
        title = title_map.get(section_id)
        if title is None:
            warnings.append(f"Generated title not found for section {section_id}.")
            continue
        position = _find_published_title_position(published_html, title)
        if position < 0:
            warnings.append(f"Published section heading not found for {section_id} ({title}).")
            continue
        heading_positions.append((section_id, title, position))

    heading_positions.sort(key=lambda item: item[2])

    for index, (section_id, title, position) in enumerate(heading_positions):
        next_position = heading_positions[index + 1][2] if index + 1 < len(heading_positions) else len(published_html)
        body_html = _extract_body_from_section_chunk(published_html[position:next_position])
        if body_html is None:
            warnings.append(f"Published body not found for section {section_id} ({title}).")
            continue
        content = _html_fragment_to_markdown(_strip_auxiliary_blocks(body_html))
        if not content:
            warnings.append(f"Published body for section {section_id} ({title}) normalized to empty content.")
            continue
        files.append(
            PublishedNarrativeFile(
                filename=f"ws_{section_id}.md",
                section_id=section_id,
                title=title,
                content=content,
                source_hash=_hash_text(content),
            )
        )

    if not files:
        raise ValueError(f"Published EML {resolved_eml_path} did not yield any importable narratives.")

    return PreparedPublishedNarratives(
        edition=edition,
        issue_number=issue_number,
        published_eml_path=resolved_eml_path,
        generated_html_path=resolved_generated_html_path,
        files=tuple(files),
        warnings=tuple(warnings),
    )


def write_published_narratives(
    prepared: PreparedPublishedNarratives,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[Path, Path]:
    directory = get_published_narratives_dir(
        prepared.edition,
        prepared.issue_number,
        archive_root=archive_root,
    )
    directory.mkdir(parents=True, exist_ok=True)
    for file in prepared.files:
        (directory / file.filename).write_text(_normalize_markdown(file.content), encoding="utf-8")

    manifest_path = directory / PUBLISHED_NARRATIVE_MANIFEST_FILENAME
    manifest_payload = {
        "schema_version": "1.0",
        "edition": prepared.edition,
        "issue_number": prepared.issue_number,
        "published_eml_path": str(prepared.published_eml_path),
        "generated_html_path": str(prepared.generated_html_path),
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "files": {
            file.filename: {
                "section_id": file.section_id,
                "title": file.title,
                "source_hash": file.source_hash,
                "source_path": "published_eml",
            }
            for file in prepared.files
        },
        "warnings": list(prepared.warnings),
    }
    manifest_path.write_text(json.dumps(manifest_payload, indent=2) + "\n", encoding="utf-8")
    return directory, manifest_path


def _resolve_generated_html_path(
    edition: str,
    issue_number: int,
    *,
    archive_root: Path,
) -> Path | None:
    entry = _find_issue_entry(edition, issue_number, archive_root=archive_root)
    if entry is not None and entry.html_path is not None:
        return Path(entry.html_path)
    conventional_path = get_archive_root(edition, archive_root) / "html" / f"issue_{issue_number:03d}.html"
    if conventional_path.exists():
        return conventional_path
    return None


def _resolve_published_eml_path(
    edition: str,
    issue_number: int,
    *,
    archive_root: Path,
) -> Path | None:
    entry = _find_issue_entry(edition, issue_number, archive_root=archive_root)
    if entry is not None and entry.metadata is not None:
        published_eml_value = entry.metadata.get("published_eml_path")
        if isinstance(published_eml_value, str) and published_eml_value.strip():
            return Path(published_eml_value)
    conventional_path = get_archive_root(edition, archive_root) / "eml" / f"issue_{issue_number:03d}.published.eml"
    if conventional_path.exists():
        return conventional_path
    return None


def _find_issue_entry(
    edition: str,
    issue_number: int,
    *,
    archive_root: Path,
):
    index = read_archive_index(edition, archive_root=archive_root)
    for entry in index.issues:
        if entry.issue_number == issue_number:
            return entry
    return None


def _extract_eml_part(path: Path, content_type: str) -> str | None:
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    for part in message.walk():
        if part.get_content_type() == content_type:
            content = part.get_content()
            if isinstance(content, str):
                return content
    return None


def _extract_section_ids_from_published_html(html: str) -> tuple[str, ...]:
    section_ids: list[str] = []
    for match in _SECTION_LINK_RE.finditer(html):
        section_id = match.group(1).strip().lower()
        if not section_id:
            continue
        if section_id not in section_ids:
            section_ids.append(section_id)
    return tuple(section_ids)


def _extract_generated_title_map(generated_html: str, section_ids: tuple[str, ...]) -> dict[str, str]:
    anchors = [
        (match.start(), match.group(1))
        for match in _SECTION_ANCHOR_RE.finditer(generated_html)
        if match.group(1) in section_ids
    ]
    title_map: dict[str, str] = {}
    for index, (position, section_id) in enumerate(anchors):
        end = anchors[index + 1][0] if index + 1 < len(anchors) else len(generated_html)
        chunk = generated_html[position:end]
        title_match = _GENERATED_TITLE_RE.search(chunk)
        if title_match is None:
            continue
        title = _strip_tags(title_match.group("title"))
        if title:
            title_map[section_id] = title
    return title_map


def _extract_exec_summary_body(html: str) -> tuple[str | None, int]:
    heading_index = html.find("Executive Summary")
    if heading_index < 0:
        return None, 0
    tail = html[heading_index:]
    for pattern in _BODY_CELL_PATTERNS:
        match = pattern.search(tail)
        if match is not None:
            return match.group("body"), heading_index + match.end()
    return None, heading_index


def _find_published_title_position(html: str, title: str) -> int:
    variants = [title, escape(title, quote=False), title.replace("&", "&amp;")]
    seen: set[str] = set()
    for variant in variants:
        if variant in seen:
            continue
        seen.add(variant)
        index = html.find(variant)
        while index >= 0:
            window = html[max(0, index - 240):index]
            if "font-size: 16px" in window or "font-size:16px" in window:
                return index
            index = html.find(variant, index + 1)
    return -1


def _extract_body_from_section_chunk(chunk: str) -> str | None:
    for pattern in _BODY_CELL_PATTERNS:
        match = pattern.search(chunk)
        if match is not None:
            return match.group("body")
    return None


def _strip_auxiliary_blocks(fragment: str) -> str:
    cleaned = _SCRIPT_STYLE_RE.sub("", fragment)
    cleaned = _AUXILIARY_BLOCK_RE.sub("", cleaned)
    cleaned = _NARRATIVE_WARNING_RE.sub("", cleaned)
    return cleaned


def _strip_tags(value: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _hash_text(value: str) -> str:
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def _normalize_markdown(content: str) -> str:
    normalized = content.rstrip()
    return f"{normalized}\n" if normalized else ""


class _MarkdownExtractor(HTMLParser):
    _BLOCK_TAGS = {"p", "div", "ul", "ol", "li", "section", "table", "tr", "td", "br"}
    _INLINE_FORMAT_MARKERS = {
        "strong": "__",
        "b": "__",
        "em": "_",
        "i": "_",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._list_depth = 0
        self._active_link_hrefs: list[str | None] = []
        self._active_link_text: list[list[str]] = []
        self._active_format_markers: list[str] = []
        self._suppress_space_before_next_text = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style"}:
            self._skip_depth += 1
            return
        if normalized in self._INLINE_FORMAT_MARKERS:
            marker = self._INLINE_FORMAT_MARKERS[normalized]
            if self._parts and not self._parts[-1].endswith(("\n", " ")):
                self._parts.append(" ")
            self._parts.append(marker)
            self._active_format_markers.append(marker)
            self._suppress_space_before_next_text = True
            return
        if normalized == "a":
            href = next((value for key, value in attrs if key.lower() == "href"), None)
            self._active_link_hrefs.append(href)
            self._active_link_text.append([])
            return
        if normalized in {"ul", "ol"}:
            self._list_depth += 1
            self._append_break(2)
            return
        if normalized == "li":
            self._append_break(1)
            self._parts.append("- ")
            return
        if normalized in self._BLOCK_TAGS:
            self._append_break(2 if normalized in {"p", "div", "section"} else 1)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if normalized in self._INLINE_FORMAT_MARKERS:
            marker = self._active_format_markers.pop() if self._active_format_markers else self._INLINE_FORMAT_MARKERS[normalized]
            self._parts.append(marker)
            return
        if normalized == "a":
            href = self._active_link_hrefs.pop() if self._active_link_hrefs else None
            text_parts = self._active_link_text.pop() if self._active_link_text else []
            text = " ".join(part for part in text_parts if part).strip()
            if not text:
                return
            self._append_text(f"[{text}]({href})" if href else text)
            return
        if normalized in {"ul", "ol"}:
            self._list_depth = max(0, self._list_depth - 1)
            self._append_break(2)
            return
        if normalized in {"li", "p", "div", "section"}:
            self._append_break(2 if normalized in {"p", "div", "section"} else 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._active_link_text:
            self._active_link_text[-1].append(text)
            return
        self._append_text(text)

    def render(self) -> str:
        output = "".join(self._parts)
        output = re.sub(r"[ \t]+\n", "\n", output)
        output = re.sub(r"\n{3,}", "\n\n", output)
        rendered_lines: list[str] = []
        for line in output.splitlines():
            normalized = re.sub(r"\s+([.,;:!?])", r"\1", line.strip())
            if not normalized:
                if rendered_lines and rendered_lines[-1] != "":
                    rendered_lines.append("")
                continue
            rendered_lines.append(normalized)
        return "\n".join(rendered_lines).strip()

    def _append_break(self, count: int) -> None:
        if not self._parts:
            return
        existing = 0
        for part in reversed(self._parts):
            if part != "\n":
                break
            existing += 1
        while existing < count:
            self._parts.append("\n")
            existing += 1

    def _append_text(self, text: str) -> None:
        if self._parts and not self._parts[-1].endswith(("\n", " ")) and not self._suppress_space_before_next_text:
            self._parts.append(" ")
        self._parts.append(text)
        self._suppress_space_before_next_text = False


def _html_fragment_to_markdown(fragment: str) -> str:
    parser = _MarkdownExtractor()
    parser.feed(fragment)
    parser.close()
    return parser.render()