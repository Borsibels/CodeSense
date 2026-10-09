import io
import posixpath
import threading
import time
import uuid
import zipfile
import zlib
from pathlib import PurePosixPath
from .analysis import EXTENSIONS, analyze, relationships
from .models import Project, SourceFile, ExplanationContext, ContextChunk
from .indexer import enrich_project

MAX_UPLOAD = 10 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_ARCHIVE_TEXT = 20 * 1024 * 1024
MAX_FILES = 30
MAX_FILE = 100 * 1024
MAX_TEXT = 2 * 1024 * 1024
CONTEXT_BYTES = 8000  # Conservative input budget; AI owner must enforce token limits too.
EXCLUDED = {'node_modules', '.git', '.venv', 'venv', '__pycache__', 'dist', 'build', '.next'}

class ProjectError(ValueError):
    pass

def safe_path(name: str) -> str:
    name = name.replace('\\', '/')
    path = PurePosixPath(name)
    if not name or '\x00' in name or path.is_absolute() or any(p in ('..', '.') or ':' in p for p in name.split('/')):
        raise ProjectError('Archive contains an unsafe path.')
    return str(path)

class ProjectStore:
    """Bounded local sessions, expiring after one hour. Restart clears all projects."""
    def __init__(self):
        self._projects = {}
        self._lock = threading.RLock()

    def put(self, project, sources):
        with self._lock:
            self._prune()
            if len(self._projects) >= 8:
                oldest = min(self._projects, key=lambda key: self._projects[key][2])
                del self._projects[oldest]
            self._projects[project.id] = (project, sources, time.monotonic())

    def _prune(self):
        now = time.monotonic()
        for key in list(self._projects):
            if now-self._projects[key][2] > 3600:
                del self._projects[key]

    def get(self, project_id):
        with self._lock:
            self._prune()
            if project_id not in self._projects:
                raise KeyError(project_id)
            project, sources, _ = self._projects[project_id]
            self._projects[project_id] = (project, sources, time.monotonic())
            return project, sources

    def delete(self, project_id):
        with self._lock:
            if project_id not in self._projects:
                raise KeyError(project_id)
            del self._projects[project_id]

store = ProjectStore()

def ingest_archive(data: bytes, name: str) -> Project:
    if len(data) > MAX_UPLOAD:
        raise ProjectError('ZIP exceeds the 10 MB upload limit.')
    indexed, sources, refs = {}, {}, {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ProjectError('ZIP exceeds the 1000 entry limit.')
            if sum(e.file_size for e in entries) > MAX_ARCHIVE_TEXT:
                raise ProjectError('ZIP exceeds the 20 MB decompressed limit.')
            seen = set()
            total = 0
            for entry in entries:
                path = safe_path(entry.filename.rstrip('/'))
                if path.casefold() in seen:
                    raise ProjectError('ZIP contains duplicate or case-colliding paths.')
                seen.add(path.casefold())
                mode = (entry.external_attr >> 16) & 0o170000
                if mode not in (0, 0o100000, 0o040000):
                    raise ProjectError('ZIP contains a link or unsupported special entry.')
                if entry.flag_bits & 1:
                    raise ProjectError('Encrypted ZIP entries are unsupported.')
                if entry.is_dir():
                    continue
                language = EXTENSIONS.get(PurePosixPath(path).suffix.lower())
                reason = None
                if PurePosixPath(path).name.lower().startswith('.env'):
                    reason = 'Sensitive environment configuration is excluded.'
                elif any(part in EXCLUDED for part in PurePosixPath(path).parts):
                    reason = 'Excluded dependency or generated directory.'
                elif language is None:
                    reason = 'Unsupported file type.'
                elif entry.file_size > MAX_FILE:
                    reason = 'File exceeds the 100 KB source limit.'
                elif len(sources) >= MAX_FILES:
                    reason = 'Project reached the 30 source file limit.'
                elif total + entry.file_size > MAX_TEXT:
                    reason = 'Project reached the 2 MB source text limit.'
                if reason:
                    indexed[path] = SourceFile(path=path, language=language, status='skipped', reason=reason, size=entry.file_size)
                    continue
                with archive.open(entry) as stream:
                    raw = stream.read(MAX_FILE + 1)
                if len(raw) > MAX_FILE:
                    raise ProjectError('An archive entry exceeds its declared resource limit.')
                try:
                    code = raw.decode('utf-8-sig')
                    if '\x00' in code:
                        raise UnicodeError('binary content')
                except UnicodeError:
                    indexed[path] = SourceFile(path=path, language=language, status='skipped', reason='Source must be UTF-8 text without NUL bytes.', size=entry.file_size)
                    continue
                indexed[path], refs[path] = analyze(path, code, language)
                sources[path] = code
                total += len(raw)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError, zlib.error) as exc:
        raise ProjectError('Invalid, damaged, or unsupported ZIP archive.') from exc
    if not sources:
        raise ProjectError('Archive contains no eligible UTF-8 source files.')
    project = enrich_project(Project(id=str(uuid.uuid4()), name=name, files=list(indexed.values()), relationships=relationships(indexed, refs)))
    store.put(project, sources)
    return project

