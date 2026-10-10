import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { CodeEditor } from './Code';
import { AnalysisLinks, AnalysisView, Checked, FileLink, ProblemsView } from './analysis';
import type { Jump } from './analysis';
import { effectiveScope, linkLabel, linksFor, scopeChoices } from './analysisText';
import type { Scope } from './api';
import { QuestionPanel } from './QuestionPanel';
import { LocalAISetup, DEFAULT_QWEN_MODEL } from './screens';
import { DIFFS } from './types';
import type { Difficulty } from './types';
import { ErrBanner, I, Ic, Lines, Panel, Pill, Seg, Sloth } from './ui';
import type { Workspace } from './useWorkspace';
import type { WorkspaceStage } from './App';

type Tab = 'explanation' | 'issues' | 'relationships' | 'practice' | 'ask';
const TABS: { id: Tab; label: string; icon: string }[] = [{ id: 'explanation', label: 'Explanation', icon: I.file }, { id: 'issues', label: 'Issues', icon: I.warn }, { id: 'relationships', label: 'Relationships', icon: I.links }, { id: 'practice', label: 'Practice', icon: I.learn }, { id: 'ask', label: 'Q&A', icon: I.chat }];

const AssistEmpty = ({ mascot, title, text, children }: { mascot: Parameters<typeof Sloth>[0]['mascot']; title: string; text: string; children?: ReactNode }) =>
  <div className="assist-empty"><Sloth size={3} mascot={mascot} /><div><b>{title}</b><p>{text}</p>{children}</div></div>;
const Working = ({ rows = 5 }: { rows?: number }) => <div role="status"><Lines n={rows} /><p className="helper">Snip is analyzing your code with the local model. This can take a little time.</p></div>;

