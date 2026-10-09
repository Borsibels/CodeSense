import { useState } from "react";
import { Shell } from "./ui";
import { UploadScreen, ExplorerScreen, DebugScreen, SetupDrawer } from "./screens";
import type { ViewState } from "./types";
type Screen = "upload" | "explorer" | "debug";
const META = { upload: ["Understand your project", "An offline workspace for understanding code and practicing debugging.", 0], explorer: ["Explore your code", "Read each file with a plain-language explanation beside it.", 1], debug: ["Fix the bug", "Try it yourself first. Hints unlock one at a time.", 2] } as const;
export default function App() {
  const [screen, setScreen] = useState<Screen>("upload"); const [view, setView] = useState<ViewState>("empty"); const [setup, setSetup] = useState(false);
  const [m, sub, step] = META[screen];
  const nav = (n: string) => setScreen(n === "practice" ? "debug" : n === "session" ? "explorer" : "upload");
  const theme = () => { const r = document.documentElement; r.dataset.theme = r.dataset.theme === "light" ? "dark" : "light"; };
  return <>
    <Shell nav={screen === "debug" ? "practice" : screen === "explorer" ? "session" : "workspace"} onNav={nav} title={m} sub={sub} step={step} analyzing={screen === "upload" && view === "loading"} model={screen === "explorer" && view === "error" ? "unavailable" : "ready"} onSetup={() => setSetup(true)} onTheme={theme}>
      {screen === "upload" && <UploadScreen view={view} onStart={() => setScreen("explorer")} />}
      {screen === "explorer" && <ExplorerScreen view={view} onChallenge={() => setScreen("debug")} />}
      {screen === "debug" && <DebugScreen view={view} />}
    </Shell>
    {setup && <SetupDrawer view={view} onClose={() => setSetup(false)} />}
    <label className="dev">Preview state <select value={view} onChange={e => setView(e.target.value as ViewState)}><option value="empty">Empty</option><option value="loading">Loading (skeleton)</option><option value="error">Error</option></select></label>
  </>;
}
