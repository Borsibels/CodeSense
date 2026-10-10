import { useEffect, useRef, useState } from "react";
import { AnimatedSloth, ErrBanner, Ic, I, Limits, Pill, Seg, Tip } from "./ui";
import { uploadError, sourceUploadError, folderFiles } from "./upload";
import type { InputMode, ProjectInput } from './upload';
import { DIFFS } from "./types";
import type { Difficulty, ViewState } from "./types";
import type { Workspace } from './useWorkspace';
import { detectLanguage, sourceExtension } from './detectLanguage';
export const DEFAULT_QWEN_MODEL = 'qwen2.5-coder:3b';

export function LocalAISetup({ model, checking, onCheck }: { model: string; checking: boolean; onCheck: () => void }) {
  const [copyStatus, setCopyStatus] = useState('');
  const modelName = /^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$/.test(model) ? model : DEFAULT_QWEN_MODEL;
  const command = `ollama pull ${modelName}`;
  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopyStatus('Command copied. Paste it into Terminal or PowerShell.');
    } catch {
      setCopyStatus('Copy was blocked. Select the command below and copy it.');
    }
  };
  return <section className="card local-ai-setup" aria-labelledby="local-ai-title">
    <div><span className="lbl">Internet required for first-time setup</span><h3 id="local-ai-title">Set up local AI</h3><p className="helper">Download Ollama and the {modelName} model once. After setup, Sift sends AI requests to the model running on this computer.</p></div>
    <ol>
      <li><a className="btn" href="https://ollama.com/download" target="_blank" rel="noreferrer">Download Ollama</a><span> Install it, then open Ollama so its local service is running.</span></li>
      <li>Download the model with the command below. This step needs an internet connection.</li>
      <li>Come back to Sift and choose <b>Check connection</b>.</li>
    </ol>
    <div className="local-ai-command"><code>{command}</code><button className="btn" onClick={() => void copyCommand()}>Copy Qwen download command</button></div>
    <p className="helper" role="status" aria-live="polite">{copyStatus || 'Run this command in Terminal (macOS/Linux) or PowerShell (Windows).'} The model download may take a while.</p>
    <button className="btn pri" disabled={checking} onClick={onCheck}>{checking ? 'Checking…' : 'Check connection'}</button>
  </section>;
}

