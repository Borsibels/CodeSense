import { Fragment } from 'react';
import type { ReactNode } from 'react';
import type { Analysis, Finding, PatternCheck } from './api';
import { RichText } from './RichText';
import { groupFindings, numberedExcerpt, outcomeLabel, rangeLabel, scopeLabel, stepLocation, strengthLabel, tierLabel, tierReasonText, verificationLabel } from './analysisText';

export type Jump = (path: string, start: number | null, end: number | null) => void;

// Provenance badges: AI text is never presented as a fact, and backend-computed data says so.
const Ai = () => <span className="badge ai" title="Written by the small AI model running on this computer. It can be wrong.">AI-written</span>;
export const Checked = ({ children = 'Checked by Sift' }: { children?: ReactNode }) => <span className="badge checked" title="Computed by Sift from your uploaded files, not written by the AI.">{children}</span>;
const Note = ({ title, tone, children }: { title: string; tone?: 'warn'; children: ReactNode }) => <div className={'note' + (tone ? ' ' + tone : '')} role="note"><b>{title}</b><span>{children}</span></div>;
const Limits = ({ items }: { items: string[] }) => items.length > 0 ? <details className="limits" open><summary>What this analysis could not see or check ({items.length})</summary><ul>{items.map((limit, i) => <li className="helper" key={i}>{limit}</li>)}</ul></details> : null;

function Where({ path, start, end, onJump, label }: { path: string | null; start: number | null; end: number | null; onJump: Jump; label?: string }) {
  if (!path) return null;
  return <button className="btn source-reference" onClick={() => onJump(path, start, end)}>{label ?? (start != null && end != null ? `${path} · ${rangeLabel(start, end)}` : path)}</button>;
}

function Excerpt({ text, start, hit }: { text: string; start: number; hit?: { start: number | null; end: number | null } }) {
  return <pre className="excerpt" aria-label="Source excerpt">{numberedExcerpt(text, start, hit).map(l => <span key={l.number} className={l.hit ? 'hit' : undefined}><span className="no">{String(l.number).padStart(4)} </span>{l.line}{'\n'}</span>)}</pre>;
}

function Scope({ data }: { data: Analysis }) {
  const { selection } = data;
  return <>
    <p className="helper">{scopeLabel(selection, data.target_file)}{selection.requested && selection.expanded ? ` · you selected ${rangeLabel(selection.requested.start_line, selection.requested.end_line)}` : ''}</p>
    {selection.expanded && selection.note && <Note title="Wider than your selection" tone="warn">{selection.note}</Note>}
  </>;
}

function Coverage({ data }: { data: Analysis }) {
  const c = data.coverage;
  return <details><summary>What the AI was shown</summary><p className="helper">{c.full_files.length} file(s) in full, {c.partial_files.length} in part, {c.outline_only_files.length} as signatures only, {c.not_included_total} not shown, {c.excluded_total} skipped. Model: {data.generation.model}{data.generation.mode === 'compact' ? ' (short answer)' : ''}.</p></details>;
}

export const FileLink = ({ path, onJump }: { path: string; onJump: Jump }) => <button className="file-link" onClick={() => onJump(path, null, null)}>{path}</button>;

/** How files connect, as found while explaining. Computed by Sift from the project, not written by the AI. */
export function AnalysisLinks({ data, onJump }: { data: Analysis; onJump: Jump }) {
  if (data.relationships.length === 0) return null;
  return <section className="links"><div className="lbl">From the latest explanation <Checked /></div><ul>{data.relationships.map((r, i) => <li key={i}>
    <FileLink path={r.from} onJump={onJump} />{r.kind === 'entry_point' ? ' looks like an entry point' : <>{r.kind === 'loads' ? ' loads ' : ' uses '}{r.resolved ? <FileLink path={r.to} onJump={onJump} /> : <>{r.to} <span className="helper">(not found in this project)</span></>}</>}
  </li>)}</ul></section>;
}

