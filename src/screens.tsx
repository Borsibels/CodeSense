import { useEffect, useRef, useState } from "react";
import { CodeEditor } from "./Code";
import { AnimatedSloth, Empty, ErrBanner, Ic, I, Limits, Pill, Seg, Sk, Tip } from "./ui";
import { uploadError, sourceUploadError, folderFiles } from "./upload";
import type { InputMode, ProjectInput } from './upload';
import type { Difficulty, Language, ViewState } from "./types";
import type { TreeNode } from './api';
import type { Workspace } from './useWorkspace';
const DIFFS = [{ id: "beginner", label: "Beginner" }, { id: "intermediate", label: "Intermediate" }, { id: "experienced", label: "Experienced" }] as { id: Difficulty; label: string }[];
const Lines = ({ n = 5 }: { n?: number }) => <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>{["90%", "60%", "75%", "45%", "80%", "55%"].slice(0, n).map((w, i) => <Sk key={i} w={w} />)}</div>;

export function UploadScreen({ view, onStart, errorMessage, disabled }: { view: ViewState; onStart: (input: ProjectInput, difficulty: Difficulty) => void; errorMessage?: string; disabled?: boolean }) {
  const [files, setFiles] = useState<File[]>([]); const [d, setD] = useState<Difficulty>("beginner");
  const [mode, setMode] = useState<InputMode>('zip');
  const [code, setCode] = useState(''); const [language, setLanguage] = useState<Language>('python');
  const [filename, setFilename] = useState('snippet.py'); const [excluded, setExcluded] = useState(0);
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
  const pasteError = !code.trim() ? 'Paste some source code first.' : new TextEncoder().encode(code).length > 100 * 1024 ? 'Pasted code exceeds the 100 KB limit.' : code.includes('\0') ? 'Source code cannot contain NUL bytes.' : !filename.trim() ? 'Provide a filename.' : null;
  const start = () => {
    if (mode === 'paste') { if (pasteError) { setError(pasteError); return; } onStart({ mode, code, language, filename: filename.trim() }, d); }
    else if (files.length) onStart({ mode, files }, d);
  };
  return <div className="grid upload-grid"><section className="card panel upload-panel" aria-busy={view === "loading"}><div className="panel-heading"><div><span className="lbl">01 / Your workspace</span><h2 className="ttl">Bring your code.</h2></div><Ic d={I.file} s={24} /></div>
    <div className="seg" aria-label="Code input method">{([['zip','ZIP archive'],['files','Files'],['folder','Folder'],['paste','Paste code']] as const).map(([id,label]) => <button key={id} className={mode === id ? 'a' : ''} aria-pressed={mode === id} disabled={loading || disabled} onClick={() => { setMode(id); setFiles([]); setError(null); setExcluded(0); }}>{label}</button>)}</div>
    {mode === 'paste' ? <div className="paste-input"><div className="row"><label>Language <select aria-label="Pasted code language" value={language} disabled={loading} onChange={e => { const lang = e.target.value as Language; setLanguage(lang); setFilename(`snippet.${{python:'py',javascript:'js',html:'html',css:'css'}[lang]}`); }}><option value="python">Python</option><option value="javascript">JavaScript</option><option value="html">HTML</option><option value="css">CSS</option></select></label><label>Filename <input aria-label="Pasted code filename" value={filename} maxLength={200} disabled={loading} onChange={e => setFilename(e.target.value)} /></label></div><label htmlFor="pasted-source">Source code</label><textarea id="pasted-source" value={code} disabled={loading} placeholder="Paste your code here…" onChange={e => { setCode(e.target.value); setError(null); }} spellCheck={false} /><p className="helper">Up to 100 KB of UTF-8 source. The selected language determines how it is parsed.</p></div> : <label className={"drop" + (dragging ? " dragging" : "") + (files.length ? " selected" : "")} onDragOver={e => { e.preventDefault(); if (!loading && mode !== 'folder') setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); if (loading) return; if (mode === 'folder') { setError('Use Choose folder to preserve the folder paths.'); return; } if (Array.from(e.dataTransfer.items).some(item => item.webkitGetAsEntry?.()?.isDirectory)) { setError('Use the Folder option to select directories.'); return; } choose(Array.from(e.dataTransfer.files)); }}>
      <span className="upload-icon"><Ic d={files.length ? I.check : I.up} s={28} /></span><b>{files.length === 1 ? files[0].name : files.length ? `${files.length} files selected` : mode === 'folder' ? 'Choose your project folder' : mode === 'files' ? 'Drop source files here' : 'Drop your project ZIP here'}</b><span>{files.length ? `${(files.reduce((n,f) => n+f.size,0)/1024).toFixed(0)} KB selected` : mode === 'folder' ? 'Keep relative paths and file relationships.' : mode === 'files' ? 'One file or several source files.' : 'One ZIP. A clearer picture of your code.'}</span><span className="btn">{mode === 'folder' ? 'Choose folder' : mode === 'files' ? 'Choose source files' : 'Choose ZIP archive'}<Ic d={I.up} s={16} /></span>
      {mode === 'folder' ? <input key="folder" ref={folderInput} type="file" multiple className="file-input" aria-label="Choose project folder" disabled={loading || disabled} onChange={e => { if (e.target.files?.length) choose(Array.from(e.target.files)); e.target.value = ''; }} /> : <input key="files" ref={fileInput} type="file" accept={mode === 'zip' ? '.zip' : '.py,.js,.html,.htm,.css'} multiple={mode === 'files'} className="file-input" aria-label={mode === 'zip' ? 'Choose project ZIP archive' : 'Choose source files'} disabled={loading || disabled} onChange={e => { if (e.target.files?.length) choose(Array.from(e.target.files)); e.target.value = ''; }} />}
    </label>}
    {excluded > 0 && <p className="helper">{excluded} dependency, generated, or environment files excluded before upload.</p>}
    {error && <ErrBanner title="Couldn't select this project" text={error} />}
    <div className="lbl">Upload limits</div><Limits /><div className="lbl">Supported languages</div><div className="row">{["HTML", "CSS", "JavaScript", "Python"].map(l => <Pill key={l}>{l}</Pill>)}</div><div style={{ color: "var(--muted)", fontSize: 14 }}>Other files are skipped.</div>
    <div className="lbl">Your learning pace</div><Seg<Difficulty> value={d} options={DIFFS} onChange={setD} /><p className="helper">{d === "beginner" ? "Start with the basics, with a little more guidance." : d === "intermediate" ? "Connect the concepts and take on trickier bugs." : "Less guidance. More room to work things out."}</p>
    {view === "error" && <ErrBanner title="Upload failed" text={errorMessage || "Check the upload limits above, then try again."} />}
    {loading ? <div role="status"><div className="bar" role="progressbar" aria-label="Analyzing locally" /><p className="helper">Indexing your project locally…</p></div> : <button className="btn pri lg" disabled={disabled || (mode === 'paste' ? !code.trim() : !files.length)} onClick={start}>Explore project <span aria-hidden="true">→</span></button>}<p className="helper">{disabled ? 'Wait for the current explanation to finish.' : 'Your source will be checked and indexed on this device.'}</p></section>
    <div className="welcome-column"><section className="card panel welcome-panel"><div className="companion"><div><span className="lbl">Meet your coding companion</span><h2>A little patience.<br /><em>A lot of progress.</em></h2><p>Big projects make more sense<br />one small step at a time.</p></div><AnimatedSloth /><span className="companion-caption"><span className="dot" />Ready when you are.</span></div><div className="workflow"><h3>From “what?” to “got it.”</h3><ol>{[["Explore", "Get your bearings in the project."], ["Understand", "Turn code into plain-language explanations."], ["Practice", "Work through a bug, with hints if you need them."], ["Reflect", "Learn from each fix and keep moving."]].map(([title, text], i) => <li key={title}><span className="workflow-number">0{i + 1}</span><div><b>{title}</b><span>{text}</span></div></li>)}</ol></div></section><Tip /></div></div>;
}

