import { useEffect, useRef, useState } from "react";
import { CodeEditor } from "./Code";
import { AnimatedSloth, Empty, ErrBanner, Ic, I, Limits, Pill, Seg, Sk, Tip } from "./ui";
import { uploadError, sourceUploadError, folderFiles } from "./upload";
import type { InputMode, ProjectInput } from './upload';
import type { Difficulty, Language, ViewState } from "./types";
import type { TreeNode } from './api';
import type { Workspace } from './useWorkspace';
import type { WorkspaceStage } from './App';
import { detectLanguage, sourceExtension } from './detectLanguage';
import { AnalysisView, ProblemsView } from './analysis';
const DIFFS = [{ id: "beginner", label: "Beginner" }, { id: "intermediate", label: "Intermediate" }, { id: "experienced", label: "Experienced" }] as { id: Difficulty; label: string }[];
const Lines = ({ n = 5 }: { n?: number }) => <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>{["90%", "60%", "75%", "45%", "80%", "55%"].slice(0, n).map((w, i) => <Sk key={i} w={w} />)}</div>;
const DEFAULT_QWEN_MODEL = 'qwen2.5-coder:3b';

function LocalAISetup({ model, checking, onCheck }: { model: string; checking: boolean; onCheck: () => void }) {
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
    <div><span className="lbl">Internet required for first-time setup</span><h3 id="local-ai-title">Set up local AI</h3><p className="helper">Download Ollama and the {modelName} model once. After setup, CodeSense sends AI requests to the model running on this computer.</p></div>
    <ol>
      <li><a className="btn" href="https://ollama.com/download" target="_blank" rel="noreferrer">Download Ollama</a><span> Install it, then open Ollama so its local service is running.</span></li>
      <li>Download the model with the command below. This step needs an internet connection.</li>
      <li>Come back to CodeSense and choose <b>Check connection</b>.</li>
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
  const [filename, setFilename] = useState(''); const [excluded, setExcluded] = useState(0);
  const language = detectLanguage(code, filename);
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
  const pasteError = !code.trim() ? 'Paste some source code first.' : new TextEncoder().encode(code).length > 100 * 1024 ? 'Pasted code exceeds the 100 KB limit.' : code.includes('\0') ? 'Source code cannot contain NUL bytes.' : !language ? 'The language is unclear. Add a filename ending in .py, .js, .html or .css.' : null;
  const start = () => {
    if (mode === 'paste') { if (pasteError || !language) { setError(pasteError); return; } onStart({ mode, code, language, filename: filename.trim() || `snippet.${sourceExtension[language]}` }, d); }
    else if (files.length) onStart({ mode, files }, d);
  };
  return <div className="grid upload-grid"><section className="card panel upload-panel" aria-busy={view === "loading"}><div className="panel-heading"><div><span className="lbl">01 / Your workspace</span><h2 className="ttl">Bring your code.</h2></div><Ic d={I.file} s={24} /></div>
    <div className="seg" aria-label="Code input method">{([['zip','ZIP archive'],['files','Files'],['folder','Folder'],['paste','Paste code']] as const).map(([id,label]) => <button key={id} className={mode === id ? 'a' : ''} aria-pressed={mode === id} disabled={loading || disabled} onClick={() => { setMode(id); setFiles([]); setError(null); setExcluded(0); }}>{label}</button>)}</div>
    {mode === 'paste' ? <div className="paste-input"><label>Filename (optional)<input aria-label="Pasted code filename" value={filename} placeholder={language ? `snippet.${sourceExtension[language]}` : 'Optional: main.py, app.js, index.html…'} maxLength={200} disabled={loading} onChange={e => { setFilename(e.target.value); setError(null); }} /></label><label htmlFor="pasted-source">Source code</label><textarea id="pasted-source" value={code} disabled={loading} placeholder="Paste your code here…" onChange={e => { setCode(e.target.value); setError(null); }} spellCheck={false} /><p className="helper">{language ? `Detected language: ${language}. Up to 100 KB of UTF-8 source.` : 'Language is detected as you type. For ambiguous code, provide a filename with a supported extension.'}</p></div> : <label className={"drop" + (dragging ? " dragging" : "") + (files.length ? " selected" : "")} onDragOver={e => { e.preventDefault(); if (!loading && mode !== 'folder') setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); if (loading) return; if (mode === 'folder') { setError('Use Choose folder to preserve the folder paths.'); return; } if (Array.from(e.dataTransfer.items).some(item => item.webkitGetAsEntry?.()?.isDirectory)) { setError('Use the Folder option to select directories.'); return; } choose(Array.from(e.dataTransfer.files)); }}>
      <span className="upload-icon"><Ic d={files.length ? I.check : I.up} s={28} /></span><b>{files.length === 1 ? files[0].name : files.length ? `${files.length} files selected` : mode === 'folder' ? 'Choose your project folder' : mode === 'files' ? 'Drop source files here' : 'Drop your project ZIP here'}</b><span>{files.length ? `${(files.reduce((n,f) => n+f.size,0)/1024).toFixed(0)} KB selected` : mode === 'folder' ? 'Keep relative paths and file relationships.' : mode === 'files' ? 'One file or several source files.' : 'One ZIP. A clearer picture of your code.'}</span><span className="btn">{mode === 'folder' ? 'Choose folder' : mode === 'files' ? 'Choose source files' : 'Choose ZIP archive'}<Ic d={I.up} s={16} /></span>
      {mode === 'folder' ? <input key="folder" ref={folderInput} type="file" multiple className="file-input" aria-label="Choose project folder" disabled={loading || disabled} onChange={e => { if (e.target.files?.length) choose(Array.from(e.target.files)); e.target.value = ''; }} /> : <input key="files" ref={fileInput} type="file" accept={mode === 'zip' ? '.zip' : '.py,.js,.html,.htm,.css'} multiple={mode === 'files'} className="file-input" aria-label={mode === 'zip' ? 'Choose project ZIP archive' : 'Choose source files'} disabled={loading || disabled} onChange={e => { if (e.target.files?.length) choose(Array.from(e.target.files)); e.target.value = ''; }} />}
    </label>}
    {excluded > 0 && <p className="helper">{excluded} dependency, generated, or environment files excluded before upload.</p>}
    {error && <ErrBanner title="Couldn't select this project" text={error} />}
    <div className="lbl">Upload limits</div><Limits /><div className="lbl">Supported languages</div><div className="row">{["HTML", "CSS", "JavaScript", "Python"].map(l => <Pill key={l}>{l}</Pill>)}</div><div style={{ color: "var(--muted)", fontSize: 14 }}>Other files are skipped.</div>
    <div className="lbl">Your learning pace</div><Seg<Difficulty> value={d} options={DIFFS} onChange={setD} /><p className="helper">{d === "beginner" ? "Start with the basics, with a little more guidance." : d === "intermediate" ? "Connect the concepts and take on trickier bugs." : "Less guidance. More room to work things out."}</p>
    {view === "error" && <ErrBanner title="Upload failed" text={errorMessage || "Check the upload limits above, then try again."} />}
    {loading ? <div role="status"><div className="bar" role="progressbar" aria-label="Analyzing locally" /><p className="helper">Indexing your project locally…</p></div> : <button className="btn pri lg" disabled={disabled || (mode === 'paste' ? !code.trim() : !files.length)} onClick={start}>Explore project <span aria-hidden="true">→</span></button>}<p className="helper">{disabled ? 'Wait for the current explanation to finish.' : 'Your source will be checked and indexed on this device.'}</p></section>
    <div className="welcome-column">{workspace.health && workspace.health.ai.status !== 'ready' && <LocalAISetup model={workspace.health.ai.model || 'qwen2.5-coder:3b'} checking={workspace.checking} onCheck={() => void workspace.checkHealth(true)} />}<section className="card panel welcome-panel"><div className="companion"><div><span className="lbl">Meet your coding companion</span><h2>A little patience.<br /><em>A lot of progress.</em></h2><p>Big projects make more sense<br />one small step at a time.</p></div><AnimatedSloth /><span className="companion-caption"><span className="dot" />Ready when you are.</span></div><div className="workflow"><h3>From “what?” to “got it.”</h3><ol>{[["Explore", "Get your bearings in the project."], ["Understand", "Turn code into plain-language explanations."], ["Practice", "Work through a bug, with hints if you need them."], ["Reflect", "Learn from each fix and keep moving."]].map(([title, text], i) => <li key={title}><span className="workflow-number">0{i + 1}</span><div><b>{title}</b><span>{text}</span></div></li>)}</ol></div></section><Tip /></div></div>;
}

