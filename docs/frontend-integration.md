# Frontend/backend integration

`src/api.ts` defines the typed HTTP client; `src/useWorkspace.ts` owns project, source, explanation, and exercise state. `App.tsx` shares that state across the sequential Upload → Explain → Debug → Verify → Learn workflow. Each stage has an explicit forward action and a back button that retains the current session. Verification appears only after a successful check request; failed checks can be revised or reviewed in Learn. Network errors stay on Debug. Practice and Session sidebar shortcuts are removed.

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

**Analysis rendering** (`src/analysis.tsx`). AI-written text carries an "AI-written" badge; anything CodeSense computed or copied from the real file carries "Checked by CodeSense" (relationships, glossary, evidence excerpts, verification, tiers, rule-based checks). The UI shows `selection.note` whenever the analysis is wider than the selection (the backend maps lines to the enclosing function/class, else the whole file). Possible problems are worded as suspicions: a `source_verified` finding is labelled "Location verified", never as a confirmed bug, and `no_clear_problem` is labelled as not proof. The `Difficulty` value `experienced` is sent as `depth: "advanced"`. Possible problems are separate from the Debug stage's missing-line challenges and do not feed them.

Errors: `request()` reads both envelopes, the host's `{code, detail}` and the engine's `{error: {code, message}}`. Analysis requests use a 330 s timeout (up to three local model calls); everything else keeps 100 s. Only one model request can run at a time; a second returns `AI_BUSY`, and the workspace disables the other AI actions while one is pending.

Challenge instructions come from `objective`; the general fallback is identified by `match.kind`. Hints are fetched only on request and the reference solution appears separately without overwriting the learner's answer. Verification displays `correct`, `feedback`, and `limitations`; it makes no execution claim.

The client uses relative `/api` URLs. Vite proxies these to `http://127.0.0.1:8000` in development. Production builds in root `dist` are served by FastAPI when present at startup. API errors display the backend's detail; invalid responses, network failures, and timeouts receive explicit messages.

Backend readiness and AI readiness remain separate. AI controls require a ready model, while browsing remains usable without Ollama; generating new missing-line exercises requires AI. Health is refreshed every 15 seconds and can be checked manually in Setup.

The backend contract remains in `openapi.json` and `backend-agreement.md`. See the repository README for build, startup, and verification commands.

Missing-line challenges return `origin: ai_missing_line`, `source_path`, and `missing_line`. The AI chooses a validated source line and three hints; the backend supplies the original source as the reference solution. No hidden answer or hints are sent in the initial response. The guided frontend requires model readiness and does not use the curated fallback.

The workspace now remains mounted across Explain, Debug, Verify, and Learn. `ExplorerScreen` switches the source editor between original read-only code and the editable challenge copy, while `InlineChallengePanel` presents hints, result, retry, and solution review. Step changes do not key/remount the workspace or navigate to separate screens. File selection is disabled during a challenge to preserve its source context.
