from __future__ import annotations

from functools import lru_cache
from html import unescape
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.core.models_v2 import EngMsPage


_ENGMS_MAX_SUMMARY_CHARS = 280
_ENGMS_FETCH_TIMEOUT_SECONDS = 5
_ENGMS_ALLOWED_HOST = "eng.ms"


class _EngMsRedirectGuard(HTTPRedirectHandler):
    """Refuse to follow redirects that leave the eng.ms host.

    The stock urlopen transparently follows redirects, so an off-host post-fetch
    check happens only *after* the outbound request was already made — too late to
    stop an SSRF. This handler validates each redirect target before the request is
    issued and aborts the chain when it points off eng.ms.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if (urlsplit(newurl).hostname or "").lower() != _ENGMS_ALLOWED_HOST:
            raise URLError(f"eng.ms SSRF guard: blocked off-host redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Default opener enforces the redirect guard. Exposed as module-level ``urlopen`` so the
# call site (and tests) bind the same name; build_opener swaps in our redirect subclass.
urlopen = build_opener(_EngMsRedirectGuard()).open


def summarize_engms_page(page: EngMsPage, *, timeout_seconds: int = _ENGMS_FETCH_TIMEOUT_SECONDS) -> str:
    description = _normalize_summary_text(page.description)
    fetched_summary = fetch_engms_page_summary(page.url, timeout_seconds=timeout_seconds)
    if description is not None and fetched_summary is not None:
        if _summary_text_equivalent(description, fetched_summary):
            return description if len(description) >= len(fetched_summary) else fetched_summary
        return _truncate_summary_text(f"{description} {fetched_summary}")
    if fetched_summary is not None:
        return fetched_summary
    if description is not None:
        return description
    return "eng.ms reference document"


@lru_cache(maxsize=64)
def fetch_engms_page_summary(url: str, *, timeout_seconds: int = _ENGMS_FETCH_TIMEOUT_SECONDS) -> str | None:
    if (urlsplit(url).hostname or "").lower() != _ENGMS_ALLOWED_HOST:
        return None
    request = Request(url, headers={"User-Agent": "Vertex/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            # Defense-in-depth: the redirect guard already blocks off-host hops before
            # they are followed; this re-checks the final resolved URL just in case.
            if (urlsplit(response.geturl()).hostname or "").lower() != _ENGMS_ALLOWED_HOST:
                return None
            content_type = _response_content_type(response)
            if content_type is not None and content_type not in {"text/html", "application/xhtml+xml"}:
                return None
            html_text = _decode_response_body(response.read(200000), content_type_header=response.headers.get("Content-Type"))
    except (HTTPError, URLError, OSError, ValueError):
        return None
    return _extract_summary_from_html(html_text)


def _response_content_type(response) -> str | None:
    content_type = response.headers.get("Content-Type")
    if not isinstance(content_type, str):
        return None
    normalized = content_type.split(";", 1)[0].strip().lower()
    return normalized or None


def _decode_response_body(payload: bytes, *, content_type_header: str | None) -> str:
    charset = None
    if isinstance(content_type_header, str):
        match = re.search(r"charset=([a-zA-Z0-9_-]+)", content_type_header, flags=re.IGNORECASE)
        if match is not None:
            charset = match.group(1).strip()
    for encoding in (charset, "utf-8", "utf-16", "latin-1"):
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="ignore")


def _extract_summary_from_html(html_text: str) -> str | None:
    meta_description_match = re.search(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if meta_description_match is not None:
        normalized = _normalize_summary_text(meta_description_match.group(1))
        if normalized is not None:
            return normalized

    paragraph_match = re.search(r"<p\b[^>]*>(.*?)</p>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if paragraph_match is not None:
        normalized = _normalize_summary_text(_strip_html(paragraph_match.group(1)))
        if normalized is not None:
            return normalized

    title_match = re.search(r"<title\b[^>]*>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if title_match is not None:
        normalized = _normalize_summary_text(_strip_html(title_match.group(1)))
        if normalized is not None:
            return normalized
    return None


def _strip_html(value: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_styles)
    return unescape(without_tags)


def _normalize_summary_text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(_strip_html(value).split())
    if not normalized:
        return None
    return _truncate_summary_text(normalized)


def _truncate_summary_text(value: str) -> str:
    if len(value) <= _ENGMS_MAX_SUMMARY_CHARS:
        return value
    truncated = value[: _ENGMS_MAX_SUMMARY_CHARS - 3].rstrip(" ,.;:")
    return f"{truncated}..."


def _summary_text_equivalent(left: str, right: str) -> bool:
    normalize = lambda value: re.sub(r"\s+", " ", value.strip().lower())
    return normalize(left) == normalize(right)