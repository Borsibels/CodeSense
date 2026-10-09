import { ReactNode } from "react";
import { LIMITS } from "./types";
export const Ic = ({ d, s = 20 }: { d: string; s?: number }) => <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="square" strokeLinejoin="miter" aria-hidden="true"><path d={d} /></svg>;
export const I = { home: "M3 11l9-8 9 8v10H3z", book: "M5 3h14v18H5zM9 8h6M9 12h6M9 16h4", clock: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 7v5l3 3", up: "M12 17V4M6 10l6-6 6 6M4 21h16", lock: "M4 11h16v10H4zM8 11V7h8v4", file: "M6 3h8l4 4v14H6z", check: "M4 12l5 5L20 6", warn: "M12 3l10 18H2zM12 10v5M12 18h.01", gear: "M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v3M12 19v3M2 12h3M19 12h3", moon: "M20 14A8 8 0 0110 4a8 8 0 1010 10z" };
const G = ["...FFFFFFFF...", "..FFFFFFFFFF..", ".FFCCCCCCCCFF.", ".FCDDCCCCDDCF.", ".FCDWCCCCWDCF.", ".FCCCCNNCCCCF.", ".FCCCCCCCCCCF.", "..FCCCDDCCCF..", "..FFCCCCCCFF..", "...FFFFFFFF..."];
const C: Record<string, string> = { F: "#89675A", C: "#F0DCBB", D: "#14131F", W: "#FFFFFF", N: "#3B2A2A" };
export const Sloth = ({ size = 6 }: { size?: number }) => <svg width={14 * size} height={10 * size} shapeRendering="crispEdges" aria-hidden="true">{G.flatMap((r, y) => [...r].map((c, x) => c === "." ? null : <rect key={x + "-" + y} x={x * size} y={y * size} width={size} height={size} fill={C[c]} />))}</svg>;
export const Sk = ({ w = "100%", h = 12 }: { w?: string; h?: number }) => <div className="sk" style={{ width: w, height: h }} />;
export const Pill = ({ children }: { children: ReactNode }) => <span className="pill">{children}</span>;
export const Empty = ({ title, text, size = 6 }: { title: string; text: string; size?: number }) => <div className="empty"><Sloth size={size} /><b>{title}</b>{text}</div>;
export const ErrBanner = ({ title, text, action, onAction }: { title: string; text: string; action?: string; onAction?: () => void }) => <div className="err" role="alert"><span style={{ color: "var(--error)" }}><Ic d={I.warn} /></span><div style={{ flex: 1 }}><b>{title}</b><span>{text}</span></div>{action && <button className="btn" onClick={onAction}>{action}</button>}</div>;
export function Seg<T extends string>({ value, options, onChange }: { value: T; options: { id: T; label: string }[]; onChange: (v: T) => void }) { return <div className="seg" role="radiogroup">{options.map(o => <button key={o.id} role="radio" aria-checked={value === o.id} className={value === o.id ? "a" : ""} onClick={() => onChange(o.id)}>{o.label}</button>)}</div>; }
export const Tip = () => <div className="card tip"><Sloth size={4} /><div><b>One step at a time.</b><span>Read the explanation first, then try the challenge on your own.</span></div></div>;
export const Limits = () => <ul style={{ margin: 0, paddingLeft: 18, color: "var(--muted)", fontSize: 14, lineHeight: 1.8 }}><li>ZIP archive only</li><li>Up to {LIMITS.files} source files</li><li>{LIMITS.totalMB} MB combined source text</li><li>{LIMITS.perFileKB} KB per file</li></ul>;
export function Stepper({ active, analyzing }: { active: number; analyzing?: boolean }) {
  const s = ["Upload", "Explain", "Debug", "Verify", "Learn"];
  return <nav className="card steps" aria-label="Progress">{s.map((n, i) => <><div key={n} className={"st" + (i === active ? " on" : "")} aria-current={i === active ? "step" : undefined}><span className="n">{i + 1}</span>{n}{analyzing && i === 0 && <span className="pill">Analyzing</span>}</div>{i < 4 && <div className="ln" />}</>)}</nav>;
}
type Nav = "workspace" | "practice" | "session";
export function Shell({ nav, onNav, title, sub, step, analyzing, model, onSetup, onTheme, children }: { nav: Nav; onNav: (n: Nav) => void; title: string; sub: string; step: number; analyzing?: boolean; model: "ready" | "unavailable"; onSetup: () => void; onTheme: () => void; children: ReactNode }) {
  const items: [Nav, string, string][] = [["workspace", "Workspace", I.home], ["practice", "Practice", I.book], ["session", "Session", I.clock]];
  return <div className="app"><aside className="side"><div className="brand"><svg width="28" height="28" viewBox="0 0 28 28" fill="none" stroke="#C48A5A" strokeWidth="2.5"><rect x="2" y="2" width="24" height="24" /><path d="M11 10l-4 4 4 4M17 10l4 4-4 4" /></svg><span className="t">CodeSense</span></div>
    {items.map(([id, l, d]) => <button key={id} className={"nav" + (nav === id ? " on" : "")} onClick={() => onNav(id)} aria-label={l}><Ic d={d} /><span className="t">{l}</span></button>)}
    <div style={{ flex: 1 }} /><button className="nav" onClick={onSetup} aria-label="Setup"><Ic d={I.gear} /><span className="t">Setup</span></button><button className="nav" onClick={onTheme} aria-label="Theme"><Ic d={I.moon} /><span className="t">Theme</span></button></aside>
    <main className="col"><header className="hd"><div><h1>{title}<i>.</i></h1><p>{sub}</p></div><div className="row"><Pill><span className="dot" />Offline</Pill><Pill><span style={{ color: model === "ready" ? "var(--success)" : "var(--warning)" }}><Ic d={model === "ready" ? I.check : I.warn} s={16} /></span>{model === "ready" ? "Model ready" : "Model unavailable"}</Pill></div></header>
      <Stepper active={step} analyzing={analyzing} />{children}<div className="foot"><Ic d={I.lock} s={15} />Your code stays on this device.</div></main></div>;
}
