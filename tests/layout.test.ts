import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DEFAULT_LAYOUT, DIVIDER_PX, MIN_PX, RAIL_PX, columnTemplate, dragDivider, nudgeDivider, panelWidths, parseLayout } from '../src/layout.ts';

const W = 1400;
const sum = (l: Parameters<typeof panelWidths>[0], total = W) => { const p = panelWidths(l, total); return p.explorer + p.editor + p.assistant + (l.collapsed ? DIVIDER_PX : DIVIDER_PX * 2); };

test('the default layout gives the editor the most space and fills the container exactly', () => {
  const p = panelWidths(DEFAULT_LAYOUT, W);
  assert.ok(p.editor > p.assistant && p.assistant > p.explorer);
  assert.ok(Math.abs(p.explorer / (W - 12) - 0.17) < 1e-9 && Math.abs(p.assistant / (W - 12) - 0.35) < 1e-9);
  assert.ok(Math.abs(sum(DEFAULT_LAYOUT) - W) < 1e-6);
});

test('dragging a divider moves it to the pointer and never squeezes a panel below its minimum', () => {
  const moved = dragDivider(DEFAULT_LAYOUT, 'explorer', 400, 100, W);
  assert.ok(Math.abs(panelWidths(moved, W).explorer - (400 - 100 - DIVIDER_PX / 2)) < 1e-6);
  assert.equal(moved.assistant, DEFAULT_LAYOUT.assistant);
  assert.ok(Math.abs(panelWidths(dragDivider(DEFAULT_LAYOUT, 'assistant', 1000, 100, W), W).assistant - (100 + W - 1000 - DIVIDER_PX / 2)) < 1e-6);
  for (const x of [-500, 0, 5000]) for (const id of ['explorer', 'assistant'] as const) {
    const p = panelWidths(dragDivider(DEFAULT_LAYOUT, id, x, 100, W), W);
    assert.ok(p.explorer >= MIN_PX.explorer - 1e-6 && p.editor >= MIN_PX.editor - 1e-6 && p.assistant >= MIN_PX.assistant - 1e-6, `${id} at ${x}`);
    assert.ok(Math.abs(sum(dragDivider(DEFAULT_LAYOUT, id, x, 100, W)) - W) < 1e-6);
  }
});

test('keyboard nudges move a divider by the requested amount and respect the same limits', () => {
  const before = panelWidths(DEFAULT_LAYOUT, W);
  assert.ok(Math.abs(panelWidths(nudgeDivider(DEFAULT_LAYOUT, 'explorer', 24, W), W).explorer - (before.explorer + 24)) < 1e-6);
  assert.ok(Math.abs(panelWidths(nudgeDivider(DEFAULT_LAYOUT, 'assistant', 24, W), W).assistant - (before.assistant - 24)) < 1e-6);
  assert.ok(panelWidths(nudgeDivider(DEFAULT_LAYOUT, 'explorer', -10000, W), W).explorer >= MIN_PX.explorer);
});

test('collapsing the explorer leaves a rail, hands its space to the other panels, and remembers its width', () => {
  const collapsed = { ...DEFAULT_LAYOUT, collapsed: true };
  const p = panelWidths(collapsed, W);
  assert.equal(p.explorer, RAIL_PX);
  assert.ok(Math.abs(sum(collapsed) - W) < 1e-6);
  assert.ok(p.editor > panelWidths(DEFAULT_LAYOUT, W).editor);
  const dragged = dragDivider(collapsed, 'assistant', 900, 0, W);
  assert.equal(dragged.explorer, DEFAULT_LAYOUT.explorer);
  assert.ok(Math.abs(panelWidths(dragged, W).assistant - (W - 900 - DIVIDER_PX / 2)) < 1e-6);
  assert.deepEqual({ ...dragged, collapsed: false }.explorer, DEFAULT_LAYOUT.explorer);
});

test('the grid template weights add up so the columns fill the container', () => {
  const weights = (t: string) => [...t.matchAll(/, (\d+)fr/g)].map(m => Number(m[1]));
  assert.equal(weights(columnTemplate(DEFAULT_LAYOUT)).reduce((a, b) => a + b), 1000);
  assert.deepEqual(weights(columnTemplate(DEFAULT_LAYOUT)), [170, 480, 350]);
  const collapsed = columnTemplate({ ...DEFAULT_LAYOUT, collapsed: true });
  assert.ok(collapsed.startsWith(`${RAIL_PX}px 0px `));
  assert.ok(Math.abs(weights(collapsed).reduce((a, b) => a + b) - 1000) <= 1);
});

test('a saved layout is only used when it is complete and in range', () => {
  assert.deepEqual(parseLayout(JSON.stringify({ explorer: 0.2, assistant: 0.3, collapsed: true })), { explorer: 0.2, assistant: 0.3, collapsed: true });
  for (const bad of [null, '', 'nope', '{}', JSON.stringify({ explorer: 'a', assistant: 0.3, collapsed: false }), JSON.stringify({ explorer: 0.01, assistant: 0.3, collapsed: false }), JSON.stringify({ explorer: 0.4, assistant: 0.5, collapsed: false }), JSON.stringify({ explorer: 0.2, assistant: 0.3 })])
    assert.deepEqual(parseLayout(bad), DEFAULT_LAYOUT);
});
