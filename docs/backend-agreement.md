# Sift: backend integration agreement

Status: implemented backend baseline for team review. This document is a proposal
for the shared contract, not evidence that every teammate has already accepted it.
No frontend or actual local model was available during verification. The application
logic and simulated Ollama integration are tested; live inference still needs testing.

## What the team needs to agree on

Review the following decisions together before connecting independently built components.
Record exceptions at the end; avoid silently changing field names in individual modules.

| Decision | Implemented baseline | Team decision |
|---|---|---|
| API transport | REST and JSON on `http://127.0.0.1:8000`; multipart ZIP upload | Accept or specify another port |
| Languages | `python`, `javascript`, `html`, `css`; no guaranteed JSX/TS support | Confirm the four-language scope |
| Identity | Opaque `project_id`; opaque `file_id` stable within that project session | Use IDs, not display paths, for new integrations |
| Project inventory | Tree, file metadata, counts, entry-point candidates and static relationships | Confirm frontend display requirements |
| Source retrieval | Separate file-content endpoint; no source text in project metadata | Confirm `code` as the content field |
| Analysis scopes | `project`, `file`, `block` | Confirm metadata-only project overview is sufficient |
| Line references | Original file, 1-based, inclusive; CRLF and LF split into logical lines | Use the same numbering in CodeMirror |
| Explanation format | `summary`, `sections`, `concepts`, `source_references`, `limitations` | Render structured sections rather than expecting one essay |
| Learning level | `beginner`, `intermediate`, `experienced` | Explanation level and exercise difficulty are independent requests |
| Context budget | Bounded excerpt, up to two related excerpts; further model budget reduction | Surface omissions and limitations in the UI |
| Inference | Local Ollama, `qwen2.5-coder:3b`, one generation at a time, 90-second outer timeout | Model owner benchmarks and confirms configuration |
| Challenge matching | Curated library; language/difficulty plus concept overlap | Keep shared singular concept names consistent |
| Verification | Structural match to a reviewed solution; no execution | UI must state that equivalent alternatives may be rejected |
| Sessions | Eight retained sessions, one-hour inactivity expiry, restart clears data | Frontend displays one active project; confirm retention policy |
| Errors | HTTP status plus `code` and `detail`; validation errors may include `fields` | Implement loading/retry/error states accordingly |
| Local deployment | One Uvicorn worker; optional built `frontend/dist` served by FastAPI | Confirm development origins and packaging |

## 1. Project ingestion and retrieval

`POST /api/projects/upload` accepts multipart field `file` with a ZIP. Returns 201
with project metadata. Unsupported files are listed as skipped; no eligible source
files results in 422. Archives are never extracted or executed.

`POST /api/projects/files` accepts repeated multipart `files`, a `paths` field containing
a JSON list of corresponding relative paths, and an optional display `name`.
Use base filenames for standalone files and browser `webkitRelativePath` values for
folders. Returns the same 201 project metadata as ZIP upload. The backend validates
paths, rejects duplicates, caps the combined input at 10 MiB and 1,000 files, and
applies the same exclusions, source limits, parsers, and indexing as ZIP ingestion.
The frontend excludes dependency/generated directories and `.env` files before
transmitting folder contents; server exclusions still apply independently.

`POST /api/projects/snippet` accepts:

```json
{"code":"def add(a, b):\n    return a + b","language":"python","filename":"main.py"}
```

Both return a project with these fields:

- `project_id`: identifier for future requests.
- `name`: supplied archive/file name.
- `files`: metadata records; each has `file_id`, `path`, `language`, `size_bytes`,
  `status`, `reason`, `constructs`, and `concepts`.
- `file_tree`: nodes with `name`, `path`, `type`, `file_id`, `status`, `children`.
  Directory nodes have null file ID/status; their children contain the files.
- `entry_points`: file IDs selected by common names; candidates, not verified launch targets.
- `language_counts`: counts of non-skipped files by language.
- `relationships`: `source_id`, nullable `target_id`, `source`, `target`, `kind`, `resolved`.
  Unresolved targets preserve the original reference string rather than a fabricated file ID.
- `total_files`, `analyzed_files`, `partial_files`, `skipped_files`, `warnings`.

`id` and file `size` are transitional aliases for `project_id` and `size_bytes`.
New clients should use the canonical fields; aliases remain during integration.

Statuses:

