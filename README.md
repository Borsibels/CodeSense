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
Use the "UI preview" selector below the footer: Empty, Loading (skeleton), Error. Sidebar: Workspace = Upload, Session = Explorer, Practice = Debug. Theme toggles dark/light. Setup opens a keyboard-accessible modal drawer (Escape closes it).

The welcome panel loops the supplied transparent `public/sloth-mascot.webm`, muted, with a pause/play button. Tips and empty states use `public/sloth-mascot.png` without a frame or background. Reduced-motion preferences or video loading errors show the static PNG instead. CSS adds screen entrances and button feedback; reduced-motion preferences disable animations and transitions. Layouts adapt to phones, tablets, and desktop screens.

ZIP selection supports drag-and-drop and keyboard browsing, rejects multiple files, non-ZIP names, and empty files. Archive contents and source limits require the future analysis runtime. The project preview does not unpack or analyze the selected archive.

Check changes with `npm run typecheck`, `npm run build`, and `node --test tests/upload.test.ts` (the last command needs Node.js 22.18+ for built-in TypeScript support).

## Wiring the backend later
Screens take typed props (src/types.ts): `files`, `code`, `explanation` (Explorer), `challenge`, `result` (Debug). Planned endpoints: /api/health, /api/projects/upload, /api/projects/{id}/files, /api/analyze, /api/challenges/select, /api/challenges/submit, /api/challenges/{id}/hints/{level}, /api/challenges/{id}/solution.
