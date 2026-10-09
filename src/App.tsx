import { useState } from "react";
import { Shell } from "./ui";
import { UploadScreen, ExplorerScreen, SetupDrawer } from "./screens";
import { useWorkspace } from './useWorkspace';
export type WorkspaceStage = 'explain' | 'debug' | 'verify' | 'learn';
const STEPS = { explain: 1, debug: 2, verify: 3, learn: 4 };
export default function App() {
  const [loaded, setLoaded] = useState(false);
  const [stage, setStage] = useState<WorkspaceStage>('explain');
  const [setup, setSetup] = useState(false);
  const workspace = useWorkspace();
  const locked = Object.values(workspace.busy).some(Boolean);
  const theme = () => { const r = document.documentElement; r.dataset.theme = r.dataset.theme === 'light' ? 'dark' : 'light'; };
  return <>
    <Shell title={loaded ? 'Explore your code' : 'Understand your project'} sub={loaded ? 'Explain, debug, and review your code in this workspace.' : 'Start by adding your code.'} step={loaded ? STEPS[stage] : 0} analyzing={workspace.busy.upload} model={workspace.health?.ai.status === 'ready' ? 'ready' : workspace.checking && !workspace.health ? 'checking' : 'unavailable'} connected={!!workspace.health} onSetup={() => setSetup(true)} onTheme={theme}>
      {loaded && <div className="row" style={{marginBottom:16}}><button className="btn" disabled={locked} onClick={() => stage === 'explain' ? setLoaded(false) : setStage('explain')}>{stage === 'explain' ? '← Back to Upload' : '← Back to explanation'}</button></div>}
      {!loaded ? <UploadScreen view={workspace.busy.upload ? 'loading' : workspace.errors.upload ? 'error' : 'empty'} errorMessage={workspace.errors.upload} disabled={locked} onStart={async (input, difficulty) => { if (await workspace.upload(input, difficulty)) { setStage('explain'); setLoaded(true); } }} /> : <ExplorerScreen workspace={workspace} stage={stage} onStage={setStage} onChallenge={() => { setStage('debug'); void workspace.loadChallenge(workspace.difficulty); }} onVerify={async () => { if (await workspace.submit()) setStage('verify'); }} onRestart={() => { setStage('explain'); setLoaded(false); }} />}
    </Shell>
    {setup && <SetupDrawer workspace={workspace} onClose={() => setSetup(false)} />}
  </>;
}
