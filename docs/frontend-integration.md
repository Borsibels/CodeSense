# Frontend/backend integration

`src/api.ts` defines the typed HTTP client; `src/useWorkspace.ts` owns project, source, explanation, and exercise state. `App.tsx` shares that state between screens so navigation retains the current session.

| UI action | Backend request |
|---|---|
| Setup / connection status | `GET /api/health` |
| Upload project | `POST /api/projects/upload` with multipart `file` |
| Select source file | `GET /api/projects/{project_id}/files/{file_id}` |
| Explain project/file/selection | `POST /api/analyze` with scope `project`, `file`, or `block` |
| Start/load exercise | `POST /api/challenges/select` |
| Check answer | `POST /api/challenges/submit` |
| Reveal next hint | `GET /api/challenges/{id}/hints/{level}` |
| Reveal reference solution | `GET /api/challenges/{id}/solution` |

Project responses supply `file_tree`, source metadata, relationships, and upload warnings. Skipped files display their reason. CodeMirror selections send inclusive original line numbers; explanation sections resolve their own `file_id` before highlighting. Stale source and explanation responses cannot replace a newer selection.

Challenge instructions come from `objective`; the general fallback is identified by `match.kind`. Hints are fetched only on request and the reference solution appears separately without overwriting the learner's answer. Verification displays `correct`, `feedback`, and `limitations`; it makes no execution claim.

The client uses relative `/api` URLs. Vite proxies these to `http://127.0.0.1:8000` in development. Production builds in root `dist` are served by FastAPI when present at startup. API errors display the backend's detail; invalid responses, network failures, and timeouts receive explicit messages.

Backend readiness and AI readiness remain separate. AI controls require a ready model, while browsing and practice remain usable without Ollama. Health is refreshed every 15 seconds and can be checked manually in Setup.

The backend contract remains in `openapi.json` and `backend-agreement.md`. See the repository README for build, startup, and verification commands.
