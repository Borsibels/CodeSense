# Sift backend

Local application logic for project ingestion, source indexing, static analysis,
file navigation, bounded context preparation, local Ollama explanations and curated
debugging challenges. No uploaded or submitted code is executed.

See [the team agreement](docs/backend-agreement.md) for the shared API schemas,
integration decisions, examples, limits and acceptance checklist.

## Run on Windows (Python 3.11+)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

The existing workspace already has a configured `.venv`. You can also start with
`powershell -File scripts/start.ps1`. For explanations, separately start Ollama with
the local `qwen2.5-coder:3b` model installed. This backend never downloads models.
Optional `OLLAMA_MODEL` selects another installed local model (`CODESENSE_MODEL` is
the older name and still works when `OLLAMA_MODEL` is unset; `:cloud` models are
refused). Both AI paths below read one shared configuration: model, `OLLAMA_BASE_URL`
(default `http://127.0.0.1:11434`) and a `4096`-token context window
(`OLLAMA_NUM_CTX`). They must agree, because Ollama reloads a model whenever
`num_ctx` changes between requests. A built `dist/` (or `frontend/dist`) is served
automatically when the backend starts.

API documentation: http://127.0.0.1:8000/docs. Dependencies need installation once;
the implemented backend has no runtime internet dependency. Run one server worker:
session state and AI concurrency control are process-local.

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python scripts/smoke_backend.py
```

The tested dependency snapshot is `requirements-lock.txt`; use it instead of
`requirements-dev.txt` to reproduce the verified environment. The current upstream
Starlette emits a deprecation warning for its httpx test client, but all tests pass.
FastAPI's optional `/docs` interface uses CDN assets; the backend's core JSON APIs
do not. Use `/openapi.json` as the offline machine-readable API specification.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/health` | Backend status and live local model readiness |
| POST | `/api/projects/upload` | Multipart `file`: project ZIP |
| POST | `/api/projects/files` | Repeated multipart `files`, JSON-list string `paths`, optional display `name`: source files or folder |
| POST | `/api/projects/snippet` | JSON `code`, `language`, optional `filename` |
| GET | `/api/projects/{id}` | Metadata, files and static relationships |
| GET | `/api/projects/{id}/files` | Indexed files, statuses and reasons |
| GET | `/api/projects/{id}/files/{file_id}` | Source by stable session file ID |
| GET | `/api/projects/{id}/source?path=app.js` | Selected file text |
| DELETE | `/api/projects/{id}` | Release a local project session |
| POST | `/api/context` | Prepare bounded context without calling AI |
| POST | `/api/analyze` | Call configured explainer, validate output (legacy; the UI uses the next row) |
| POST | `/api/projects/{id}/analysis` | Overview, explanation or possible-problem check of an uploaded session project (see below) |
| GET | `/api/challenges` | List public exercise metadata |
| POST | `/api/challenges/select` | Match verified exercise to language/concepts/level |
| POST | `/api/challenges/submit` | Check constrained solution without execution |
| GET | `/api/challenges/{id}/hints/{level}` | Reveal cumulative hints, levels 1-3 |
| GET | `/api/challenges/{id}/solution` | Explicitly reveal reviewed solution |

Context and analysis requests:

```json
{"project_id":"returned-id","file_id":"returned-file-id","scope":"block","start_line":1,"end_line":12,"difficulty":"beginner"}
```

Use `scope: "file"` without line numbers for a bounded file explanation, or
`scope: "project"` without a file ID for a metadata-only project overview. File IDs
are stable within a session. Project responses include an explicit file tree;
directory-only empty entries are not retained. Legacy path-based requests still work.
Files have `analyzed`, `partial`, or `skipped` status. Partial means a parser found
syntax errors, not that the program's behavior has been verified.

## AI integration contract

