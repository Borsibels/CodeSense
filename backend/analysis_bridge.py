"""Bridge from the session host to the Phase 4.5 analysis engine (``backend/app``).

The engine analyses a project ZIP. The host already holds the uploaded project in a session, so the stored
sources are repacked into an in-memory ZIP and run through the engine's own pipeline: the user never uploads
twice, and the engine behaves exactly as it does behind ``/api/ai/analyze``. The bridge adds only what the
engine does not have:

* mapping a selected line range to the smallest enclosing function or class (file-level fallback),
  and saying so whenever the analysed scope is wider than the selection;
* the host's single inference lock (one model request at a time, shared with the legacy AI routes);
* the host's ``{code, detail}`` error envelope.

Nothing here talks to the network except the engine's own local Ollama client.
"""
import asyncio
import io
import logging
import time
import unicodedata
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from . import _engine  # noqa: F401  (makes the engine's `app` package importable)
from app.api.errors import describe_ollama_error
from app.api.inference import audit_token_usage
from app.config import ApiSettings, OllamaSettings, ProjectSettings
from app.services import OllamaError, OllamaService, PromptTooLargeError, StructuredGenerator, TokenBudget
from app.services.analysis_models import AnalysisResponse
from app.services.code_analysis_service import AnalysisBudgetError, AnalysisRequest, AnalysisUnavailableError, CodeAnalysisService, NoAnalyzableSourceError
from app.services.context_selection import SelectionError
from app.services.project_ingestion import ArchiveError, normalize_member_path
from app.services.project_models import FileAnalysis, ProcessingTimeout, ProjectAnalysis
from app.services.project_pipeline import analyze_project
from .projects import ProjectError, store

logger = logging.getLogger(__name__)

# Up to three model calls (first try, one retry, one compact fallback) at the engine's per-call read timeout.
ANALYSIS_TIMEOUT = 300.0
# Parser kinds that count as "a function or class" for selection mapping, and how a note names them.
UNITS = {'function': 'function', 'async_function': 'function', 'method': 'method', 'async_method': 'method', 'class': 'class'}
# Why the engine leaves a file out, in words a learner can act on.
EXCLUSIONS = {'ignored_directory': 'it is inside a folder CodeSense ignores (such as coverage, build or .vscode)', 'generated_file': 'it looks machine-generated',
              'minified': 'it looks minified', 'too_large': 'it is too large', 'unsupported_extension': 'its file type is not supported',
              'binary_content': 'it is not a text file', 'invalid_encoding': 'it is not valid UTF-8'}


class BridgeError(Exception):
    """An error already expressed in the host's ``{code, detail}`` envelope."""
    def __init__(self, status, code, detail, headers=None):
        super().__init__(f'{code}: {detail}')
        self.status, self.code, self.detail, self.headers = status, code, detail, headers


class LineSpan(BaseModel):
    start_line: int
    end_line: int


class AnalysisBody(BaseModel):
    """What the client asks for. ``file_id`` (or the transitional ``path``) picks the file; lines pick a selection."""
    model_config = ConfigDict(extra='forbid')
    intent: Literal['overview', 'explain', 'debug'] = 'explain'
    # ``experienced`` is the frontend's name for the engine's ``advanced``.
    depth: Literal['beginner', 'intermediate', 'advanced'] = 'beginner'
    file_id: str | None = Field(default=None, max_length=64)
    path: str | None = Field(default=None, max_length=300)
    symbol: str | None = Field(default=None, max_length=200)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @field_validator('depth', mode='before')
    @classmethod
    def depth_alias(cls, value):
        return 'advanced' if value == 'experienced' else value

    @model_validator(mode='after')
    def validate_scope(self):
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError('Give both start_line and end_line, or neither.')
        if self.start_line and self.start_line > self.end_line:
            raise ValueError('start_line must not exceed end_line.')
        if self.intent == 'overview':
            if self.symbol or self.start_line:
                raise ValueError('An overview does not accept a symbol or a line range.')
        elif not (self.file_id or self.path):
            raise ValueError('Provide file_id for explain and debug.')
        if self.symbol and self.start_line:
            raise ValueError('Give either a symbol or a line range, not both.')
        if self.symbol and not (self.file_id or self.path):
            raise ValueError('A symbol needs the file that contains it.')
        return self