export function AssistantPanel({ workspace: w, stage, hidden, onStage, onChallenge, onVerify, onRestart, onJump }: { workspace: Workspace; stage: WorkspaceStage; hidden: boolean; onStage: (stage: WorkspaceStage) => void; onChallenge: () => void; onVerify: () => void; onRestart: () => void; onJump: Jump }) {
  const uid = useId();
  const [tab, setTab] = useState<Tab>('explanation');
  const resume = useRef<WorkspaceStage>('debug');
  const practicing = stage !== 'explain';
  // A challenge that starts anywhere (or is resumed) shows its own tab.
  useEffect(() => { if (stage !== 'explain') setTab('practice'); }, [stage]);

  // Each tab keeps its own scroll position; a fresh result starts at its top.
  const scrolls = useRef<Partial<Record<Tab, number>>>({});
  const body = () => document.getElementById(`${uid}-panel`);
  const toTop = (which: Tab) => { scrolls.current[which] = 0; const el = body(); if (el && tab === which) el.scrollTop = 0; };
  useLayoutEffect(() => { const el = body(); if (el) el.scrollTop = scrolls.current[tab] ?? 0; }, [tab]);
  useLayoutEffect(() => toTop('explanation'), [w.explanation, w.busy.analysis]);
  useLayoutEffect(() => toTop('issues'), [w.problems, w.busy.problems]);
  useLayoutEffect(() => toTop('practice'), [stage]);

  const modelReady = w.health?.ai.status === 'ready';
  const analysisReady = modelReady && w.health?.analysis?.available !== false;
  const idle = !w.busy.analysis && !w.busy.problems && !w.busy.source && !w.busy.challenge && !w.busy.submit && !w.busy.question;
  const canExplain = !!w.project && analysisReady && idle;
  const canCheck = canExplain && !practicing && !!w.selected && w.selected.status !== 'skipped';
  const choices = scopeChoices(w.selected, practicing ? null : w.range);
  const scope = effectiveScope(w.scope, choices);

  // Leaving Practice shows the source again; the challenge, answer and hints stay in the workspace so Practice can resume.
  const show = (next: Tab) => {
    scrolls.current[tab] = body()?.scrollTop ?? 0;
    if (practicing && next !== 'practice') { resume.current = stage; w.setRange(null); onStage('explain'); }
    else if (!practicing && next === 'practice' && w.exercise) onStage(resume.current);
    setTab(next);
  };
  const explain = () => { show('explanation'); void w.explain(practicing && scope === 'block' ? 'file' : scope); };
  const keys = (e: KeyboardEvent) => {
    const at = TABS.findIndex(t => t.id === tab);
    const to = e.key === 'ArrowRight' ? (at + 1) % TABS.length : e.key === 'ArrowLeft' ? (at + TABS.length - 1) % TABS.length : e.key === 'Home' ? 0 : e.key === 'End' ? TABS.length - 1 : -1;
    if (to < 0) return;
    e.preventDefault(); show(TABS[to].id); document.getElementById(`${uid}-${TABS[to].id}`)?.focus();
  };
  const issueCount = w.problems ? w.problems.findings.length + w.problems.pattern_checks.length : 0;
  const state: Record<Tab, 'busy' | 'ready' | undefined> = { explanation: w.busy.analysis ? 'busy' : w.explanation ? 'ready' : undefined, issues: w.busy.problems ? 'busy' : w.problems ? 'ready' : undefined, relationships: undefined, practice: w.busy.challenge || w.busy.submit ? 'busy' : w.exercise ? 'ready' : undefined, ask: w.busy.question ? 'busy' : w.questions.length ? 'ready' : undefined };
  const locked = !!(w.busy.challenge || w.busy.submit || w.busy.hint || w.busy.solution);

  const footer = tab !== 'practice' || !practicing ? undefined : stage === 'debug'
    ? <button className="btn pri" disabled={!w.exercise || locked || !w.answer.trim()} onClick={onVerify}>{w.busy.submit ? 'Checking…' : 'Check my fix'}</button>
    : stage === 'verify' ? <><button className="btn" onClick={() => onStage('debug')}>Revise my fix</button><button className="btn pri" onClick={() => onStage('learn')}>Continue → Learn</button></>
    : <><button className="btn" onClick={() => onStage('debug')}>Back to my fix</button><button className="btn pri" onClick={onRestart}>Start a new project</button></>;

  return <Panel className={'assistant-panel' + (hidden ? ' is-hidden' : '')} busy={!!(w.busy.analysis || w.busy.problems || w.busy.challenge)} title={<><Ic d={I.spark} s={16} />Ask Snip<span className="sr-only"> (AI assistant)</span></>}
    controls={<>
      <div className="analyze">
        <label className="lbl" htmlFor={`${uid}-scope`}>Analyze</label>
        <div className="analyze-row">
          <select id={`${uid}-scope`} value={scope} disabled={!!w.busy.analysis} onChange={e => w.changeScope(e.target.value as Scope)}>{choices.map(c => <option key={c.id} value={c.id} disabled={c.disabled}>{c.label}</option>)}</select>
          <button className="btn pri" disabled={!canExplain} onClick={explain}>{w.busy.analysis ? 'Explaining…' : 'Explain'}</button>
        </div>
        <div className="pace"><span className="lbl">Learning pace</span><Seg<Difficulty> value={w.difficulty} options={DIFFS} onChange={w.changeDifficulty} /></div>
      </div>
      <div className="tabs" role="tablist" aria-label="Snip assistant results" onKeyDown={keys}>{TABS.map(t => <button key={t.id} role="tab" id={`${uid}-${t.id}`} aria-selected={tab === t.id} aria-controls={`${uid}-panel`} tabIndex={tab === t.id ? 0 : -1} className={'tab' + (tab === t.id ? ' on' : '')} onClick={() => show(t.id)}>
        <Ic d={t.icon} s={15} /><span>{t.label}</span>
        {t.id === 'issues' && issueCount > 0 ? <span className="tab-count">{issueCount}<span className="sr-only"> possible issues</span></span> : state[t.id] && <span className="tab-state" data-state={state[t.id]}><span className="sr-only">{state[t.id] === 'busy' ? ' (working)' : ' (has results)'}</span></span>}
      </button>)}</div></>}
    footer={footer} bodyProps={{ id: `${uid}-panel`, role: 'tabpanel', 'aria-labelledby': `${uid}-${tab}` }}>
    {tab !== 'relationships' && !modelReady && <><ErrBanner title={w.health ? 'Model unavailable' : 'Backend unavailable'} text={w.healthError || 'Snip needs the local model for explanations, questions and missing-line challenges. File browsing remains available.'} action={w.health ? undefined : 'Check again'} onAction={w.health ? undefined : () => void w.checkHealth()} />{w.health && <LocalAISetup model={w.health.ai.model || DEFAULT_QWEN_MODEL} checking={w.checking} onCheck={() => void w.checkHealth(true)} />}</>}
    {tab !== 'relationships' && modelReady && !analysisReady && <ErrBanner title="Code analysis unavailable" text={w.health?.analysis?.reason || 'Code analysis is not available on this server.'} />}
    {tab === 'explanation' && <>
      {w.errors.analysis && <ErrBanner title="Explanation failed" text={w.errors.analysis} action="Retry" onAction={explain} />}
      {w.busy.analysis ? <Working /> : w.explanation ? <AnalysisView data={w.explanation} onJump={onJump} /> : !w.errors.analysis && <AssistEmpty mascot="explanation" title="No explanation yet" text="Choose what to analyze, then press Explain. Snip will give you a plain-language walkthrough." />}
    </>}
    {tab === 'issues' && <>
      <div className="tab-intro"><p className="helper">Ask Snip to look for things that might be wrong. Snip can flag code that is fine and miss real problems, so every result is a suspicion to check, not a confirmed bug.</p>
        <button className="btn" disabled={!canCheck} onClick={() => void w.checkProblems()}>{w.busy.problems ? 'Looking locally…' : w.range ? `Check lines ${w.range.start_line}–${w.range.end_line}` : 'Check this file'}</button></div>
      {w.errors.problems && <ErrBanner title="Check failed" text={w.errors.problems} action="Retry" onAction={() => void w.checkProblems()} />}
      {w.busy.problems ? <Working rows={4} /> : w.problems ? <ProblemsView data={w.problems} onJump={onJump} /> : !w.errors.problems && <AssistEmpty mascot="default" title="No issues checked yet" text="Check the open file, or select some lines first to check only those." />}
    </>}
    {tab === 'relationships' && <Relationships w={w} onJump={onJump} />}
    {tab === 'practice' && <Practice w={w} stage={stage} locked={locked} modelReady={modelReady} canStart={modelReady && idle && !!w.selected && w.selected.status !== 'skipped'} onChallenge={onChallenge} />}
    {/* Stays mounted while another tab is open, so a half-written question is not lost. It restarts for each project and file. */}
    <QuestionPanel key={`${w.project?.project_id}:${w.selected?.file_id}`} workspace={w} hidden={tab !== 'ask'} />
  </Panel>;
}

