import { useEffect, useRef, useState } from "react";
import { CodeEditor } from "./Code";
import { AnimatedSloth, Empty, ErrBanner, Ic, I, Limits, Pill, Seg, Sk, Tip } from "./ui";
import { uploadError } from "./upload";
import type { Challenge, Difficulty, ExplanationData, FileNode, SubmitResult, ViewState } from "./types";
const DIFFS = [{ id: "beginner", label: "Beginner" }, { id: "intermediate", label: "Intermediate" }, { id: "experienced", label: "Experienced" }] as { id: Difficulty; label: string }[];
const Lines = ({ n = 5 }: { n?: number }) => <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>{["90%", "60%", "75%", "45%", "80%", "55%"].slice(0, n).map((w, i) => <Sk key={i} w={w} />)}</div>;

export function UploadScreen({ view, onStart }: { view: ViewState; onStart: () => void }) {
  const [file, setFile] = useState<File | null>(null); const [d, setD] = useState<Difficulty>("beginner");
  const [error, setError] = useState<string | null>(null); const [dragging, setDragging] = useState(false);
  const choose = (files: File[]) => { const message = uploadError(files); setError(message); setFile(message ? null : files[0]); };
  return <div className="grid upload-grid"><section className="card panel upload-panel" aria-busy={view === "loading"}><div className="panel-heading"><div><span className="lbl">01 / Your workspace</span><h2 className="ttl">Bring your code.</h2></div><Ic d={I.file} s={24} /></div>
    <label className={"drop" + (dragging ? " dragging" : "") + (file ? " selected" : "")} onDragOver={e => { e.preventDefault(); if (view !== "loading") setDragging(true); }} onDragLeave={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false); }} onDrop={e => { e.preventDefault(); setDragging(false); if (view !== "loading") choose(Array.from(e.dataTransfer.files)); }}>
      <span className="upload-icon"><Ic d={file ? I.check : I.up} s={28} /></span><b>{file ? file.name : "Drop your project here"}</b><span>{file ? (file.size / 1024).toFixed(0) + " KB · ZIP archive selected" : "One ZIP. A clearer picture of your code."}</span><span className="btn">{file ? "Choose another ZIP" : "Choose ZIP archive"}<Ic d={I.up} s={16} /></span><input type="file" accept=".zip" className="file-input" aria-label="Choose project ZIP archive" disabled={view === "loading"} onChange={e => { if (e.target.files?.length) choose(Array.from(e.target.files)); e.target.value = ""; }} /></label>
    {error && <ErrBanner title="Couldn't select this project" text={error} />}
    <div className="lbl">Upload limits</div><Limits /><div className="lbl">Supported languages</div><div className="row">{["HTML", "CSS", "JavaScript", "Python"].map(l => <Pill key={l}>{l}</Pill>)}</div><div style={{ color: "var(--muted)", fontSize: 14 }}>Other files are skipped.</div>
    <div className="lbl">Your learning pace</div><Seg<Difficulty> value={d} options={DIFFS} onChange={setD} /><p className="helper">{d === "beginner" ? "Start with the basics, with a little more guidance." : d === "intermediate" ? "Connect the concepts and take on trickier bugs." : "Less guidance. More room to work things out."}</p>
    {view === "error" && <ErrBanner title="Upload failed" text="Check the upload limits above, then choose another ZIP archive." />}
    {view === "loading" ? <div role="status"><div className="bar" role="progressbar" aria-label="Analyzing locally" /><p className="helper">Previewing local analysis… Take it one step at a time.</p></div> : <button className="btn pri lg" disabled={!file} onClick={onStart}>Explore project preview <span aria-hidden="true">→</span></button>}<p className="helper">{file ? "Archive selected. This UI preview doesn't analyze its contents yet." : "Choose a ZIP to open the project preview."}</p></section>
    <div className="welcome-column"><section className="card panel welcome-panel"><div className="companion"><div><span className="lbl">Meet your coding companion</span><h2>A little patience.<br /><em>A lot of progress.</em></h2><p>Big projects make more sense<br />one small step at a time.</p></div><AnimatedSloth /><span className="companion-caption"><span className="dot" />Ready when you are.</span></div><div className="workflow"><h3>From “what?” to “got it.”</h3><ol>{[["Explore", "Get your bearings in the project."], ["Understand", "Turn code into plain-language explanations."], ["Practice", "Work through a bug, with hints if you need them."], ["Reflect", "Learn from each fix and keep moving."]].map(([title, text], i) => <li key={title}><span className="workflow-number">0{i + 1}</span><div><b>{title}</b><span>{text}</span></div></li>)}</ol></div></section><Tip /></div></div>;
}

