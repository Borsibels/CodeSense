import { useEffect, useRef, useState } from 'react';
import type { Workspace } from './useWorkspace';
import { ErrBanner } from './ui';
import { RichText } from './RichText';
import { conversationScope } from './conversation';

/** Local conversation; recent turns are sent only within the same project and scope. */
export function QuestionPanel({ workspace: w, hidden }: { workspace: Workspace; hidden?: boolean }) {
  const [question, setQuestion] = useState('');
  const available = w.health?.ai.status === 'ready' && !!w.selected && w.selected.status !== 'skipped';
  const busy = Object.values(w.busy).some(Boolean);
  const latest = useRef<HTMLElement>(null);
  const newest = w.questions[w.questions.length - 1];
  const scope = w.project ? conversationScope(w.project.project_id, w.scope, w.selected?.file_id, w.range, w.difficulty).scope : 'file';
  // A new answer opens at its first line, not at the end of a long answer.
  useEffect(() => { if (newest) latest.current?.scrollIntoView({ block: 'start' }); }, [newest]);
  return <section className={'qa-panel' + (hidden ? ' is-hidden' : '')} aria-label="Local AI Q&A">
    <div className="qa-thread">
      <h3 className="tab-title">Ask about this code</h3>
      <p className="helper">{scope === 'project' ? 'Uses bounded project source excerpts.' : scope === 'block' ? `Uses selected lines ${w.range!.start_line}–${w.range!.end_line}.` : `Uses ${w.selected?.path || 'the selected file'}.`} Snip remembers recent turns in this scope. Changing the project, file, selection, scope or difficulty starts a fresh conversation. Your code stays on this device.</p>
      <div className="qa-answers" aria-live="polite">{w.questions.map((item, i) => <article key={i} ref={item === newest ? latest : undefined}><b>{item.question}</b><p className="helper">{item.context}</p><RichText text={item.answer} />
        {!!item.references?.length && <div><b>Verified source locations</b><ul>{item.references.map((r, j) => <li key={j}>{r.path}, lines {r.start_line}–{r.end_line}</li>)}</ul></div>}
        {!!item.context_used?.length && <details><summary>What the AI was shown</summary><ul>{item.context_used.map((r, j) => <li key={j}>{r.path}, lines {r.start_line}–{r.end_line}</li>)}</ul></details>}
        {item.limitations.map((text, j) => <p className="helper" key={j}>{text}</p>)}</article>)}</div>
      {w.errors.question && <ErrBanner title="Could not answer" text={w.errors.question} />}
    </div>
    <form className="qa-form" onSubmit={async e => { e.preventDefault(); if (available && !busy && question.trim() && await w.askQuestion(question)) setQuestion(''); }}>
      <label htmlFor="code-question">Your question</label>
      <textarea id="code-question" value={question} onChange={e => setQuestion(e.target.value)} maxLength={1000} rows={3} disabled={busy} placeholder="Why does this function return this value?" />
      <button className="btn pri" type="submit" disabled={!available || busy || !question.trim()}>{w.busy.question ? 'Answering locally…' : 'Ask Snip'}</button>
      {!available && <p className="helper">Select a supported file and start the local model to ask questions.</p>}
    </form>
  </section>;
}
