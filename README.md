# CodeSense

Local code exploration and debugging practice with React, CodeMirror, and a FastAPI backend. The frontend now calls the backend for ZIP ingestion, source browsing, explanations, challenges, hints, and answer verification.

## Run the integrated application

Install Node.js 22.18+ and Python 3.11+. From the repository root:

```powershell
npm ci
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-lock.txt
npm run build
powershell -File scripts/start.ps1
```

Open http://127.0.0.1:8000. FastAPI serves the built frontend and `/api` from the same address. Build before starting the server. The startup script also supports the existing `.venv` in the parent workspace when no repository virtual environment exists.

For frontend development, keep the backend running and run `npm run dev` in another terminal. Open http://127.0.0.1:5173; Vite forwards `/api` requests to port 8000. Rebuild to update the frontend served on port 8000.

## What works

Choose ZIP archive, Files, Folder, or Paste code in Workspace. ZIPs and file/folder uploads are limited to 10 MB; pasted code and individual source files are limited to 100 KB. Folder selection preserves relative paths and excludes dependency/generated directories and `.env` files before upload. Choose folders with the picker; dragging folders is not supported. Files accepts one or several Python, JavaScript, HTML, or CSS files. Paste code provides a language selector and filename.

Browse eligible source files, inspect relationships and skipped-file reasons, and request explanations of a project, a file, or selected lines. All input modes share the same backend indexing, source limits, and debugging workflow. Explanation references open the corresponding file and highlight its original lines.

Practice uses curated exercises matched to project concepts where available, with an explicit general-exercise fallback. Edit the starter code, check the answer, reveal hints one at a time, and explicitly reveal a reference solution. Verification checks constrained source structure; it does not execute code or prove general correctness.

Backend connectivity and local model readiness are shown separately. Browsing and exercises work without AI. Explanations require Ollama running locally on port 11434 with `qwen2.5-coder:3b` available (or the backend's configured `CODESENSE_MODEL`). Model installation and runtime setup belong to the local AI integration. See [backend details](BACKEND.md).

Project data is temporary server memory; a browser refresh resets the visible workspace. Sessions expire after one hour and are lost when the backend restarts. Navigation between Session and Practice preserves the current frontend state. Bundled fonts, styling, and existing mascot assets are retained.

## Checks

```powershell
npm run typecheck
npm run build
node --experimental-strip-types --test tests/api.test.ts tests/upload.test.ts
.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"
```

The API client tests cover JSON requests, multipart uploads, backend errors, unavailable connections, invalid responses, and timeouts. Backend tests cover ingestion, parsers, contexts, exercise verification, and simulated Ollama responses. Live model quality requires testing with the actual local AI runtime.

See [integration details](docs/frontend-integration.md) and [backend agreement](docs/backend-agreement.md).
