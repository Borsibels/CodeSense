# Sift backend

React/Vite → **FastAPI** → `OllamaService` → Ollama → `qwen2.5-coder:3b`

Fully local: no cloud calls, no credentials, no internet needed once dependencies are installed and the model is pulled.

```
backend/
  app/
    main.py                     # create_app() + module-level `app` (uvicorn target app.main:app)
    config.py                   # OllamaSettings + ApiSettings + ProjectSettings: defaults and env overrides
    api/
      ai.py                     # GET /api/ai/health, POST /api/ai/generate
      projects.py               # POST /api/projects/inspect, /api/projects/context (ZIP body)
      analysis.py               # POST /api/ai/analyze (ZIP body): thin route over CodeAnalysisService
      schemas.py                # Pydantic request/response models (AI)
      project_schemas.py        # Pydantic response models (projects)
      errors.py                 # one error envelope; Ollama/budget/structured error -> HTTP mapping
      limiter.py                # bounded inference concurrency (API layer)
      inference.py              # run_structured() under the limiter; token-usage audit
      middleware.py             # host guard, request guards, 500 safety net (CORS-aware order)
      docs.py                   # offline /docs (vendored Swagger UI)
    services/
      ollama_service.py         # OllamaService (framework-independent)
      token_budget.py           # TokenBudget + ConservativeTokenEstimator (reusable)
      structured.py             # StructuredGenerator: schema-validated JSON with bounded retries
      project_ingestion.py      # secure in-memory ZIP validation + bounded reads
      project_discovery.py      # supported files, exclusion reasons, decoding
      python_analysis.py        # ast-based analysis
      js_analysis.py            # conservative JavaScript scanner (heuristic)
      web_analysis.py           # HTML (html.parser) + CSS scanner
      static_analysis.py        # dispatch + per-file output caps
      dependency_graph.py       # edges, resolution statuses, cycles, entry points
      context_selection.py      # which files/regions matter, in what order
      context_assembly.py       # token-budgeted context + coverage report
      project_pipeline.py       # ZIP bytes -> analysis (no Ollama, no disk)
      project_models.py         # shared dataclasses
      analysis_models.py        # Phase 4: model-facing draft schemas + public response models
      analysis_prompts.py       # Phase 4: versioned templates, delimiting, control-token defusing
      evidence.py               # Phase 4: deterministic source-reference validation
      glossary.py               # Phase 4: plain-language definitions (beginner depth)
      code_analysis_service.py  # Phase 4: orchestration (context -> prompt -> model -> validation)
    static/swagger/             # vendored Swagger UI + PROVENANCE.md + licenses
  scripts/check_ollama_live.py  # standalone live check of the service (no FastAPI)
  scripts/check_projects_live.py  # live check of /api/projects/* against a real uvicorn process
  scripts/check_analysis_live.py  # live evaluation of /api/ai/analyze against the real model
  tests/                        # all mocked; no Ollama, GPU or internet needed
```

## Setup (PowerShell)