export function UploadScreen({ view, onStart, errorMessage, disabled, workspace }: { view: ViewState; onStart: (input: ProjectInput, difficulty: Difficulty) => void; errorMessage?: string; disabled?: boolean; workspace: Workspace }) {
  const [files, setFiles] = useState<File[]>([]); const [d, setD] = useState<Difficulty>("beginner");
  const [mode, setMode] = useState<InputMode>('zip');
  const [code, setCode] = useState('');
  const [excluded, setExcluded] = useState(0);
  const language = detectLanguage(code);
  const fileInput = useRef<HTMLInputElement>(null); const folderInput = useRef<HTMLInputElement>(null);
  const loading = view === 'loading';
  useEffect(() => { folderInput.current?.setAttribute('webkitdirectory', ''); }, [mode]);
  const [error, setError] = useState<string | null>(null); const [dragging, setDragging] = useState(false);
  const choose = (incoming: File[]) => {
    const chosen = mode === 'folder' ? folderFiles(incoming) : incoming;
    setExcluded(incoming.length - chosen.length);
    const message = mode === 'zip' ? uploadError(chosen) : sourceUploadError(chosen, mode === 'folder');
    setError(message); setFiles(message ? [] : chosen);
  };
  const pasteError = !code.trim() ? 'Paste some source code first.' : new TextEncoder().encode(code).length > 100 * 1024 ? 'Pasted code exceeds the 100 KB limit.' : code.includes('\0') ? 'Source code cannot contain NUL bytes.' : !language ? 'The language is unclear. Paste a more complete snippet, or upload a source file.' : null;
  const start = () => {
    if (mode === 'paste') { if (pasteError || !language) { setError(pasteError); return; } onStart({ mode, code, language, filename: `snippet.${sourceExtension[language]}` }, d); }
    else if (files.length) onStart({ mode, files }, d);
  };
  return <div className="grid upload-grid"><section className="card panel upload-panel" aria-busy={view === "loading"}><div className="panel-heading"><div><span className="lbl">01 / Your workspace</span><h2 className="ttl">Bring your code.</h2></div><Ic d={I.file} s={24} /></div>
    <div className="seg" aria-label="Code input method">{([['zip','ZIP archive'],['files','Files'],['folder','Folder'],['paste','Paste code']] as const).map(([id,label]) => <button key={id} className={mode === id ? 'a' : ''} aria-pressed={mode === id} disabled={loading || disabled} onClick={() => { setMode(id); setFiles([]); setError(null); setExcluded(0); }}>{label}</button>)}</div>
    {mode === 'paste' ? <div className="paste-input"><label htmlFor="pasted-source">Source code</label><textarea id="pasted-source" value={code} disabled={loading} placeholder="Paste your code here…" onChange={e => { setCode(e.target.value); setError(null); }} spellCheck={false} /><p className="helper">{language ? `Detected language: ${language}. Up to 100 KB of UTF-8 source.` : 'Language is detected automatically. If it is unclear, paste a more complete snippet or upload a source file.'}</p></div> : <label className={"drop" + (dragging ? " dragging" : "") + (files.length ? " selected" : "")} onDragOver={e => { e.preventDefault(); if (!loading && mode !== 'folder') setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); if (loading) return; if (mode === 'folder') { setError('Use Choose folder to preserve the folder paths.'); return; } if (Array.from(e.dataTransfer.items).some(item => item.webkitGetAsEntry?.()?.isDirectory)) { setError('Use the Folder option to select directories.'); return; } choose(Array.from(e.dataTransfer.files)); }}>
      <span className="upload-icon"><Ic d={files.length ? I.check : I.up} s={28} /></span><b>{files.length === 1 ? files[0].name : files.length ? `${files.length} files selected` : mode === 'folder' ? 'Choose your project folder' : mode === 'files' ? 'Drop source files here' : 'Drop your project ZIP here'}</b><span>{files.length ? `${(files.reduce((n,f) => n+f.size,0)/1024).toFixed(0)} KB selected` : mode === 'folder' ? 'Keep relative paths and file relationships.' : mode === 'files' ? 'One file or several source files.' : 'One ZIP. A clearer picture of your code.'}</span><span className="btn">{mode === 'folder' ? 'Choose folder' : mode === 'files' ? 'Choose source files' : 'Choose ZIP archive'}<Ic d={I.up} s={16} /></span>
      {mode === 'folder' ? <input key="folder" ref={folderInput} type="file" multiple className="file-input" aria-label="Choose project folder" disabled={loading || disabled} onChange={e => { if (e.target.files?.length) choose(Array.from(e.target.files)); e.target.value = ''; }} /> : <input key="files" ref={fileInput} type="file" accept={mode === 'zip' ? '.zip' : '.py,.js,.html,.htm,.css'} multiple={mode === 'files'} className="file-input" aria-label={mode === 'zip' ? 'Choose project ZIP archive' : 'Choose source files'} disabled={loading || disabled} onChange={e => { if (e.target.files?.length) choose(Array.from(e.target.files)); e.target.value = ''; }} />}
    </label>}
    {excluded > 0 && <p className="helper">{excluded} dependency, generated, or environment files excluded before upload.</p>}
    {error && <ErrBanner title="Couldn't select this project" text={error} />}
    <div className="lbl">Upload limits</div><Limits /><div className="lbl">Supported languages</div><div className="row">{["HTML", "CSS", "JavaScript", "Python"].map(l => <Pill key={l}>{l}</Pill>)}</div><div style={{ color: "var(--muted)", fontSize: 14 }}>Other files are skipped.</div>
    <div className="lbl">Your learning pace</div><Seg<Difficulty> value={d} options={DIFFS} onChange={setD} /><p className="helper">{d === "beginner" ? "Start with the basics, with a little more guidance." : d === "intermediate" ? "Connect the concepts and take on trickier bugs." : "Less guidance. More room to work things out."}</p>
    {view === "error" && <ErrBanner title="Upload failed" text={errorMessage || "Check the upload limits above, then try again."} />}
    {loading ? <div role="status"><div className="bar" role="progressbar" aria-label="Analyzing locally" /><p className="helper">Sift is indexing your project locally…</p></div> : <button className="btn pri lg" disabled={disabled || (mode === 'paste' ? !code.trim() : !files.length)} onClick={start}>Explore project <span aria-hidden="true">→</span></button>}<p className="helper">{disabled ? 'Wait for the current explanation to finish.' : 'Your source will be checked and indexed on this device.'}</p></section>
    <div className="welcome-column">{workspace.health && workspace.health.ai.status !== 'ready' && <LocalAISetup model={workspace.health.ai.model || DEFAULT_QWEN_MODEL} checking={workspace.checking} onCheck={() => void workspace.checkHealth(true)} />}<section className="card panel welcome-panel"><div className="companion"><div><span className="lbl">Meet Snip, your coding companion</span><h2>A little patience.<br /><em>A lot of progress.</em></h2><p>Big projects make more sense<br />one small step at a time.</p></div><AnimatedSloth /><span className="companion-caption"><span className="dot" />Snip is ready when you are.</span></div><div className="workflow"><h3>From “what?” to “got it.”</h3><ol>{[["Explore", "Get your bearings in the project."], ["Understand", "Turn code into plain-language explanations."], ["Practice", "Work through a bug, with hints if you need them."], ["Reflect", "Learn from each fix and keep moving."]].map(([title, text], i) => <li key={title}><span className="workflow-number">0{i + 1}</span><div><b>{title}</b><span>{text}</span></div></li>)}</ol></div></section><Tip /></div></div>;
}

export function SetupDrawer({ workspace: w, onClose }: { workspace: Workspace; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const drawer = dialog.current; const previous = document.activeElement as HTMLElement; drawer?.showModal(); return () => { drawer?.close(); previous?.focus(); }; }, []);
  return <dialog ref={dialog} className="drawer" aria-label="Setup" onCancel={onClose}><div className="row"><h2 className="ttl" style={{flex:1}}>Setup</h2><button className="btn" onClick={onClose}>Close</button></div>
    <div className="hint">Backend <span>{w.health?.backend || 'Unreachable'}</span></div><div className="hint">Local model <span>{w.health?.ai.status || 'Not checked'}</span></div><div className="hint">Model name <span>{w.health?.ai.model || 'Not available'}</span></div><div className="hint">Session storage <span>{w.health?.storage || 'Not checked'}</span></div>
    {(!w.health || w.health.ai.status === 'ready') && <button className="btn" disabled={w.checking} onClick={() => void w.checkHealth(true)}>{w.checking ? 'Checking…' : 'Check connection'}</button>}
    {w.healthError && <ErrBanner title="Backend unreachable" text={w.healthError} />}{w.health && w.health.ai.status !== 'ready' && <LocalAISetup model={w.health.ai.model || DEFAULT_QWEN_MODEL} checking={w.checking} onCheck={() => void w.checkHealth(true)} />}
    <div className="lbl">Offline readiness</div><p className="helper">Fonts and application assets are bundled locally. A full offline restart still needs checking on your demo laptop.</p><Tip /></dialog>;
}