class SelectionInfo(BaseModel):
    """What was asked for versus what was analysed. Deterministic: computed by the backend, never by the AI."""
    scope: Literal['project', 'file', 'symbol'] = Field(description='`project`: whole-project overview. `file`: one whole file. `symbol`: one function, method or class.')
    requested: LineSpan | None = Field(default=None, description='The learner\'s selection, exactly as sent. Null when no lines were selected.')
    analyzed_symbol: str | None = Field(default=None, description='Qualified name of the function/class that was analysed, if any.')
    analyzed_lines: LineSpan | None = Field(default=None, description='Lines the analysis was centred on: the symbol, or the whole file. Null for a project overview.')
    expanded: bool = Field(default=False, description='True when `analyzed_lines` is wider than `requested`.')
    note: str | None = Field(default=None, description='Plain-language disclosure shown whenever `expanded` is true.')


class BridgeAnalysisResponse(AnalysisResponse):
    """The engine's response, unchanged, plus the session identifiers and the selection disclosure."""
    project_id: str
    file_id: str | None = None
    selection: SelectionInfo


@dataclass
class Plan:
    path: str | None
    symbol: str | None
    scope: str
    requested: LineSpan | None = None
    fallback: str | None = None


def pick_file(project, sources, body):
    """The session file the request is about (None for a project overview), with the host's existing errors."""
    if body.file_id:
        file = next((f for f in project.files if f.file_id == body.file_id), None)
        if file is None:
            raise KeyError(body.file_id)
        if body.path and body.path != file.path:
            raise ProjectError('file_id and path identify different files.')
    elif body.path:
        file = next((f for f in project.files if f.path == body.path), None)
    else:
        return None
    if file is None or file.path not in sources:
        raise ProjectError('Selected file is unavailable or was skipped.')
    return file


def enclosing_symbol(file: FileAnalysis, start: int, end: int):
    """Smallest function/method/class containing every selected line, or ``(None, reason)``.

    A name defined more than once in the file cannot be selected unambiguously through the engine, so that
    case falls back to the whole file rather than silently analysing every definition.
    """
    inside = [s for s in file.symbols if s.kind in UNITS and s.start_line <= start and end <= s.end_line]
    if not inside:
        return None, 'outside'
    best = min(inside, key=lambda s: (s.end_line - s.start_line, -s.start_line))
    if sum(s.qualified_name == best.qualified_name for s in file.symbols) > 1:
        return None, 'ambiguous'
    return best, None


def plan_selection(analysis: ProjectAnalysis, path, body) -> Plan:
    if path and path not in analysis.by_path:  # the session accepted it but the engine did not
        left_out = next((e.reason for e in analysis.excluded if e.path == path), None)
        reason = EXCLUSIONS.get(left_out, 'its path cannot be analysed safely' if left_out is None else left_out)
        raise BridgeError(422, 'FILE_NOT_ANALYZED', f'The AI cannot analyse this file because {reason}. You can still read it in the file browser.')
    if body.intent == 'overview':
        return Plan(path, None, 'project')
    if body.symbol:
        return Plan(path, body.symbol.strip() or None, 'symbol')
    if body.start_line is None:
        return Plan(path, None, 'file')
    if body.end_line > analysis.by_path[path].line_count:
        raise ProjectError('Line range is outside the selected file.')
    requested = LineSpan(start_line=body.start_line, end_line=body.end_line)
    symbol, fallback = enclosing_symbol(analysis.analyses[path], body.start_line, body.end_line)
    if symbol:
        return Plan(path, symbol.qualified_name, 'symbol', requested)
    return Plan(path, None, 'file', requested, fallback)