export function ExplorerScreen({ workspace: w, onChallenge }: { workspace: Workspace; onChallenge: () => void }) {
  const [focusedLines, setFocusedLines] = useState<[number, number] | null>(null);
  useEffect(() => setFocusedLines(null), [w.selected?.file_id]);
  const modelReady = w.health?.ai.status === 'ready';
  const canExplain = !!w.project && modelReady && !w.busy.analysis && !w.busy.source;
  const tree = (nodes: TreeNode[]): React.ReactNode => <ul className="file-tree">{nodes.map(n => <li key={n.path}>{n.type === 'directory' ? <details open><summary>{n.name}</summary>{tree(n.children)}</details> : <button className={'file-choice' + (n.file_id === w.selected?.file_id ? ' selected' : '')} onClick={() => { const file = w.project?.files.find(f => f.file_id === n.file_id); if (file) void w.selectFile(file); }}><Ic d={I.file} s={15} /><span>{n.name}</span><small>{n.status}</small></button>}</li>)}</ul>;
  return <div className="grid explorer-grid">
    <section className="card panel project-panel"><h2 className="ttl">Project files</h2>{w.project ? <><p className="helper">{w.project.name} · {w.project.analyzed_files} analyzed · {w.project.partial_files} partial · {w.project.skipped_files} skipped</p>{tree(w.project.file_tree)}{w.project.warnings.length > 0 && <details><summary>Upload notes ({w.project.warnings.length})</summary><ul>{w.project.warnings.map((note,i) => <li key={i} className="helper">{note}</li>)}</ul></details>}</> : <Empty mascot="project" title="No project loaded" text="Upload a ZIP from Workspace to see its files." size={4} />}</section>
    <section className="card panel source-panel" aria-busy={!!w.busy.source}><div className="row"><h2 className="ttl" style={{flex:1}}>Code</h2><button className="btn" disabled={!canExplain || !w.selected || w.selected.status === 'skipped'} onClick={() => void w.explain('file')}>Explain file</button><button className="btn" disabled={!canExplain || !w.range} onClick={() => void w.explain('block')}>Explain selection</button></div>
      {w.selected && <p className="helper">{w.selected.path}{focusedLines && ` · Referenced lines ${focusedLines[0]}–${focusedLines[1]}`}</p>}
      {w.errors.source && <ErrBanner title="Couldn't load this file" text={w.errors.source} action="Retry" onAction={() => w.selected && void w.selectFile(w.selected)} />}
      {w.busy.source ? <Lines n={6} /> : w.selected?.status === 'skipped' ? <Empty title="File skipped" text={w.selected.reason || 'Unsupported source file.'} /> : w.selected ? <><CodeEditor key={w.selected.file_id} value={w.code} language={w.selected.language || 'python'} readOnly onSelection={w.setRange} highlight={focusedLines} /><p className="helper">{w.range ? `Selected lines ${w.range.start_line}–${w.range.end_line}` : 'Highlight code in the editor to explain selected lines.'}</p>{w.selected.reason && <p className="helper">{w.selected.reason}</p>}</> : <Empty mascot="empty" title="No file selected" text="Choose a file in the tree to read it here." />}
    </section>
    <section className="card panel explanation-panel" aria-busy={!!w.busy.analysis}><h2 className="ttl">Your code, explained</h2><div className="seg">{(['project','file','block'] as const).map(scope => <button key={scope} className={w.scope === scope ? 'a' : ''} disabled={!!w.busy.analysis} onClick={() => w.changeScope(scope)}>{scope === 'block' ? 'Selection' : scope[0].toUpperCase()+scope.slice(1)}</button>)}</div><Seg<Difficulty> value={w.difficulty} options={DIFFS} onChange={w.changeDifficulty} />
      {!modelReady && <ErrBanner title={w.health ? 'Model unavailable' : 'Backend unavailable'} text={w.healthError || 'Explanations need the local model. Browsing and exercises remain available when the backend is running.'} action="Check again" onAction={() => void w.checkHealth()} />}
      {w.errors.analysis && <ErrBanner title="Explanation failed" text={w.errors.analysis} action="Retry" onAction={() => void w.explain()} />}
      <button className="btn" disabled={!canExplain || (w.scope !== 'project' && (!w.selected || w.selected.status === 'skipped')) || (w.scope === 'block' && !w.range)} onClick={() => void w.explain()}>{w.busy.analysis ? 'Explaining locally…' : `Explain ${w.scope === 'block' ? 'selection' : w.scope}`}</button>
      {w.busy.analysis ? <div role="status"><Lines n={5} /><p className="helper">The local model is working. This can take a little time.</p></div> : w.explanation ? <><p>{w.explanation.summary}</p><div className="row">{w.explanation.concepts.map(c => <Pill key={c}>{c}</Pill>)}</div>{w.explanation.sections.map((section,i) => <section key={i}><b>{section.title}</b><p>{section.explanation}</p><button className="btn source-reference" onClick={async () => { const file = w.project?.files.find(f => f.file_id === section.file_id); if (file) { if (file.file_id !== w.selected?.file_id) await w.selectFile(file, w.project, true); setFocusedLines([section.start_line,section.end_line]); } }}>{w.project?.files.find(f => f.file_id === section.file_id)?.path || 'Source'} · lines {section.start_line}–{section.end_line}</button></section>)}{w.explanation.limitations.length > 0 && <><div className="lbl">Analysis limits</div><ul>{w.explanation.limitations.map((limit,i) => <li className="helper" key={i}>{limit}</li>)}</ul></>}</> : !w.errors.analysis && <Empty mascot="explanation" title="Nothing explained yet" text="Choose Project, File or Selection and request an explanation." />}
      {w.project && <details><summary>Static file relationships</summary><ul>{w.project.relationships.map((r,i) => <li className="helper" key={i}>{r.source} → {r.target} ({r.resolved ? r.kind : 'unresolved'})</li>)}</ul></details>}
      <button className="btn pri lg" disabled={!w.health || !!w.busy.challenge} onClick={onChallenge}>Start debugging challenge</button><Tip />
    </section></div>;
}