Python 3.11+ is **tested**; 3.10 is the **theoretical** minimum (see [Python versions](#python-versions)).

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
python -m pip install -r requirements-dev.txt   # fastapi, uvicorn, pydantic, httpx + pytest
```

Ollama must be installed separately, with the model already pulled (the backend never downloads models):

```powershell
ollama pull qwen2.5-coder:3b     # one-time, needs internet
ollama list                      # confirm it is installed
```

## Run the API

From the `backend/` directory:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API starts even if Ollama is not running; `GET /api/ai/health` then reports `unavailable`. Stop it with `Ctrl+C` (clean shutdown, exit code 0, Ollama client closed). Keep the default `127.0.0.1` bind; see [Security](#security).

## Server health vs. model readiness vs. inference

| Question | How to tell |
|---|---|
| Is the **API** running? | `GET /api/ai/health` returns HTTP 200 at all. |
| Is **Ollama** reachable? | `ollama.healthy` is `true`. |
| Is the **model** installed? | `model.available` is `true` (`false` = not installed, `null` = could not be checked). |
| Can it actually **generate**? | Only `POST /api/ai/generate` proves that. `status: "ready"` means "everything needed is present", not "a generation just succeeded". |

The health endpoint never runs inference and never waits for the inference slot, so it stays responsive during a generation.

## Endpoints

### `GET /api/ai/health`

Diagnostic. **Always HTTP 200** while the API is up; readiness is in the body.

```json
{
  "status": "ready",
  "ollama": { "healthy": true, "version": "0.40.2", "detail": null },
  "model":  { "name": "qwen2.5-coder:3b", "available": true, "detail": null }
}
```

| `status` | Meaning |
|---|---|
| `ready` | Ollama reachable and model installed. |
| `degraded` | Ollama reachable, but the model is missing (`available: false`) or could not be checked (`available: null`). |
| `unavailable` | Ollama not reachable (`model.available` is `null`, not checked). |

### `POST /api/ai/generate`

Integration-test interface, not the final analysis endpoint. The model and all generation settings are fixed server-side; unknown fields (such as `model`) are rejected with 422. The body must be JSON (`Content-Type: application/json`).

```json
{ "prompt": "Explain what a Python for loop does." }
```

Response `200`: `{"model": "qwen2.5-coder:3b", "response": "...", "status": "completed"}`.
`status` is `"truncated"` when the model hit its output-token limit and `response` is only a partial answer (still HTTP 200).

The prompt must pass **two** size checks, both answered `422`: the character limit (`PROMPT_TOO_LONG`) and the estimated-token budget (`PROMPT_TOO_MANY_TOKENS`). Prompts are rejected, never truncated.

### `GET /docs`, `GET /openapi.json`

Interactive API docs and the OpenAPI schema. Both work offline. `/redoc` is disabled (see [Offline documentation](#offline-documentation)).

### Error format

Every error, including validation errors and unknown routes, has this shape:

```json
{ "error": { "code": "OLLAMA_UNAVAILABLE", "message": "The local AI service (Ollama) is unavailable. Start Ollama and try again." } }
```

Validation errors add a `details` list and never echo submitted input. Messages are fixed strings; stack traces, paths and raw Ollama responses stay in the server log.

| HTTP | `code` | Cause |
|---|---|---|
| 422 | `INVALID_REQUEST` | Empty/blank/missing/non-string prompt, malformed JSON, unknown field |
| 422 | `PROMPT_TOO_LONG` | Prompt longer than `AI_MAX_PROMPT_CHARS` |
| 422 | `PROMPT_TOO_MANY_TOKENS` | Estimated tokens exceed the input budget (the message says it is an estimate) |
| 400 | `INVALID_HOST` | `Host` header is not an allowed local name |
| 413 | `REQUEST_TOO_LARGE` | Body larger than `API_MAX_BODY_BYTES` (`PROJECT_MAX_ZIP_BYTES` on the two ZIP routes) |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Body not sent as `application/json` (`application/zip` on the ZIP routes) |
| 503 | `AI_BUSY` | No inference slot free within `AI_QUEUE_WAIT_TIMEOUT` (`Retry-After: 5`) |
| 503 | `OLLAMA_UNAVAILABLE` / `OLLAMA_CONNECT_TIMEOUT` | Ollama not reachable / connect timed out |
| 503 | `MODEL_NOT_INSTALLED` | Model missing; message includes the `ollama pull` command |
| 503 / 502 | `OLLAMA_UPSTREAM_ERROR` | Ollama answered 503 (overloaded) / any other HTTP error |
| 504 | `GENERATION_TIMEOUT` | Generation exceeded `OLLAMA_READ_TIMEOUT` |
| 502 | `INVALID_OLLAMA_RESPONSE` | Ollama's reply was malformed, incomplete or empty |
| 502 | `INVALID_STRUCTURED_OUTPUT` | Structured generation failed validation on every allowed attempt |
| 502 | `OUTPUT_TRUNCATED` | Structured output was cut off at the token limit |
| 500 | `INFERENCE_FAILED` / `INTERNAL_ERROR` | Other Ollama-side failure / unexpected backend bug |
| 404 / 405 | `NOT_FOUND` / `METHOD_NOT_ALLOWED` | Unknown route / wrong method |
| 413 / 415 / 422 / 503 / 504 | `ARCHIVE_*`-style codes, `FILE_*`, `SYMBOL_NOT_FOUND`, `PROJECT_BUSY`, `PROJECT_PROCESSING_TIMEOUT` | See [Project analysis](#project-analysis-phase-3) |
| 422 | `NO_ANALYZABLE_SOURCE` | `/api/ai/analyze`: the archive has no supported source, or none of it fit the input window |
| 503 | `ANALYSIS_UNAVAILABLE` | `/api/ai/analyze`: `AI_MAX_INPUT_TOKENS` is too small for the analysis instructions (other endpoints are unaffected) |

## Project analysis (Phase 3)

Turns an uploaded project folder (as a ZIP) into deterministic facts and a **token-budgeted context** for the model. It never calls Ollama, never executes anything from the archive, and keeps nothing after the request.

```
Project ZIP -> Secure ingestion -> File discovery -> Static analysis -> Dependency graph
            -> Context selection -> Token-budgeted context  (-> Phase 4: AI analysis)
```

> **Static analysis is not execution and not full understanding.** It reads the text of the files and reports what the syntax says: which functions exist, which imports are written, which files they point at. It cannot know what happens at run time, which dependency version is installed, what a dynamic `require(name)` loads, or whether the program is correct. Everything is labelled `confirmed` (a real parser produced it) or `heuristic` (a lightweight scanner produced it); treat `heuristic` as "probably right".

### Why a raw ZIP body (API choice)

The routes take the ZIP as the **raw request body** (`Content-Type: application/zip`), with options in the query string, rather than JSON-with-base64 or `multipart/form-data`:

* a browser can send it with `fetch(url, { method: "POST", body: file })`; base64-in-JSON would inflate it by 33 % and push a 10 MiB project past a 13 MiB JSON body;
* no multipart parser (`python-multipart`) is installed, and the size limit is enforced **while the body streams**, before anything is buffered beyond the cap;
* the 128 KiB JSON-only limit protecting every other route stays untouched: the two upload paths get a *route-specific* policy (`BodyPolicy` in `app/api/middleware.py`: cap = `PROJECT_MAX_ZIP_BYTES`, content types `application/zip`, `application/x-zip-compressed`, `application/octet-stream`).

### `POST /api/projects/inspect`

Body: the ZIP. Returns `InspectResponse`:

| Field | Content |
|---|---|
| `notice` | The "static analysis only" disclaimer |
| `summary` | Archive/source byte counts, files per language, symbol/reference counts, diagnostic counts |
| `files[]` | One per analysed source file: `path`, `language`, `size_bytes`, `line_count`, `parser`, `confidence`, `symbols[]` (name, qualified name, kind, `start_line`/`end_line`, signature, parent, `exported`), `references[]` (kind, specifier, line), `exports[]`, `diagnostics[]` (severity, code, message, line), plus HTML (`title`, inline script/style counts, `element_ids`), CSS (`css_classes`, `css_ids`) and Python (`doc`, `has_main_guard`, `has_syntax_error`) extras |
| `excluded[]` | **Every** archive file that was not analysed, with `reason` and `detail` |
| `graph` | `nodes[]`, `edges[]` (source, specifier, kind, line, `status`, `target`, `external_kind`, `reason`), `cycles[]`, `entry_points[]`, `external_dependencies[]` |

The response contains names, line numbers and signatures, **not source code**. Identical archives give byte-identical responses (everything is sorted; nothing depends on archive order, clocks or dict ordering).

### `POST /api/projects/context`

Body: the ZIP. Query parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `intent` | `overview` | `overview`, `explain` or `debug` (what the context is *for*; no inference is run) |
| `file` | none | Project-relative path of the selected file. **Required** for `explain` / `debug` |
| `symbol` | none | A symbol inside `file`: `handler`, or `Class.method` (a bare method name also matches). Requires `file` |
| `instruction_reserve_tokens` | `700` (`PROJECT_INSTRUCTION_RESERVE_TOKENS`) | Tokens kept free for the future system prompt, task instructions, JSON-schema guidance and chat formatting |

Returns `ContextResponse`: `context` (the text), `budget`, `coverage_summary`, `files[]` (per-file coverage), `omitted_regions[]`, `omitted_region_total`, `warnings[]`, `target_file`, `target_symbols`, `project` (summary) and the same `notice`.

Unknown files or symbols are `422` (`FILE_NOT_FOUND`, `FILE_NOT_ANALYZED` with the exclusion reason, `SYMBOL_NOT_FOUND` listing the symbols that do exist, `FILE_REQUIRED`).

### PowerShell examples

Build the ZIP with `tar` (built into Windows 10+, writes `/` separators) and leave dependency folders out; the archive entry limit is 500:

```powershell
cd C:\path\to\myproject
tar -a -c -f $env:TEMP\myproject.zip --exclude node_modules --exclude .git --exclude .venv --exclude dist --exclude build .
$zip = "$env:TEMP\myproject.zip"

# inspect: files, symbols, diagnostics, dependency graph
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/projects/inspect -ContentType "application/zip" -InFile $zip
$r.summary | ConvertTo-Json
$r.graph.edges | Where-Object status -eq "missing" | Format-Table source, line, specifier
$r.excluded | Format-Table path, reason

# context for one symbol, ready to be placed in a prompt
$c = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/projects/context?file=src/app.py&symbol=handler&intent=debug" -ContentType "application/zip" -InFile $zip
$c.context
$c.budget | ConvertTo-Json
$c.files | Where-Object status -ne "not_included" | Format-Table path, status, estimated_tokens

# an invalid upload: Invoke-RestMethod throws on 4xx, so catch it to see status and body
try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/projects/inspect -ContentType "application/zip" -InFile C:\Windows\win.ini }
catch { "HTTP $([int]$_.Exception.Response.StatusCode)"; $_.ErrorDetails.Message }
```

`Compress-Archive` also works (Windows PowerShell 5.1 writes `\` separators; they are normalised, not rejected), but it cannot exclude folders; copy the sources somewhere first.

### Supported files

`.py`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.html`, `.htm`, `.css` (case-insensitive). TypeScript is **not** analysed (`.ts`/`.tsx` appear in `excluded` as `unsupported_extension`). Paths are `/`-separated and relative to the archive root; every list is sorted by path.

Every archive file is either in `files` or in `excluded` with a reason; nothing is dropped silently:

| `reason` | When |
|---|---|
| `ignored_directory` | Any path segment is `node_modules`, `.git`, `.venv`, `venv`, `__pycache__`, `dist`, `build`, `.next`, `coverage` (case-insensitive), or one of the extras `.pytest_cache`, `.mypy_cache`, `.tox`, `.idea`, `.vscode`, `__MACOSX` |
| `generated_file` | `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, ...; `*.bundle.js`, `*.chunk.js`, source maps |
| `minified` | `*.min.js` / `*.min.css`, or a JS/CSS file with a line over 1000 characters (or an average over 200 for files above 2 KB) |
| `unsupported_extension` | Anything else: images, `.json`, `.md`, `.ts`, ... |
| `too_large` | A supported file over `PROJECT_MAX_FILE_BYTES` (512 KiB). It is not decompressed |
| `binary_content` | NUL bytes, or mostly control characters |
| `invalid_encoding` | Not UTF-8 (a UTF-8 BOM is accepted; UTF-16 and Windows-1252 are not) |

Line endings are normalised to `\n` so line numbers mean the same to every analyser.

### ZIP requirements and security limits

Nothing is extracted to disk. The archive stays in memory (at most `PROJECT_MAX_ZIP_BYTES`); metadata is validated first, and file contents are decompressed on demand through a bounded stream whose real length must equal the declared length. Only supported, in-limit source files are ever decompressed.

| Check | Default | Setting | Error `code` (HTTP) |
|---|---|---|---|
| Archive size | 10 MiB | `PROJECT_MAX_ZIP_BYTES` | `REQUEST_TOO_LARGE` (413; also enforced on streamed bodies) |
| Entries (files + folders) | 500 | `PROJECT_MAX_ENTRIES` | `TOO_MANY_ENTRIES` (422); read from the end-of-central-directory record *before* the directory is parsed |
| Total uncompressed size (declared) | 25 MiB | `PROJECT_MAX_UNCOMPRESSED_BYTES` | `UNCOMPRESSED_TOO_LARGE` |
| Any single entry | 5 MiB | `PROJECT_MAX_ENTRY_BYTES` | `ENTRY_TOO_LARGE` |
| One supported source file | 512 KiB | `PROJECT_MAX_FILE_BYTES` | not fatal: excluded as `too_large` |
| Compression ratio (per entry and overall) | 100:1 | `PROJECT_MAX_COMPRESSION_RATIO` | `SUSPICIOUS_COMPRESSION`; only applied at 64 KiB or more (`PROJECT_RATIO_FLOOR_BYTES`) because tiny payloads say nothing about bombs |
| Overlapping entries (compressed sizes add up to more than the file) | | | `SUSPICIOUS_COMPRESSION` |
| Path length / depth | 240 chars / 32 levels | `PROJECT_MAX_PATH_CHARS` / `_DEPTH` | `UNSAFE_PATH` |
| Not a ZIP / corrupt / bad CRC / truncated | | | `INVALID_ARCHIVE` |
| No entries | | | `EMPTY_ARCHIVE` |
| Encrypted entry | | | `ENCRYPTED_ARCHIVE` |
| Symlink, device, FIFO, socket, unsupported compression (only stored/deflate) | | | `UNSUPPORTED_ENTRY` |
| Duplicate path, case-insensitive or Unicode-normalisation collision, a name that is both file and folder | | | `DUPLICATE_PATH` |
| `..`, absolute `/x`, drive `C:`, `:` (alternate data streams), `\` traversal, empty or `.` components, trailing dot/space, control / bidi characters | | | `UNSAFE_PATH` |

Names that would need "fixing" to be safe are rejected rather than repaired. Error messages escape hostile names. The ZIP signature must be at byte 0 (no prefixed polyglots). File names, extensions and MIME types are never trusted: only the bytes are validated.

Two more error codes: `PROJECT_BUSY` (503, `Retry-After: 5`) when more than `PROJECT_MAX_CONCURRENT` (2) analyses run at once and none frees up within `PROJECT_QUEUE_WAIT_TIMEOUT` (10 s); `PROJECT_PROCESSING_TIMEOUT` (504) when a project exceeds `PROJECT_PROCESSING_TIMEOUT` (30 s).

### Static analysis

| Language | Method | Extracted | `confidence` |
|---|---|---|---|
| Python | stdlib `ast` (parse only) | imports (module, names, relative level, line, top-level or not), functions, async functions, classes, methods (qualified `Class.method`), top-level variables, signatures, first docstring line, decorator-inclusive line ranges, `__main__` guard, **syntax errors with line**, star-import and dynamic-import notes | `confirmed` |
| JavaScript | conservative tokenizer + bracket matcher | ES `import`/`export` (named, default, re-exports, `export *`), CommonJS `require`/`module.exports`/`exports.x`, dynamic `import('x')`, top-level functions, arrow functions, classes and their methods, variables, line ranges | `heuristic` |
| HTML | stdlib `html.parser` (tokenizer; never runs scripts) | title, `<script src>`, stylesheets, other local resources (img, icons, ...), inline script/style counts, element ids, `<base href>` note | `confirmed` |
| CSS | comment/string-aware scanner | `@import`, `url()` references, rules with selector summaries and line ranges (also inside `@media`), class and id names | `heuristic` |

**Known limitations**

* *JavaScript is not parsed.* It never reports a JS syntax error, only scan warnings (`unterminated_string`, `unbalanced_brackets`). Regex-vs-division is decided from the previous token; automatic semicolon insertion is approximated; very long comma-joined statements are cut after 80 top-level steps; TypeScript, Flow and JSX syntax are not understood (`.jsx` files are scanned as plain JS with a `jsx_heuristic` note, so ranges can be off).
* Only *top-level* JS declarations and class methods become symbols; nested functions, object-literal methods and `this.x = function` do not.
* `require(name)`, `import(path)`, `importlib.import_module(x)` and `__import__(x)` are reported as notes, not edges: the target is decided at run time.
* Python names assigned dynamically (`globals()[...]`, `setattr`), decorators that rewrite functions, and conditional definitions are seen as written.
* CSS is not compiled: `@import` inside strings or unusual at-rules may be missed, nested CSS rules are not separate symbols, custom-property values are not analysed.
* HTML inline `<script>`/`<style>` bodies are counted, not analysed.
* Per file at most 3000 symbols, 5000 references, 1000 exports are returned (a `*_truncated` diagnostic says so); 200 top-level Python variables are listed.
* Deeply nested Python can exceed the interpreter's parser limit; that is reported as `unparseable`.

### Dependency graph

Every import/script/stylesheet/`@import`/`url()` becomes an edge with a `status`:

| `status` | Meaning |
|---|---|
| `resolved` | Points at an analysed file (`target` is set) |
| `excluded` | The target exists in the archive but was not analysed (`reason` says why: `.png`, `too_large`, `.ts`, ...) |
| `missing` | An explicit relative path / relative import that matches nothing in the archive: a **confirmed dangling reference** |
| `external` | A package, standard-library module (`stdlib`), Node builtin (`builtin`) or URL (`url`); not local |
| `unresolved` | Cannot be decided statically: bundler aliases (`@/x`, `~/x`), a bare specifier that names a local folder (possible base-URL alias), root-relative web paths (`/app.js`: the server root is unknown), namespace packages, paths leaving the project |

Resolution rules: JS tries the exact path, then `.js .mjs .cjs .jsx .json .ts .tsx`, then `index.*` in a folder. Python resolves `import a.b` / `from a import b` / relative imports to `a/b.py` or `a/b/__init__.py`, trying the importer's folder, then each parent up to the root (so a zip of a parent folder still works), then `src/`; `from pkg import name` also links the submodule `pkg/name.py` if it exists. An absolute Python import that finds nothing is an assumed installed package (`external`), unless its first component exists locally (then `missing`). HTML/CSS paths resolve against the file's own folder; query strings and fragments are stripped.

Also reported: `cycles` (strongly connected components via an iterative Tarjan, plus one concrete example loop each), `entry_points` (heuristic: `__main__` guards, `index.html`, conventionally named files nothing imports) and `external_dependencies`.

Limits: this reads text. It does not run a package manager, honour `sys.path`/`PYTHONPATH`, `package.json` (`main`, `exports`, aliases), `tsconfig` paths, bundler config, conditional imports or `<base href>`. Absence of an edge does not prove independence; presence of `resolved` means "a file by that name exists", not "the program loads it".

### Context selection

Deterministic and rule-based; no embeddings, vector store or scoring model. Candidates are tried in this fixed order until the budget is spent (`intent` changes what is requested from related files, not who comes first):

* **`explain`**: (1) the selected file, or the selected symbol plus the imports header of its file; (2) direct dependencies as signature **outlines**; (3) usage sites in files that import the target (the symbols there that mention the target's names, else an outline); (4) entry points (outline); (5) when a symbol was selected, an outline of the rest of its file; (6) remaining files, most-imported first (outlines).
* **`debug`**: (1) as above; (2) the **code of the dependency symbols the target actually mentions** (top-level functions/classes matched by name, at most 12 per file); (3) outlines of the direct dependencies; (4) usage sites; (5) entry points; (6) outline of the selected file; (7) neighbouring symbols (previous/next sibling, as code); (8) remaining files.
* **`overview`** (no file needed): (1) outline of the focus file, if given; (2) entry points; (3) most-imported files; (4) every other file (all as outlines); then, only if room remains, code of the focus file and of the entry points.

Related files are never concatenated wholesale: symbol-level regions (functions, classes, methods, CSS rules) or signature outlines are used, in a fixed order (tier, then dependency order, then path, then line).

### Token budget and coverage

* Same budget object as Phase 2.5: `4096 - 1024 - 256 = 2816` **estimated** input tokens (`TokenBudget.input_limit`), counted with the existing `ConservativeTokenEstimator` (no second estimator; it over-estimates ordinary code roughly 1.5-3.5x, so the real prompt is usually smaller).
* `context_limit = input_limit - instruction_reserve`. With the default reserve of 700 tokens, **2116 estimated tokens** are available for the context text. The reserve covers the future system instructions, task text, JSON-schema guidance and chat template; code never uses it.
* The finished text is re-counted as a whole and trimmed from the lowest priority end if the per-block sums under-estimated. `budget.estimated_tokens <= budget.context_limit` always holds; `is_estimate` is always `true`.
* **No silent truncation.** Each file appears as `FILE path [FULL: all N lines]` or `FILE path [PARTIAL: lines 1-9, 30-44 of 120]`, with `[... lines 10-29 not included ...]` at every gap and numbered source lines (`  31 | code`) that are the real line numbers. Signature-only sections are headed `OUTLINE ... bodies are NOT included` and say `(+N more entries not listed)` when cut.
* If a region does not fit it is split at symbol boundaries (a file into its functions, a class into its methods). A single oversized target symbol contributes its leading lines (at least three). An oversized *selected file* gets an outline of the whole file first (at most 30 % of the budget), then code up to half of what remains; leftovers are given back at the end. A single line over 400 characters is clipped with an explicit marker (and a warning).
* Coverage per file: `full` (every non-blank line present), `partial` (some code; `omitted_ranges` lists the rest), `outline_only` (signatures, no bodies), `not_included`. `omitted_regions` lists every requested region/outline that did not make it in (first 200; `omitted_region_total` is the real count). `warnings` repeats the important caveats ("N files are only partially included; do not treat them as fully analysed").
* Syntax errors found by the Python parser are placed in the text (`NOTE: static analysis reports SyntaxError ... at line N`).

### Performance (measured, not promised)

Measured on the development machine (Windows 11, Python 3.14, localhost; `scripts\check_projects_live.py`, median of 15 over real HTTP):

| Input | Archive | `inspect` | `context` (overview) |
|---|---|---|---|
| 5-file Python project | 0.9 KiB | 2.6 ms | 3.8 ms |
| 9-file mixed Python/JS/HTML/CSS project | 2.2 KiB | 3.8 ms | 5.8 ms |
| This backend (`app/*.py`, 28 files, ~480 symbols) | 79.5 KiB | 94 ms | 91 ms |
| 40-function module (758 lines, over budget) | 1.2 KiB | 17 ms | 20 ms |

Single-file worst cases (in-process, 512 KiB of text): JavaScript ~0.3-0.35 s, Python ~0.6-0.8 s, CSS ~0.1 s, HTML ~0.16-0.18 s; adversarial JS (100 000 unmatched brackets, 20 000 comma-joined declarations, ...) finishes in under 2 s. A 4 MiB ZIP of one binary file is accepted in about 25 ms; the server's working set did not visibly change. These are small compared with local LLM inference, but they are single-machine numbers, not guarantees. CPU-bound work runs in a worker thread (`asyncio.to_thread`) so the event loop stays responsive; at most `PROJECT_MAX_CONCURRENT` analyses run at once, each stops at `PROJECT_PROCESSING_TIMEOUT`, and nothing quadratic is used for graph building (hash lookups), cycle detection (iterative Tarjan) or context packing.

### Offline operation

Everything in this section uses the standard library plus the existing dependencies: no network, no Ollama, no model. The endpoints answer while Ollama is stopped (`GET /api/ai/health` then reports `unavailable`, and the project routes are unaffected). The live check opens a fake Ollama listener and asserts the project routes never connect to it.

## AI code analysis (Phase 4)

`POST /api/ai/analyze` explains or debugs the code in a project ZIP with the local model and returns a structured, **evidence-checked** answer. It reuses everything from Phases 1-3: the raw-ZIP upload and its limits, the project pipeline, context selection/assembly, the token budget, `StructuredGenerator` and the inference limiter. The HTTP transport (`OllamaService`) is untouched.

```
ZIP -> secure ingestion -> static analysis -> dependency graph -> context selection (all Phase 3, no model)
    -> prompt (versioned, delimited, control tokens defused)
    -> ONE inference slot: Qwen2.5-Coder 3B, schema-constrained JSON (<= 2 tries, + 1 compact fallback)
    -> schema validation -> evidence validation (deterministic) -> flat JSON response
```

> **AI-generated findings are suggestions to investigate, not defects.** The model is a 3B network. In testing it reported something in *every* run on code that had no bug (see [Known model accuracy limits](#known-model-accuracy-limits)). A finding marked `source_verified` means the file, the lines and the quoted code are real and were shown to the model; it **never** means the bug is confirmed.

### Who it is for: plain English by default

Sift is aimed at people with little or no programming background (vibecoders, AI-assisted developers, students). So `depth` defaults to `beginner`:

| `depth` | Audience | What the answer looks like |
|---|---|---|
| `beginner` (default) | Plain English | Starts with what the code accomplishes in everyday words, walks through it step by step, says how it helps the application, defines unavoidable terms, and ends with **one** programming concept to learn. Adds an analogy only if the model thinks it truly fits. For `debug`: what may be wrong, what could happen, the likely cause and a direction to fix it, always worded as a suspicion. |
| `intermediate` | Developer | What it does and why, control flow, key concepts, inputs/outputs, how it uses related code. |
| `advanced` | Technical | Architecture, dependencies, tradeoffs, edge cases, technical implications. |

The enum values are unchanged from the original contract (`beginner` / `intermediate` / `advanced`). Beginner answers are also given a small **deterministic glossary** (see below), because prompting alone did not make the model define its terms.

### Request

`POST /api/ai/analyze?intent=&depth=&file_path=&symbol=` with the ZIP as the raw body (`Content-Type: application/zip`, same 10 MiB cap and content types as Phase 3).

| Query | Values | Notes |
|---|---|---|
| `intent` | `overview` \| `explain` \| `debug` | default `explain`. `overview`: what the project does. `explain`: a file or symbol. `debug`: possible bugs. |
| `depth` | `beginner` \| `intermediate` \| `advanced` | default `beginner` |
| `file_path` | project-relative path, max 300 chars | required for `explain` and `debug` (`FILE_REQUIRED`) |
| `symbol` | e.g. `handler`, `Class.method`, max 200 chars | needs `file_path` |

The model, temperature, token limits and every generation option are fixed by the server; the endpoint accepts no overrides (extra query parameters are ignored).

### Response (flat)

Every field is labelled in `/docs` as **AI-generated** or **deterministic** (computed by the backend from the real project; the model contributes nothing to those).

| Field | Source | Meaning |
|---|---|---|
| `status`, `notice`, `intent`, `depth`, `target_file`, `target_symbols` | deterministic | `notice` is the fixed "advice, not proof" reminder |
| `summary` | AI for `explain`/`overview`; **deterministic for `debug`** (Phase 4.5) | overview in the requested depth's language; for `debug` it is built from `debug_outcome` and `coverage`, and the AI's own debug summary is discarded |
| `debug_outcome` | deterministic (Phase 4.5) | `debug` only: `no_clear_problem` or `possible_problems`; `null` otherwise. **`no_clear_problem` is not proof that the code is bug-free.** |
| `pattern_checks[]` | deterministic (Phase 4.5) | `debug` only: hits from a few hand-written rules, each with its `assumptions` (see "Phase 4.5" below) |
| `relationships[]` | deterministic (Phase 4.5) | `explain`/`overview` only: `{from, to, kind, resolved}` dependency edges from the parsers (`kind`: `uses`, `loads`, `entry_point`) |
| `analogy`, `role_in_app`, `concept_to_learn {name, explanation}` | AI | `beginner` explain/overview only; otherwise `null` |
| `explanations[]` | AI text, validated location | ordered steps/parts: `title`, `description`, `file_path`, `start_line`, `end_line`, `location_status`, `location_issue`. Empty for `debug`. |
| `findings[]` | AI text, validated evidence, deterministic `tier` | `debug` only, at most 3, ordered `possible_problem` first (see below) |
| `assumptions[]` | AI | what the AI says it could not confirm |
| `glossary[]` | deterministic | `{term, meaning}`, at most 8: plain-language meanings of common programming words that appear in the AI's text, written by Sift. `beginner` depth only. |
| `limitations[]` | deterministic | what could not be seen or checked (partial files, signature-only files, skipped files, defused control sequences, instruction-like text in the source, compact fallback, the debug caution) |
| `coverage` | deterministic | `full_files`, `partial_files` (included/omitted ranges), `outline_only_files`, `not_included_files` (+ total), `excluded_files` (+ total), `context_/instruction_/prompt_estimated_tokens`, `input_limit`, `is_estimate: true` |
| `generation` | deterministic | `model`, `prompt_version`, `attempts` (model calls), `mode` (`standard` or `compact`), Ollama's `prompt_eval_count` / `eval_count` |
| `timings_ms` | deterministic | `project`, `context`, `inference`, `validation`, `total` |

A finding:

```json
{
  "id": "F1",
  "title": "Skips the first price",
  "category": "off_by_one",
  "problem": "The loop starts at index 1, so it may skip the first price.",
  "what_could_happen": "The total might be too low.",
  "likely_cause": "The range starts at 1 instead of 0.",
  "suggestion": "Start counting at 0.",
  "severity": "medium", "confidence": "medium",
  "verification": "source_verified",
  "file_path": "cart.py", "start_line": 7, "end_line": 7,
  "evidence": { "source_excerpt": "    for i in range(1, len(prices)):", "excerpt_start_line": 7, "excerpt_end_line": 7, "excerpt_matched": true },
  "evidence_issue": null,
  "tier": "possible_problem", "tier_reasons": ["CORROBORATED_BY_RULE"]
}
```

`tier` and `tier_reasons` (Phase 4.5) are deterministic **presentation heuristics, not correctness guarantees**; see "Phase 4.5" below.

**Severity** (`low` | `medium` | `high`) is the AI's qualitative view of the impact *if* the problem is real. **Confidence** (`low` | `medium` | `high`) is the AI's qualitative view of its own claim. It is not a probability and is not calibrated; the backend lowers it (`unsupported` -> `low`, an unmatched quote -> at most `medium`). `category` is one of `logic`, `condition`, `off_by_one`, `variable_usage`, `edge_case`, `null_access`, `api_misuse`, `type_assumption`, `integration`, `other`.

### Evidence verification

Each reference the model makes is checked against the **uploaded project** and against **exactly what was put in the prompt**:

| `verification` | Meaning |
|---|---|
| `source_verified` | The file exists, the lines are in range, **all of them were shown to the model**, and the code it quoted really appears in them. Proves the *citation*; says nothing about whether the bug is real. |
| `hypothesis` | The location is valid and was shown, but the quote is missing, trivial (under 8 letters/digits) or does not match. `evidence_issue` is `NO_EXCERPT` or `EXCERPT_MISMATCH`. |
| `unsupported` | The location itself is invalid. It is **removed** (never "corrected"), `confidence` becomes `low`, and `evidence_issue` says why: `NO_LOCATION`, `FILE_NOT_IN_PROJECT`, `FILE_EXCLUDED`, `LINES_OUT_OF_RANGE`, `LINES_NOT_IN_CONTEXT` (the lines exist but the model was never shown them) or `LINES_OUTLINE_ONLY` (the model saw only a signature). |

Rules that never bend: line numbers are never clamped, shifted or searched for (a real line quoted under the wrong line number is simply a `hypothesis`); the excerpt returned is copied by the backend from the real file, never the model's own quote; excluded files and parts of a partially included file that the model never saw are not "inspected evidence"; coverage statistics are never taken from the model. `explanations[].location_status` is `in_context`, `outline_only` (the range matches a symbol the model saw only as a signature), `file_only`, `none`, or `rejected`. **`in_context` means the lines exist and were shown, not that the model picked the best lines.**

### Prompt templates and versioning

`app/services/analysis_prompts.py` holds the templates as module constants (never built in routes). `PROMPT_VERSION` (currently `analysis-v2`; v2 changed only the explain/overview wording, the debug templates are byte-for-byte the v1 ones and are pinned separately) is returned in `generation.prompt_version`, and a test pins a digest of every rendered template, so changing a word forces a version bump. Layout: role, short rules, task, audience wording, field guide, then the source between `BEGIN_SOURCE_<nonce>` / `END_SOURCE_<nonce>`, then a reminder *after* the source that it is data, not instructions. There are no few-shot examples (they would cost 200-400 estimated tokens); the reply is constrained to the schema by Ollama's `format`, and the model sees each JSON key as it writes the value.

**Prompt injection.** Uploaded code is untrusted. Measured on the real tokenizer, `<|im_end|>`, `<|im_start|>`, `<|endoftext|>`, `<|fim_*|>`, `<tool_call>` and `</tool_call>` inside the text become real control tokens, and a file containing a fake system turn made the model answer with the attacker's word. The prompt builder therefore inserts a space into every such sequence (`< |im_end| >`), in file contents *and* file paths, and the evidence check applies the same transform before comparing. This is verified live (`scripts\check_analysis_live.py` prints the token counts: 19 per 20 raw sequences vs 60-120 defused). Delimiters + the trailing reminder + output validation reduce the risk but **cannot remove it**: a model can still be talked into repeating a claim like "this project is secure" in its free-text `summary`, which no validator can fully catch. When instruction-like text or control sequences are found in the source the response says so in `limitations`.

### Token budget

Same budget as Phase 3: `4096 - 1024 - 256 = 2816` **estimated** input tokens. The instruction reserve is sized **per request** from the real template plus a 160-token retry allowance and 16 of slack (never below the configured `PROJECT_INSTRUCTION_RESERVE_TOKENS`, default 700). Beginner templates are longer, so they leave less room for code:

| intent / depth | template (est. tokens) | reserve | context for code |
|---|---|---|---|
| explain, `intermediate` / `advanced` | 469 / 463 | 700 | 2116 |
| explain, `beginner` | 775 | 951 | 1865 |
| overview, `beginner` | 784 | 960 | 1856 |
| debug, `intermediate` / `advanced` | 742 / 744 | 918 / 920 | 1898 / 1896 |
| debug, `beginner` | 907 | 1083 | 1733 |

The finished prompt is checked as a whole (`ensure_fits`). If concatenation overshoots, the context is rebuilt once with a larger reserve; if it still does not fit the API answers `500 INTERNAL_ERROR` (a server bug) and nothing is sent, never a silent overflow. A retry is only attempted if the retry prompt, with a maximum-length realistic rejection reason, provably fits. Measured: real `prompt_eval_count` is 1.4-1.96x smaller than the estimate (largest observed 1797 real tokens, plus 1024 for output, is under 4096). If `AI_MAX_INPUT_TOKENS` is so small that less than 1000 estimated tokens remain for code, only this endpoint refuses (`503 ANALYSIS_UNAVAILABLE`, message names the variable); every other endpoint keeps working.

### Retry and truncation behavior

* One inference slot is held for all model calls of a request; ZIP ingestion and validation happen outside it. A busy slot answers `503 AI_BUSY` (with `Retry-After`) before anything is sent to the model.
* Standard attempt: at most **one** corrective retry for invalid JSON / schema violations (`AI_STRUCTURED_MAX_RETRIES` is capped at 1 for this endpoint).
* Connectivity and timeout errors are never retried.
* If the answer is **cut off at the 1024-token limit**, repeating the same request would be pointless, so the strategy changes once: a smaller schema (at most 2 sections or 1 finding, shorter texts) with a "be brief" instruction, no retry, only if less than 150 s have been used. `generation.mode` becomes `compact` and `limitations` says so. If that is cut off too: `502 OUTPUT_TRUNCATED`.
* Hard maximum: **3 model calls** per request.
* AI prose longer than its cap is clipped with an ellipsis rather than rejected (a rejection costs a full regeneration); structure, enums, line numbers and evidence stay strict. Ollama's schema grammar cuts text at the cap mid-word, so text that nearly fills its cap without a sentence ending is also marked with an ellipsis.

### Errors

Existing envelope `{"error": {"code", "message"}}`.

| Status | Codes |
|---|---|
| 413 / 415 | `REQUEST_TOO_LARGE`, `UNSUPPORTED_MEDIA_TYPE` (ZIP body policy) |
| 422 | `INVALID_REQUEST`, `FILE_REQUIRED`, `FILE_NOT_FOUND`, `FILE_NOT_ANALYZED`, `SYMBOL_NOT_FOUND`, the Phase 3 archive codes, **`NO_ANALYZABLE_SOURCE`** (no supported source, or none of it fit the input window) |
| 503 | `PROJECT_BUSY`, `AI_BUSY`, `OLLAMA_UNAVAILABLE`, `OLLAMA_CONNECT_TIMEOUT`, `MODEL_NOT_INSTALLED`, **`ANALYSIS_UNAVAILABLE`** (token budget configured too small) |
| 504 | `GENERATION_TIMEOUT`, `PROJECT_PROCESSING_TIMEOUT` |
| 502 | `OUTPUT_TRUNCATED`, `INVALID_STRUCTURED_OUTPUT`, `INVALID_OLLAMA_RESPONSE`, `OLLAMA_UPSTREAM_ERROR` |
| 500 | `INTERNAL_ERROR` (including a prompt that cannot fit after a rebuild) |

### PowerShell examples

```powershell
# zip a project folder (any tool works), then ask for a plain-English explanation of one file
Compress-Archive -Path .\myproject\* -DestinationPath $env:TEMP\myproject.zip -Force
$u = "http://127.0.0.1:8000/api/ai/analyze?intent=explain&file_path=cart.py"   # depth defaults to beginner
$r = Invoke-RestMethod -Method Post -Uri $u -ContentType "application/zip" -InFile $env:TEMP\myproject.zip
$r.summary; $r.explanations | Format-Table title, description; $r.concept_to_learn; $r.glossary | Format-Table

# look for possible bugs in one function, developer wording
$u = "http://127.0.0.1:8000/api/ai/analyze?intent=debug&depth=intermediate&file_path=cart.py&symbol=total_price"
$r = Invoke-RestMethod -Method Post -Uri $u -ContentType "application/zip" -InFile $env:TEMP\myproject.zip
$r.findings | Format-List id, title, verification, start_line, end_line, problem, suggestion
$r.limitations

# what does the whole project do?
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/ai/analyze?intent=overview" -ContentType "application/zip" -InFile $env:TEMP\myproject.zip

# errors: Invoke-RestMethod throws on 4xx/5xx
try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/ai/analyze?intent=debug" -ContentType "application/zip" -InFile $env:TEMP\myproject.zip }
catch { "HTTP $([int]$_.Exception.Response.StatusCode)"; $_.ErrorDetails.Message }   # 422 FILE_REQUIRED
```

### Offline operation

Nothing here uses the network. Only the model call needs Ollama (running locally); ingestion, selection, prompt building, validation, the glossary and all error handling work without it, and the endpoint answers `503 OLLAMA_UNAVAILABLE` while Ollama is stopped. Uploaded code is analysed in memory only: it is never executed, stored or logged, and neither are model replies.

### Live evaluation (real `qwen2.5-coder:3b`)

```powershell
python backend\scripts\check_analysis_live.py --runs 3 --depths beginner            # every development fixture
python backend\scripts\check_analysis_live.py --runs 1 --depths intermediate,advanced
python backend\scripts\check_analysis_live.py --fixtures off_by_one_py --intent debug --runs 5
python backend\scripts\check_analysis_live.py --mode inprocess --split dev --intent debug --runs 5 --out dev.json
python backend\scripts\check_analysis_live.py --rescore dev.json                    # re-score recorded drafts, no model
```

`--mode http` (default) starts a real uvicorn process and sends real HTTP requests; `--mode inprocess` runs the same application in the harness process and also **captures the model's raw draft** (the API never returns it) so deterministic changes can be re-scored offline with `--rescore`, which replays the drafts through the real `CodeAnalysisService`. Fixtures live in `tests\analysis_fixtures.py` (development) and `tests\heldout_fixtures.py` (sealed, `--split heldout --final`, run once). The harness scores debug findings against planted ground truth by line span and by function, measures the clean-code `no_clear_problem` rate, the corrected jargon rate, relationship and identifier checks, latency, token counts, peak VRAM and backend memory, writes a JSON report outside the repo, and prints tables. It also probes the real tokenizer for control-token defusing. Valid JSON is not treated as a correct analysis: explanation quality additionally needs a human; `--review-sheet` writes a blinded, shuffled review sheet.

### Known model accuracy limits

Observed on the development machine (Windows 11, RTX 3050 Ti 4 GB, Ollama 0.40.2, 56 live requests, small samples, not benchmarks):

* **False positives on clean code are the norm.** On a defect-free file the model returned a finding in 5 of 5 debug runs (and gave an empty list in 0 of 20 clean-code runs in total, including runs with stricter wording), and most of those findings were `source_verified` because they cited real lines. Treat every finding as something to check.
* **It finds obvious bugs but cites them imprecisely.** The deliberate Python off-by-one was cited in 7/7 runs and its quote verified every time; the JS reversed-comparison bug was cited in only 1/5 (the model points at the `for` line above it). Cited line numbers are often off by one or two and are never repaired, so such findings become `hypothesis`.
* **Explanations are usually right on small code but can contain wrong details** (for example calling a separate function a "method of the class" or saying a value "increases by 1 each time" when it does not), and `role_in_app` is often generic speculation.
* **Beginner wording is only partly plain.** The structure is followed (what it does -> steps -> role -> concept) but technical words remain: about 6.6 undefined technical words per 100 words even after the stronger wording (9.4 before). The glossary exists for this reason. Analogies were produced in 0 of 26 beginner explanations.
* The window is small: roughly 80-120 lines of code per request. Large projects are covered by outlines and honest `coverage`, not by analysis of every file.
* A single 400-file import cycle fills the Phase 3 context preface and leaves no room for code; the API then answers `422 NO_ANALYZABLE_SOURCE`.

### Performance (measured, not promised)

56 live requests, one development machine: median total **4.7 s** (beginner 5.7 s, intermediate 3.9 s, advanced 4.4 s), maximum 11.1 s. Per stage, medians: project inspection 1.6 ms, context assembly 2.5 ms, evidence validation 0.7 ms, **model inference 4.6 s**. The model is effectively the whole cost; typical replies are 200-450 generated tokens (maximum observed 693 of 1024). Ingestion cost grows with project size (100 files x 300 lines: 0.29 s; 450 files x 400 lines: 1.8-2.3 s) and stays small next to inference. Output truncation never happened at the real 1024-token limit in these runs. Lowering `OLLAMA_NUM_PREDICT` to provoke it: at 230 the compact fallback succeeded in 5 of 6 requests; at 150 both attempts were cut off and the API answered `502 OUTPUT_TRUNCATED` after exactly two model calls (about 5 s).

## Phase 4.5: fewer false bug reports, better-grounded explanations

Phase 4 measured that a source-verified finding is not a bug: on clean code the model produced a finding in every run and almost all of them cited real lines. Phase 4.5 keeps the single model call and the **unchanged debug prompt** (three redesigned debug prompts were tried on the real model; each cut false positives only by losing real bugs; the byte-for-byte digest of the debug templates is pinned in `tests/test_analysis_prompts.py`) and adds deterministic layers **after** the model call. No second model call, no extra model, no new dependency, no network.

### What was added

| Piece | Where | What it does |
|---|---|---|
| Finding tiers | `app/services/finding_triage.py` | Sorts findings into `possible_problem` (first) and `worth_checking`. **A presentation heuristic, not a correctness guarantee. Demoted findings are never removed or edited.** |
| Deterministic outcome and summary | `finding_triage.py` | `debug_outcome` is `no_clear_problem` or `possible_problems`. The debug `summary` is built from it plus the real coverage; the AI's own debug summary is discarded. |
| Bug-pattern rules | `app/services/bug_patterns.py` | A few rules found with `ast` (Python) and the Phase 3 token stream (JavaScript), each stating the assumptions it needs. Reported separately as `pattern_checks`, never mixed into the AI `findings`, never sent to the model. |
| CODE MAP | `app/services/context_assembly.py` | `explain`/`overview` only: the parser's exact list of the selected file's classes, methods, functions and cross-file imports, at most 120 estimated tokens, counted inside the context budget. Debug context has none. |
| Dependency `relationships` | `code_analysis_service.py` | `explain`/`overview` only: graph edges `{from, to, kind, resolved}` from the parsers. |
| Prompt `analysis-v2` | `analysis_prompts.py` | Explain/overview only: use the CODE MAP, describe only the selection, and write `role_in_app` as one sentence grounded in the dependency lines (or the fixed sentence "Nothing else in this upload uses this file."). |
| Glossary | `glossary.py` | Words with two meanings (`element`, `attribute`, `list`, `class`) define both senses. |

### Tiers and reason codes

`tier` and `tier_reasons` on each finding. A finding is a `possible_problem` when its citation was verified, the cited lines are not an example block, and the claim is not about unseen input; or when an independent rule fired on the same lines. Otherwise it is `worth_checking`.

| Reason | Effect | Meaning |
|---|---|---|
| `QUOTE_NOT_MATCHED` | demotes | the place is real but the AI's quoted code does not match it |
| `LOCATION_NOT_VERIFIED` | demotes | the AI pointed at a place that does not exist or that it was not shown |
| `IN_DEMO_CODE` | demotes | the lines are inside an `if __name__ == "__main__":` block (found from the Python AST) |
| `INPUT_ASSUMPTION` | demotes | the title or first 90 characters of the problem is about empty / missing / unexpected input, validation, edge cases or division by zero. **This is a brittle wording heuristic**; a genuine missing input check is a real kind of bug and is demoted, not hidden. |
| `CORROBORATED_BY_RULE` | promotes | a `pattern_checks` rule fired on the same lines (overrides the demoting reasons, which stay listed) |

`debug_outcome` is `possible_problems` only if a `possible_problem` finding or a `problem_if_assumptions_hold` pattern check exists. **`no_clear_problem` always says in words that the examined code has not been proven bug-free**; the summary also states exactly which lines the AI saw and how many files it did not.

### Pattern checks (hand-written, high-precision, assumption-stating)

| Rule | Strength | Assumes |
|---|---|---|
| `PY_INDEX_PAST_END`: `for i in range(len(x) + k)` reading `x[i]` | problem if assumptions hold | `x` is a list/tuple/string; the loop is not left early (skipped when guarded by a length test, a `try/except IndexError`, or when `x` is resized in the loop) |
| `PY_SKIPS_FIRST_ITEM`: `range(1, len(x))` reading `x[i]` | worth checking | the loop should visit every item (skipped for `best = x[0]` idioms, neighbour reads like `t[i - 1]`, and loops that fill another table) |
| `PY_IS_LITERAL`: `is` / `is not` against a string or number literal | problem if assumptions hold | the intent was equality |
| `PY_MUTABLE_DEFAULT`: a default list/dict/set changed by a method call (`items.append(x)`) or `+=` | problem if assumptions hold | called more than once without that argument; sharing is not a deliberate cache |
| `PY_SYNTAX_ERROR`: Python cannot read the file | problem if assumptions hold | the file is meant to be Python 3 for this server's Python |
| `JS_INDEX_PAST_END`: `for (...; i <= x.length; i++)` reading `x[i]` | problem if assumptions hold | `x` is an array/string; the loop is not left early |
| `JS_ASSIGN_IN_CONDITION`: `if (x = 5)`, a **constant** assigned in an `if` | problem if assumptions hold | the `=` is a typo for `===` |
| `JS_NAN_COMPARE`: `x === NaN`, `x != NaN`, ... | problem if assumptions hold | the intent was to detect NaN |
| `MISSING_LOCAL_FILE`: a local script/stylesheet/import not in the upload | worth checking | the upload is the whole project; no build step creates the file |

Every hit carries its assumptions (always shown with it), whether it was found with Python's parser (`confirmed`) or the lightweight JavaScript scanner (`heuristic`), a backend-copied source excerpt, and whether the AI was shown those lines (`shown_to_ai`; a rule reads the whole selected file or symbol, so it can flag lines the AI never saw). **A rule hit is never presented as a definite defect.** Intent-dependent bugs (a flipped comparison, a wrong formula, `> 18` where the comment says "18 or older") are deliberately not detected: no structural rule can know the intent, and a rule for them would only overfit the fixtures.

**Precision gate.** Each rule has positive and negative tests and must not fire on the clean fixtures or on this backend's own code (`tests/test_bug_patterns.py`). Two rules were narrowed after a sweep over real code, because their first versions fired on deliberate idioms: a default dict written as a cache (`cache[key] = value`, 4 hits in the Python standard library) and `if (match = re.exec(s))` (19 of 19 hits in `node_modules`). After narrowing: 0 false alarms over 1,600 standard-library files (902k lines), 1,899 JavaScript files in `node_modules` (222k lines) and this backend; the one standard-library hit is a file that is deliberately invalid Python.

### Explain and overview

`relationships` (above) and the CODE MAP come from the parsers, so the AI is told which methods belong to which class and which files use the selected file, instead of inferring them. The AI still writes the prose and can still be wrong; see the measured results and limits below.

### Measured results (real `qwen2.5-coder:3b`, one development machine, small samples)

All numbers are exploratory: 5 runs per fixture, fixtures written by the team, and the model's output varies run to run. **Development-fixture numbers are in-sample** (the triage wording was tuned looking at that kind of code). The sealed held-out numbers were produced once, after all tuning, and are the more honest estimate, but the held-out fixtures were also written by the implementing agent (three clean ones are verbatim CPython standard-library excerpts), so they are not an independent benchmark.

| Debug, clean code | Phase 4 behaviour | Phase 4.5 `no_clear_problem` | Non-bug items in the top tier per run |
|---|---|---|---|
| Development, 35 clean runs (in-sample) | 0/35 | **23/35 (66%)**, 95% CI 49-79% | 0.34 |
| Development, plain clean fixtures only (25 runs) | 0/25 | 22/25 (88%) | 0.12 |
| **Sealed held-out, 25 clean runs** | 0/25 | **13/25 (52%)**, 95% CI 33-70% | **0.60** |

* Targets: >=60% in-sample (met), >=50% held-out (met by a thin margin, interval wide), <=0.5 non-bug items per clean run (met in-sample, **missed held-out: 0.60**).
* Held-out per fixture: `ho_colors` 4/5, `ho_cart_js` 4/5, `ho_filter` 2/5, `ho_inventory` 2/5, `ho_search` 1/5. The false claims that still reach the top tier on held-out code are mostly labelled `off_by_one` (12 of 15), not the "empty/None input" claims the wording heuristic recognises: that heuristic is tuned to failure modes of tiny code, and generalises only partly.
* Two kinds of clean file defeat triage by design: code whose comment asks for a false report (`clean_injection`, 0/5) and code whose intent is unknowable (`ambiguous_slice`, 1/5 abstained, i.e. 4/5 overclaims).
* Of the findings placed in the top tier, 15 of 36 (development) and 12 of 45 (held-out) were the planted bug. The top tier is a priority list, not a list of bugs.

| Debug, real bugs | Result |
|---|---|
| Citation recall, the 5 development fixtures that have a Phase 4 baseline | 15/25 (60%) vs 12/20 (60%) from the recorded Phase 4 drafts; every cited bug was in the top tier |
| Why recall cannot fall | All 48 debug prompts (24 fixtures x 2 depths) built by the pre-change code and by the current code are byte-for-byte identical, and triage never removes or edits a finding |
| Held-out bugs | 10/20 cited (`ho_scores_js` 5/5 and `ho_tags` 5/5, both with a rule hit; the mutated `bisect` and the floor-division paging bug 0/5, as intent-dependent bugs always were) |
| Real bug demoted by wording | 1 of the 39 live runs in which the planted bug was cited (`cross_file_call`, described as a "null check"; still shown, with `INPUT_ASSUMPTION`) |
| Reassurance risk | On buggy files, `no_clear_problem` came out in 15/35 development runs and 1/20 held-out runs. In none of them had the model cited the bug (so Phase 4 would have shown 23 unrelated findings there too), and the summary says it is not proof, but a user can still be falsely reassured. |

Structural: 188 live requests (development, held-out, real-socket HTTP, all three depths), 188 HTTP 200, 188 schema-valid, 0 retries, 0 compact fallbacks, 0 truncations, 0 `unsupported` findings of 271, 0 prompt-injection leaks; control tokens still defused on the real tokenizer.

Latency (median / p90 / max, seconds): development debug 4.0 / 9.0 / 13.2 (the first call includes model load); real-socket debug 3.7 / 8.6 / 10.3; explain 5.3 / 6.2 / 7.9; overview 5.6. **Held-out debug median 7.9 s (target 5.5 s): missed**, because those files are longer real code and the model wrote a median 542 tokens instead of 255. The new deterministic layers cost a median 2.2 ms (maximum 57 ms). Peak VRAM 3.2 GiB of 4 GiB (the model is resident); backend memory about 69 MiB.

Explain v2, a same-day control (Phase 4 prompt and context vs `analysis-v2`, 15 answers each on `simple_loop`, `multi_file_py`, `partial_project`; deterministic checks, not a blind human review):

| | Phase 4 prompt | `analysis-v2` |
|---|---|---|
| `role_in_app` that is a useful, grounded sentence | 10/15 | **15/15** |
| `role_in_app` that is generic speculation | 5/15 | 0/15 |
| `cart.py` summary credits another file's formatting to it | 2/5 | 0/5 |
| relationship contradictions / invented identifiers | 0 / 0 | 0 / 0.07 per answer |
| undefined technical words per 100 words (corrected metric) | **7.05** | 8.13 (**worse**) |
| median latency | 5.3 s | 5.8 s |

The first wording of the role instruction made the model copy the dependency lines (`depends on: None`); that was found by reading the answers, fixed, and is why the instruction now asks for one full sentence. The jargon target (-20%) was **not** met; the CODE MAP's "class/method/function" vocabulary is the suspected cause (not tested). 98% of the remaining undefined technical words are covered by the glossary entry shown next to the answer. The corrected jargon metric no longer counts "list", "loop" or "return" as jargon (Phase 4's headline was inflated by "list"); the Phase 4 baseline re-measured with it is 7.3 per 100 words.

### How the evaluation works

`scripts\check_analysis_live.py` (see "Live evaluation") scores debug findings against planted ground truth by line span and by function, and records the model's raw drafts in `--mode inprocess` so triage, rule and glossary changes can be re-scored offline (`--rescore`, `--rescore-baseline`) by replaying them through the real service. Fixtures: `tests/analysis_fixtures.py` (development, which the heuristics WERE tuned against, so results on them are in-sample) and `tests/heldout_fixtures.py` (sealed, hash-pinned in `tests/data/heldout_manifest.json`, run once with `--split heldout --final`).

## Token-aware context budgeting

`app/services/token_budget.py` (`TokenBudget`, `ConservativeTokenEstimator`) is reusable for Phase 3 context assembly:

```python
budget = TokenBudget(total_context=4096, reserved_generation=1024, safety_margin=256)   # input_limit == 2816
check = budget.ensure_fits(prompt)    # raises PromptTooLargeError; never truncates
check.tokens, check.is_estimate, check.remaining
```

* **Split:** `num_ctx` (4096) − `num_predict` (1024) − safety margin (256, `AI_SAFETY_MARGIN_TOKENS`) = **2816** estimated input tokens. `AI_MAX_INPUT_TOKENS` can lower this, never raise it. Impossible arithmetic (output + margin ≥ context, or an input limit above what is available) stops the server at startup with a `ConfigError`.
* **Estimates, not counts.** No tokenizer or vocabulary file is available offline and Ollama 0.40.2 has no tokenize endpoint (`/api/tokenize` → 404), so exact counts are impossible before a request. Every result carries `is_estimate=True`.
* **The estimate** (details in the module docstring) prices character classes: runs of letters at ⌈len/2.5⌉, digits and punctuation at the 1-token-per-ASCII-character ceiling, hash-like runs per character, non-ASCII at 1.25 (2.0 for emoji). The old 6000-character limit stays as a cheap first guard.
* **Chat-template overhead** is about 29 tokens (measured: `"a"` is 1 token raw, 30 templated). It is not added to estimates; it sits inside the 256-token margin.

**Evidence** (ground truth = Ollama's `prompt_eval_count` with `raw=true, num_predict=1`, qwen2.5-coder:3b, Ollama 0.40.2):

* 290 measured samples (144 + 73 + 73; Python, JS/TS, CSS, HTML, minified code, prose, markdown, lockfile hashes, CJK/Cyrillic/Arabic/Hindi/Thai text, emoji, code with non-ASCII comments, hex, base64, UUIDs, digit lists, random symbol noise, long identifiers, whitespace-heavy text): **none was under-estimated** by the final estimator. The tightest were space-separated and plain digit lists at exactly 1.00× of real, then random noise at 1.02×.
* Ordinary code and prose are over-estimated by about **1.5-3.8×** (Python: mean 2.0×, range 1.6-3.5×). That is the cost of safety: the 2816-token budget admits roughly 1200-1300 real tokens of ordinary code.
* **The budget protects the window:** prompts grown to the full 2816-token estimate (Python, JS, hex, base64, symbol noise, digits, CJK, emoji) used 1226-2845 real tokens including the template; with a full 1024-token answer the worst case totalled 3869 of 4096, leaving **227 tokens of headroom**.

**Limitations (be careful):**

* It is calibrated, not proven. The three corpora overlap in source files (different slices and seeds), so they are not independent. The final weights were tuned on the first two; the third only re-sliced the same files. Unusual text could tokenize denser than anything measured.
* Adversarial input sits at the edge: space-separated digits and random printable noise measured at ≈1.0× of real. The safety margin and the audit below are the backstop.
* **Runtime audit:** after each `/api/ai/generate`, the server compares the estimate with Ollama's reported `prompt_eval_count` and logs a `WARNING` if the estimate was too low or if prompt + reserved output exceeds the window. `prompt_eval_count` excludes tokens Ollama served from its prompt cache, so it can under-report; the audit is advisory and log-only.

To re-measure a file (PowerShell 5.1: read the file with .NET; `Get-Content -Raw` serialises as an object):

```powershell
$text = [System.IO.File]::ReadAllText("$PWD\app\config.py")
$body = @{ model="qwen2.5-coder:3b"; prompt=$text; raw=$true; stream=$false; options=@{num_predict=1; num_ctx=8192} } | ConvertTo-Json -Depth 4
(Invoke-RestMethod -Method Post -Uri http://127.0.0.1:11434/api/generate -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))).prompt_eval_count
```

`tests/data/token_samples.json` holds 39 real measurements that the test suite checks the estimator against.

## Structured JSON generation

`StructuredGenerator` (`app/services/structured.py`) asks Ollama for schema-constrained output (its `format` field) and validates the reply against a Pydantic model. There is intentionally **no public endpoint** for it yet; Phase 3 routes should call `app.api.inference.run_structured(app, prompt, Model)`, which holds one inference slot for all attempts.

* Success requires **valid JSON that validates against the model**. Syntax alone is never enough.
* **Retries:** at most `AI_STRUCTURED_MAX_RETRIES` extra attempts (default 1, maximum 5), never recursive. A retry resends the original prompt plus one fixed sentence quoting a short reason built from field names and messages only (never the model's offending values).
* **Not retried:** any Ollama/transport failure (down, timeout, HTTP error, malformed Ollama reply) and **output truncated at the token limit** (`OUTPUT_TRUNCATED`): the same prompt would hit the same limit and burn another full generation. A complete, schema-valid object is accepted even if `done_reason` was `length`.
* Each attempt's prompt is budget-checked first; a retry prompt that would not fit is skipped.
* **Live result (real qwen2.5-coder:3b):** 5/5 prompts succeeded on the first attempt (flat schema, and a nested schema using `$defs`, a list and an enum), 0.5-5.7 s each; the forced-truncation case (`num_predict=24`) raised `OUTPUT_TRUNCATED` after one attempt with no retries spent. The schema guarantees **form, not correctness**: one nested result contained plainly wrong findings. The retry path was exercised with mocked replies only, since the real model never produced an invalid reply in these runs.

## Concurrency

By default exactly **one** generation runs at a time (4 GB VRAM). Further requests wait without blocking the event loop for up to `AI_QUEUE_WAIT_TIMEOUT` seconds, then get `503 AI_BUSY`. The slot is released on success, error, timeout and cancellation. Requests that fail validation never take a slot.

## Offline documentation

FastAPI's built-in docs load Swagger UI from a CDN and render blank offline. Here `/docs` is replaced by a page that references only `/docs-assets/*`, served from a fixed whitelist of vendored files (`app/static/swagger/`, **swagger-ui-dist 5.33.1, Apache-2.0**, byte-for-byte; hashes, source and licences in `PROVENANCE.md`, checked by a test). No directory is mounted; unknown names are 404. Swagger UI's validator badge (which would call `validator.swagger.io`) is disabled. `/redoc` is disabled rather than vendored (another ~1 MB for little gain). `/openapi.json` is unchanged.

Verified in headless Edge with every non-loopback host name blocked (`--host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"`): `/docs` rendered the full UI (both operations listed), while FastAPI's default docs under the same block did not.

## Security

Intended deployment: the backend and its browser on one machine, reached over loopback.

* **Bind address:** the documented command uses `127.0.0.1`. **Binding to `0.0.0.0` exposes an unauthenticated API to your whole network**: anyone who can reach the port can use your GPU, read model output, and probe the service. Don't do it on shared networks. If the frontend ever runs on another device, that needs its own security design (authentication, TLS, host/origin policy); it is deliberately not solved here.
* **Loopback is not authentication.** Any program or user on the machine can call the API. **CORS is not authentication either**: it only tells *browsers* which pages may read responses; it does nothing against `curl`, scripts or other local processes.
* **Browser-borne attacks on a local server are handled:**
  * *Cross-site "simple" requests* (a malicious web page POSTing `text/plain` without a preflight): non-JSON bodies are refused with 415 before any handler runs.
  * *DNS rebinding* (a hostile domain re-pointed to 127.0.0.1): the `Host` header must be `127.0.0.1`, `localhost` or `[::1]` (`API_ALLOWED_HOSTS`); anything else is `400 INVALID_HOST`.
  * *CORS* is an exact allow-list (`API_CORS_ORIGINS`, Vite on :5173/:4173 by default), never `*`, no credentials, only `GET`/`POST` and the `Content-Type` header. Unexpected 500s now carry the same CORS headers (see below), so browsers show the real error.
* **Size limits:** prompt characters, estimated prompt tokens, and a request-body cap (`API_MAX_BODY_BYTES`, default 128 KiB) enforced from `Content-Length` and while streaming, before parsing. The two ZIP routes have their *own* cap (`PROJECT_MAX_ZIP_BYTES`, 10 MiB) and content types through a per-route `BodyPolicy`; the 128 KiB JSON-only rule still applies to every other route (tested both ways).
* **No filesystem or command access:** the only routes are health, generate, the two project routes, docs and the whitelisted docs assets. Tests assert the exact route set, that probes like `/api/files` or `/.env` return 404, that path traversal on the docs assets fails, and that app source contains no `subprocess`/`eval`/`exec`/`shutil`/file-upload constructs. Only `docs.py` reads files from disk (a fixed whitelist); `project_ingestion.py` calls `ZipFile.open` on an in-memory buffer and a test asserts the ingestion modules never use `Path`, `os`, `tempfile` or `extractall`. Uploaded projects are parsed, never imported, compiled, executed or fetched (URLs found in uploaded HTML are recorded, not requested).
* **Callers cannot change the model or generation settings:** body fields, query parameters and headers were all tested; only the server environment controls them.
* **Middleware order** (`app/main.py`): HostGuard → CORS → RequestGuard → UnexpectedError → routes. Starlette runs a plain `Exception` handler in its outermost layer, *outside* CORS, so 500s lost their CORS headers and browsers reported a misleading CORS failure. A middleware inside the CORS layer now produces the standard 500 envelope instead.

Not addressed (out of scope here): authentication, TLS, rate limiting beyond the single inference slot, audit logging.

## Windows shutdown exit code

`Ctrl+C` stops the server with **exit code 0** and a complete shutdown (verified with a real console Ctrl+C). Under `CTRL_BREAK` (what test harnesses send) uvicorn exits with code **3**. Investigation:

* Shutdown is complete either way: lifespan shutdown ran, the Ollama client was closed, and `python -X dev` showed no `ResourceWarning`/unclosed-resource output.
* The code comes from uvicorn, not from this app: after a graceful stop, `Server.capture_signals` restores the original handler and re-raises the captured signal (`signal.raise_signal`). A 10-line control script doing exactly that exits 3 on Windows after `CTRL_BREAK`; the same script without the re-raise exits 0.
* No code change: suppressing it would hide real failures. If a supervisor must treat 3 as success after `CTRL_BREAK`, handle it there.

## Python versions

| Version | Status |
|---|---|
| 3.14.7 | **Tested** (development interpreter, 700 tests + live runs) |
| 3.12, 3.11 | **Tested** (Phase 3): all 700 tests pass in clean virtualenvs with the latest resolvable dependencies. Phase 2.5 recorded the same for 3.12.0 / 3.11.9 with FastAPI 0.143.0, Starlette 1.7.0, Pydantic 2.14.0, Uvicorn 0.54.0 |
| 3.10 | **Not run** (not installed). Theoretical minimum: FastAPI, Starlette, Uvicorn, anyio and pytest all declare `Requires-Python >=3.10`. Source parses with the 3.10 grammar and uses no 3.11+ stdlib APIs (static check only) |
| < 3.10 | Unsupported (dependencies refuse it) |

The declared dependency **minimums** were also run on Python 3.12: FastAPI 0.115.0 (Starlette 0.38.6), Uvicorn 0.30.0, Pydantic 2.7.0, httpx 0.27.0, pytest 8.0.0 → all 700 tests pass (re-verified for Phase 3).

Phase 3 note: `time.monotonic()` has ~15 ms resolution on Windows before Python 3.13, so the processing deadline uses `time.perf_counter()`; the deadline test uses a fake clock.

## Configuration (environment variables)

Defaults live in `app/config.py`. Settings are read at startup; a bad value stops the server with a message naming the variable.

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama server |
| `OLLAMA_MODEL` | `qwen2.5-coder:3b` | Model used for all requests |
| `OLLAMA_NUM_CTX` | `4096` | Context window (tokens) |
| `OLLAMA_NUM_PREDICT` | `1024` | Max generated tokens |
| `OLLAMA_TEMPERATURE` | `0.2` | Sampling temperature |
| `OLLAMA_CONNECT_TIMEOUT` / `_READ_TIMEOUT` / `_WRITE_TIMEOUT` / `_POOL_TIMEOUT` | `5` / `120` / `10` / `5` | Seconds |
| `OLLAMA_HEALTH_READ_TIMEOUT` | `10` | Read timeout for health / model-list calls |
| `AI_MAX_PROMPT_CHARS` | `6000` | Max prompt characters (first guard) |
| `AI_SAFETY_MARGIN_TOKENS` | `256` | Tokens kept free beyond the reserved output |
| `AI_MAX_INPUT_TOKENS` | derived (`2816`) | Optional lower input-token limit |
| `AI_MAX_CONCURRENT_REQUESTS` | `1` | Simultaneous generations |
| `AI_QUEUE_WAIT_TIMEOUT` | `30` | Seconds a request waits for a slot before `AI_BUSY` (`0` = fail at once if busy) |
| `AI_STRUCTURED_MAX_RETRIES` | `1` | Extra structured-output attempts (0-5) |
| `API_CORS_ORIGINS` | Vite dev/preview on `127.0.0.1`/`localhost` (`:5173`, `:4173`) | Comma-separated browser origins |
| `API_ALLOWED_HOSTS` | `127.0.0.1,localhost,[::1]` | Accepted `Host` names (`*` disables the check) |
| `API_MAX_BODY_BYTES` | `131072` | Request body cap (all routes except the two ZIP routes) |
| `PROJECT_MAX_ZIP_BYTES` | `10485760` | Largest ZIP accepted (413 above) |
| `PROJECT_MAX_UNCOMPRESSED_BYTES` | `26214400` | Declared uncompressed total |
| `PROJECT_MAX_ENTRY_BYTES` | `5242880` | Largest single entry (any type) |
| `PROJECT_MAX_FILE_BYTES` | `524288` | Largest supported source file (bigger = excluded `too_large`) |
| `PROJECT_MAX_ENTRIES` | `500` | Entries (files + folders) |
| `PROJECT_MAX_COMPRESSION_RATIO` | `100` | Per-entry and overall ratio limit |
| `PROJECT_RATIO_FLOOR_BYTES` | `65536` | Ratios are only checked at/above this uncompressed size |
| `PROJECT_MAX_PATH_CHARS` / `PROJECT_MAX_PATH_DEPTH` | `240` / `32` | Path limits |
| `PROJECT_PROCESSING_TIMEOUT` | `30` | Seconds before a project analysis gives up (504) |
| `PROJECT_MAX_CONCURRENT` | `2` | Simultaneous project analyses |
| `PROJECT_QUEUE_WAIT_TIMEOUT` | `10` | Seconds to wait for a free analysis slot before `PROJECT_BUSY` |
| `PROJECT_INSTRUCTION_RESERVE_TOKENS` | `700` | Input tokens kept free for future instructions |

```powershell
$env:AI_QUEUE_WAIT_TIMEOUT = "10"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Remove-Item Env:AI_QUEUE_WAIT_TIMEOUT
```

## Tests

```powershell
cd backend
python -m pytest            # all mocked: no Ollama, GPU or internet required
python -m pytest tests\test_project_security.py tests\test_project_discovery.py `
                 tests\test_project_analysis.py tests\test_project_graph.py `
                 tests\test_project_context.py tests\test_project_api.py   # Phase 3 only
```

Phase 4 tests (`test_analysis_models.py`, `test_evidence.py`, `test_analysis_prompts.py`, `test_glossary.py`, `test_code_analysis_service.py`, `test_analysis_api.py`) use a scripted generator or a mocked Ollama transport, and the controlled projects in `tests\analysis_fixtures.py`; no model is needed.

Phase 3 tests use small synthetic projects and hostile ZIPs built in memory (`tests\project_fixtures.py`); nothing is downloaded and nothing from an archive is executed.

Live check of the project endpoints against a **real uvicorn process** (no Ollama needed; it starts the server itself and uses a fake Ollama listener to prove the routes never connect to it):

```powershell
python backend\scripts\check_projects_live.py
```

Live evaluation of `/api/ai/analyze` against the **real model** (needs Ollama running with `qwen2.5-coder:3b`; see [AI code analysis](#ai-code-analysis-phase-4)):

```powershell
python backend\scripts\check_analysis_live.py --runs 3 --depths beginner
```

## Live integration testing (needs Ollama running with the model installed)

```powershell
# Terminal 1
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Terminal 2: health, generate
Invoke-RestMethod http://127.0.0.1:8000/api/ai/health | ConvertTo-Json -Depth 5
$body = @{ prompt = "Explain this Python code line by line:`n`nnumbers = [1, 2, 3]`n`nfor number in numbers:`n    print(number)" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/ai/generate -ContentType "application/json" -Body $body

# invalid request: Invoke-RestMethod throws on 4xx/5xx, so catch it to see status and body
try {
  Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/ai/generate -ContentType "application/json" -Body '{"prompt":"   "}'
} catch { "HTTP $([int]$_.Exception.Response.StatusCode)"; $_.ErrorDetails.Message }
```

Then open <http://127.0.0.1:8000/docs>. Stop the server with `Ctrl+C`. To test the service alone, without FastAPI: `python scripts\check_ollama_live.py`. (Avoid passing JSON to `curl.exe` from PowerShell: native-argument quoting differs between versions and mangles the body.)

## Offline deployment notes

* Install dependencies and run `ollama pull qwen2.5-coder:3b` once while online; afterwards nothing needs the internet. Swagger UI is vendored, so `/docs` works offline too.
* The API ignores `HTTP_PROXY`/`HTTPS_PROXY` for its calls to Ollama.
* Browser calls from the Vite dev server (`http://127.0.0.1:5173`) are allowed by CORS; add other origins via `API_CORS_ORIGINS`.
