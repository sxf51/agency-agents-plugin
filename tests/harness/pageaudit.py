"""Static checks on a plugin's dashboard pages.

A page fails quietly. The host serves it inside a sandboxed iframe with a strict
CSP, so a blocked script produces a blank box and no error the author will ever
see. Everything here is something that looks fine in an editor and does not work
in the dashboard.

The dynamic half is `main.py serve`, which opens the page in a real browser
against the plugin's real endpoints.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Mirrors src/extension/plugin_governance.py.
MAX_PAGE_FILE_COUNT = 200
MAX_PAGE_TOTAL_BYTES = 8 * 1024 * 1024
BRIDGE_ASSET_NAME = "__bridge.js"
PAGE_ENTRY_FILE = "index.html"
AUDITED_SUFFIXES = frozenset({".html", ".htm", ".css", ".js"})

# src=/href= or url() pointing at another host, which the page CSP refuses.
EXTERNAL_ASSET = re.compile(
    r"""(?:src|href)\s*=\s*["']((?:https?:)?//[^"']+)["']|url\(\s*["']?((?:https?:)?//[^)"']+)""",
    re.IGNORECASE,
)
# A local asset the entry document pulls in.
LOCAL_ASSET = re.compile(r"""(?:src|href)\s*=\s*["'](?!https?:|//|data:|#)([^"']+)["']""", re.IGNORECASE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
HEAD_OPEN = re.compile(r"<head[\s>]", re.IGNORECASE)
# Every bridge method whose first argument names one of the plugin's endpoints.
BRIDGE_ENDPOINT_CALL = re.compile(
    r"\b(apiGet|apiPost|apiPut|apiDelete|dataUrl|download|upload|subscribeSSE)\(\s*['\"]([^'\"]+)['\"]"
)
# Which HTTP method each of those implies, for matching against the route table.
BRIDGE_METHODS = {
    "apiGet": "GET",
    "apiPost": "POST",
    "apiPut": "PUT",
    "apiDelete": "DELETE",
    "dataUrl": "GET",
    "download": "GET",
    "upload": "POST",
    "subscribeSSE": "GET",
}


def page_directories(plugin_dir: Path) -> list[Path]:
    """Every `pages/<name>/` the plugin ships, declared or not."""
    pages_root = plugin_dir / "pages"
    if not pages_root.is_dir():
        return []
    return sorted(path for path in pages_root.iterdir() if path.is_dir() and not path.name.startswith("."))


def page_files(page_dir: Path) -> list[Path]:
    return sorted(path for path in page_dir.rglob("*") if path.is_file())


def bridge_calls(page_dir: Path) -> list[tuple[str, str, str, int]]:
    """Endpoints the page's own scripts call: (file, method, endpoint, line)."""
    calls: list[tuple[str, str, str, int]] = []
    for path in page_files(page_dir):
        if path.suffix.lower() not in {".js", ".html", ".htm"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in BRIDGE_ENDPOINT_CALL.finditer(text):
            line = text[: match.start()].count("\n") + 1
            calls.append((path.name, BRIDGE_METHODS[match.group(1)], match.group(2), line))
    return calls


def external_references(page_dir: Path) -> list[tuple[str, str, int]]:
    """Off-origin assets the CSP will block: (file, url, line)."""
    found: list[tuple[str, str, int]] = []
    for path in page_files(page_dir):
        if path.suffix.lower() not in AUDITED_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in EXTERNAL_ASSET.finditer(text):
            url = match.group(1) or match.group(2) or ""
            found.append((path.name, url[:120], text[: match.start()].count("\n") + 1))
    return found


def missing_local_assets(page_dir: Path) -> list[str]:
    """Local files the entry document references but that are not there."""
    entry = page_dir / PAGE_ENTRY_FILE
    if not entry.is_file():
        return []
    text = entry.read_text(encoding="utf-8", errors="replace")
    missing: list[str] = []
    for match in LOCAL_ASSET.finditer(text):
        reference = match.group(1).split("?")[0].split("#")[0].lstrip("./")
        if not reference or reference == BRIDGE_ASSET_NAME:
            continue
        if not (page_dir / reference).is_file():
            missing.append(reference)
    return sorted(set(missing))


def head_tag_hidden_in_comment(entry_text: str) -> bool:
    """Does the first head tag in the raw source sit inside a comment?

    The host inserts the bridge script after the first one it finds by scanning
    the text, comments included. A decorative one in a banner comment wins that
    match, and the bridge lands where no browser will run it.
    """
    match = HEAD_OPEN.search(entry_text)
    if match is None:
        return False
    return any(start <= match.start() < end for start, end in _comment_spans(entry_text))


def self_injected_bridge(entry_text: str) -> bool:
    """Does the page load `__bridge.js` itself? The host already inserts it."""
    spans = _comment_spans(entry_text)
    for match in re.finditer(re.escape(BRIDGE_ASSET_NAME), entry_text):
        if not any(start <= match.start() < end for start, end in spans):
            return True
    return False


def _comment_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in HTML_COMMENT.finditer(text)]