export function ExplorerScreen({ view, files = [], code, explanation, onChallenge, onRetry }: { view: ViewState; files?: FileNode[]; code?: string; explanation?: ExplanationData; onChallenge: () => void; onRetry?: () => void }) {
  const load = view === "loading"; const tabs = ["Project", "File", "Selection"];
  const row = (f: FileNode): any => <li key={f.path}><Ic d={I.file} s={16} />{f.path}<span className="pill" style={{ height: 24, marginLeft: "auto" }}>{f.status}</span></li>;
  return <div className="grid" style={{ alignItems: "stretch" }}>
    <section className="card panel" style={{ flex: "0 0 270px" }} aria-busy={load}><div className="ttl">Project files</div>{load ? <Lines n={6} /> : files.length ? <ul className="tree">{files.map(row)}</ul> : <Empty mascot="project" title="No project loaded" text="Upload a ZIP to see its files." size={4} />}</section>
    <section className="card panel" style={{ flex: 1.4 }} aria-busy={load}><div className="row"><div className="ttl" style={{ flex: 1 }}>Code</div><button className="btn" disabled={!code}>Explain file</button><button className="btn" disabled={!code}>Explain selection</button></div>{load ? <div className="cm-host" style={{ padding: 20 }}><Lines n={6} /></div> : code ? <CodeEditor value={code} readOnly /> : <Empty title="No file selected" text="Choose a file in the tree to read it here." />}</section>
    <section className="card panel" style={{ flex: 1.2 }} aria-busy={load}><div className="ttl">Your code, explained</div><div className="seg">{tabs.map((t, i) => <button key={t} className={explanation?.level === t.toLowerCase() || (!explanation && i === 0) ? "a" : ""}>{t}</button>)}</div>
      {view === "error" && <ErrBanner title="Model unavailable" text="Explanations are paused. You can still browse files and try exercises." action="Retry" onAction={onRetry} />}
      {load ? <><Sk h={44} /><Lines n={4} /></> : explanation ? <><p>{explanation.summary}</p><div className="lbl">Key concepts</div><div className="row">{explanation.concepts.map(c => <Pill key={c}>{c}</Pill>)}</div><div className="lbl">How it works</div><ol>{explanation.steps.map((s, i) => <li key={i}>{s.text}</li>)}</ol>{explanation.limits.length > 0 && <div className="lbl">Analysis limits</div>}</> : view !== "error" && <Empty title="Nothing to explain yet" text="Select a file or a code range, then choose an Explain action." />}
      <button className="btn pri lg" onClick={onChallenge}>Start debugging challenge</button><Tip /></section></div>;
}

export function DebugScreen({ view, challenge, result }: { view: ViewState; challenge?: Challenge; result?: SubmitResult }) {
  const [d, setD] = useState<Difficulty>("beginner"); const [shown, setShown] = useState(0); const load = view === "loading";
  const tone = { passed: ["Passed", "var(--success)"], partial: ["Partially met", "var(--warning)"], not_met: ["Not met", "var(--error)"] };
  return <div className="grid"><section className="card panel" aria-busy={load}><div className="ttl">Challenge</div><div className="lbl">Difficulty</div><Seg<Difficulty> value={d} options={DIFFS} onChange={setD} />
    {load ? <Lines n={4} /> : challenge ? <><div className="row"><Pill>{challenge.is_general ? "General exercise" : "Matched to a concept in your project"}</Pill><Pill>{challenge.language}</Pill></div><p>{challenge.instructions}</p></> : <Empty mascot="challenge" title="Your next challenge awaits" text="Project exercises will appear here when the analysis runtime is connected." size={5} />}
    {view === "error" && <ErrBanner title="No verified exercise available" text="Try the general exercise instead." />}
    <div className="lbl">Hints</div>{[0, 1, 2].map(i => <div className="hint" key={i}><Ic d={I.lock} s={16} />Hint {i + 1}<span style={{ marginLeft: "auto" }}>{i < shown ? (challenge?.hint_levels[i] ?? "Unlocked") : "Locked"}</span></div>)}</section>
    <section className="card panel"><div className="ttl">Your fix</div>{load ? <div className="cm-host" style={{ padding: 20 }}><Lines n={6} /></div> : <CodeEditor value={challenge?.starter_code ?? ""} language={challenge?.language} />}
      {result && <div className="err" role="status" style={{ borderColor: tone[result.overall][1] }}><b>{tone[result.overall][0]}</b></div>}
      {result && <ul style={{ margin: 0, paddingLeft: 18 }}>{result.criteria.map(c => <li key={c.label}>{c.met ? "Met" : "Not met"}: {c.label}</li>)}</ul>}
      {result && <p style={{ color: "var(--muted)", fontSize: 13 }}>This check covers the exercise's defined criteria. It does not prove the whole program is correct.</p>}
      <div className="row" style={{ justifyContent: "space-between" }}><button className="btn" disabled={!challenge || load || shown >= Math.min(3, challenge.hint_levels.length)} onClick={() => setShown(shown + 1)}>Reveal next hint</button><button className="btn pri" disabled title="Solution checking requires a connected analysis runtime">Check my solution</button></div><p className="helper">Solution checking is available when the analysis runtime is connected.</p></section></div>;
}

export function SetupDrawer({ view, onClose }: { view: ViewState; onClose: () => void }) {
  const rows = ["Model status", "Runtime health", "Storage and memory"];
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const drawer = dialog.current; const previous = document.activeElement as HTMLElement;
    drawer?.showModal();
    return () => { drawer?.close(); previous?.focus(); };
  }, []);
  return <dialog ref={dialog} className="drawer" aria-label="Setup" onCancel={onClose}><div className="row"><h2 className="ttl" style={{ flex: 1 }}>Setup</h2><button className="btn" onClick={onClose}>Close</button></div>
    {rows.map(r => <div key={r} className="hint">{r}{view === "loading" ? <span style={{ marginLeft: "auto", width: 80 }}><Sk /></span> : <span style={{ marginLeft: "auto" }}>Not checked</span>}</div>)}
    <div className="lbl">Offline readiness</div>{["Model weights present", "Fonts and assets local", "Restart works offline"].map(r => <div key={r} className="hint">{r}<span style={{ marginLeft: "auto" }}>Not checked</span></div>)}
    {view === "error" && <ErrBanner title="Runtime unreachable" text="Start the local model runtime, then check again." />}<Tip /></dialog>;
}
