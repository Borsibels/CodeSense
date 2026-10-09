# CodeSense UI (UI/UX only)

React + TypeScript + Vite + Tailwind + CodeMirror 6. No API calls, no mock data, fully offline at runtime (fonts are bundled via @fontsource).

## Setup
1. Install Node.js 18+ (check: `node -v`, `npm -v`).
2. `cd codesense-ui`
3. `npm install`   (needs internet once; afterwards everything works offline)
4. `npm run dev`   -> open http://127.0.0.1:5173
5. `npm run build` -> production build in `dist/`; `npm run preview` to test it
6. Optional: `npm run typecheck`

## Viewing every state
Use the "Preview state" selector (bottom of the page): Empty, Loading (skeleton), Error. Sidebar: Workspace = Upload, Session = Explorer, Practice = Debug. Theme toggles dark/light. Setup opens the drawer.

## Wiring the backend later
Screens take typed props (src/types.ts): `files`, `code`, `explanation` (Explorer), `challenge`, `result` (Debug). Planned endpoints: /api/health, /api/projects/upload, /api/projects/{id}/files, /api/analyze, /api/challenges/select, /api/challenges/submit, /api/challenges/{id}/hints/{level}, /api/challenges/{id}/solution.
