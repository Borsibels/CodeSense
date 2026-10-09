"""HTML and CSS static analysis.

HTML uses the standard-library ``html.parser`` (a tokenizer: it never executes
scripts, fetches resources or builds a DOM), so its facts are ``confirmed``
tokens of the markup. CSS uses a small scanner (comments/strings aware, no
selector grammar), so CSS results are ``heuristic``.
"""

from __future__ import annotations

import bisect
import re
from html.parser import HTMLParser

from app.services.project_models import (
    Diagnostic,
    ElementId,
    FileAnalysis,
    ProjectFile,
    Reference,
    Symbol,
)

_MAX_IDS = 300
_MAX_RULES = 400
_MAX_NAMES = 300
_MAX_SELECTOR = 120

_ASSET_SRC_TAGS = frozenset({"img", "source", "video", "audio", "iframe", "embed", "track"})
_SCRIPT_TYPES_NOT_JS = ("application/json", "application/ld+json", "text/template", "importmap", "speculationrules")


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
class _HtmlCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[Reference] = []
        self.ids: list[ElementId] = []
        self.ids_truncated = False
        self.diagnostics: list[Diagnostic] = []
        self.title_parts: list[str] = []
        self.title: str | None = None
        self.in_title = False
        self.inline_scripts = 0
        self.inline_styles = 0

    def _ref(self, kind: str, value: str | None, line: int) -> None:
        if value and value.strip():
            self.references.append(Reference(kind, value.strip(), line))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        attributes = {name: (value or "") for name, value in attrs}
        element_id = attributes.get("id", "").strip()
        if element_id:
            if len(self.ids) < _MAX_IDS:
                self.ids.append(ElementId(element_id, tag, line))
            else:
                self.ids_truncated = True

        if tag == "title" and self.title is None:
            self.in_title = True
        elif tag == "script":
            if "src" in attributes:
                self._ref("script", attributes["src"], line)
            elif attributes.get("type", "").strip().lower() not in _SCRIPT_TYPES_NOT_JS:
                self.inline_scripts += 1
        elif tag == "style":
            self.inline_styles += 1
        elif tag == "link":
            rel = attributes.get("rel", "").lower().split()
            if "stylesheet" in rel:
                self._ref("stylesheet", attributes.get("href"), line)
            elif "modulepreload" in rel:
                self._ref("script", attributes.get("href"), line)
            elif rel:
                self._ref("asset", attributes.get("href"), line)
        elif tag in _ASSET_SRC_TAGS or (tag == "input" and attributes.get("type", "").lower() == "image"):
            self._ref("asset", attributes.get("src"), line)
        elif tag == "base" and attributes.get("href"):
            self.diagnostics.append(
                Diagnostic(
                    "info",
                    "base_href",
                    "A <base href> changes how relative paths resolve; references are resolved against the file's own folder.",
                    line,
                )
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.in_title:
            self.in_title = False
            self.title = " ".join("".join(self.title_parts).split())[:200]

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def analyze_html(file: ProjectFile) -> FileAnalysis:
    collector = _HtmlCollector()
    diagnostics: list[Diagnostic] = []
    try:
        collector.feed(file.text)
        collector.close()
    except Exception as exc:  # noqa: BLE001 - html.parser is lenient, but never let one file abort a project
        diagnostics.append(
            Diagnostic("warning", "html_parse_incomplete", f"HTML parsing stopped early ({type(exc).__name__}).")
        )
    diagnostics.extend(collector.diagnostics)
    if collector.ids_truncated:
        diagnostics.append(Diagnostic("info", "ids_truncated", f"Only the first {_MAX_IDS} element ids are listed."))
    title = collector.title
    if title is None and collector.in_title:  # <title> never closed
        title = " ".join("".join(collector.title_parts).split())[:200]
    return FileAnalysis(
        path=file.path,
        language="html",
        parser="html.parser",
        confidence="confirmed",
        line_count=file.line_count,
        references=tuple(sorted(collector.references, key=lambda r: (r.line, r.kind, r.specifier))),
        diagnostics=tuple(sorted(diagnostics, key=lambda d: (d.line or 0, d.code))),
        title=title or None,
        inline_script_count=collector.inline_scripts,
        inline_style_count=collector.inline_styles,
        element_ids=tuple(collector.ids),
    )


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.DOTALL)
_URL = re.compile(r"""url\(\s*(?:"([^"\n]*)"|'([^'\n]*)'|([^)"'\s][^)\n]*?))\s*\)""", re.IGNORECASE)
_IMPORT = re.compile(
    r"""^@import\s+(?:url\(\s*(?:"([^"\n]*)"|'([^'\n]*)'|([^)"'\s][^)\n]*?))\s*\)|"([^"\n]*)"|'([^'\n]*)')""",
    re.IGNORECASE,
)
_CLASS = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
_ID = re.compile(r"#(-?[_a-zA-Z][\w-]*)")
_NESTING_AT_RULES = frozenset({"media", "supports", "layer", "container", "document", "scope", "starting-style"})