def build_analysis(sources, settings: ProjectSettings):
    """Run the engine's pipeline over the session sources -> ``(ProjectAnalysis, dropped_count)``.

    The engine rejects a whole archive for one unsafe path, whereas the host's upload rules are looser, so
    paths the engine would refuse are left out (and counted) instead of failing the project. The ZIP is
    uncompressed: its content is trusted, and the engine's compression-ratio check would misfire on
    repetitive source.
    """
    buffer, seen, dropped = io.BytesIO(), set(), 0
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
        for path, code in sources.items():
            key = unicodedata.normalize('NFC', path).casefold()
            try:
                clean, _ = normalize_member_path(path, settings)
            except ArchiveError:
                clean = None
            if clean != path or key in seen:
                dropped += 1
                continue
            seen.add(key)
            archive.writestr(path, code.encode('utf-8'))
    if dropped == len(sources):
        raise BridgeError(422, 'NO_ANALYZABLE_SOURCE', 'None of this project\'s file paths can be analysed safely.')
    return analyze_project(buffer.getvalue(), settings), dropped


def translate(exc, model) -> BridgeError:
    """Map an engine failure to the host's envelope. Messages are fixed strings: no source, paths or Ollama bodies."""
    if isinstance(exc, BridgeError):
        return exc
    if isinstance(exc, SelectionError):
        return BridgeError(422, exc.code, exc.message)
    if isinstance(exc, NoAnalyzableSourceError):
        return BridgeError(422, 'NO_ANALYZABLE_SOURCE', exc.message)
    if isinstance(exc, AnalysisUnavailableError):
        return BridgeError(503, 'ANALYSIS_UNAVAILABLE', exc.message)
    if isinstance(exc, ArchiveError):
        return BridgeError(exc.status, exc.code, exc.message)
    if isinstance(exc, ProcessingTimeout):
        return BridgeError(504, 'PROJECT_PROCESSING_TIMEOUT', 'Analysing the project took too long. Try a smaller project.')
    if isinstance(exc, PromptTooLargeError):
        return BridgeError(422, 'PROMPT_TOO_MANY_TOKENS', 'The selected code is too large for the local model\'s context window. Select a smaller function or section.')
    if isinstance(exc, OllamaError):
        status, code, message = describe_ollama_error(exc, model)
        return BridgeError(status, code, message, {'Retry-After': '5'} if status == 503 else None)
    if isinstance(exc, TimeoutError):
        return BridgeError(504, 'AI_TIMEOUT', 'The local analysis took too long. Try a smaller selection.')
    if isinstance(exc, AnalysisBudgetError):
        return BridgeError(500, 'ANALYSIS_BUDGET_ERROR', 'The analysis could not be fitted into the local model\'s window. This is a CodeSense bug; check the server log.')
    return BridgeError(500, 'ANALYSIS_FAILED', 'The analysis failed unexpectedly. Check the server log.')


def selection_info(plan: Plan, analysis: ProjectAnalysis, response: AnalysisResponse):
    """Disclosure of what was really analysed, from the engine's own answer (``target_symbols``) and the parsed project."""
    path, analyzed = response.target_file, None
    if path and path in analysis.analyses:
        spans = [(s.start_line, s.end_line) for s in analysis.analyses[path].symbols if s.qualified_name in response.target_symbols]
        if spans:
            analyzed = LineSpan(start_line=min(a for a, _ in spans), end_line=max(b for _, b in spans))
        elif plan.scope != 'project':
            analyzed = LineSpan(start_line=1, end_line=max(analysis.by_path[path].line_count, 1))
    requested = plan.requested
    expanded = bool(requested and analyzed and (analyzed.start_line < requested.start_line or analyzed.end_line > requested.end_line))
    note = None
    if expanded:
        wanted = f'lines {requested.start_line}–{requested.end_line}' if requested.start_line != requested.end_line else f'line {requested.start_line}'
        if plan.scope == 'symbol':
            kind = UNITS.get(next((s.kind for s in analysis.analyses[path].symbols if s.qualified_name in response.target_symbols), ''), 'section')
            note = f'You selected {wanted}. CodeSense analysed the whole {kind} \'{response.target_symbols[0]}\' (lines {analyzed.start_line}–{analyzed.end_line}) that contains it, so the analysis can mention code you did not select.'
        elif plan.fallback == 'ambiguous':
            note = f'You selected {wanted}, but the function or class around it shares its name with another definition in this file, so CodeSense analysed the whole file.'
        else:
            note = f'You selected {wanted}, which is not inside a single function or class, so CodeSense analysed the whole file.'
    return SelectionInfo(scope=plan.scope, requested=requested, analyzed_symbol=response.target_symbols[0] if response.target_symbols else None, analyzed_lines=analyzed, expanded=expanded, note=note)


