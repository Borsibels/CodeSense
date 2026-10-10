import { useState } from 'react';
import type { Workspace } from './useWorkspace';
import { ErrBanner } from './ui';

export function QuestionPanel({ workspace: w }: { workspace: Workspace }) {
  const [question, setQuestion] = useState('');
  const available = w.health?.ai.status === 'ready' && !!w.selected && w.selected.status !== 'skipped';
  const busy = Object.values(w.busy).some(Boolean);
  return <section className="qa-panel" aria-label="Local AI Q&A">
    <h3>Ask about this code</h3>
    <p className="helper">{w.range ? `Uses selected lines ${w.range.start_line}–${w.range.end_line}.` : `Uses ${w.selected?.path || 'the selected file'}.`} Each question is answered independently. Your code stays on this device.</p>
    <div className="qa-answers" aria-live="polite">{w.questions.map((item, i) => <article key={i}><b>{item.question}</b><p className="helper">{item.context}</p><p className="qa-answer">{item.answer}</p>{item.limitations.map((text, j) => <p className="helper" key={j}>{text}</p>)}</article>)}</div>
    <form onSubmit={async e => { e.preventDefault(); if (available && !busy && question.trim() && await w.askQuestion(question)) setQuestion(''); }}>
      <label htmlFor="code-question">Your question</label>
      <textarea id="code-question" value={question} onChange={e => setQuestion(e.target.value)} maxLength={1000} rows={3} disabled={busy} placeholder="Why does this function return this value?" />
      <button className="btn" type="submit" disabled={!available || busy || !question.trim()}>{w.busy.question ? 'Answering locally…' : 'Ask local AI'}</button>
    </form>
    {!available && <p className="helper">Select a supported file and start the local model to ask questions.</p>}
    {w.errors.question && <ErrBanner title="Could not answer" text={w.errors.question} />}
  </section>;
}
