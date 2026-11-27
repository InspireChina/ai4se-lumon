"""Read inert acceptance text from Jira HTML and Excel references."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from lumon.auto_test.workbook import read_reference_workbook
from lumon.errors import PreflightError

REFERENCE_SUFFIXES = frozenset({".html", ".htm", ".xlsx"})
MAX_REFERENCE_BYTES = 2 * 1024 * 1024
MAX_REFERENCE_TEXT = 32000
_BLOCK_TAGS = frozenset({"p", "div", "br", "dt", "dd", "li", "h1", "h2", "h3", "h4", "tr", "td"})


class _ReferenceHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden = tag
        if self.hidden is None and tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden == tag:
            self.hidden = None
        if self.hidden is None and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden is None and (text := " ".join(data.split())):
            self.parts.extend((text, " "))


def read_reference(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REFERENCE_BYTES:
        raise PreflightError("The Jira reference document is unavailable or too large.")
    if path.suffix == ".xlsx":
        return read_reference_workbook(path, limit=MAX_REFERENCE_TEXT)
    if path.suffix in {".html", ".htm"}:
        parser = _ReferenceHTML()
        parser.feed(path.read_text(encoding="utf-8-sig"))
        parser.close()
        lines = (line.strip() for line in "".join(parser.parts).splitlines())
        return "\n".join(line for line in lines if line)[:MAX_REFERENCE_TEXT]
    raise PreflightError("The Jira reference format is unsupported.")
