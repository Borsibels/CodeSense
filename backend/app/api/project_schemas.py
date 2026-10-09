"""Pydantic response schemas for ``/api/projects/*``.

The service layer uses frozen dataclasses; these models are the public contract.
``confidence`` is ``confirmed`` for facts produced by a real parser (Python
``ast``, ``html.parser``) and ``heuristic`` for scanner output (JavaScript, CSS).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["confirmed", "heuristic"]
Language = Literal["python", "javascript", "html", "css"]


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ----- static analysis ------------------------------------------------------- #
class SymbolOut(_Model):
    name: str
    qualified_name: str
    kind: str = Field(description="function, async_function, class, method, async_method, variable or rule (CSS)")
    start_line: int
    end_line: int
    signature: str | None
    parent: str | None
    doc: str | None
    exported: bool | None = Field(description="JavaScript only: whether the file appears to export it; null = unknown")
    confidence: Confidence


class ReferenceOut(_Model):
    kind: str = Field(description="import, require, dynamic_import, reexport, script, stylesheet, css_import or asset")
    specifier: str
    line: int
    names: list[str]
    level: int = Field(description="Python relative-import dots (0 for absolute imports)")
    top_level: bool
    confidence: Confidence


class ExportOut(_Model):
    name: str
    kind: str
    line: int
    source: str | None
    confidence: Confidence


class DiagnosticOut(_Model):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    line: int | None
    column: int | None
    confidence: Confidence


class ElementIdOut(_Model):
    id: str
    tag: str
    line: int


class SourceFileOut(_Model):
    path: str
    language: Language
    size_bytes: int
    line_count: int
    parser: str
    confidence: Confidence
    has_syntax_error: bool
    doc: str | None
    has_main_guard: bool
    title: str | None
    inline_script_count: int
    inline_style_count: int
    element_ids: list[ElementIdOut]
    css_classes: list[str]
    css_ids: list[str]
    symbols: list[SymbolOut]
    references: list[ReferenceOut]
    exports: list[ExportOut]
    diagnostics: list[DiagnosticOut]


class ExcludedFileOut(_Model):
    path: str
    reason: str = Field(
        description="ignored_directory, generated_file, unsupported_extension, too_large, "
        "binary_content, invalid_encoding or minified"
    )
    detail: str
    size_bytes: int


# ----- dependency graph --------------------------------------------------------- #
class GraphEdgeOut(_Model):
    source: str
    specifier: str
    kind: str
    line: int
    status: Literal["resolved", "excluded", "missing", "external", "unresolved"]
    target: str | None
    external_kind: str | None
    reason: str | None
    confidence: Confidence


class GraphNodeOut(_Model):
    path: str
    language: Language
    dependency_count: int
    dependent_count: int


class CycleOut(_Model):
    files: list[str]
    example: list[str]


class EntryPointOut(_Model):
    path: str
    reason: str
    confidence: Confidence


class ExternalDependencyOut(_Model):
    name: str
    kind: str
    files: list[str]


class GraphOut(_Model):
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    cycles: list[CycleOut]
    entry_points: list[EntryPointOut]
    external_dependencies: list[ExternalDependencyOut]


# ----- summaries ------------------------------------------------------------------ #
class DiagnosticCounts(BaseModel):
    error: int
    warning: int
    info: int


class ProjectSummary(BaseModel):
    archive_bytes: int
    entry_count: int
    uncompressed_bytes: int
    source_files: int
    source_bytes: int
    excluded_files: int
    languages: dict[str, int]
    symbol_count: int
    reference_count: int
    diagnostics: DiagnosticCounts


class InspectResponse(BaseModel):
    notice: str
    summary: ProjectSummary
    files: list[SourceFileOut]
    excluded: list[ExcludedFileOut]
    graph: GraphOut


# ----- context ----------------------------------------------------------------------- #
class LineRangeOut(BaseModel):
    start_line: int
    end_line: int


class BudgetOut(BaseModel):
    input_limit: int = Field(description="Estimated-token input budget of the model (context - output - margin)")
    instruction_reserve: int = Field(description="Tokens kept free for future instructions; code never uses them")
    context_limit: int = Field(description="input_limit - instruction_reserve: the most the context text may use")
    estimated_tokens: int
    remaining_tokens: int
    is_estimate: bool = Field(description="Always true: counts come from the conservative estimator, not a tokenizer")


class CoverageSummaryOut(BaseModel):
    files_total: int
    files_full: int
    files_partial: int
    files_outline_only: int
    files_not_included: int


class FileCoverageOut(BaseModel):
    path: str
    status: Literal["full", "partial", "outline_only", "not_included"]
    line_count: int
    included_ranges: list[LineRangeOut]
    omitted_ranges: list[LineRangeOut]
    outline_entries_listed: int | None
    outline_entries_total: int | None
    reasons: list[str]
    estimated_tokens: int


class OmittedRegionOut(BaseModel):
    path: str
    kind: Literal["code", "outline"]
    start_line: int | None
    end_line: int | None
    symbol: str | None
    reason: str
    requested_for: str


class ContextResponse(BaseModel):
    notice: str
    intent: Literal["overview", "explain", "debug"]
    target_file: str | None
    target_symbols: list[str]
    context: str = Field(description="The assembled text, ready to be placed in a future prompt")
    budget: BudgetOut
    coverage_summary: CoverageSummaryOut
    files: list[FileCoverageOut]
    omitted_regions: list[OmittedRegionOut]
    omitted_region_total: int
    warnings: list[str]
    project: ProjectSummary
