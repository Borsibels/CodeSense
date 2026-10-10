import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent, RefObject } from 'react';
import { AssistantPanel } from './assistant';
import { CodeEditor } from './Code';
import { rangeLabel } from './analysisText';
import { COMPACT_QUERY, DEFAULT_LAYOUT, LAYOUT_KEY, MIN_PX, columnTemplate, dragDivider, nudgeDivider, panelWidths, parseLayout, withWidths } from './layout';
import type { DividerId, Layout } from './layout';
import type { SourceFile, TreeNode } from './api';
import type { Language } from './types';
import { Empty, ErrBanner, I, Ic, Lines, Panel } from './ui';
import type { Workspace } from './useWorkspace';
import type { WorkspaceStage } from './App';

type View = 'files' | 'code' | 'assistant';
const VIEWS: { id: View; label: string }[] = [{ id: 'files', label: 'Files' }, { id: 'code', label: 'Code' }, { id: 'assistant', label: 'Ask Snip' }];
const LANGUAGE: Record<Language, string> = { python: 'Python', javascript: 'JavaScript', html: 'HTML', css: 'CSS' };
const STATUS: Record<string, string> = { analyzed: 'Analyzed', partial: 'Partial', skipped: 'Skipped' };

function useCompact() {
  const [compact, setCompact] = useState(() => window.matchMedia(COMPACT_QUERY).matches);
  useEffect(() => { const query = window.matchMedia(COMPACT_QUERY); const update = () => setCompact(query.matches); query.addEventListener('change', update); return () => query.removeEventListener('change', update); }, []);
  return compact;
}
function useWidth(ref: RefObject<HTMLElement>) {
  const [width, setWidth] = useState(0);
  useEffect(() => { const el = ref.current; if (!el) return; setWidth(el.getBoundingClientRect().width); const watch = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width)); watch.observe(el); return () => watch.disconnect(); }, [ref]);
  return width;
}
// Panel sizes are a per-viewer convenience: they are saved locally, and the workspace works if storage is unavailable.
function useLayout() {
  const [layout, setLayout] = useState<Layout>(() => { try { return parseLayout(window.localStorage.getItem(LAYOUT_KEY)); } catch { return DEFAULT_LAYOUT; } });
  useEffect(() => { const save = window.setTimeout(() => { try { window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* storage blocked */ } }, 200); return () => window.clearTimeout(save); }, [layout]);
  return [layout, setLayout] as const;
}

/** A draggable, keyboard-operable divider between two panels. Double-click or Enter restores the default sizes. */
function Divider({ label, now, min, max, onMove, onNudge, onReset }: { label: string; now: number; min: number; max: number; onMove: (x: number) => void; onNudge: (delta: number) => void; onReset: () => void }) {
  const [dragging, setDragging] = useState(false);
  const key = (e: KeyboardEvent) => {
    const step = e.shiftKey ? 64 : 16;
    if (e.key === 'ArrowLeft') onNudge(-step); else if (e.key === 'ArrowRight') onNudge(step); else if (e.key === 'Enter') onReset(); else return;
    e.preventDefault();
  };
  const down = (e: PointerEvent) => { e.currentTarget.setPointerCapture(e.pointerId); setDragging(true); };
  const up = (e: PointerEvent) => { e.currentTarget.releasePointerCapture(e.pointerId); setDragging(false); };
  return <div role="separator" aria-orientation="vertical" aria-label={label} aria-valuenow={Math.round(now)} aria-valuemin={Math.round(min)} aria-valuemax={Math.round(max)} tabIndex={0} className={'divider' + (dragging ? ' dragging' : '')}
    onPointerDown={down} onPointerMove={e => { if (dragging) onMove(e.clientX); }} onPointerUp={up} onPointerCancel={up} onKeyDown={key} onDoubleClick={onReset} />;
}

