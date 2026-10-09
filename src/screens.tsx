import { useState } from "react";
import { CodeEditor } from "./Code";
import { Empty, ErrBanner, Ic, I, Limits, Pill, Seg, Sk, Tip } from "./ui";
import type { Challenge, Difficulty, ExplanationData, FileNode, SubmitResult, ViewState } from "./types";
const DIFFS = [{ id: "beginner", label: "Beginner" }, { id: "intermediate", label: "Intermediate" }, { id: "experienced", label: "Experienced" }] as { id: Difficulty; label: string }[];
const Lines = ({ n = 5 }: { n?: number }) => <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>{["90%", "60%", "75%", "45%", "80%", "55%"].slice(0, n).map((w, i) => <Sk key={i} w={w} />)}</div>;

export function UploadScreen({ view, onStart }: { view: ViewState; onStart: () => void }) {
  const [file, setFile] = useState<File | null>(null); const [d, setD] = useState<Difficulty>("beginner");
  return <div className="grid"><section className="card panel" aria-busy={view === "loading"}><div className="ttl">Upload</div>
    <label className="drop"><Ic d={I.up} s={28} /><b style={{ color: "var(--text)" }}>{file ? file.name : "Drop a project .zip here"}</b><span>{file ? (file.size / 1024).toFixed(0) + " KB" : "or choose a file"}</span><input type="file" accept=".zip" hidden onChange={e => setFile(e.target.files?.[0] ?? null)} /></label>
    <div className="lbl">Upload limits</div><Limits /><div className="lbl">Supported languages</div><div className="row">{["HTML", "CSS", "JavaScript", "Python"].map(l => <Pill key={l}>{l}</Pill>)}</div><div style={{ color: "var(--muted)", fontSize: 14 }}>Other files are skipped.</div>
    <div className="lbl">Difficulty</div><Seg value={d} options={DIFFS} onChange={setD} />
    {view === "error" && <ErrBanner title="Upload failed" text="Check the upload limits above, then choose another ZIP archive." />}
    {view === "loading" ? <div><div className="bar" role="progressbar" aria-label="Analyzing locally" /><p style={{ color: "var(--muted)", fontSize: 14 }}>Analyzing locally</p></div> : <button className="btn pri lg" disabled={!file} onClick={onStart}>Start project analysis</button>}</section>
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}><section className="card panel" style={{ flex: 1 }}><div className="ttl">What happens next</div><ol style={{ color: "var(--muted)", lineHeight: 2.2, paddingLeft: 20, margin: 0 }}><li>Analyze the structure</li><li>Explain the code</li><li>Try a debugging challenge</li><li>Learn from feedback</li></ol></section><Tip /></div></div>;
}

export function ExplorerScreen({ view, files = [], code, explanation, onChallenge, onRetry }: { view: ViewState; files?: FileNode[]; code?: string; explanation?: ExplanationData; onChallenge: () => void; onRetry?: () => void }) {
  const load = view === "loading"; const tabs = ["Project", "File", "Selection"];
  const row = (f: FileNode): any => <li key={f.path}><Ic d={I.file} s={16} />{f.path}<span className="pill" style={{ height: 24, marginLeft: "auto" }}>{f.status}</span></li>;
  return <div className="grid" style={{ alignItems: "stretch" }}>
    <section className="card panel" style={{ flex: "0 0 270px" }} aria-busy={load}><div className="ttl">Project files</div>{load ? <Lines n={6} /> : files.length ? <ul className="tree">{files.map(row)}</ul> : <Empty title="No project loaded" text="Upload a ZIP to see its files." size={4} />}</section>
    <section className="card panel" style={{ flex: 1.4 }} aria-busy={load}><div className="row"><div className="ttl" style={{ flex: 1 }}>Code</div><button className="btn" disabled={!code}>Explain file</button><button className="btn" disabled={!code}>Explain selection</button></div>{load ? <div className="cm-host" style={{ padding: 20 }}><Lines n={6} /></div> : code ? <CodeEditor value={code} readOnly /> : <Empty title="No file selected" text="Choose a file in the tree to read it here." />}</section>
    <section className="card panel" style={{ flex: 1.2 }} aria-busy={load}><div className="ttl">Your code, explained</div><div className="seg">{tabs.map((t, i) => <button key={t} className={explanation?.level === t.toLowerCase() || (!explanation && i === 0) ? "a" : ""}>{t}</button>)}</div>
      {view === "error" && <ErrBanner title="Model unavailable" text="Explanations are paused. You can still browse files and try exercises." action="Retry" onAction={onRetry} />}
      {load ? <><Sk h={44} /><Lines n={4} /></> : explanation ? <><p>{explanation.summary}</p><div className="lbl">Key concepts</div><div className="row">{explanation.concepts.map(c => <Pill key={c}>{c}</Pill>)}</div><div className="lbl">How it works</div><ol>{explanation.steps.map((s, i) => <li key={i}>{s.text}</li>)}</ol>{explanation.limits.length > 0 && <div className="lbl">Analysis limits</div>}</> : view !== "error" && <Empty title="Nothing to explain yet" text="Select a file or a code range, then choose an Explain action." />}
      <button className="btn pri lg" onClick={onChallenge}>Start debugging challenge</button><Tip /></section></div>;
}

