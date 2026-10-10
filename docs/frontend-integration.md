# Frontend/backend integration

`src/api.ts` defines the typed HTTP client; `src/useWorkspace.ts` owns project, source, explanation, and exercise state. `App.tsx` shares that state between the upload screen and the workspace, and tracks the practice stage (Debug → Verify → Learn). Verification appears only after a successful check request; failed checks can be revised or reviewed in Learn. Network errors stay on Debug. There is no step timeline or back row: the Explorer's upload button returns to the upload screen, and the Practice tab resumes a challenge.

| UI action | Backend request |
|---|---|
| Setup / connection status | `GET /api/health` |
| Upload project | `POST /api/projects/upload` with multipart `file` |
| Upload source files / folder | `POST /api/projects/files` with repeated multipart `files`, JSON `paths`, and display `name` |
| Paste code | `POST /api/projects/snippet` with `code`, `language`, and `filename` |
| Select source file | `GET /api/projects/{project_id}/files/{file_id}` |
| Explain project/file/selection | `POST /api/projects/{project_id}/analysis` with `intent` `overview` (Project) or `explain` (File, Selection), `depth`, `file_id`, and for Selection `start_line`/`end_line` |
| Check for possible problems | The same route with `intent: "debug"`; uses the selected lines if any, otherwise the whole file |
| Start/load exercise | `POST /api/challenges/generate` |
| Check answer | `POST /api/challenges/submit` |
| Reveal next hint | `GET /api/challenges/{id}/hints/{level}` |
| Reveal reference solution | `GET /api/challenges/{id}/solution` |

Project responses supply `file_tree`, source metadata, relationships, and upload warnings. Skipped files display their reason. CodeMirror selections send inclusive original line numbers. Analysis steps and findings carry a `file_path` and verified lines; `src/analysisText.ts#stepLocation` turns only verified locations (`in_context`, `outline_only`, `file_only`) into clickable references, which open the file by path and highlight the lines. A `rejected` or `none` location is never rendered as a link. Stale source, explanation and problem-check responses cannot replace a newer selection (separate epochs).

**Analysis rendering** (`src/analysis.tsx`). AI-written text carries an "AI-written" badge; anything Sift computed or copied from the real file carries "Checked by Sift" (relationships, glossary, evidence excerpts, verification, tiers, rule-based checks). The UI shows `selection.note` whenever the analysis is wider than the selection (the backend maps lines to the enclosing function/class, else the whole file). Possible problems are worded as suspicions: a `source_verified` finding is labelled "Location verified", never as a confirmed bug, and `no_clear_problem` is labelled as not proof. The `Difficulty` value `experienced` is sent as `depth: "advanced"`. Possible problems are separate from the Debug stage's missing-line challenges and do not feed them.

Errors: `request()` reads both envelopes, the host's `{code, detail}` and the engine's `{error: {code, message}}`. Analysis requests use a 330 s timeout (up to three local model calls); everything else keeps 100 s. Only one model request can run at a time; a second returns `AI_BUSY`, and the workspace disables the other AI actions while one is pending.

Challenge instructions come from `objective`; the general fallback is identified by `match.kind`. Hints are fetched only on request and the reference solution appears separately without overwriting the learner's answer. Verification displays `correct`, `feedback`, and `limitations`; it makes no execution claim.

The client uses relative `/api` URLs. Vite proxies these to `http://127.0.0.1:8000` in development. Production builds in root `dist` are served by FastAPI when present at startup. API errors display the backend's detail; invalid responses, network failures, and timeouts receive explicit messages.

Backend readiness and AI readiness remain separate. AI controls require a ready model, while browsing remains usable without Ollama; generating new missing-line exercises requires AI. Health is refreshed every 15 seconds and can be checked manually in Setup.

The backend contract remains in `openapi.json` and `backend-agreement.md`. See the repository README for build, startup, and verification commands.

Missing-line challenges return `origin: ai_missing_line`, `source_path`, and `missing_line`. The AI chooses a validated source line and three hints; the backend supplies the original source as the reference solution. No hidden answer or hints are sent in the initial response. The guided frontend requires model readiness and does not use the curated fallback.

**Workspace layout** (`src/workspace.tsx`, `src/assistant.tsx`, `src/layout.ts`). The Code Explorer is three panels with fixed headings and independently scrolling bodies: Explorer (files, upload notes), Code (CodeMirror editor with a status bar) and the AI assistant panel, titled "Ask Snip" (Snip is Sift's mascot and assistant persona). Draggable, keyboard-operable dividers resize them (arrow keys, Shift for larger steps, Enter or double-click to reset); the Explorer collapses to a rail. Sizes are stored as fractions in `localStorage` (`codesense.workspace.layout.v1`; the key keeps the old product name so saved sizes still load) and the workspace works if storage is blocked. The sizing rules are pure functions in `src/layout.ts` and are unit tested. Below 1180px the panels become a Files / Code / Ask Snip switcher.

**Ask Snip (the AI assistant panel).** One *Analyze* control picks the scope (Entire project, Current file, Selected code) and a single Explain button runs the existing request: `overview` for the project, `explain` for a file or selection. Results live in `useWorkspace`, so switching tabs never re-runs anything. The tabs are Explanation, Issues (the `debug` intent, run from the tab's own button), Relationships (the static project relationships plus any the latest explanation reported) and Practice. Leaving Practice shows the source again while the challenge, answer, hints and result stay in the workspace; returning resumes the same stage. `ExplorerScreen` still switches the editor between the original read-only code and the editable challenge copy, and file selection is disabled while a challenge is open to preserve its source context.
