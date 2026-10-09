import { useState } from "react";
import { Shell } from "./ui";
import { UploadScreen, ExplorerScreen, DebugScreen, SetupDrawer } from "./screens";
import { useWorkspace } from './useWorkspace';
type Screen = "workspace" | "session";
type SessionTab = "explore" | "practice";
const META = {
  workspace: ["Understand your project", "An offline workspace for understanding code and practicing debugging.", 0],
  explore: ["Explore your code", "Read each file with a plain-language explanation beside it.", 1],
  practice: ["Fix the bug", "Try it yourself first. Hints unlock one at a time.", 2],
} as const;
export default function App() {
  const [screen, setScreen] = useState<Screen>("workspace");
  const [sessionTab, setSessionTab] = useState<SessionTab>("explore");
  const [setup, setSetup] = useState(false);
  const workspace = useWorkspace();
  const [m, sub, step] = META[screen === "workspace" ? "workspace" : sessionTab];
  const nav = (n: Screen) => setScreen(n);
  const theme = () => { const r = document.documentElement; r.dataset.theme = r.dataset.theme === "light" ? "dark" : "light"; };
  return <>
    <Shell nav={screen} onNav={nav} title={m} sub={sub} step={screen === "session" && sessionTab === "practice" && workspace.result?.correct ? 4 : step} analyzing={workspace.busy.upload} model={workspace.health?.ai.status === 'ready' ? 'ready' : workspace.checking && !workspace.health ? 'checking' : 'unavailable'} connected={!!workspace.health} onSetup={() => setSetup(true)} onTheme={theme}>
      {screen === "workspace" && <UploadScreen view={workspace.busy.upload ? 'loading' : workspace.errors.upload ? 'error' : 'empty'} errorMessage={workspace.errors.upload} disabled={workspace.busy.analysis} onStart={async (file, difficulty) => { if (await workspace.upload(file, difficulty)) { setSessionTab('explore'); setScreen('session'); } }} />}
      {screen === "session" && <div className="session-content">
        <nav className="session-tabs" role="group" aria-label="Session activities">
          <button className="session-tab" aria-pressed={sessionTab === "explore"} onClick={() => setSessionTab("explore")}>Explore</button>
          <button className="session-tab" aria-pressed={sessionTab === "practice"} onClick={() => setSessionTab("practice")}>Practice</button>
        </nav>
        <div className="session-panel">
          {sessionTab === "explore"
            ? <ExplorerScreen workspace={workspace} onChallenge={() => { setSessionTab('practice'); void workspace.loadChallenge(workspace.difficulty); }} />
            : <DebugScreen workspace={workspace} />}
        </div>
      </div>}
    </Shell>
    {setup && <SetupDrawer workspace={workspace} onClose={() => setSetup(false)} />}
  </>;
}