function ExplorerPanel({ w, practicing, hidden, onOpen, onRestart, onCollapse }: { w: Workspace; practicing: boolean; hidden: boolean; onOpen: (file: SourceFile) => void; onRestart: () => void; onCollapse?: () => void }) {
  const p = w.project;
  const locked = Object.values(w.busy).some(Boolean);
  const tree = (nodes: TreeNode[]): React.ReactNode => <ul className="file-tree">{nodes.map(n => <li key={n.path}>{n.type === 'directory'
    ? <details open><summary><Ic d={I.folder} s={14} />{n.name}</summary>{tree(n.children)}</details>
    : <button className={'file-choice' + (n.file_id === w.selected?.file_id ? ' selected' : '') + (n.status === 'skipped' ? ' skipped' : '')} aria-current={n.file_id === w.selected?.file_id ? 'true' : undefined} disabled={practicing || !!w.busy.challenge} title={practicing ? 'Leave Practice to open another file' : n.path}
        onClick={() => { const file = p?.files.find(f => f.file_id === n.file_id); if (file) onOpen(file); }}>
        <Ic d={I.file} s={14} /><span className="name">{n.name}</span><span className={'status ' + n.status} title={n.status ?? undefined}>{n.status === 'skipped' ? 'skipped' : <span className="sr-only">{n.status}</span>}</span></button>}</li>)}</ul>;
  return <Panel className={'explorer-panel' + (hidden ? ' is-hidden' : '')} title="Explorer"
    actions={<><button className="icon-btn" aria-label="Upload a different project" title="Upload a different project" disabled={locked} onClick={onRestart}><Ic d={I.up} s={15} /></button>
      {onCollapse && <button className="icon-btn" aria-label="Collapse explorer" aria-expanded="true" title="Collapse explorer" onClick={onCollapse}><Ic d={I.left} s={15} /></button>}</>}
    footer={p && p.warnings.length > 0 && <details className="notes"><summary>Upload notes ({p.warnings.length})</summary><ul>{p.warnings.map((note, i) => <li key={i} className="helper">{note}</li>)}</ul></details>}>
    {p && <div className="project-row"><Ic d={I.folder} s={16} /><div><b>{p.name}</b><span className="helper">{p.files.length} files · {p.analyzed_files} analyzed{p.partial_files > 0 && ` · ${p.partial_files} partial`}{p.skipped_files > 0 && ` · ${p.skipped_files} skipped`}</span></div></div>}
    {p ? tree(p.file_tree) : <Empty mascot="project" title="No project loaded" text="Upload a ZIP from Workspace to see its files." size={4} />}
  </Panel>;
}

function EditorPanel({ w, stage, hidden, focusedLines }: { w: Workspace; stage: WorkspaceStage; hidden: boolean; focusedLines: [number, number] | null }) {
  const file = w.selected;
  const practicing = stage !== 'explain';
  const challenge = practicing && !!w.exercise;
  const language = challenge ? w.exercise!.language : file?.language ?? null;
  const [caret, setCaret] = useState({ line: 1, col: 1 });
  useEffect(() => setCaret({ line: 1, col: 1 }), [file?.file_id, practicing]);
  const path = challenge ? w.exercise!.source_path : file?.path;
  const name = path?.split('/').pop();
  const dir = path && path !== name ? path.slice(0, path.length - (name?.length ?? 0)) : '';
  return <Panel scroll={false} className={'editor-panel' + (hidden ? ' is-hidden' : '')} busy={!!w.busy.source}
    title={file ? <span className="file-tab" title={path}><Ic d={I.file} s={15} />{name}{dir && <span className="dir">{dir}</span>}</span> : 'Code'}
    actions={<>{challenge && <span className="chip accent">Challenge</span>}{language && <span className="chip">{LANGUAGE[language]}</span>}</>}
    footer={file && file.status !== 'skipped' && <div className="statusbar"><span>Ln {caret.line}, Col {caret.col}</span>{language && <span>{LANGUAGE[language]}</span>}
      <span className="grow">{practicing ? w.exercise ? `Restore missing line ${w.exercise.missing_line}. Your original file is unchanged.` : 'Generating a missing-line challenge…' : w.range ? `Selected ${rangeLabel(w.range.start_line, w.range.end_line)}` : focusedLines ? `Referenced ${rangeLabel(focusedLines[0], focusedLines[1])}` : 'Select lines to analyze just those'}</span>
      <span className={'status ' + file.status}>{STATUS[file.status] ?? file.status}</span></div>}>
    {w.errors.source && <ErrBanner title="Couldn't load this file" text={w.errors.source} action="Retry" onAction={() => file && void w.selectFile(file)} />}
    {file?.reason && file.status !== 'skipped' && <p className="helper editor-note">{file.reason}</p>}
    {w.busy.source ? <div className="editor-loading"><Lines n={6} /></div> : file?.status === 'skipped' ? <Empty title="File skipped" text={file.reason || 'Unsupported source file.'} /> : file
      ? <CodeEditor key={file.file_id} value={challenge ? w.answer : w.code} language={challenge ? w.exercise!.language : file.language || 'python'} readOnly={!challenge || !!w.busy.challenge || !!w.busy.submit || stage !== 'debug'} onChange={practicing ? w.editAnswer : undefined} onSelection={practicing ? undefined : w.setRange} onCursor={setCaret} highlight={practicing ? null : focusedLines} />
      : <Empty mascot="empty" title="No file selected" text="Choose a file in the tree to read it here." />}
  </Panel>;
}

