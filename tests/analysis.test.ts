import assert from 'node:assert/strict';
import { test } from 'node:test';
import { groupFindings, numberedExcerpt, outcomeLabel, rangeLabel, scopeLabel, stepLocation, strengthLabel, tierLabel, tierReasonText, verificationLabel } from '../src/analysisText.ts';

const step = (over: object) => ({ file_path: 'shop.py', start_line: 7, end_line: 9, location_status: 'in_context' as const, ...over });

test('only locations the backend verified become clickable', () => {
  assert.deepEqual(stepLocation(step({})), { path: 'shop.py', start: 7, end: 9, label: 'shop.py · lines 7–9' });
  assert.equal(stepLocation(step({ start_line: 7, end_line: 7 }))!.label, 'shop.py · line 7');
  assert.match(stepLocation(step({ location_status: 'outline_only' }))!.label, /signature only/);
  assert.deepEqual(stepLocation(step({ location_status: 'file_only', start_line: null, end_line: null })), { path: 'shop.py', start: null, end: null, label: 'shop.py' });
  // A rejected or missing location is never offered, even if the fields are somehow present.
  assert.equal(stepLocation(step({ location_status: 'rejected' })), null);
  assert.equal(stepLocation(step({ location_status: 'none', file_path: null })), null);
  assert.equal(stepLocation(step({ start_line: null })), null);
});

test('findings keep the backend order within their tier', () => {
  const f = (id: string, tier: string) => ({ id, tier }) as any;
  const { possible, worth } = groupFindings([f('F1', 'possible_problem'), f('F2', 'worth_checking'), f('F3', 'possible_problem')]);
  assert.deepEqual(possible.map(x => x.id), ['F1', 'F3']);
  assert.deepEqual(worth.map(x => x.id), ['F2']);
});

test('wording never presents a suspicion as a confirmed bug', () => {
  assert.match(verificationLabel.source_verified.long, /does not prove/);
  assert.equal(tierLabel('possible_problem'), 'Possible problem');
  assert.equal(outcomeLabel('no_clear_problem'), 'No clear problem found');
  assert.equal(strengthLabel('problem_if_assumptions_hold'), 'A problem if the assumptions hold');
  for (const text of [...Object.values(verificationLabel).flatMap(v => [v.short, v.long]), ...Object.values(tierReasonText), outcomeLabel('possible_problems')]) {
    assert.doesNotMatch(text, /confirmed bug|definitely|is a bug/i);
  }
});

test('scope labels say what was analysed', () => {
  const sel = (over: object) => ({ scope: 'file', requested: null, analyzed_symbol: null, analyzed_lines: null, expanded: false, note: null, ...over }) as any;
  assert.equal(scopeLabel(sel({ scope: 'project' }), null), 'Whole-project overview');
  assert.equal(scopeLabel(sel({ scope: 'file', analyzed_lines: { start_line: 1, end_line: 20 } }), 'shop.py'), 'shop.py (lines 1–20)');
  assert.equal(scopeLabel(sel({ scope: 'symbol', analyzed_symbol: 'total', analyzed_lines: { start_line: 5, end_line: 9 } }), 'shop.py'), 'total (lines 5–9) in shop.py');
  assert.equal(rangeLabel(4, 4), 'line 4');
});

test('excerpts are numbered from the backend start line and mark the cited lines', () => {
  const rows = numberedExcerpt('a\nb\nc', 10, { start: 11, end: 11 });
  assert.deepEqual(rows.map(r => [r.number, r.line, r.hit]), [[10, 'a', false], [11, 'b', true], [12, 'c', false]]);
  assert.ok(numberedExcerpt('x', 1).every(r => !r.hit));
});