def ingest_snippet(code: str, language: str, filename: str) -> Project:
    if not code.strip() or '\x00' in code or len(code.encode('utf-8')) > MAX_FILE:
        raise ProjectError('Provide nonempty UTF-8 source text of at most 100 KB without NUL bytes.')
    path = safe_path(filename)
    source, refs = analyze(path, code, language)
    project = enrich_project(Project(id=str(uuid.uuid4()), name=path, files=[source], relationships=relationships({path: source}, {path: refs})))
    store.put(project, {path: code})
    return project

def excerpt(path, code, start, end, budget):
    lines = code.splitlines() or ['']
    selected = []
    used = 0
    for line in lines[start-1:end]:
        cost = len((line+'\n').encode('utf-8'))
        if used + cost > budget:
            break
        selected.append(line)
        used += cost
    if not selected:
        raise ProjectError('Selected line exceeds the context budget. Select a smaller code section.')
    return ContextChunk(path=path, start_line=start, end_line=start+len(selected)-1, code='\n'.join(selected)), used

def select_context(request) -> ExplanationContext:
    project, sources = store.get(request.project_id)
    if request.scope == 'project':
        files = [f for f in project.files if f.status != 'skipped']
        return ExplanationContext(project_id=project.id, scope='project', difficulty=request.difficulty, related=[], constructs=[], concepts=sorted({c for f in files for c in f.concepts}), relationships=project.relationships[:40], truncated=len(project.relationships)>40, limitations=['Project overview uses static metadata only; file contents have not been inspected by the model.', 'Entry points are filename-based candidates, not verified launch targets.'], metadata={'name':project.name, 'files':[{'file_id':f.file_id,'path':f.path,'language':f.language,'status':f.status} for f in files], 'language_counts':project.language_counts,'entry_points':project.entry_points,'skipped_files':project.skipped_files})
    if request.file_id:
        matches = [f for f in project.files if f.file_id == request.file_id]
        if not matches:
            raise KeyError(request.file_id)
        if request.path and request.path != matches[0].path:
            raise ProjectError('file_id and path identify different files.')
        request = request.model_copy(update={'path':matches[0].path})
    if request.path not in sources:
        raise ProjectError('Selected file is unavailable or was skipped.')
    file = next(f for f in project.files if f.path == request.path)
    lines = sources[request.path].splitlines() or ['']
    start = request.start_line or 1
    end = request.end_line or len(lines)
    if start > end or end > len(lines):
        raise ProjectError('Line range is outside the selected file.')
    selected, used = excerpt(request.path, sources[request.path], start, end, CONTEXT_BYTES)
    selected.file_id = file.file_id
    relevant = [r for r in project.relationships if request.path in (r.source, r.target)]
    related = []
    for relation in relevant:
        target = relation.target if relation.source == request.path else relation.source
        if relation.resolved and target in sources and target != request.path and target not in [c.path for c in related] and CONTEXT_BYTES-used >= 100:
            try:
                chunk, consumed = excerpt(target, sources[target], 1, len(sources[target].splitlines()) or 1, min(1500, CONTEXT_BYTES-used))
            except ProjectError:
                continue
            related.append(chunk)
            chunk.file_id = next(f.file_id for f in project.files if f.path == target)
            used += consumed
            if len(related) == 2:
                break
    limitations = ['Static relationships are hints, not a complete dependency graph.', 'Source is analyzed as text and is never executed.', 'The AI integration must enforce its own total prompt token budget.']
    truncated = selected.end_line < end
    if truncated:
        limitations.append('Selected source was truncated to fit the context budget.')
    if file.reason:
        limitations.append(file.reason)
    if any(not r.resolved for r in relevant):
        limitations.append('Some file references could not be resolved locally.')
    return ExplanationContext(project_id=project.id, scope='block' if request.start_line else request.scope, difficulty=request.difficulty, language=file.language, selected=selected, related=related, constructs=[c for c in file.constructs if c.start_line <= selected.end_line and c.end_line >= start][:80], concepts=file.concepts, relationships=relevant[:50], truncated=truncated, limitations=limitations)
