import { Fragment, ReactNode, useEffect, useRef, useState } from "react";
import { LIMITS } from "./types";
export const Ic = ({ d, s = 20 }: { d: string; s?: number }) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="square" strokeLinejoin="miter" aria-hidden="true"><path d={d} /></svg>;
export const I = { home: "M3 11l9-8 9 8v10H3z", book: "M5 3h14v18H5zM9 8h6M9 12h6M9 16h4", clock: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 7v5l3 3", up: "M12 17V4M6 10l6-6 6 6M4 21h16", lock: "M4 11h16v10H4zM8 11V7h8v4", file: "M6 3h8l4 4v14H6z", check: "M4 12l5 5L20 6", warn: "M12 3l10 18H2zM12 10v5M12 18h.01", gear: "M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v3M12 19v3M2 12h3M19 12h3", moon: "M20 14A8 8 0 0110 4a8 8 0 1010 10z" };
const MASCOTS = {
  default: ["/sloth-mascot.png", "CodeSense sloth mascot wearing headphones and coding on a laptop"],
  challenge: ["/sloth-mascot-challenge.png", "CodeSense challenge mascot wearing a headband and holding a sword and shield"],
  project: ["/sloth-mascot-project.png", "CodeSense project mascot holding a folder of source files"],
} as const;
export const Sloth = ({ size = 6, mascot = "default" }: { size?: number; mascot?: keyof typeof MASCOTS }) => <span className="mascot" style={{ width: size * 24 }}><img src={MASCOTS[mascot][0]} alt={MASCOTS[mascot][1]} /></span>;
export function AnimatedSloth() {
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [failed, setFailed] = useState(false);
  const [paused, setPaused] = useState(true);
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(preference.matches);
    preference.addEventListener("change", update);
    return () => preference.removeEventListener("change", update);
  }, []);
  if (reducedMotion || failed) return <Sloth size={10} />;
  const toggle = () => {
    if (video.current?.paused) video.current.play().catch(() => setPaused(true));
    else video.current?.pause();
  };
  return <span className="mascot mascot-player"><video ref={video} src="/sloth-mascot.webm" poster="/sloth-mascot.png" autoPlay muted loop playsInline aria-label="Animated CodeSense sloth mascot" onPlay={() => setPaused(false)} onPause={() => setPaused(true)} onError={() => setFailed(true)} /><button className="mascot-control" onClick={toggle} aria-label={paused ? "Play mascot animation" : "Pause mascot animation"}>{paused ? "Play" : "Pause"}</button></span>;
}
export const Sk = ({ w = "100%", h = 12 }: { w?: string; h?: number }) => <div className="sk" style={{ width: w, height: h }} />;
export const Pill = ({ children }: { children: ReactNode }) => <span className="pill">{children}</span>;
export const Empty = ({ title, text, size = 6, mascot = "default" }: { title: string; text: string; size?: number; mascot?: keyof typeof MASCOTS }) => <div className="empty"><Sloth size={size} mascot={mascot} /><b>{title}</b>{text}</div>;
export const ErrBanner = ({ title, text, action, onAction }: { title: string; text: string; action?: string; onAction?: () => void }) => <div className="err" role="alert"><span style={{ color: "var(--error)" }}><Ic d={I.warn} /></span><div style={{ flex: 1 }}><b>{title}</b><span>{text}</span></div>{action && <button className="btn" disabled={!onAction} onClick={onAction}>{action}</button>}</div>;
export function Seg<T extends string>({ value, options, onChange }: { value: T; options: { id: T; label: string }[]; onChange: (v: T) => void }) { return <div className="seg" role="group" aria-label="Difficulty">{options.map(o => <button key={o.id} aria-pressed={value === o.id} className={value === o.id ? "a" : ""} onClick={() => onChange(o.id)}>{o.label}</button>)}</div>; }
export const Tip = () => <div className="card tip"><Sloth size={4} /><div><b>One step at a time.</b><span>Read the explanation first, then try the challenge on your own.</span></div></div>;
export const Limits = () => <ul style={{ margin: 0, paddingLeft: 18, color: "var(--muted)", fontSize: 14, lineHeight: 1.8 }}><li>ZIP, files, folder, or pasted code</li><li>10 MB maximum upload</li><li>Up to {LIMITS.files} source files</li><li>{LIMITS.totalMB} MB combined source text</li><li>{LIMITS.perFileKB} KB per file</li></ul>;
export function Stepper({ active, analyzing }: { active: number; analyzing?: boolean }) {
  const s = ["Upload", "Explain", "Debug", "Verify", "Learn"];
  return <nav className="card steps" aria-label="Progress">{s.map((n, i) => <Fragment key={n}><div className={"st" + (i === active ? " on" : "")} aria-current={i === active ? "step" : undefined}><span className="n">{i + 1}</span><span className="step-label">{n}</span>{analyzing && i === 0 && <span className="pill">Analyzing</span>}</div>{i < 4 && <div className="ln" />}</Fragment>)}</nav>;
}
export function Shell({ title, sub, step, analyzing, model, connected, onSetup, onTheme, children }: { title: string; sub: string; step: number; analyzing?: boolean; model: "ready" | "unavailable" | "checking"; connected: boolean; onSetup: () => void; onTheme: () => void; children: ReactNode }) {
  return <div className="app"><aside className="side"><div className="brand"><svg width="28" height="28" viewBox="0 0 28 28" fill="none" stroke="#C48A5A" strokeWidth="2.5"><rect x="2" y="2" width="24" height="24" /><path d="M11 10l-4 4 4 4M17 10l4 4-4 4" /></svg><span className="t">CodeSense</span></div>
    <div className="nav on"><Ic d={I.home} /><span className="t">Workspace</span></div>
    <div className="side-spacer" /><div className="side-note"><span className="lbl">Small steps. Better code.</span><span>A space to learn at your pace.</span></div><button className="nav" onClick={onSetup} aria-label="Setup" title="Setup"><Ic d={I.gear} /><span className="t">Setup</span></button><button className="nav" onClick={onTheme} aria-label="Theme" title="Switch theme"><Ic d={I.moon} /><span className="t">Theme</span></button></aside>
    <main className="col"><header className="hd"><div><h1>{title}<i>.</i></h1><p>{sub}</p></div><div className="row"><Pill><span className="dot" />Local workspace</Pill><Pill><span style={{ color: model === "ready" ? "var(--success)" : "var(--warning)" }}><Ic d={model === "ready" ? I.gear : I.warn} s={16} /></span>{model === "ready" ? "Model ready" : model === 'checking' ? 'Checking runtime' : !connected ? 'Backend unavailable' : "Model unavailable"}</Pill></div></header>
      <Stepper active={step} analyzing={analyzing} /><div className="screen">{children}</div><div className="foot"><Ic d={I.lock} s={15} />Your code stays on this device.<span className="prototype-note">{connected ? 'Connected to local backend' : 'Waiting for local backend'}</span></div></main></div>;
}
