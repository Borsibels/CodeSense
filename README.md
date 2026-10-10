# Sift

Sift is a local learning tool for understanding and practicing with Python, JavaScript, HTML, and CSS code. You can upload a project, ask for a plain-language explanation, inspect possible problems, and try an AI-generated missing-line challenge. The app uses a React frontend, a FastAPI backend, and a local Ollama model. It does not run your uploaded code.

This guide walks through setup and use from a fresh checkout. The backend entry point is `backend.main:app`.

## What you need

- Windows, macOS, or Linux
- [Python 3.12](https://www.python.org/downloads/) recommended (Python 3.11+ may work)
- [Node.js 22.18 or newer](https://nodejs.org/en/download), with npm
- Ollama and the `qwen2.5-coder:3b` model for AI features

You can open and browse uploaded files without Ollama. Explanations, possible-problem checks, and new debugging challenges need the local model.

## 1. Install and prepare Ollama

1. Install Ollama from [ollama.com](https://ollama.com).
2. Start the Ollama app or service.
3. In a terminal, download the model once:

   ```sh
   ollama pull qwen2.5-coder:3b
   ```

4. Confirm it is installed:

   ```sh
   ollama list
   ```

The first AI request can take longer while Ollama loads the model. CodeSense connects to Ollama at `http://127.0.0.1:11434` by default. Downloading packages and the model needs internet access; normal app use sends model requests to your local Ollama service.

## 2. Install CodeSense dependencies

Open a terminal in the repository root (the directory containing this README and `package.json`). If you do not have the project files yet, clone the repository using its URL from your Git host, then change into the `CodeSense` folder. Create a Python virtual environment, install the locked backend dependencies, and install the frontend dependencies.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
npm ci
```

If the Python launcher does not find 3.12, install Python 3.12 or substitute the Python command available on your machine.

### macOS or Linux

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
npm ci
```

If `python3.12` is not available, use `python3` if it points to Python 3.11 or newer.

## 3. Start CodeSense

Choose either the all-in-one mode or the frontend development mode.

### Option A: Run the built app at one address

Build the frontend once:

```sh
npm run build
```

Then start the backend from the repository root.

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

**macOS or Linux:**

```sh
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). FastAPI serves the built frontend and API from this address. Keep the terminal running while you use the app. On Windows, `powershell -File scripts/start.ps1` can start the same backend after `.venv` is set up.

Re-run `npm run build` after frontend changes so the integrated app serves the updated files.

### Option B: Run the frontend in development mode

Start the backend as shown above in **Terminal 1**. In **Terminal 2**, from the repository root, run:

```sh
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Vite serves the frontend and forwards `/api` requests to the backend on port 8000. This mode refreshes the frontend as you edit it; the backend still needs to be running.

To stop either server, press **Ctrl+C** in its terminal.

## 4. Use the app

1. **Upload your code.** Choose a ZIP archive, individual source files, a project folder, or paste a snippet. For pasted code, use a filename ending in `.py`, `.js`, `.html`, or `.css` if the language is unclear.
2. **Explore files.** Select an eligible file in the project tree. Skipped files show a reason. The code shown in the editor is the source that was uploaded.
3. **Explain code.** Choose Project, File, or Selection, set a difficulty, and ask for an explanation. To explain selected lines, highlight them in the editor first. Use **Possible problems** as a review aid: its results can be wrong or incomplete and are not confirmed bugs.
4. **Practice debugging.** Choose **Generate missing-line challenge**. CodeSense creates a challenge from a copy of the source and keeps your uploaded file unchanged. Edit the copy in the editor. Reveal hints one at a time, then choose **Check my fix**.
5. **Review your result.** A failed check can be revised. A successful check can continue to Learn, where you can reveal the original source and compare it with your fix.

Challenge verification compares parsed source structure. It does not execute code, prove that the original program is correct, or accept every equivalent rewrite. Challenge generation currently creates one missing-line challenge at a time; difficulty guides the local model but cannot guarantee quality.

## Upload limits and session behavior

- ZIP archives and combined file uploads: up to 10 MB
- Source files: up to 100 KB each
- Projects: up to 30 eligible files and 2 MB of source text
- Supported languages: Python, JavaScript, HTML, and CSS
- ZIPs: up to 1,000 entries and 20 MB declared decompressed content
- Common dependency/build folders and `.env` files are excluded

Projects and generated challenges are held temporarily in backend memory. Sessions expire after one hour of inactivity and are cleared when the backend restarts. Upload your project again after a restart or an expired-session message.


## Configuration

These optional environment variables are read when the backend starts:

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5-coder:3b` | Name of an installed local Ollama model |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama service address |
| `OLLAMA_NUM_CTX` | `4096` | Model context size; changing it can affect memory use and prompt capacity |

`CODESENSE_MODEL` is accepted as a legacy fallback if `OLLAMA_MODEL` is unset. Restart the backend after changing an environment variable. Run one backend worker so in-memory sessions and the AI request lock stay consistent.

## Run checks

From the repository root:

```sh
npm run typecheck
npm run build
node --experimental-strip-types --test tests/api.test.ts tests/upload.test.ts tests/detectLanguage.test.ts tests/analysis.test.ts
```

Run the root Python tests with the virtual environment:

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

**macOS or Linux:**

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

The separate `backend/` analysis-engine tests use pytest. From the repository root, install pytest into the virtual environment:

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
Set-Location backend
..\.venv\Scripts\python.exe -m pytest -q
```

**macOS or Linux:**

```sh
.venv/bin/python -m pip install pytest
cd backend
../.venv/bin/python -m pytest -q
```

## More details

- [Backend API, limits, and model integration](BACKEND.md)
- [Frontend/backend integration notes](docs/frontend-integration.md)
- [Backend agreement and API behavior](docs/backend-agreement.md)

### Local AI Q&A

In the Explain stage, use **Ask about this code** below the explanation tools. Ask about the selected file, or highlight lines first to limit the source context. It uses the configured local Ollama model and difficulty, with bounded context and the existing single inference lock. Each question is independent; the panel displays the last five answers for the selected file and clears when switching files. Q&A does not edit uploaded source or change challenge verification. It requires a ready model; there is no offline canned-answer fallback.

The optional `POST /api/questions` endpoint accepts the existing context fields (`project_id`, `file_id`, `scope`, optional line range, `difficulty`) plus `question` (1–1000 characters, at most 2000 UTF-8 bytes). It returns `answer` and `limitations`. The existing setup, upload, analysis, and debugging endpoints are unchanged.
