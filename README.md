# Sift

**Make sense of every line.**

Sift is a local learning tool for understanding and practicing with Python, JavaScript, HTML, and CSS code. Add a project, get a plain-language explanation, review possible problems, ask questions, and practice with a missing-line challenge.

Snip, the programmer sloth, is Sift’s friendly coding companion. Sift uses a React frontend, a FastAPI backend, and a local Ollama model. It does not run your uploaded code.

This guide explains how to install, start, and use Sift. The backend entry point is `backend.main:app`.

## What you need

- Windows, macOS, or Linux
- Python 3.12 recommended; Python 3.11 or newer may work
- Node.js 22.18 or newer, with npm
- Ollama and the `qwen2.5-coder:3b` model for AI features

You can browse uploaded files without Ollama. Explanations, questions, possible-problem checks, and new debugging challenges require the local model.

## 1. Install and prepare Ollama

1. Install [Ollama](https://ollama.com).
2. Start the Ollama app or service.
3. Open a terminal and download the model:

   ```sh
   ollama pull qwen2.5-coder:3b
   ```

4. Confirm the model is installed:

   ```sh
   ollama list
   ```

Downloading Ollama, the model, and project dependencies requires an internet connection. After setup, Sift sends model requests to the Ollama service on your computer at `http://127.0.0.1:11434` by default. The first AI request may take longer while Ollama loads the model.

## 2. Install Sift dependencies

Open a terminal in the repository root—the folder containing this README and `package.json`. If you do not have the project files yet, clone the repository from your Git host, then change into the cloned repository folder. The folder may still be named `CodeSense` even though the app is now called Sift.

Create a Python virtual environment, install the backend dependencies, and install the frontend dependencies.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
npm ci
```

If `py -3.12` is unavailable, install Python 3.12 or use a Python 3.11+ command available on your computer.

### macOS or Linux

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
npm ci
```

If `python3.12` is unavailable, use `python3` if it points to Python 3.11 or newer.

## 3. Start Sift

Choose one of the following options.

### Option A: Run the built app at one address

Build the frontend:

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

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). FastAPI serves both the built frontend and API from this address. Keep the terminal open while you use Sift.

On Windows, you can also start the backend with:

```powershell
powershell -File scripts/start.ps1
```

Re-run `npm run build` after frontend changes so the integrated app serves the latest version.

### Option B: Run the frontend in development mode

Start the backend as shown above in **Terminal 1**. In **Terminal 2**, from the repository root, run:

```sh
npm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Vite serves the frontend and forwards API requests to the backend on port 8000. The frontend refreshes as you edit it, but the backend must remain running.

To stop either server, press **Ctrl+C** in its terminal.

## 4. Use Sift

1. **Add your code.** From the Workspace screen, choose a ZIP archive, source files, a project folder, or **Paste code**. Pasted code is detected automatically; if the language is unclear, provide a more complete snippet or upload a source file. Choose a learning pace: Beginner, Intermediate, or Experienced. Then select **Explore project**.

2. **Explore the project.** The workspace shows the project’s files, source code, and **Ask Snip** assistant. Select an eligible file to view its source. Files marked as skipped include a reason. On a compact screen, use the **Files**, **Code**, and **Ask Snip** view buttons to switch panels. On a wide screen, drag the dividers to resize the panels.

3. **Explain code.** In **Ask Snip**, choose what to analyze: the project, the open file, or selected lines. Highlight code in the editor first to analyze a selection. Choose a learning pace and press **Explain**.

   The **Explanation** tab shows a plain-language walkthrough. Items labelled **AI-written** were written by the local model; items labelled **Checked by Sift** come from Sift’s own checks.

4. **Review other tabs.**
   - **Issues** asks Snip to look for possible problems. Results may be wrong or incomplete, so treat them as suggestions to investigate—not confirmed bugs.
   - **Relationships** shows imports and references Sift found between uploaded files. This is a static view and may not include every dependency or connection.
   - **Q&A** lets you ask the local AI about the open file or selected lines. Questions do not change your source code.

5. **Practice debugging.** Open the **Practice** tab and select **Generate missing-line challenge**. Sift creates a challenge from a copy of the selected source file; your uploaded file stays unchanged. Restore the missing line in the editor. Reveal hints one at a time if you need help.

   Sift generates one missing-line challenge at a time. The difficulty setting guides the local model, but cannot guarantee that every challenge will be a good fit.

6. **Check and learn.** Select **Check my fix** to see the result. You can choose **Revise my fix** or continue to **Learn**. In Learn, select **Reveal original solution** to compare the original source with your attempt. You can return to your fix or start a new project.

Challenge checking compares parsed code structure. It does not execute the program or prove that the original program is correct. An equivalent rewrite may not match the expected structure.

## Upload limits and session behavior

- ZIP archives and combined file uploads: up to 10 MB
- Individual source files: up to 100 KB
- Projects: up to 30 eligible files and 2 MB of source text
- Supported languages: Python, JavaScript, HTML, and CSS
- ZIP archives: up to 1,000 entries and 20 MB of declared decompressed content
- Common dependency and build folders, along with `.env` files, are excluded

Project sessions and generated challenges are held temporarily in backend memory. Sessions expire after one hour of inactivity and are cleared when the backend restarts. Upload your project again after a restart or an expired-session message.

## Configuration

These optional environment variables are read when the backend starts:

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5-coder:3b` | Name of an installed Ollama model |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama service address |
| `OLLAMA_NUM_CTX` | `4096` | Model context size; changing it can affect memory use and prompt capacity |

`CODESENSE_MODEL` is accepted as a legacy fallback if `OLLAMA_MODEL` is unset. Restart the backend after changing an environment variable. Run one backend worker so in-memory sessions and the AI request lock stay consistent.

## Run checks

Run these commands from the repository root:

```sh
npm run typecheck
npm run build
node --experimental-strip-types --test tests/api.test.ts tests/upload.test.ts tests/detectLanguage.test.ts tests/analysis.test.ts tests/layout.test.ts
```

Run the root Python tests using the virtual environment.

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

**macOS or Linux:**

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

The separate backend analysis-engine tests use pytest. From the repository root, install pytest into the virtual environment first.

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

## Naming

The product is **Sift** and its programmer-sloth mascot and assistant persona is **Snip**. Some technical identifiers keep the earlier `CodeSense` name for compatibility, including the `CODESENSE_MODEL` environment-variable fallback, the `codesense-ui` npm package name, the `codesense.workspace.layout.v1` browser storage key, temporary file names used by backend live-check scripts, and the repository folder name.
