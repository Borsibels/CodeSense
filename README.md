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

Choose ZIP archive, Files, Folder, or Paste code in Workspace. ZIPs and file/folder uploads are limited to 10 MB; pasted code and individual source files are limited to 100 KB. Folder selection preserves relative paths and excludes dependency/generated directories and `.env` files before upload. Choose folders with the picker; dragging folders is not supported. Files accepts one or several Python, JavaScript, HTML, or CSS files. Paste code detects the language from a supported filename extension or distinctive source syntax. If the snippet is ambiguous, provide a .py, .js, .html, or .css filename; the app does not silently guess.

Browse eligible source files, inspect relationships and skipped-file reasons, and request explanations of a project, a file, or selected lines. All input modes share the same backend indexing, source limits, and debugging workflow. Explanation references open the corresponding file and highlight its original lines.

Debug now generates a missing-line challenge from the uploaded source using local AI. AI selects one eligible meaningful line and writes three progressively specific hints based on difficulty; the backend replaces that line with a placeholder in a copy of the file and retains the original privately as the answer key. The uploaded source is unchanged. Changing difficulty regenerates the challenge and resets answers, hints, and results. Hints are generated with the challenge and revealed on demand; they are not regenerated for every keystroke. Verification compares parsed source structure with the original, without executing code or claiming that the original program is correct.

Generation currently supports parseable files under 9 KB, selecting from a bounded first excerpt. AI may choose the same line on repeated requests; a difficulty label guides the prompt but cannot guarantee challenge quality. Invalid line choices or hints quoting the full answer are rejected. Generated exercises expire after one hour and at most 32 are retained. The old curated API remains for compatibility, but the guided frontend does not silently fall back to it.

Backend connectivity and local model readiness are shown separately. Browsing works without AI. Explanations and missing-line challenge generation require Ollama running locally on port 11434 with `qwen2.5-coder:3b` available (or the backend's configured `CODESENSE_MODEL`). Model installation and runtime setup belong to the local AI integration. See [backend details](BACKEND.md).

Project data is temporary server memory; a browser refresh resets the visible workspace. Sessions expire after one hour and are lost when the backend restarts. The guided workflow proceeds through Upload, Explain, Debug, Verify, and Learn. Explain, Debug, Verify, and Learn share the same workspace: the code editor stays visible and the side panel changes modes. Debugging edits a challenge copy in that editor; checking and retry stay inline. Back to explanation restores the original read-only source view without discarding the challenge answer. Sidebar shortcuts to Practice and Session are removed. Bundled fonts, styling, and existing mascot assets are retained.

## Checks

```powershell
npm run typecheck
npm run build
node --experimental-strip-types --test tests/api.test.ts tests/upload.test.ts tests/detectLanguage.test.ts
.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"
```

The API client tests cover JSON requests, multipart uploads, backend errors, unavailable connections, invalid responses, and timeouts. Backend tests cover ingestion, parsers, contexts, exercise verification, and simulated Ollama responses. Live model quality requires testing with the actual local AI runtime.

See [integration details](docs/frontend-integration.md) and [backend agreement](docs/backend-agreement.md).
