import { useEffect, useState } from "react";
import { Shell } from "./ui";
import { UploadScreen, SetupDrawer } from "./screens";
import { ExplorerScreen } from "./workspace";
import { useWorkspace } from './useWorkspace';
export type WorkspaceStage = 'explain' | 'debug' | 'verify' | 'learn';
export default function App() {
  const [loaded, setLoaded] = useState(false);
  const [stage, setStage] = useState<WorkspaceStage>('explain');
  const [setup, setSetup] = useState(false);
  const workspace = useWorkspace();
  const locked = Object.values(workspace.busy).some(Boolean);
  useEffect(() => {
    if (!workspace.connectionNotice) return;
    const timer = window.setTimeout(workspace.dismissConnectionNotice, 6500);
    return () => window.clearTimeout(timer);
  }, [workspace.connectionNotice]);
  const theme = () => { const r = document.documentElement; r.dataset.theme = r.dataset.theme === 'light' ? 'dark' : 'light'; };
  return <>
    <Shell title={loaded ? 'Explore your code' : 'Understand your project'} sub={loaded ? 'Explain, debug, and review your code in this workspace.' : 'Welcome to Sift. Start by adding your code.'} dashboard={loaded} model={workspace.health?.ai.status === 'ready' ? 'ready' : workspace.checking && !workspace.health ? 'checking' : 'unavailable'} connected={!!workspace.health} onSetup={() => setSetup(true)} onTheme={theme}>
      {!loaded ? <UploadScreen view={workspace.busy.upload ? 'loading' : workspace.errors.upload ? 'error' : 'empty'} errorMessage={workspace.errors.upload} disabled={locked} workspace={workspace} onStart={async (input, difficulty) => { if (await workspace.upload(input, difficulty)) { setStage('explain'); setLoaded(true); } }} /> : <ExplorerScreen workspace={workspace} stage={stage} onStage={setStage} onChallenge={() => { setStage('debug'); void workspace.loadChallenge(workspace.difficulty); }} onVerify={async () => { if (await workspace.submit()) setStage('verify'); }} onRestart={() => { setStage('explain'); setLoaded(false); }} />}
    </Shell>
    {setup && <SetupDrawer workspace={workspace} onClose={() => setSetup(false)} />}
    {workspace.connectionNotice && <div className={'connection-toast' + (workspace.connectionNotice.startsWith('Local AI is active') ? ' is-ready' : '')} role="status" aria-live="polite">
      <div><b>Connection check</b><span>{workspace.connectionNotice}</span></div>
      <button className="connection-toast-close" aria-label="Dismiss connection message" onClick={workspace.dismissConnectionNotice}>×</button>
    </div>}
  </>;
}