def _blank_comments(text: str) -> str:
    return _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


class _Open:
    __slots__ = ("transparent", "selector", "start_line")

    def __init__(self, transparent: bool, selector: str | None = None, start_line: int = 0) -> None:
        self.transparent = transparent
        self.selector = selector
        self.start_line = start_line


def analyze_css(file: ProjectFile) -> FileAnalysis:
    clean = _blank_comments(file.text)
    newlines = [m.start() for m in re.finditer("\n", clean)]

    def line_at(offset: int) -> int:
        return bisect.bisect_left(newlines, offset) + 1

    symbols: list[Symbol] = []
    references: list[Reference] = []
    diagnostics: list[Diagnostic] = []
    classes: set[str] = set()
    ids: set[str] = set()
    import_spans: list[tuple[int, int]] = []
    rules_seen = 0
    stack: list[_Open] = []
    buf_start = 0
    i = 0
    n = len(clean)

    while i < n:
        c = clean[i]
        if c in "\"'":
            j = i + 1
            while j < n and clean[j] != c and clean[j] != "\n":
                j += 2 if clean[j] == "\\" else 1
            i = j + 1
            continue
        if c == "{":
            prelude = clean[buf_start:i]
            if stack and not stack[-1].transparent:
                stack.append(_Open(False))
            else:
                text = prelude.strip()
                if text.startswith("@"):
                    name = re.match(r"@([\w-]+)", text)
                    stack.append(_Open((name.group(1).lower() if name else "") in _NESTING_AT_RULES))
                else:
                    lead = len(prelude) - len(prelude.lstrip())
                    stack.append(_Open(False, " ".join(text.split()), line_at(buf_start + lead)))
            buf_start = i + 1
        elif c == "}":
            if stack:
                entry = stack.pop()
                if entry.selector is not None:
                    rules_seen += 1
                    classes.update(_CLASS.findall(entry.selector))
                    ids.update(_ID.findall(entry.selector))
                    if len(symbols) < _MAX_RULES:
                        summary = (
                            entry.selector
                            if len(entry.selector) <= _MAX_SELECTOR
                            else entry.selector[: _MAX_SELECTOR - 3] + "..."
                        )
                        symbols.append(
                            Symbol(
                                name=summary,
                                qualified_name=summary,
                                kind="rule",
                                start_line=entry.start_line,
                                end_line=max(line_at(i), entry.start_line),
                                confidence="heuristic",
                            )
                        )
            else:
                diagnostics.append(
                    Diagnostic(
                        "warning", "unbalanced_braces", "A closing '}' has no matching '{'.", line_at(i), confidence="heuristic"
                    )
                )
            buf_start = i + 1
        elif c == ";":
            if not stack or stack[-1].transparent:
                raw = clean[buf_start:i]
                statement = raw.strip()
                if statement.lower().startswith("@import"):
                    match = _IMPORT.match(statement)
                    if match:
                        target = next((g for g in match.groups() if g), "")
                        lead = len(raw) - len(raw.lstrip())
                        references.append(
                            Reference("css_import", target.strip(), line_at(buf_start + lead), confidence="heuristic")
                        )
                    import_spans.append((buf_start, i))
                buf_start = i + 1
        i += 1

    if stack:
        diagnostics.append(
            Diagnostic(
                "warning",
                "unclosed_block",
                "A '{' block is never closed; later rules in this file may be missed.",
                confidence="heuristic",
            )
        )
    if rules_seen > _MAX_RULES:
        diagnostics.append(
            Diagnostic("info", "rules_truncated", f"Only the first {_MAX_RULES} of {rules_seen} rules are listed.")
        )

    for match in _URL.finditer(clean):
        if any(start <= match.start() < end for start, end in import_spans):
            continue
        target = next((g for g in match.groups() if g), "").strip()
        if target:
            references.append(Reference("asset", target, line_at(match.start()), confidence="heuristic"))

    return FileAnalysis(
        path=file.path,
        language="css",
        parser="css-scanner",
        confidence="heuristic",
        line_count=file.line_count,
        symbols=tuple(symbols),
        references=tuple(sorted(references, key=lambda r: (r.line, r.kind, r.specifier))),
        diagnostics=tuple(sorted(diagnostics, key=lambda d: (d.line or 0, d.code))),
        css_classes=tuple(sorted(classes)[:_MAX_NAMES]),
        css_ids=tuple(sorted(ids)[:_MAX_NAMES]),
    )
