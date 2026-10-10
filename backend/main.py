import traceback
import asyncio
import io
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from .ai import OllamaExplainer, UnconfiguredExplainer, ExplainerUnavailable, validate_explanation
from .models import Project, SnippetRequest, ContextRequest, ExplanationContext, Explanation
from .projects import MAX_UPLOAD, MAX_ENTRIES, safe_path, ProjectError, ingest_archive, ingest_snippet, store, select_context
from .middleware import BodyLimitMiddleware
from .challenges import ChallengeEngine, ChallengeSelect, ChallengeSubmit
from .missing_line import generate as generate_missing_line
from .qa import QuestionRequest, QuestionResult, build_question_prompt, ask
from .analysis_bridge import AnalysisBridge, AnalysisBody, BridgeAnalysisResponse, BridgeError

def create_app(explainer=None, inference_timeout=90, analysis=None):
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await app.state.analysis.aclose()

    app = FastAPI(title='Sift Backend', description='Sift is an offline AI-powered code comprehension and debugging platform. Make sense of every line.', version='0.3.0', lifespan=lifespan)
    app.state.explainer = explainer or OllamaExplainer()
    # Project analysis (Phase 4.5 engine) reads the same model settings as the explainer.
    app.state.analysis = analysis or AnalysisBridge(ollama_settings=getattr(app.state.explainer, 'settings', None))
    # One lock gates every local-model request, whichever route makes it.
    app.state.generation_lock = app.state.analysis.lock
    app.state.challenges = ChallengeEngine()
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173'], allow_methods=['GET', 'POST', 'DELETE'], allow_headers=['Content-Type'])

    @app.exception_handler(ProjectError)
    async def invalid_project(request, exc):
        return JSONResponse(status_code=422, content={'code':'INVALID_PROJECT', 'detail': str(exc)})

    @app.exception_handler(BridgeError)
    async def analysis_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'code':exc.code, 'detail':exc.detail}, headers=exc.headers)

    @app.exception_handler(KeyError)
    async def missing_project(request, exc):
        return JSONResponse(status_code=404, content={'code':'NOT_FOUND', 'detail': 'Resource not found or project session expired.'})

    @app.exception_handler(HTTPException)
    async def api_error(request, exc):
        codes = {404:'NOT_FOUND', 422:'INVALID_INPUT', 429:'AI_BUSY', 502:'INVALID_AI_RESPONSE', 503:'AI_UNAVAILABLE', 504:'AI_TIMEOUT'}
        return JSONResponse(status_code=exc.status_code, content={'code':codes.get(exc.status_code,'REQUEST_FAILED'), 'detail':exc.detail}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        # Do not echo complete uploaded source in validation errors.
        fields = [{'location':list(e['loc']),'message':e['msg']} for e in exc.errors()]
        return JSONResponse(status_code=422, content={'code':'INVALID_INPUT','detail':'Request validation failed.', 'fields':fields})

    @app.get('/api/health')
    async def health():
        adapter = app.state.explainer
        readiness = await adapter.readiness() if hasattr(adapter, 'readiness') else {'status':'unknown', 'model':None}
        return {'backend': 'ready', 'ai':readiness, 'analysis':app.state.analysis.readiness(), 'storage': 'temporary', 'languages': ['html', 'css', 'javascript', 'python']}

    @app.post('/api/projects/upload', response_model=Project, status_code=201)
    def upload(file: UploadFile = File(...)):
        try:
            if not file.filename or not file.filename.lower().endswith('.zip'):
                raise ProjectError('Upload a .zip project archive.')
            data = file.file.read(MAX_UPLOAD + 1)
            return ingest_archive(data, file.filename)
        finally:
            file.file.close()

    @app.post('/api/projects/snippet', response_model=Project, status_code=201)
    def snippet(body: SnippetRequest):
        return ingest_snippet(body.code, body.language, body.filename)

    @app.post('/api/projects/files', response_model=Project, status_code=201)
    def upload_files(files: list[UploadFile] = File(...), paths: str = Form(...), name: str = Form('Uploaded files')):
        try:
            try:
                names = json.loads(paths)
            except (ValueError, TypeError):
                raise ProjectError('Provide a JSON list of relative source paths.')
            if not isinstance(names, list) or len(names) != len(files) or not all(isinstance(p, str) for p in names):
                raise ProjectError('Each uploaded file must have one relative path.')
            if not files or len(files) > MAX_ENTRIES:
                raise ProjectError('Choose between 1 and 1000 files.')
            buffer = io.BytesIO()
            total = 0
            seen = set()
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for file, path in zip(files, names):
                    path = safe_path(path)
                    if path.casefold() in seen:
                        raise ProjectError('Files contain duplicate or case-colliding paths.')
                    seen.add(path.casefold())
                    raw = file.file.read(MAX_UPLOAD - total + 1)
                    total += len(raw)
                    if total > MAX_UPLOAD:
                        raise ProjectError('Files exceed the 10 MB combined upload limit.')
                    archive.writestr(path, raw)
            # All input modes share the same exclusions, parser, indexing and resource limits.
            return ingest_archive(buffer.getvalue(), name[:200] or 'Uploaded files')
        finally:
            for file in files:
                file.file.close()

    @app.get('/api/projects/{project_id}', response_model=Project)
    def project(project_id: str):
        return store.get(project_id)[0]

    @app.get('/api/projects/{project_id}/files')
    def files(project_id: str):
        return store.get(project_id)[0].files

    @app.get('/api/projects/{project_id}/source')
    def source(project_id: str, path: str):
        _, sources = store.get(project_id)
        if path not in sources:
            raise HTTPException(404, 'Source file not found or was skipped.')
        return {'path': path, 'code': sources[path]}

    @app.get('/api/projects/{project_id}/files/{file_id}')
    def source_by_id(project_id: str, file_id: str):
        project, sources = store.get(project_id)
        matches = [f for f in project.files if f.file_id == file_id]
        if not matches:
            raise HTTPException(404, 'File not found in this project.')
        file = matches[0]
        if file.path not in sources:
            raise HTTPException(422, 'File was skipped; no source content is available.')
        return {'file_id':file_id, 'path':file.path, 'language':file.language, 'code':sources[file.path]}

    @app.delete('/api/projects/{project_id}', status_code=204)
    def delete(project_id: str):
        store.delete(project_id)

    @app.post('/api/context', response_model=ExplanationContext)
    def context(body: ContextRequest):
        return select_context(body)

    @app.post('/api/analyze', response_model=Explanation)
    async def explain(body: ContextRequest):
        context = await asyncio.to_thread(select_context, body)
        if app.state.generation_lock.locked():
            raise HTTPException(429, 'An explanation is already being generated. Try again after it completes.')
        async with app.state.generation_lock:
            try:
                result = await asyncio.wait_for(app.state.explainer.explain(context), timeout=inference_timeout)
                return validate_explanation(result, context)
            except ExplainerUnavailable as exc:
                raise HTTPException(503, str(exc)) from exc
            except TimeoutError as exc:
                raise HTTPException(504, 'Local explanation timed out.') from exc
            except (ValidationError, ValueError, TypeError) as exc:
                traceback.print_exc()
                raise HTTPException(502, 'AI response failed schema or source-line validation.') from exc
            except Exception as exc:
                traceback.print_exc()
                raise HTTPException(502, 'Local explanation service failed.') from exc

    @app.post('/api/questions', response_model=QuestionResult)
    async def question(body: QuestionRequest):
        if app.state.generation_lock.locked():
            raise HTTPException(429, 'Local AI is busy. Wait for the current request to finish.')
        async with app.state.generation_lock:
            try:
                messages, schema, context = await asyncio.to_thread(
                    build_question_prompt, body, getattr(app.state.explainer, 'budget', None))
                return await asyncio.wait_for(ask(app.state.explainer, messages, schema, context), timeout=inference_timeout)
            except ExplainerUnavailable as exc:
                raise HTTPException(503, str(exc)) from exc
            except TimeoutError as exc:
                raise HTTPException(504, 'Local AI Q&A timed out.') from exc
            except ProjectError:
                raise
            except KeyError:
                raise
            except (ValidationError, ValueError, TypeError) as exc:
                raise HTTPException(502, 'Local AI returned an invalid answer. Try a shorter question or source selection.') from exc
            except Exception as exc:
                raise HTTPException(502, 'Local AI Q&A failed. Try again.') from exc

    @app.post('/api/projects/{project_id}/analysis', response_model=BridgeAnalysisResponse)
    async def project_analysis(project_id: str, body: AnalysisBody):
        return await app.state.analysis.analyze(project_id, body)

    @app.get('/api/challenges')
    def challenge_list():
        return [app.state.challenges.public(item) for item in app.state.challenges.exercises.values()]

    @app.post('/api/challenges/select')
    def challenge_select(body: ChallengeSelect):
        languages = [body.language] if body.language else []
        concepts = body.concepts
        if body.file_id and not body.project_id:
            raise HTTPException(422, 'file_id requires project_id.')
        if body.project_id:
            project, _ = store.get(body.project_id)
            files = [f for f in project.files if f.status != 'skipped']
            if body.file_id:
                files = [f for f in files if f.file_id == body.file_id]
                if not files:
                    raise HTTPException(404, 'Eligible file not found in this project.')
            if body.language:
                files = [f for f in files if f.language == body.language]
            languages = sorted({f.language for f in files})
            concepts = {language:sorted({c for f in files if f.language == language for c in f.concepts}) for language in languages}
        if not languages:
            raise HTTPException(422, 'Supply a supported language or eligible project context.')
        return app.state.challenges.select(body, languages, concepts)

    @app.post('/api/challenges/submit')
    def challenge_submit(body: ChallengeSubmit):
        return app.state.challenges.submit(body)

    @app.post('/api/challenges/generate')
    async def challenge_generate(body: ChallengeSelect):
        if app.state.generation_lock.locked():
            raise HTTPException(429, 'Local AI is busy. Wait for the current request to finish.')
        async with app.state.generation_lock:
            try:
                return await asyncio.wait_for(generate_missing_line(app.state.explainer, app.state.challenges, body), timeout=inference_timeout)
            except ProjectError:
                raise
            except ExplainerUnavailable as exc:
                raise HTTPException(503, str(exc)) from exc
            except TimeoutError as exc:
                raise HTTPException(504, 'Local challenge generation timed out.') from exc
            except (ValidationError, ValueError, TypeError) as exc:
                raise HTTPException(502, 'AI challenge failed source-line or hint validation. Try generating again.') from exc
            except Exception as exc:
                raise HTTPException(502, 'Local challenge generation failed.') from exc

    @app.get('/api/challenges/{challenge_id}/hints/{level}')
    def hints(challenge_id: str, level: int):
        item = app.state.challenges.get(challenge_id)
        if not 1 <= level <= len(item['hints']):
            raise HTTPException(422, 'Hint level must be between 1 and 3.')
        return {'challenge_id':challenge_id, 'level':level, 'hints':item['hints'][:level], 'remaining':len(item['hints'])-level}

    @app.get('/api/challenges/{challenge_id}/solution')
    def solution(challenge_id: str):
        item = app.state.challenges.get(challenge_id)
        return {'challenge_id':challenge_id,'solution':item['solution'],'feedback':item['feedback']}

    frontend = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'
    if not (frontend/'index.html').is_file():
        frontend = Path(__file__).resolve().parent.parent / 'dist'
    if (frontend/'index.html').is_file():
        @app.get('/{resource:path}', include_in_schema=False)
        def frontend_asset(resource: str):
            if resource.startswith('api/'):
                raise HTTPException(404, 'API route not found.')
            target = (frontend/resource).resolve()
            if not target.is_relative_to(frontend.resolve()):
                raise HTTPException(404, 'File not found.')
            if target.is_file():
                return FileResponse(target)
            if Path(resource).suffix:
                raise HTTPException(404, 'Frontend asset not found.')
            return FileResponse(frontend/'index.html')
    return app

app = create_app()