export function ExplorerScreen({ workspace: w, stage, onStage, onChallenge, onVerify, onRestart }: { workspace: Workspace; stage: WorkspaceStage; onStage: (stage: WorkspaceStage) => void; onChallenge: () => void; onVerify: () => void; onRestart: () => void }) {
  const debugging = stage !== 'explain';
  const [focusedLines, setFocusedLines] = useState<[number, number] | null>(null);
  useEffect(() => setFocusedLines(null), [w.selected?.file_id]);
  const modelReady = w.health?.ai.status === 'ready';
  const analysisReady = modelReady && w.health?.analysis?.available !== false;
  const canExplain = !debugging && !!w.project && analysisReady && !w.busy.analysis && !w.busy.problems && !w.busy.source;
  const canCheck = canExplain && !!w.selected && w.selected.status !== 'skipped';
  // Open the file a result points at and highlight the verified lines.
  const jump = async (path: string, start: number | null, end: number | null) => {
    const file = w.project?.files.find(f => f.path === path);
    if (!file) return;
    if (file.file_id !== w.selected?.file_id) await w.selectFile(file, w.project, true);
    setFocusedLines(start != null && end != null ? [start, end] : null);
  };
  const tree = (nodes: TreeNode[]): React.ReactNode => <ul className="file-tree">{nodes.map(n => <li key={n.path}>{n.type === 'directory' ? <details open><summary>{n.name}</summary>{tree(n.children)}</details> : <button className={'file-choice' + (n.file_id === w.selected?.file_id ? ' selected' : '')} disabled={debugging || !!w.busy.challenge} onClick={() => { const file = w.project?.files.find(f => f.file_id === n.file_id); if (file) void w.selectFile(file); }}><Ic d={I.file} s={15} /><span>{n.name}</span><small>{n.status}</small></button>}</li>)}</ul>;
  return <div className="grid explorer-grid">
    <section className="card panel project-panel"><h2 className="ttl">Project files</h2>{w.project ? <><p className="helper">{w.project.name} · {w.project.analyzed_files} analyzed · {w.project.partial_files} partial · {w.project.skipped_files} skipped</p>{tree(w.project.file_tree)}{w.project.warnings.length > 0 && <details><summary>Upload notes ({w.project.warnings.length})</summary><ul>{w.project.warnings.map((note,i) => <li key={i} className="helper">{note}</li>)}</ul></details>}</> : <Empty mascot="project" title="No project loaded" text="Upload a ZIP from Workspace to see its files." size={4} />}</section>
    <section className="card panel source-panel" aria-busy={!!w.busy.source}><div className="row"><h2 className="ttl" style={{flex:1}}>Code</h2><button className="btn" disabled={!canExplain || !w.selected || w.selected.status === 'skipped'} onClick={() => void w.explain('file')}>Explain file</button><button className="btn" disabled={!canExplain || !w.range} onClick={() => void w.explain('block')}>Explain selection</button></div>
      {w.selected && <p className="helper">{debugging && w.exercise ? w.exercise.source_path : w.selected.path}{!debugging && focusedLines && ` · Referenced lines ${focusedLines[0]}–${focusedLines[1]}`}</p>}
      {w.errors.source && <ErrBanner title="Couldn't load this file" text={w.errors.source} action="Retry" onAction={() => w.selected && void w.selectFile(w.selected)} />}
      {w.busy.source ? <Lines n={6} /> : w.selected?.status === 'skipped' ? <Empty title="File skipped" text={w.selected.reason || 'Unsupported source file.'} /> : w.selected ? <><CodeEditor key={w.selected.file_id} value={debugging && w.exercise ? w.answer : w.code} language={debugging && w.exercise ? w.exercise.language : w.selected.language || 'python'} readOnly={!debugging || !w.exercise || !!w.busy.challenge || !!w.busy.submit || stage !== 'debug'} onChange={debugging ? w.editAnswer : undefined} onSelection={debugging ? undefined : w.setRange} highlight={debugging ? null : focusedLines} /><p className="helper">{debugging ? w.exercise ? `Restore missing line ${w.exercise.missing_line}. Your original file is unchanged.` : 'Generating a missing-line challenge from this source…' : w.range ? `Selected lines ${w.range.start_line}–${w.range.end_line}` : 'Highlight code in the editor to explain selected lines.'}</p>{w.selected.reason && <p className="helper">{w.selected.reason}</p>}</> : <Empty mascot="empty" title="No file selected" text="Choose a file in the tree to read it here." />}
    </section>
    <section className="card panel explanation-panel" aria-busy={!!w.busy.analysis}>{debugging ? <InlineChallengePanel workspace={w} stage={stage} onStage={onStage} onVerify={onVerify} onRestart={onRestart} /> : <><h2 className="ttl">Your code, explained</h2><div className="seg">{(['project','file','block'] as const).map(scope => <button key={scope} className={w.scope === scope ? 'a' : ''} disabled={!!w.busy.analysis} onClick={() => w.changeScope(scope)}>{scope === 'block' ? 'Selection' : scope[0].toUpperCase()+scope.slice(1)}</button>)}</div><Seg<Difficulty> value={w.difficulty} options={DIFFS} onChange={w.changeDifficulty} />
      {!modelReady && <><ErrBanner title={w.health ? 'Model unavailable' : 'Backend unavailable'} text={w.healthError || 'Explanations and missing-line challenges need the local model. File browsing remains available.'} />{w.health && <LocalAISetup model={w.health.ai.model || DEFAULT_QWEN_MODEL} checking={w.checking} onCheck={() => void w.checkHealth(true)} />}</>}
      {modelReady && !analysisReady && <ErrBanner title="Code analysis unavailable" text={w.health?.analysis?.reason || 'Code analysis is not available on this server.'} />}
      {w.errors.analysis && <ErrBanner title="Explanation failed" text={w.errors.analysis} action="Retry" onAction={() => void w.explain()} />}
      <button className="btn" disabled={!canExplain || (w.scope !== 'project' && (!w.selected || w.selected.status === 'skipped')) || (w.scope === 'block' && !w.range)} onClick={() => void w.explain()}>{w.busy.analysis ? 'Explaining locally…' : `Explain ${w.scope === 'block' ? 'selection' : w.scope}`}</button>
      {w.busy.analysis ? <div role="status"><Lines n={5} /><p className="helper">The local model is working. This can take a little time.</p></div> : w.explanation ? <AnalysisView data={w.explanation} onJump={jump} /> : !w.errors.analysis && <Empty mascot="explanation" title="Nothing explained yet" text="Choose Project, File or Selection and request an explanation." />}
      <div className="problems-block"><div className="lbl">Possible problems</div><p className="helper">Ask the local AI to look for things that might be wrong. It can flag code that is fine and miss real problems, so every result is a suspicion to check, not a confirmed bug.</p>
        <button className="btn" disabled={!canCheck} onClick={() => void w.checkProblems()}>{w.busy.problems ? 'Looking locally…' : w.range ? `Check lines ${w.range.start_line}–${w.range.end_line}` : 'Check this file'}</button>
        {w.errors.problems && <ErrBanner title="Check failed" text={w.errors.problems} action="Retry" onAction={() => void w.checkProblems()} />}
        {w.busy.problems ? <div role="status"><Lines n={4} /><p className="helper">The local model is working. This can take a little time.</p></div> : w.problems && <ProblemsView data={w.problems} onJump={jump} />}</div>
      {w.project && <details><summary>Static file relationships</summary><ul>{w.project.relationships.map((r,i) => <li className="helper" key={i}>{r.source} → {r.target} ({r.resolved ? r.kind : 'unresolved'})</li>)}</ul></details>}
      <button className="btn pri lg" disabled={!modelReady || !!w.busy.challenge || !!w.busy.analysis || !!w.busy.problems || !!w.busy.source || !w.selected || w.selected.status === 'skipped'} onClick={onChallenge}>Generate missing-line challenge</button><Tip /></>}
    </section></div>;
}