class AnalysisBridge:
    def __init__(self, ollama_settings=None, api_settings=None, project_settings=None, transport=None, lock=None, timeout=ANALYSIS_TIMEOUT):
        self.ollama_settings = ollama_settings or OllamaSettings.from_env()
        api = api_settings or ApiSettings.from_env()
        self.project_settings = project_settings or ProjectSettings.from_env()
        self.timeout = timeout
        # The one gate for local inference: the host's legacy AI routes take this same lock.
        self.lock = lock or asyncio.Lock()
        budget = TokenBudget(total_context=self.ollama_settings.num_ctx, reserved_generation=self.ollama_settings.num_predict, safety_margin=api.safety_margin_tokens, max_input_tokens=api.max_input_tokens)
        self.ollama = OllamaService(self.ollama_settings, transport=transport)
        structured = StructuredGenerator(self.ollama, max_retries=api.structured_max_retries, budget=budget)
        self.service = CodeAnalysisService(structured, budget, reserve=self.project_settings.instruction_reserve_tokens, inference_slot=self.slot, on_generation=lambda estimated, generation: audit_token_usage(budget, estimated, generation))
        if self.service.unavailable_reason:
            logger.warning('Project analysis is disabled: %s', self.service.unavailable_reason)

    @asynccontextmanager
    async def slot(self):
        if self.lock.locked():
            raise BridgeError(429, 'AI_BUSY', 'Local AI is busy. Wait for the current request to finish.')
        async with self.lock:
            yield

    def readiness(self):
        return {'available': self.service.unavailable_reason is None, 'reason': self.service.unavailable_reason}

    async def aclose(self):
        await self.ollama.aclose()

    async def analyze(self, project_id, body: AnalysisBody) -> BridgeAnalysisResponse:
        project, sources = store.get(project_id)  # KeyError -> the host's 404
        file = pick_file(project, sources, body)
        try:
            started = time.perf_counter()
            analysis, dropped = await asyncio.to_thread(build_analysis, sources, self.project_settings)
            project_ms = (time.perf_counter() - started) * 1000
            plan = plan_selection(analysis, file.path if file else None, body)
            response = await asyncio.wait_for(self.service.analyze(analysis, AnalysisRequest(intent=body.intent, depth=body.depth, file_path=plan.path, symbol=plan.symbol), project_ms=project_ms), self.timeout)
        except ProjectError:
            raise
        except Exception as exc:
            error = translate(exc, self.ollama_settings.model)
            if error is exc:
                raise
            if error.status == 500:  # unexpected: keep the stack; the rest are ordinary operational failures
                logger.error('Analysis failed: %s', type(exc).__name__, exc_info=exc)
            else:
                logger.warning('Analysis refused: %s (%s)', error.code, type(exc).__name__)
            raise error from exc
        selection = selection_info(plan, analysis, response)
        skipped = sum(f.status == 'skipped' for f in project.files)
        lead = [selection.note] if selection.note else []
        tail = []
        if skipped:
            tail.append(f'{skipped} file(s) were skipped when the project was uploaded (unsupported type, too large, or not UTF-8) and are not part of this analysis.')
        if dropped:
            tail.append(f'{dropped} file(s) were left out because their path cannot be analysed safely.')
        data = response.model_dump()
        data['limitations'] = lead + data['limitations'] + tail
        return BridgeAnalysisResponse(**data, project_id=project.id, file_id=file.file_id if file else None, selection=selection)