export function ExplorerScreen({ workspace: w, stage, onStage, onChallenge, onVerify, onRestart }: { workspace: Workspace; stage: WorkspaceStage; onStage: (stage: WorkspaceStage) => void; onChallenge: () => void; onVerify: () => void; onRestart: () => void }) {
  const compact = useCompact();
  const box = useRef<HTMLDivElement>(null);
  const total = useWidth(box);
  const [layout, setLayout] = useLayout();
  const [view, setView] = useState<View>('code');
  const [focusedLines, setFocusedLines] = useState<[number, number] | null>(null);
  useEffect(() => setFocusedLines(null), [w.selected?.file_id]);
  const practicing = stage !== 'explain';

  // Open the file a result points at and highlight the verified lines.
  const jump = async (path: string, start: number | null, end: number | null) => {
    const file = w.project?.files.find(f => f.path === path);
    if (!file) return;
    if (file.file_id !== w.selected?.file_id) await w.selectFile(file, w.project, true);
    setFocusedLines(start != null && end != null ? [start, end] : null);
    setView('code');
  };
  const open = (file: SourceFile) => { void w.selectFile(file); setView('code'); };
  const size = panelWidths(layout, total);
  const room = (id: DividerId) => { const edge = withWidths(layout, total, id === 'explorer' ? { explorer: 1e6 } : { assistant: 1e6 }); return panelWidths(edge, total)[id]; };
  const move = (id: DividerId) => (x: number) => { const area = box.current!.getBoundingClientRect(); setLayout(l => dragDivider(l, id, x, area.left, area.width)); };
  const nudge = (id: DividerId) => (delta: number) => setLayout(l => nudgeDivider(l, id, delta, box.current!.getBoundingClientRect().width));
  const reset = () => setLayout(l => ({ ...DEFAULT_LAYOUT, collapsed: l.collapsed }));
  const hide = (v: View) => compact && view !== v;

  return <div ref={box} className={'workspace' + (compact ? ' is-compact' : '')} style={compact ? undefined : { gridTemplateColumns: columnTemplate(layout) }}>
    {compact && <div className="seg view-switch" role="group" aria-label="Workspace view">{VIEWS.map(v => <button key={v.id} className={view === v.id ? 'a' : ''} aria-pressed={view === v.id} onClick={() => setView(v.id)}>{v.label}</button>)}</div>}
    {!compact && layout.collapsed
      ? <div className="card explorer-rail"><button className="icon-btn" aria-label="Show explorer" aria-expanded="false" title="Show explorer" onClick={() => setLayout(l => ({ ...l, collapsed: false }))}><Ic d={I.right} s={15} /></button><span>Explorer</span></div>
      : <ExplorerPanel w={w} practicing={practicing} hidden={hide('files')} onOpen={open} onRestart={onRestart} onCollapse={compact ? undefined : () => setLayout(l => ({ ...l, collapsed: true }))} />}
    {!compact && (layout.collapsed ? <span aria-hidden="true" /> : <Divider label="Resize explorer" now={size.explorer} min={MIN_PX.explorer} max={room('explorer')} onMove={move('explorer')} onNudge={nudge('explorer')} onReset={reset} />)}
    <EditorPanel w={w} stage={stage} hidden={hide('code')} focusedLines={focusedLines} />
    {!compact && <Divider label="Resize Snip AI assistant panel" now={size.assistant} min={MIN_PX.assistant} max={room('assistant')} onMove={move('assistant')} onNudge={nudge('assistant')} onReset={reset} />}
    <AssistantPanel workspace={w} stage={stage} hidden={hide('assistant')} onStage={onStage} onChallenge={onChallenge} onVerify={onVerify} onRestart={onRestart} onJump={jump} />
  </div>;
}