| Status | Meaning |
|---|---|
| `analyzed` | Basic static parsing finished; no claim of behavioral correctness |
| `partial` | Syntax issue detected; available source can still be explained with limitations |
| `skipped` | No source retained for this file; `reason` explains why |

`GET /api/projects/{project_id}` returns the same metadata.
`GET /api/projects/{project_id}/files` returns the metadata array.
`GET /api/projects/{project_id}/files/{file_id}` returns:

```json
{"file_id":"file-id","path":"main.py","language":"python","code":"def add(a, b):\n    return a + b"}
```

`DELETE /api/projects/{project_id}` returns 204 with no body. Afterwards retrieval
returns 404. Expired sessions and sessions lost after restart require re-upload.
File IDs are not guaranteed to survive a new upload or restart.

## 2. Explanation requests and responses

`POST /api/context` prepares AI context without generating an explanation.
`POST /api/analyze` accepts the same request and returns the explanation.

File request:

```json
{"project_id":"project-id","scope":"file","file_id":"file-id","difficulty":"beginner"}
```

Block request:

```json
{"project_id":"project-id","scope":"block","file_id":"file-id","start_line":2,"end_line":2,"difficulty":"beginner"}
```

Project request:

```json
{"project_id":"project-id","scope":"project","difficulty":"beginner"}
```

Project scope rejects a file ID or line range. Block scope requires both line
numbers. File scope normally omits lines. Legacy path-based requests remain supported,
but clients should send `file_id`. If both path and ID are supplied they must agree.

Example file explanation:

```json
{
  "overview":"This function adds two values.",
  "summary":"This function adds two values.",
  "sections":[
    {"title":"Return the sum","explanation":"It adds a and b, then sends the total back to the caller.","file_id":"file-id","start_line":2,"end_line":2}
  ],
  "concepts":["function","return"],
  "source_references":[{"file_id":"file-id","start_line":2,"end_line":2}],
  "limitations":["Source is analyzed as text and is never executed."]
}
```

`overview` is the AI adapter's field; `summary` is its identical frontend alias.
There is no separate top-level `explanation` essay. Use section explanations.
Project summaries have empty sections/references because only metadata is supplied.
References can cite selected or related supplied excerpts; nonexistent IDs and
out-of-range lines are rejected. Formatting validation does not establish truth.

## 3. Backend-to-AI contract

The adapter exposes `async explain(context: ExplanationContext) -> Explanation`.
Use the shared Pydantic models in `backend/models.py`; no second index or re-parsing.

Context contains:

- `project_id`, `scope`, `difficulty`, nullable `language`.
- Nullable `selected`: `file_id`, `path`, `code`, original `start_line`, `end_line`.
- `related`: up to two source excerpts with the same shape.
- `constructs`, `concepts`, `relationships`, `metadata`, `truncated`, `limitations`.

For project scope, selected is null; metadata supplies file inventory, language counts
and entry-point candidates. It does not justify claims about uninspected functions.

The adapter handles prompt construction, local HTTP calls and structured output.
The backend validates the returned fields and source references. Source comments
and strings are data, never permissions or instructions.

Model settings: 4,096-token context (`OLLAMA_NUM_CTX`), temperature 0.2, 768 output
tokens for this adapter, 30-minute keep-alive. Context selection caps source at 8,000
UTF-8 bytes; the adapter then requires instructions, schema and payload together to fit
3,072 estimated tokens (4,096 - 768 output - 256 margin, using the engine's pessimistic
estimator) and 6,000 bytes, removing related context/metadata before reducing selected
source at line boundaries. Benchmark before increasing limits; never silently omit
limitations.

The backend never installs/pulls a model or calls a remote inference service.
Ollama must be running with the local model already available. Configuration is shared
with the project-analysis engine (section 7): `OLLAMA_MODEL` (default
`qwen2.5-coder:3b`; `CODESENSE_MODEL` is honoured as a fallback when it is unset),
`OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`) and `OLLAMA_NUM_CTX` (default
4096). Do not set different context windows for the two paths: Ollama reloads the model
when `num_ctx` changes. `:cloud` / `-cloud` model names are rejected. Pointing
`OLLAMA_BASE_URL` at a non-local host is possible but makes the app no longer offline.

## 4. Challenges, hints and verification

