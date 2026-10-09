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

The AI features need [Ollama](https://ollama.com) running locally with the model already pulled (`ollama pull qwen2.5-coder:3b`, once, while online). Nothing at runtime uses the internet. The app targets a 4 GB GPU and keeps the model at a 4096-token context. Optional environment variables, read once at startup: `OLLAMA_MODEL` (default `qwen2.5-coder:3b`; `CODESENSE_MODEL` is the older name and works as a fallback), `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`), `OLLAMA_NUM_CTX` (default `4096`; leave it alone, see [BACKEND.md](BACKEND.md)). Run a single server worker. The first AI request after Ollama has been idle takes about 10 s while the model loads.

For frontend development, keep the backend running and run `npm run dev` in another terminal. Open http://127.0.0.1:5173; Vite forwards `/api` requests to port 8000. Rebuild to update the frontend served on port 8000.

## What works

Choose ZIP archive, Files, Folder, or Paste code in Workspace. ZIPs and file/folder uploads are limited to 10 MB; pasted code and individual source files are limited to 100 KB. Folder selection preserves relative paths and excludes dependency/generated directories and `.env` files before upload. Choose folders with the picker; dragging folders is not supported. Files accepts one or several Python, JavaScript, HTML, or CSS files. Paste code detects the language from a supported filename extension or distinctive source syntax. If the snippet is ambiguous, provide a .py, .js, .html, or .css filename; the app does not silently guess.

Browse eligible source files, inspect relationships and skipped-file reasons, and request explanations of a project, a file, or selected lines. All input modes share the same backend indexing, source limits, and debugging workflow. Explanation references open the corresponding file and highlight its original lines, but only when the backend verified that those lines exist.

Explanations come from the project-analysis engine, using the project you already uploaded. They come with a plain-language summary (and, at Beginner level, an analogy, the file's role, a concept to learn and a short glossary), step-by-step sections with verified source references, and the file relationships it found. AI-written text is labelled "AI-written"; items CodeSense computed or copied from your files are labelled "Checked by CodeSense". **Explain selection** analyses the smallest function, method or class containing your selected lines, or the whole file if there is none, and tells you whenever it analysed more than you selected. **Possible problems** asks the model to look for things that might be wrong in the selected lines or the whole file, then checks each claim's location against the real code and runs a few rule-based checks. Results are suspicions to check, not confirmed bugs: a verified location only means the cited code exists, and "No clear problem found" is not proof that the code is correct. This is separate from the Debug stage's missing-line challenges.

Debug now generates a missing-line challenge from the uploaded source using local AI. AI selects one eligible meaningful line and writes three progressively specific hints based on difficulty; the backend replaces that line with a placeholder in a copy of the file and retains the original privately as the answer key. The uploaded source is unchanged. Changing difficulty regenerates the challenge and resets answers, hints, and results. Hints are generated with the challenge and revealed on demand; they are not regenerated for every keystroke. Verification compares parsed source structure with the original, without executing code or claiming that the original program is correct.

Generation currently supports parseable files under 9 KB, selecting from a bounded first excerpt. AI may choose the same line on repeated requests; a difficulty label guides the prompt but cannot guarantee challenge quality. Invalid line choices or hints quoting the full answer are rejected. Generated exercises expire after one hour and at most 32 are retained. The old curated API remains for compatibility, but the guided frontend does not silently fall back to it.

Backend connectivity and local model readiness are shown separately. Browsing works without AI. Explanations and missing-line challenge generation require Ollama running locally on port 11434 with `qwen2.5-coder:3b` available (or the backend's configured `CODESENSE_MODEL`). Model installation and runtime setup belong to the local AI integration. See [backend details](BACKEND.md).

Project data is temporary server memory; a browser refresh resets the visible workspace. Sessions expire after one hour and are lost when the backend restarts. The guided workflow proceeds through Upload, Explain, Debug, Verify, and Learn. Explain, Debug, Verify, and Learn share the same workspace: the code editor stays visible and the side panel changes modes. Debugging edits a challenge copy in that editor; checking and retry stay inline. Back to explanation restores the original read-only source view without discarding the challenge answer. Sidebar shortcuts to Practice and Session are removed. Bundled fonts, styling, and existing mascot assets are retained.

## Checks

```powershell
npm run typecheck
npm run build
node --experimental-strip-types --test tests/api.test.ts tests/upload.test.ts tests/detectLanguage.test.ts tests/analysis.test.ts
.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"
cd backend; ..\.venv\Scripts\python -m pytest -q
```

The API client tests cover JSON requests, multipart uploads, both backend error formats, the analysis request mapping, unavailable connections, invalid responses, and timeouts. The root Python tests cover ingestion, parsers, contexts, exercise verification, the session-to-analysis bridge (selection mapping, error mapping, the shared inference gate) and simulated Ollama responses. The `backend/` pytest suite covers the analysis engine itself (`pytest` is installed with `pip install pytest`). Live model quality requires testing with the actual local AI runtime.

## Known limitations

- Possible-problems results come from a 3B model plus a few rules. In the Phase 4.5 evaluation the model reported a problem in clean code in every run before triage, and on the held-out clean-code set 13 of 25 runs still ended as "no clear problem" after triage (see `backend/README.md`). Treat every item as something to check.
- Explanations and checks see only what fits a 4096-token window: large files are shown in part or as signatures, and the result says so under "What this analysis could not see".
- Selection mapping covers Python and JavaScript functions (including arrow functions assigned to a name), methods and classes. Selections in HTML, CSS, module-level code, object-literal methods, or a name defined twice are analysed as the whole file, and the result says so.
- One AI request runs at a time; a second gets "Local AI is busy". The legacy `/api/analyze` route still exists but sends about half as much source as it did at the old 8192 window.
- Files in folders such as `coverage/` or `.vscode/`, minified files and paths with a trailing dot or space can be browsed but not analysed by the AI.
- The guided UI has been verified through its API and rendering code with a real model, but a browser-driven end-to-end pass and a full restart with Wi-Fi disabled are still manual checks on the demo laptop.

See [integration details](docs/frontend-integration.md) and [backend agreement](docs/backend-agreement.md).
