const { test } = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const Renderer = require('react-test-renderer');
const { renderToStaticMarkup } = require('react-dom/server');
const esbuild = require('esbuild');
const bundle = esbuild.buildSync({ stdin: { contents: `export { useWorkspace } from './src/useWorkspace'; export { QuestionPanel } from './src/QuestionPanel'; export { RichText } from './src/RichText'; export * from './src/conversation';`, resolveDir: process.cwd(), loader: 'tsx' }, bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic', external: ['react', 'react-dom', 'react-dom/server', 'react/jsx-runtime'] });
const bundled = { exports: {} };
new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(require, bundled, bundled.exports);
const { useWorkspace, QuestionPanel, RichText, conversationScope, conversationKey, recentHistory } = bundled.exports;

test('safe Markdown renders headings, code, lists and entities without activating HTML or invented links', () => {
  const html = renderToStaticMarkup(React.createElement(RichText, { text: "## Explanation\n\nUse **sum** &apos;here&apos;.\n\n1. First\n2. Second\n\n```python\nprint('<value>')\n```\n\n<script>alert('x')</script>\n\n[fake source](javascript:alert(1))\n\n![image](https://example.com/track.png)" }));
  assert.match(html, /<h2>Explanation<\/h2>/);
  assert.match(html, /<strong>sum<\/strong>/);
  assert.match(html, /<ol>/);
  assert.match(html, /<pre><code class="language-python">/);
  assert.doesNotMatch(html, /&amp;apos;|<script|<a |<img|javascript:/);
  assert.match(html, /fake source/);
});

test('scope contract follows Analyze and never carries history from another project or code range', () => {
  const file = conversationScope('p', 'file', 'f', { start_line: 2, end_line: 3 }, 'beginner');
  assert.equal(file.start_line, undefined);
  const project = conversationScope('p', 'project', 'f', { start_line: 2, end_line: 3 }, 'beginner');
  assert.equal(project.file_id, undefined);
  const key = conversationKey(file);
  assert.deepEqual(recentHistory([{ scopeKey: key, question: 'one', answer: 'first' }, { scopeKey: 'other', question: 'unrelated', answer: 'wrong' }], key), [{ question: 'one', answer: 'first' }]);
});

test('actual workspace supports follow-ups, preserves tab drafts and answers after errors, and resets on new context', async () => {
  const oldWindow = global.window, oldFetch = global.fetch;
  global.window = { setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {} };
  let w, renderer, uploaded = 0, failNext = false, deferred = null;
  const sent = [];
  const file = { file_id: 'f', path: 'total.py', language: 'python', status: 'parsed' };
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  global.fetch = async (url, options = {}) => {
    if (url.endsWith('/health')) return json({ backend: { status: 'ready' }, ai: { status: 'ready', model: 'test' } });
    if (url.endsWith('/projects/snippet')) return json({ project_id: `p${++uploaded}`, files: [file], relationships: [] });
    if (url.endsWith('/files/f')) return json({ code: 'def total(prices):\n    return sum(prices)' });
    if (url.endsWith('/analysis')) return json({ project_id: `p${uploaded}`, file_id: 'f', summary: 'Adds all prices.', explanations: [{ description: 'sum computes the total.' }], selection: { scope: 'file', requested: null } });
    if (url.endsWith('/questions')) {
      sent.push(JSON.parse(options.body));
      if (deferred) return await deferred;
      if (failNext) { failNext = false; return json({ code: 'AI_UNAVAILABLE', detail: 'Local model unavailable.' }, 503); }
      return json({ answer: '## Answer\n\nIt adds prices.', limitations: [], references: [], context_used: [] });
    }
    throw new Error(`Unexpected request ${url}`);
  };
  function Probe({ hidden = false }) { w = useWorkspace(); return React.createElement(QuestionPanel, { workspace: w, hidden }); }
  try {
    await Renderer.act(async () => { renderer = Renderer.create(React.createElement(Probe)); });
    await Renderer.act(async () => { assert.equal(await w.upload({ mode: 'paste', code: 'return sum(prices)', language: 'python', filename: 'total.py' }, 'beginner'), true); });
    await Renderer.act(async () => { await w.explain('file'); });
    await Renderer.act(async () => { assert.equal(await w.askQuestion('What does it do?'), true); });
    assert.match(sent[0].previous_explanation, /Adds all prices/);
    await Renderer.act(async () => { await w.askQuestion('Why?'); });
    await Renderer.act(async () => { await w.askQuestion('Can you explain that?'); });
    assert.equal(sent[2].history.length, 2);
    assert.equal(sent[2].history[1].question, 'Why?');
    await Renderer.act(async () => { renderer.root.findByType('textarea').props.onChange({ target: { value: 'Draft follow-up' } }); renderer.update(React.createElement(Probe, { hidden: true })); });
    await Renderer.act(async () => { renderer.update(React.createElement(Probe, { hidden: false })); });
    assert.equal(renderer.root.findByType('textarea').props.value, 'Draft follow-up');
    assert.equal(w.questions.length, 3);
    failNext = true;
    await Renderer.act(async () => { assert.equal(await w.askQuestion('Another question'), false); });
    assert.equal(w.questions.length, 3);
    assert.match(w.errors.question, /unavailable/);
    let resolve;
    deferred = new Promise(r => { resolve = r; });
    let pending;
    await Renderer.act(async () => { pending = w.askQuestion('Slow question'); });
    assert.equal(w.busy.question, true);
    assert.equal(await w.askQuestion('Duplicate question'), false);
    const count = sent.length;
    await Renderer.act(async () => { w.changeScope('project'); });
    await Renderer.act(async () => { resolve(json({ answer: 'Stale response', limitations: [] })); assert.equal(await pending, false); });
    deferred = null;
    assert.equal(sent.length, count);
    assert.equal(w.questions.length, 0);
    await Renderer.act(async () => { await w.askQuestion('How do these files connect?'); });
    assert.equal(sent.at(-1).scope, 'project');
    assert.equal(sent.at(-1).file_id, undefined);
    assert.deepEqual(sent.at(-1).history, []);
    await Renderer.act(async () => { w.changeScope('block'); w.setRange({ start_line: 2, end_line: 2 }); });
    await Renderer.act(async () => { await w.askQuestion('What is this selected line?'); });
    assert.equal(sent.at(-1).scope, 'block');
    assert.equal(sent.at(-1).start_line, 2);
    await Renderer.act(async () => { await w.upload({ mode: 'paste', code: 'x=1', language: 'python', filename: 'new.py' }, 'beginner'); });
    assert.equal(w.questions.length, 0);
    await Renderer.act(async () => { await w.askQuestion('New project question'); });
    assert.equal(sent.at(-1).project_id, 'p2');
    assert.deepEqual(sent.at(-1).history, []);
    assert.equal(sent.at(-1).previous_explanation, '');
  } finally {
    await Renderer.act(async () => { renderer?.unmount(); });
    global.window = oldWindow; global.fetch = oldFetch;
  }
});