`GET /api/challenges` lists 12 public exercises (three per language). Public objects
contain ID, language, difficulty, concepts, title, objective, starter code, hint count
and verification policy. They do not include solutions or hint text.

Select from project context:

```json
{"project_id":"project-id","file_id":"file-id","difficulty":"beginner"}
```

Or select independently of a project/AI:

```json
{"language":"javascript","concepts":["condition"],"difficulty":"beginner"}
```

`POST /api/challenges/select` returns `status`, `challenge`, and `match` with kind
`concept` or `general`. The latter must be labeled as a general exercise in the UI.
Concept vocabulary includes `function`, `condition`, `loop`, `assignment`, `return`,
`class`, `import`, `exception`, `dom`, `event`, `markup`, `form`, `styling`.

Submission:

```json
{"challenge_id":"js-condition-001","code":"function isPassing(score) { return score >= 50; }"}
```

`POST /api/challenges/submit` returns `attempt_id`, `challenge_id`, `correct`,
`verification: "structural"`, `feedback`, `limitations`. Incorrect or syntactically
invalid code returns 200 with correct=false, not an HTTP failure. Unknown IDs return 404.

`GET /api/challenges/{id}/hints/{level}` accepts 1 through 3 and returns cumulative
hints plus remaining count. The frontend decides when the user requests the next hint.
`GET /api/challenges/{id}/solution` reveals the reference correction and feedback;
call only after the user selects Reveal solution. There is no server-side attempt gate.

Checks compare the parsed solution structure, not execution results. Comments and
formatting are accepted where the parser normalizes them. Other equivalent algorithms
may fail; each objective specifies a constrained edit. Extra statements are rejected.
Do not present a passed exercise as evidence that the uploaded application is correct.

## 5. Failure and loading behavior

| HTTP | Code | Frontend behavior |
|---|---|---|
| 413 | `REQUEST_TOO_LARGE` | Ask for a smaller upload |
| 422 | `INVALID_INPUT` or `INVALID_PROJECT` | Show detail and field errors |
| 404 | `NOT_FOUND` | Handle missing file/challenge or re-upload expired project |
| 429 | `AI_BUSY` | Keep current data; let user retry when generation finishes |
| 502 | `INVALID_AI_RESPONSE` | Explanation failed; allow retry with a smaller section |
| 503 | `AI_UNAVAILABLE` | Explain local model setup/readiness; keep browsing/challenges usable |
| 504 | `AI_TIMEOUT` | Stop loading and offer retry |

`POST /api/projects/{id}/analysis` (section 7) returns more specific codes in the same
`{code, detail}` shape: 422 `FILE_NOT_ANALYZED`, `SYMBOL_NOT_FOUND`, `NO_ANALYZABLE_SOURCE`,
`PROMPT_TOO_MANY_TOKENS`; 503 `OLLAMA_UNAVAILABLE`, `OLLAMA_CONNECT_TIMEOUT`,
`MODEL_NOT_INSTALLED`, `ANALYSIS_UNAVAILABLE`; 504 `GENERATION_TIMEOUT`,
`PROJECT_PROCESSING_TIMEOUT`; 502 `OUTPUT_TRUNCATED`, `INVALID_STRUCTURED_OUTPUT`,
`INVALID_OLLAMA_RESPONSE`, `OLLAMA_UPSTREAM_ERROR`; 500 `ANALYSIS_FAILED`. Clients that
show `detail` need no code-specific handling. The standalone engine app
(`app.main:app`) uses a different envelope, `{"error": {"code", "message"}}`; the
frontend client accepts both.

All these errors have `code` and human-readable `detail`. Validation errors may add
`fields` records with `location` and `message`. Do not assume all responses are successes.
The backend checks one generation at a time; the frontend should also disable repeat
submission while an explanation is pending. No streaming/job polling is implemented.

`GET /api/health` returns backend ready plus `ai.status`: `ready`, `model_missing`,
`unavailable`, `unconfigured` or `unknown` for a custom adapter without readiness.
Ready means the configured model is listed locally; it is not a successful generation
benchmark or guarantee that enough memory is available.

## 6. Bounds, offline behavior and acceptance

- ZIP upload 10 MiB; entire HTTP body 11 MiB; 1,000 archive entries; 20 MiB declared
  decompressed size. Source limits: 30 files, 100 KiB each, 2 MiB total UTF-8 text.