The default `OllamaExplainer` implements async `explain(context: ExplanationContext)
-> Explanation`. Custom adapters can be passed to `create_app(explainer=adapter)`.
The adapter owns inference, prompt construction and readiness checks. Source comments
and strings are untrusted data, never instructions. Context contains a selected
excerpt, up to two related excerpts, constructs, concepts, and static references.
The context selector caps source at 8,000 bytes. The adapter further requires its
complete instructions, schema and payload to fit 3,072 *estimated* tokens (the
`4,096`-token window minus 768 output tokens and a 256-token margin, counted with the
engine's deliberately pessimistic `ConservativeTokenEstimator`), with a 6,000-byte
hard ceiling. It cuts at line boundaries and reports omissions. At the 4,096 window
this carries roughly half as much source as the earlier 8,192-token setting did.

The returned object must have:

```json
{
  "overview": "This function adds two numbers.",
  "sections": [{"title":"Addition","explanation":"Adds a and b and returns the total.","start_line":1,"end_line":2}],
  "limitations": []
}
```

Section line numbers refer to original supplied excerpts, with file IDs added to
each section. The public response also adds `summary`, `concepts`, and
`source_references`. Metadata-only project explanations have no line sections.
Invalid AI responses return 502, unavailable inference 503,
generation timeout 504, and concurrent generation 429. Inference has a 90-second
timeout; adapters must cooperate with cancellation to stop work on timeout.
Health checks local model listing; that does not guarantee generation quality or
sufficient free memory. Ollama was unavailable during the latest real server smoke
test; live AI generation remains unverified. Simulated local HTTP tests verify the
adapter, response validation and failure behavior.

## Debugging challenges

`challenges/exercises.json` contains 12 reviewed exercises, three per language.
Structural validators accept the reviewed correction and reject starter code and
extra statements. They may reject equivalent alternative algorithms; the objectives
specify constrained edits. Passing a challenge does not establish general program
correctness. Progressive hints and solution reveal are driven by explicit UI actions.

## Bounds and limitations

- ZIP: 10 MB upload, 1,000 entries, 20 MB declared decompressed size; the entire
  HTTP request is capped at 11 MB before multipart parsing.
- Source: 30 eligible files, 100 KB each, 2 MB combined UTF-8 text.
- Path traversal, absolute paths, links, special entries, encrypted entries and
  duplicate/case-colliding paths are rejected. Archives are read in memory, never extracted.
- Unsupported files, `.env*` files and common dependency/build directories are skipped
  with reasons. Archives with no eligible source files are rejected.
- Eight sessions maximum; one hour of inactivity expires a session; restart clears state.
- HTML uses a forgiving parser. CSS and HTML line ranges identify opening constructs,
  not complete block spans. JavaScript supports plain JS, not guaranteed JSX/TypeScript.
- Static links, imports and DOM identifier matches are best-effort. DOM matches do not
  prove runtime association. Python imports support simple relative modules and a
  common enclosing ZIP directory; full package resolution and dynamic imports are not implemented.
- This is a single-user loopback backend. CORS permits the Vite development origins.
  It is not a public upload service or a hardened arbitrary-code execution sandbox.

## Integration status

Backend application logic is implemented and, as of 2026-10-10, exercised end to end
against a real local `qwen2.5-coder:3b` (upload, explain, possible problems, missing-line
challenge, verify, solution). Real model *quality*, and a full application restart with
Wi-Fi disabled, are still acceptance checks on the demo setup. Read
`docs/backend-agreement.md` before changing schemas.

## Project analysis: `POST /api/projects/{id}/analysis`

The Phase 4.5 analysis engine (`backend/app`, also runnable on its own as
`app.main:app` from `backend/`) is reached from the session host through
`backend/analysis_bridge.py`. The uploaded session project is repacked into an in-memory
ZIP and analysed by the engine's own pipeline, so nothing is uploaded twice and the
engine behaves exactly as it does behind `/api/ai/analyze`. `backend/_engine.py` aliases
the engine's top-level `app` package so both entry points work.

```json
{"intent":"explain","depth":"beginner","file_id":"returned-file-id","start_line":7,"end_line":8}
```

- `intent`: `overview` (whole project, optional focus file), `explain`, or `debug`
  (*possible problems*; unrelated to the missing-line challenges).
- `depth`: `beginner`, `intermediate`, `advanced` (`experienced` is accepted as an alias).
- `file_id` (or the transitional `path`) is required for `explain` and `debug`.
  `symbol` selects a function/class by name. Lines and `symbol` are mutually exclusive.
- **Selection mapping.** A line range is mapped to the smallest enclosing function,
  method or class; if none contains it (module-level code, a range across several
  definitions, or a name defined twice) the whole file is analysed. The response's
  `selection` object always preserves what was asked (`requested`), says what was
  analysed (`analyzed_symbol`, `analyzed_lines`), and sets `expanded: true` with a
  plain-language `note` (also the first entry of `limitations`) whenever the analysis is
  wider than the selection.
- The response is the engine's `AnalysisResponse` unchanged (AI-written fields are
  separated from deterministic ones; findings carry `tier` and `verification`; evidence
  excerpts are copied by the backend, never the AI) plus `project_id`, `file_id` and
  `selection`. A `source_verified` finding means the cited code is real and was shown to
  the AI, **not** that a bug exists.
- Errors use the host's `{code, detail}` envelope. Codes: `NOT_FOUND` (404),
  `INVALID_INPUT`/`INVALID_PROJECT`/`FILE_NOT_ANALYZED`/`SYMBOL_NOT_FOUND`/`NO_ANALYZABLE_SOURCE`/
  `PROMPT_TOO_MANY_TOKENS` (422), `AI_BUSY` (429), `OLLAMA_UNAVAILABLE`/`OLLAMA_CONNECT_TIMEOUT`/
  `MODEL_NOT_INSTALLED`/`ANALYSIS_UNAVAILABLE` (503, with `Retry-After` for the Ollama ones),
  `AI_TIMEOUT`/`GENERATION_TIMEOUT`/`PROJECT_PROCESSING_TIMEOUT` (504),
  `OUTPUT_TRUNCATED`/`INVALID_STRUCTURED_OUTPUT`/`INVALID_OLLAMA_RESPONSE`/`OLLAMA_UPSTREAM_ERROR` (502),
  `ANALYSIS_FAILED`/`ANALYSIS_BUDGET_ERROR` (500).
- **One inference gate.** The route takes the same `asyncio.Lock` as `/api/analyze` and
  `/api/challenges/generate`: a second model request while one is running gets `429 AI_BUSY`
  immediately. An analysis may make up to three model calls and is bounded at 300 s overall.
- `GET /api/health` adds `analysis: {available, reason}`; `available: false` means the
  token budget is too small for code analysis (raise `AI_MAX_INPUT_TOKENS`/the window).
- Files the session accepted but the engine will not analyse (inside `coverage/`,
  `.vscode/`, minified, or a path the engine considers unsafe, e.g. a component ending in
  a dot) are left out of the analysis and disclosed in `limitations`; selecting one
  returns `422 FILE_NOT_ANALYZED` with the reason. The engine reads line numbers the way
  the editor does (`\n`, `\r\n`, `\r`), so selected lines line up with the code shown.

## AI missing-line challenge

`POST /api/challenges/generate` accepts `project_id`, optional `file_id`, and `difficulty`. The local Ollama adapter selects one validated line from a bounded source excerpt and supplies three progressive hints. Source files must parse and fit the 9 KB exercise limit. The original uploaded source is unchanged; the challenge contains a placeholder copy, and the original becomes the structural answer key. Hints and solutions use the existing reveal endpoints. No AI service means 503, with no curated fallback in the guided frontend. Generation quality still needs review with the live model.