export function DebugScreen({ workspace: w }: { workspace: Workspace }) {
  const locked = !!(w.busy.challenge || w.busy.submit || w.busy.hint || w.busy.solution);
  return <div className="grid"><section className="card panel" aria-busy={!!w.busy.challenge}><h2 className="ttl">Challenge</h2><div className="lbl">Difficulty</div><Seg<Difficulty> value={w.challengeDifficulty} options={DIFFS} onChange={d => { if (!locked) void w.loadChallenge(d); }} />
    {!w.project && <label className="helper">Language <select aria-label="Exercise language" value={w.challengeLanguage} disabled={locked} onChange={e => void w.loadChallenge(w.challengeDifficulty,e.target.value as Language)}>{['python','javascript','html','css'].map(l => <option key={l}>{l}</option>)}</select></label>}
    <button className="btn" disabled={!w.health || locked} onClick={() => void w.loadChallenge()}>Load challenge</button>
    {w.errors.challenge && <ErrBanner title="Couldn't load an exercise" text={w.errors.challenge} />}
    {w.busy.challenge ? <Lines n={4} /> : w.exercise ? <><h3>{w.exercise.title}</h3><div className="row"><Pill>{w.general ? 'General exercise' : 'Matched to your project'}</Pill><Pill>{w.exercise.language}</Pill></div><p>{w.exercise.objective}</p><p className="helper">{w.exercise.verification_policy}</p></> : <Empty mascot="challenge" title="Your next challenge awaits" text="Load an exercise to practice. A local AI model is not required." size={5} />}
    {w.exercise && <><div className="lbl">Hints</div>{Array.from({length:w.exercise.hint_count},(_,i) => <div className="hint" key={i}><Ic d={i < w.hints.length ? I.check : I.lock} s={16} /><span>Hint {i+1}: {w.hints[i] || 'Locked'}</span></div>)}<button className="btn" disabled={locked || w.hints.length >= w.exercise.hint_count} onClick={() => void w.revealHint()}>Reveal next hint</button></>}
    {w.errors.hint && <ErrBanner title="Hint unavailable" text={w.errors.hint} />}</section>
    <section className="card panel"><h2 className="ttl">Your fix</h2>{w.exercise ? <CodeEditor key={w.exercise.id} value={w.answer} language={w.exercise.language} onChange={w.editAnswer} readOnly={!!w.busy.submit} /> : <Empty title="No exercise selected" text="Load a challenge to start editing." />}
      {w.errors.submit && <ErrBanner title="Check failed" text={w.errors.submit} />}
      {w.result && <div className="err verification" role="status" style={{borderColor:w.result.correct ? 'var(--success)' : 'var(--warning)'}}><div><b>{w.result.correct ? 'Passed' : 'Not met'}</b><p>{w.result.feedback}</p>{w.result.limitations.map((l,i) => <p className="helper" key={i}>{l}</p>)}</div></div>}
      <button className="btn pri" disabled={!w.exercise || locked || !w.answer.trim()} onClick={() => void w.submit()}>{w.busy.submit ? 'Checking…' : 'Check my solution'}</button>
      {w.exercise && <button className="btn" disabled={locked || !!w.solution} onClick={() => void w.revealSolution()}>Reveal solution</button>}
      {w.errors.solution && <ErrBanner title="Solution unavailable" text={w.errors.solution} />}
      {w.solution && <section><h3>Reviewed solution</h3><CodeEditor value={w.solution} language={w.exercise?.language} readOnly /><p className="helper">Use this to understand the correction. Your own answer is unchanged.</p></section>}
    </section></div>;
}

