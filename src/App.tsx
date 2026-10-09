import { useState } from "react";
import { Shell } from "./ui";
import { UploadScreen, ExplorerScreen, DebugScreen, SetupDrawer } from "./screens";
import { useWorkspace } from './useWorkspace';
type Screen = "upload" | "explorer" | "debug";
const META = { upload: ["Understand your project", "An offline workspace for understanding code and practicing debugging.", 0], explorer: ["Explore your code", "Read each file with a plain-language explanation beside it.", 1], debug: ["Fix the bug", "Try it yourself first. Hints unlock one at a time.", 2] } as const;
export default function App() {
  const [screen, setScreen] = useState<Screen>("upload"); const [setup, setSetup] = useState(false);
  const workspace = useWorkspace();
  const [m, sub, step] = META[screen];
  const nav = (n: string) => setScreen(n === "practice" ? "debug" : n === "session" ? "explorer" : "upload");
  const theme = () => { const r = document.documentElement; r.dataset.theme = r.dataset.theme === "light" ? "dark" : "light"; };
  return <>
    <Shell nav={screen === "debug" ? "practice" : screen === "explorer" ? "session" : "workspace"} onNav={nav} title={m} sub={sub} step={screen === 'debug' && workspace.result?.correct ? 4 : step} analyzing={workspace.busy.upload} model={workspace.health?.ai.status === 'ready' ? 'ready' : workspace.checking && !workspace.health ? 'checking' : 'unavailable'} connected={!!workspace.health} onSetup={() => setSetup(true)} onTheme={theme}>
      {screen === "upload" && <UploadScreen view={workspace.busy.upload ? 'loading' : workspace.errors.upload ? 'error' : 'empty'} errorMessage={workspace.errors.upload} disabled={workspace.busy.analysis} onStart={async (file, difficulty) => { if (await workspace.upload(file, difficulty)) setScreen('explorer'); }} />}
      {screen === "explorer" && <ExplorerScreen workspace={workspace} onChallenge={() => { setScreen('debug'); void workspace.loadChallenge(workspace.difficulty); }} />}
      {screen === "debug" && <DebugScreen workspace={workspace} />}
    </Shell>
    {setup && <SetupDrawer workspace={workspace} onClose={() => setSetup(false)} />}
  </>;
}