/** Project overview or file/selection explanation. */
export function AnalysisView({ data, onJump }: { data: Analysis; onJump: Jump }) {
  return <div className="analysis">
    <Scope data={data} />
    <div><Ai /><RichText text={data.summary} /></div>
    {data.analogy && <div><b>Think of it like this.</b><RichText text={data.analogy} /></div>}
    {data.role_in_app && <div><b>Its job in the app.</b><RichText text={data.role_in_app} /></div>}
    {data.concept_to_learn && <div className="step"><span className="lbl">Concept to learn</span><b>{data.concept_to_learn.name}</b><RichText text={data.concept_to_learn.explanation} /></div>}
    {data.explanations.length > 0 && <div className="lbl">Step by step</div>}
    {data.explanations.map((step, i) => {
      const where = stepLocation(step);
      return <section className="step" key={i}><b>{step.title}</b><RichText text={step.description} />
        {where ? <Where path={where.path} start={where.start} end={where.end} label={where.label} onJump={onJump} /> : step.location_status === 'rejected' && <span className="helper">The AI named a location that could not be verified, so none is shown.</span>}</section>;
    })}
    {data.glossary.length > 0 && <details><summary>Words explained <Checked>Written by Sift</Checked></summary><dl className="facts">{data.glossary.map(g => <Fragment key={g.term}><dt>{g.term}</dt><dd>{g.meaning}</dd></Fragment>)}</dl></details>}
    {data.assumptions.length > 0 && <details><summary>What the AI was unsure about <Ai /></summary><ul>{data.assumptions.map((a, i) => <li className="helper" key={i}>{a}</li>)}</ul></details>}
    <Limits items={data.limitations} />
    <Coverage data={data} />
    <p className="helper">{data.notice}</p>
  </div>;
}

function FindingCard({ finding: f, onJump }: { finding: Finding; onJump: Jump }) {
  const verified = verificationLabel[f.verification];
  return <article className={'finding ' + (f.tier === 'possible_problem' ? 'possible' : 'worth')}>
    <header><b>{f.id} · {f.title}</b><span className="badge">{tierLabel(f.tier)}</span><span className={'badge' + (f.verification === 'source_verified' ? ' checked' : '')} title={verified.long}>{verified.short}</span></header>
    <div><Ai /><RichText text={f.problem} /></div>
    <dl className="facts"><dt>What could happen</dt><dd><RichText text={f.what_could_happen} /></dd><dt>Likely cause</dt><dd><RichText text={f.likely_cause} /></dd><dt>Idea to try</dt><dd><RichText text={f.suggestion} /></dd><dt>AI's rating</dt><dd>{f.severity} impact, {f.confidence} confidence (a guess, not a probability)</dd></dl>
    {f.tier_reasons.length > 0 && <ul>{f.tier_reasons.map(r => <li className="helper" key={r}>{tierReasonText[r] ?? r}</li>)}</ul>}
    {f.evidence && <><span className="lbl">The code it points at <Checked>Copied from your file</Checked></span><Excerpt text={f.evidence.source_excerpt} start={f.evidence.excerpt_start_line} hit={{ start: f.start_line, end: f.end_line }} /></>}
    <Where path={f.file_path} start={f.start_line} end={f.end_line} onJump={onJump} />
  </article>;
}

function RuleCard({ check: p, onJump }: { check: PatternCheck; onJump: Jump }) {
  return <article className="finding rule">
    <header><b>{p.id} · {p.title}</b><span className="badge">{strengthLabel(p.strength)}</span><Checked>{p.parser === 'confirmed' ? 'Rule-based · parsed' : 'Rule-based · approximate scan'}</Checked></header>
    <p>{p.explanation}</p>
    {p.assumptions.length > 0 && <><span className="lbl">This is a problem only if</span><ul>{p.assumptions.map((a, i) => <li className="helper" key={i}>{a}</li>)}</ul></>}
    <Excerpt text={p.source_excerpt} start={p.excerpt_start_line} hit={{ start: p.start_line, end: p.end_line }} />
    {p.corroborates.length > 0 && <p className="helper">Also raised by the AI: {p.corroborates.join(', ')}.</p>}
    <Where path={p.file_path} start={p.start_line} end={p.end_line} onJump={onJump} />
  </article>;
}

/** Possible problems: AI suspicions with verified locations, plus rule-based checks. Never worded as confirmed bugs. */
export function ProblemsView({ data, onJump }: { data: Analysis; onJump: Jump }) {
  const { possible, worth } = groupFindings(data.findings);
  const none = data.findings.length === 0 && data.pattern_checks.length === 0;
  return <div className="analysis problems">
    <Scope data={data} />
    <Note title={outcomeLabel(data.debug_outcome) || 'Result'} tone={data.debug_outcome === 'possible_problems' ? 'warn' : undefined}>{data.summary} <Checked /></Note>
    {possible.length > 0 && <><div className="lbl">Possible problems</div>{possible.map(f => <FindingCard key={f.id} finding={f} onJump={onJump} />)}</>}
    {worth.length > 0 && <><div className="lbl">Worth checking</div>{worth.map(f => <FindingCard key={f.id} finding={f} onJump={onJump} />)}</>}
    {data.pattern_checks.length > 0 && <><div className="lbl">Automatic checks</div>{data.pattern_checks.map(p => <RuleCard key={p.id} check={p} onJump={onJump} />)}</>}
    {none && <p className="helper">Nothing was flagged in the code examined. That does not mean the code is correct.</p>}
    <Limits items={data.limitations} />
    <Coverage data={data} />
  </div>;
}