export function SetupDrawer({ workspace: w, onClose }: { workspace: Workspace; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const drawer = dialog.current; const previous = document.activeElement as HTMLElement; drawer?.showModal(); return () => { drawer?.close(); previous?.focus(); }; }, []);
  return <dialog ref={dialog} className="drawer" aria-label="Setup" onCancel={onClose}><div className="row"><h2 className="ttl" style={{flex:1}}>Setup</h2><button className="btn" onClick={onClose}>Close</button></div>
    <div className="hint">Backend <span>{w.health?.backend || 'Unreachable'}</span></div><div className="hint">Local model <span>{w.health?.ai.status || 'Not checked'}</span></div><div className="hint">Model name <span>{w.health?.ai.model || 'Not available'}</span></div><div className="hint">Session storage <span>{w.health?.storage || 'Not checked'}</span></div>
    <button className="btn" disabled={w.checking} onClick={() => void w.checkHealth()}>{w.checking ? 'Checking…' : 'Check connection'}</button>
    {w.healthError && <ErrBanner title="Backend unreachable" text={w.healthError} />}{w.health && w.health.ai.status !== 'ready' && <p className="helper">Start Ollama with the installed local model to enable explanations. Projects and exercises work independently.</p>}
    <div className="lbl">Offline readiness</div><p className="helper">Fonts and application assets are bundled locally. A full offline restart still needs checking on your demo laptop.</p><Tip /></dialog>;
}