export function DebugScreen({ view, challenge, result }: { view: ViewState; challenge?: Challenge; result?: SubmitResult }) {
  const [d, setD] = useState<Difficulty>("beginner"); const [shown, setShown] = useState(0); const [dirty, setDirty] = useState(false); const load = view === "loading";
  const tone = { passed: ["Passed", "var(--success)"], partial: ["Partially met", "var(--warning)"], not_met: ["Not met", "var(--error)"] };
  return <div className="grid"><section className="card panel" aria-busy={load}><div className="ttl">Challenge</div><div className="lbl">Difficulty</div><Seg value={d} options={DIFFS} onChange={setD} />
    {load ? <Lines n={4} /> : challenge ? <><div className="row"><Pill>{challenge.is_general ? "General exercise" : "Matched to a concept in your project"}</Pill><Pill>{challenge.language}</Pill></div><p>{challenge.instructions}</p></> : <Empty title="No challenge loaded" text="Choose a difficulty and start. The challenge description will appear here." size={5} />}
    {view === "error" && <ErrBanner title="No verified exercise available" text="Try the general exercise instead." />}
    <div className="lbl">Hints</div>{[0, 1, 2].map(i => <div className="hint" key={i}><Ic d={I.lock} s={16} />Hint {i + 1}<span style={{ marginLeft: "auto" }}>{i < shown ? (challenge?.hint_levels[i] ?? "Unlocked") : "Locked"}</span></div>)}</section>
    <section className="card panel"><div className="ttl">Your fix</div>{load ? <div className="cm-host" style={{ padding: 20 }}><Lines n={6} /></div> : <CodeEditor value={challenge?.starter_code ?? ""} language={challenge?.language} onChange={() => setDirty(true)} />}
      {result && <div className="err" role="status" style={{ borderColor: tone[result.overall][1] }}><b>{tone[result.overall][0]}</b></div>}
      {result && <ul style={{ margin: 0, paddingLeft: 18 }}>{result.criteria.map(c => <li key={c.label}>{c.met ? "Met" : "Not met"}: {c.label}</li>)}</ul>}
      {result && <p style={{ color: "var(--muted)", fontSize: 13 }}>This check covers the exercise's defined criteria. It does not prove the whole program is correct.</p>}
      <div className="row" style={{ justifyContent: "space-between" }}><button className="btn" disabled={shown >= 3} onClick={() => setShown(shown + 1)}>Reveal next hint</button><button className="btn pri" disabled={!dirty}>Check my solution</button></div></section></div>;
}

export function SetupDrawer({ view, onClose }: { view: ViewState; onClose: () => void }) {
  const rows = ["Model status", "Runtime health", "Storage and memory"];
  return <aside className="drawer" role="dialog" aria-label="Setup"><div className="row"><div className="ttl" style={{ flex: 1 }}>Setup</div><button className="btn" onClick={onClose}>Close</button></div>
    {rows.map(r => <div key={r} className="hint">{r}{view === "loading" ? <span style={{ marginLeft: "auto", width: 80 }}><Sk /></span> : <span style={{ marginLeft: "auto" }}>Not checked</span>}</div>)}
    <div className="lbl">Offline verified</div>{["Model weights present", "Fonts and assets local", "Restart works offline"].map(r => <div key={r} className="hint"><Ic d={I.check} s={16} />{r}<span style={{ marginLeft: "auto" }}>Not checked</span></div>)}
    {view === "error" && <ErrBanner title="Runtime unreachable" text="Start the local model runtime, then check again." />}</aside>;
}
