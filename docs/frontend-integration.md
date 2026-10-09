# Existing frontend to backend mapping

The repository currently contains a UI built around props in `src/types.ts`; this PR
adds backend APIs without changing those components or wiring them to HTTP yet.

Use the backend agreement and `openapi.json` as the wire contract. Keep a small
mapping layer in the future frontend API client rather than assuming UI props equal
server JSON. For example:

| Existing UI prop | Backend field / behavior |
|---|---|
| `FileNode.path`, `children` | `file_tree.path`, `children`; directory nodes have no source |
| File language/status | Lookup metadata by `file_id`; skipped files include a reason |
| `Relationship.from`, `to` | `source`, `target`; use IDs when resolving a local file |
| Explanation `level: selection` | Request `scope: block` with original inclusive line range |
| Explanation `summary`, `concepts` | Identical public fields |
| Explanation `steps` | Map `sections` to `{lines: [start_line, end_line], text: explanation}` |
| Explanation `limits` | `limitations` |
| Explanation relationships | From project metadata/context, not invented by the model |
| Challenge `concept_tags` | `concepts` |
| Challenge `instructions` | `objective` plus optional title |
| Challenge `hint_levels` | Fetch the hints endpoint only when requested |
| Challenge `is_general` | Selection response `match.kind === "general"` |
| Submission `overall` | `correct ? "passed" : "not_met"`; no partial scoring exists |
| Submission criteria | One structural requirement with `met: correct`, plus feedback |

Keep `file_id` with UI selection state to retrieve source or request analysis. Backend
source content is returned as `code`. Explanation sections can reference related files,
so do not highlight every section against the currently selected file blindly.

The model readiness status is separate from backend readiness. Disable AI controls
when unavailable, but retain browsing and independent exercises. Do not expose a
reference solution before the user explicitly asks to reveal it.

The local backend can serve a built frontend at root `dist` or `frontend/dist` when
started after the build. Development remains two processes on ports 5173 and 8000.
The UI's existing offline font/assets setup remains unchanged.