/** Static relationships the upload found, plus any the latest explanation reported. Nothing here is inferred by the UI. */
function Relationships({ w, onJump }: { w: Workspace; onJump: Jump }) {
  const all = w.project?.relationships ?? [];
  const here = linksFor(all, w.selected?.path ?? null);
  const row = (r: (typeof all)[number], i: number) => <li key={i}><FileLink path={r.source} onJump={onJump} /> → {r.resolved ? <FileLink path={r.target} onJump={onJump} /> : r.target} <span className="helper">({linkLabel(r)})</span></li>;
  const explained = w.explanation && w.explanation.relationships.length > 0;
  if (!all.length && !explained) return <AssistEmpty mascot="project" title="No relationships found" text="Sift found no imports or references between these files." />;
  const missing = (list: typeof all) => list.some(r => !r.resolved) && <details><summary>Not found in this project ({list.filter(r => !r.resolved).length})</summary><p className="helper">These names are used, but no matching file was uploaded.</p><ul>{list.filter(r => !r.resolved).map(row)}</ul></details>;
  const uses = here.uses.filter(r => r.resolved);
  return <div className="analysis links-view">
    <p className="helper">Found by reading the imports and references in your files. They are hints, not a complete dependency graph. <Checked /></p>
    {w.selected && <section className="links"><div className="lbl">This file · {w.selected.path}</div>
      {here.uses.length + here.usedBy.length === 0 ? <p className="helper">No connections to other files were found.</p> : <>
        {uses.length > 0 && <><b>Uses</b><ul>{uses.map(row)}</ul></>}
        {here.usedBy.length > 0 && <><b>Used by</b><ul>{here.usedBy.map(row)}</ul></>}
        {uses.length === 0 && here.usedBy.length === 0 && <p className="helper">This file does not connect to other uploaded files.</p>}
        {missing(here.uses)}</>}</section>}
    {w.explanation && <AnalysisLinks data={w.explanation} onJump={onJump} />}
    {all.length > 0 && <details className="links"><summary>All project relationships ({all.filter(r => r.resolved).length} between files)</summary><ul>{all.filter(r => r.resolved).map(row)}</ul>{missing(all)}</details>}
  </div>;
}