function InlineChallengePanel({ workspace: w, stage, onStage, onVerify, onRestart }: { workspace: Workspace; stage: WorkspaceStage; onStage: (stage: WorkspaceStage) => void; onVerify: () => void; onRestart: () => void }) {
  const locked = !!(w.busy.challenge || w.busy.submit || w.busy.hint || w.busy.solution);
  return <><h2 className="ttl">{stage === 'learn' ? 'Learn from your fix' : stage === 'verify' ? 'Verify your fix' : 'Restore the missing line'}</h2>
    <p className="helper">Keep working in the code editor beside this panel.</p>
    {w.busy.challenge ? <div role="status"><Lines n={4} /><p>Generating with local AI…</p></div> : w.exercise && <><p>{w.exercise.objective}</p><div className="row"><Pill>{w.exercise.language}</Pill><Pill>{w.exercise.difficulty}</Pill></div><p className="helper">{w.exercise.verification_policy}</p></>}
    {w.errors.challenge && <ErrBanner title="Challenge generation failed" text={w.errors.challenge} />}
    {stage === 'debug' && <><div className="lbl">Difficulty</div><Seg<Difficulty> value={w.challengeDifficulty} options={DIFFS} onChange={d => { if (!locked && w.health?.ai.status === 'ready') void w.loadChallenge(d); }} /><p className="helper">Changing difficulty generates a new challenge and resets your answer and hints.</p><button className="btn" disabled={locked || w.health?.ai.status !== 'ready'} onClick={() => void w.loadChallenge()}>Regenerate challenge</button>
      {w.exercise && <><div className="lbl">Hints</div>{Array.from({length:w.exercise.hint_count},(_,i) => <div className="hint" key={i}><Ic d={i < w.hints.length ? I.check : I.lock} s={16} /><span>Hint {i+1}: {w.hints[i] || 'Locked'}</span></div>)}<button className="btn" disabled={locked || w.hints.length >= w.exercise.hint_count} onClick={() => void w.revealHint()}>{w.busy.hint ? 'Loading hint…' : w.hints.length >= w.exercise.hint_count ? 'All hints revealed' : `Reveal hint ${w.hints.length+1} of ${w.exercise.hint_count}`}</button></>}
      {w.errors.hint && <ErrBanner title="Hint unavailable" text={w.errors.hint} />}{w.errors.submit && <ErrBanner title="Check failed" text={w.errors.submit} />}
      <button className="btn pri" disabled={!w.exercise || locked || !w.answer.trim()} onClick={onVerify}>{w.busy.submit ? 'Checking…' : 'Check my fix'}</button></>}
    {(stage === 'verify' || stage === 'learn') && w.result && <div role="status"><b>{w.result.correct ? 'Restored' : 'Not restored yet'}</b><p>{w.result.feedback}</p>{w.result.limitations.map((l,i) => <p className="helper" key={i}>{l}</p>)}</div>}
    {stage === 'verify' && <><button className="btn" onClick={() => onStage('debug')}>Revise my fix</button><button className="btn pri" onClick={() => onStage('learn')}>Continue → Learn</button></>}
    {stage === 'learn' && <><p>Compare your fix with the original line and explain why it matters.</p><button className="btn" disabled={locked || !!w.solution} onClick={() => void w.revealSolution()}>Reveal original solution</button>{w.errors.solution && <ErrBanner title="Solution unavailable" text={w.errors.solution} />}{w.solution && <><h3>Original source</h3><CodeEditor value={w.solution} language={w.exercise?.language} readOnly /></>}<button className="btn" onClick={() => onStage('debug')}>Back to my fix</button><button className="btn pri" onClick={onRestart}>Start a new project</button></>}
  </>;
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
