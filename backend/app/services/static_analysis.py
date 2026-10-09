"""Static analysis entry point: pick the analyser for a file, never let one file abort the rest."""

from __future__ import annotations

import logging
from dataclasses import replace

from app.services.js_analysis import analyze_javascript
from app.services.project_models import Deadline, Diagnostic, FileAnalysis, ProjectFile
from app.services.python_analysis import analyze_python
from app.services.web_analysis import analyze_css, analyze_html

logger = logging.getLogger(__name__)

# Per-file output caps: one 512 KiB file of tiny functions must not produce a response of
# hundreds of thousands of entries. Truncation is always reported as a diagnostic.
MAX_SYMBOLS_PER_FILE = 3000
MAX_REFERENCES_PER_FILE = 5000
MAX_EXPORTS_PER_FILE = 1000
MAX_DIAGNOSTICS_PER_FILE = 200

_ANALYZERS = {
    "python": analyze_python,
    "javascript": analyze_javascript,
    "html": analyze_html,
    "css": analyze_css,
}


def _capped(analysis: FileAnalysis) -> FileAnalysis:
    notes = []
    changes: dict = {}
    for name, limit in (
        ("symbols", MAX_SYMBOLS_PER_FILE),
        ("references", MAX_REFERENCES_PER_FILE),
        ("exports", MAX_EXPORTS_PER_FILE),
    ):
        items = getattr(analysis, name)
        if len(items) > limit:
            changes[name] = items[:limit]
            notes.append(
                Diagnostic(
                    "warning",
                    f"{name}_truncated",
                    f"Only the first {limit} of {len(items)} {name} are listed for this file.",
                    confidence="confirmed",
                )
            )
    if len(analysis.diagnostics) + len(notes) > MAX_DIAGNOSTICS_PER_FILE:
        changes["diagnostics"] = analysis.diagnostics[: MAX_DIAGNOSTICS_PER_FILE - len(notes)]
    if not changes and not notes:
        return analysis
    diagnostics = tuple(changes.get("diagnostics", analysis.diagnostics)) + tuple(notes)
    return replace(analysis, **{**changes, "diagnostics": diagnostics})


def analyze_file(file: ProjectFile) -> FileAnalysis:
    try:
        return _capped(_ANALYZERS[file.language](file))
    except Exception as exc:  # noqa: BLE001 - an analyser bug must degrade one file, not the project
        # Log the type only: the message could echo source text.
        logger.error("Static analysis failed for a %s file (%s)", file.language, type(exc).__name__)
        return FileAnalysis(
            path=file.path,
            language=file.language,
            parser="unavailable",
            confidence="heuristic",
            line_count=file.line_count,
            diagnostics=(
                Diagnostic(
                    "error",
                    "analysis_failed",
                    f"Static analysis of this file failed internally ({type(exc).__name__}).",
                    confidence="heuristic",
                ),
            ),
        )


def analyze_files(files: tuple[ProjectFile, ...], deadline: Deadline | None = None) -> dict[str, FileAnalysis]:
    """Analyse every file; the result is keyed (and ordered) by path."""
    analyses: dict[str, FileAnalysis] = {}
    for file in files:
        if deadline:
            deadline.check()
        analyses[file.path] = analyze_file(file)
    return analyses