/** The missing-line challenge: set up, restore, verify and learn. The editor beside it holds the challenge copy of the file. */
function Practice({ w, stage, locked, modelReady, canStart, onChallenge }: { w: Workspace; stage: WorkspaceStage; locked: boolean; modelReady: boolean; canStart: boolean; onChallenge: () => void }) {
  if (stage === 'explain') return <>
    {w.errors.challenge && <ErrBanner title="Challenge generation failed" text={w.errors.challenge} />}
    <AssistEmpty mascot="challenge" title="Practice with a missing line" text="Snip takes one line out of your file. Restore it, with hints if you need them.">
      <button className="btn pri" disabled={!canStart} onClick={onChallenge}>Generate missing-line challenge</button></AssistEmpty></>;
  return <>
    <h3 className="tab-title">{stage === 'learn' ? 'Learn from your fix' : stage === 'verify' ? 'Verify your fix' : 'Restore the missing line'}</h3>
    <p className="helper">Keep working in the code editor beside this panel.</p>
    {w.busy.challenge ? <div role="status"><Lines n={4} /><p>Snip is generating a challenge with the local model…</p></div> : w.exercise && <div className="coach"><Sloth size={3} mascot="challenge" /><div><p>{w.exercise.objective}</p><div className="row"><Pill>{w.exercise.language}</Pill><Pill>{w.exercise.difficulty}</Pill></div><p className="helper">{w.exercise.verification_policy}</p></div></div>}
    {w.errors.challenge && <ErrBanner title="Challenge generation failed" text={w.errors.challenge} />}
    {stage === 'debug' && <>
      <div className="lbl">Difficulty</div><Seg<Difficulty> value={w.challengeDifficulty} options={DIFFS} onChange={d => { if (!locked && modelReady) void w.loadChallenge(d); }} /><p className="helper">Changing difficulty generates a new challenge and resets your answer and hints.</p>
      <button className="btn" disabled={locked || !modelReady} onClick={() => void w.loadChallenge()}>Regenerate challenge</button>
      {w.exercise && <><div className="lbl">Hints</div>{Array.from({ length: w.exercise.hint_count }, (_, i) => <div className="hint" key={i}><Ic d={i < w.hints.length ? I.check : I.lock} s={16} /><span>Hint {i + 1}: {w.hints[i] || 'Locked'}</span></div>)}<button className="btn" disabled={locked || w.hints.length >= w.exercise.hint_count} onClick={() => void w.revealHint()}>{w.busy.hint ? 'Loading hint…' : w.hints.length >= w.exercise.hint_count ? 'All hints revealed' : `Reveal hint ${w.hints.length + 1} of ${w.exercise.hint_count}`}</button></>}
      {w.errors.hint && <ErrBanner title="Hint unavailable" text={w.errors.hint} />}{w.errors.submit && <ErrBanner title="Check failed" text={w.errors.submit} />}</>}
    {(stage === 'verify' || stage === 'learn') && w.result && <div role="status" className={'coach note' + (w.result.correct ? '' : ' warn')}><Sloth size={3} mascot={w.result.correct ? 'default' : 'explanation'} /><div><b>{w.result.correct ? 'Restored' : 'Not restored yet'}</b><p>{w.result.feedback}</p>{w.result.limitations.map((l, i) => <p className="helper" key={i}>{l}</p>)}</div></div>}
    {stage === 'learn' && <><p>Compare your fix with the original line and explain why it matters.</p><button className="btn" disabled={locked || !!w.solution} onClick={() => void w.revealSolution()}>Reveal original solution</button>{w.errors.solution && <ErrBanner title="Solution unavailable" text={w.errors.solution} />}{w.solution && <><h4 className="lbl">Original source</h4><CodeEditor value={w.solution} language={w.exercise?.language} readOnly /></>}</>}
  </>;
}