- Common dependency/build directories, unsupported files and `.env*` files are skipped.
  Unsafe paths, duplicate/case-colliding paths, encrypted entries and special entries fail.
- No uploaded or submitted code is executed. No runtime cloud service is used.
- Development CORS origins: localhost/127.0.0.1 on port 5173. Production built frontend
  can be placed at `frontend/dist` and served by the same backend process.
- Swagger `/docs` uses CDN assets; `/openapi.json` is the offline JSON specification.
- Source is temporary in memory. No accounts, database, analytics or progress persistence.

Before calling integration complete, confirm:

- [ ] Teammates accept the canonical fields and explanation shape above.
- [ ] Frontend uploads, renders the file tree and fetches a file by ID.
- [ ] Project/file/block requests render correctly, including partial/skipped states.
- [ ] Real local model generates accurate explanations with valid references.
- [ ] Missing model, timeout and busy states are understandable in the UI.
- [ ] Each challenge accepts its reviewed solution and rejects its starter code.
- [ ] Hints and explicit solution reveal behave as intended.
- [ ] Full application starts and completes the workflow after an offline restart.

Backend automated tests pass, and on 2026-10-10 the full workflow (upload, explanations,
possible problems, missing-line challenge, hints, verification, solution) was run
against a real local `qwen2.5-coder:3b` through the HTTP API. Model *quality* and a
browser-driven pass of the UI remain acceptance work.

## 7. Project analysis

`POST /api/projects/{project_id}/analysis` reuses the uploaded session project; there
is no second upload. Request (`extra` fields are rejected):

```json
{"intent":"explain","depth":"beginner","file_id":"file-id","start_line":7,"end_line":8}
```

| Field | Rule |
|---|---|
| `intent` | `overview` (project; optional focus `file_id`), `explain`, `debug` (possible problems) |
| `depth` | `beginner` (default), `intermediate`, `advanced`; `experienced` is an alias of `advanced` |
| `file_id` / `path` | One is required for `explain` and `debug`; `path` is transitional; both must agree |
| `symbol` | Optional function/class (`Class.method`); not combined with lines |
| `start_line`, `end_line` | Both or neither; inclusive original lines; must lie inside the file |

The response is the engine's `AnalysisResponse` (see `/openapi.json`) plus `project_id`,
`file_id` and `selection`:

```json
"selection": {"scope":"symbol","requested":{"start_line":7,"end_line":8},
  "analyzed_symbol":"total","analyzed_lines":{"start_line":5,"end_line":9},
  "expanded":true,"note":"You selected lines 7-8. Sift analysed the whole function 'total' ..."}
```

Rules the client must respect:

- **Selection scope.** Lines map to the smallest enclosing function, method or class,
  otherwise to the whole file (`scope: "file"`). When `expanded` is true, show `note`; it
  is also the first `limitations` entry. `requested` is always the user's own selection.
- **Provenance.** Fields documented as AI-generated must be labelled as such. Deterministic
  fields (`selection`, `relationships`, `glossary`, `coverage`, `limitations`, `tier`,
  `verification`, `evidence`, `pattern_checks`, a `debug` summary) come from Sift.
- **No fabricated citations.** `explanations[].file_path/start_line/end_line` and finding
  locations are validated against what the AI was shown; `location_status` of `rejected`
  or `none` has null fields and must not be rendered as a source link. Evidence excerpts
  are copied by the backend from the uploaded file.
- **Suspicions, not bugs.** `debug` findings are possible problems. `source_verified`
  means the cited code is real and was shown to the AI, never that a bug exists;
  `no_clear_problem` is not proof the code is correct. These are separate from the
  missing-line challenges, which stay in the Debug stage.
- **Concurrency.** One local model request at a time across `/api/analyze`,
  `/api/challenges/generate` and this route; a second request gets `429 AI_BUSY` at once.
  Allow up to about five minutes (up to three model calls); the frontend uses 330 s.

## Team sign-off

Contract version: 0.3.0 (additive over 0.2.0: the analysis route, `analysis` in health,
shared model configuration). Record the chosen baseline before editing clients.

| Item | Accepted value or requested change |
|---|---|
| Reviewer(s) and date | |
| File/source fields and IDs | |
| Explanation response shape | |
| Context/model settings | |
| Session policy | |
| Challenge correction rules | |
| API origins and packaging | |
| Other exceptions | |
