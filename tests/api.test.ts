import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ANALYSIS_TIMEOUT_MS, ApiError, analysisBody, analyzeProject, post, request, uploadProject, ingestProject } from '../src/api.ts';

test('API client sends JSON, propagates server errors, and preserves multipart boundaries', async () => {
  const oldFetch = globalThis.fetch;
  const oldWindow = globalThis.window;
  globalThis.window = globalThis as any;
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url, '/api/analyze');
      assert.equal(init?.method, 'POST');
      assert.equal((init?.headers as any)['Content-Type'], 'application/json');
      assert.equal(JSON.parse(init?.body as string).scope, 'file');
      return new Response(JSON.stringify({ summary: 'Adds numbers.' }), { status: 200 });
    };
    assert.equal((await post<any>('/analyze', { scope: 'file' })).summary, 'Adds numbers.');
    globalThis.fetch = async () => new Response(JSON.stringify({ code: 'AI_UNAVAILABLE', detail: 'Start the local model.' }), { status: 503 });
    await assert.rejects(request('/analyze'), (e: ApiError) => e.status === 503 && e.code === 'AI_UNAVAILABLE' && e.message === 'Start the local model.');
    globalThis.fetch = async (url, init) => {
      assert.equal(url, '/api/projects/upload');
      assert.ok(init?.body instanceof FormData);
      assert.equal(init?.headers, undefined);
      assert.equal((init?.body as FormData).get('file') instanceof File, true);
      return new Response(JSON.stringify({ project_id: 'test' }), { status: 201 });
    };
    assert.equal((await uploadProject(new File(['zip'], 'demo.zip'))).project_id, 'test');
  } finally { globalThis.fetch = oldFetch; globalThis.window = oldWindow; }
});

test('new project inputs send folder paths and snippet language to their endpoints', async () => {
  const oldFetch = globalThis.fetch, oldWindow = globalThis.window;
  globalThis.window = globalThis as any;
  try {
    const file = new File(['print(1)'], 'main.py');
    Object.defineProperty(file, 'webkitRelativePath', { value: 'demo/src/main.py' });
    globalThis.fetch = async (url, init) => {
      assert.equal(url, '/api/projects/files');
      const data = init?.body as FormData;
      assert.deepEqual(JSON.parse(data.get('paths') as string), ['demo/src/main.py']);
      assert.equal(data.get('name'), 'demo');
      assert.equal(data.getAll('files').length, 1);
      assert.equal(init?.headers, undefined);
      return new Response(JSON.stringify({project_id:'folder'}), {status:201});
    };
    assert.equal((await ingestProject({mode:'folder',files:[file]})).project_id, 'folder');
    globalThis.fetch = async (url, init) => {
      assert.equal(url, '/api/projects/snippet');
      assert.deepEqual(JSON.parse(init?.body as string), {code:'print(1)',language:'python',filename:'snippet.py'});
      return new Response(JSON.stringify({project_id:'paste'}), {status:201});
    };
    assert.equal((await ingestProject({mode:'paste',code:'print(1)',language:'python',filename:'snippet.py'})).project_id,'paste');
  } finally { globalThis.fetch = oldFetch; globalThis.window = oldWindow; }
});

test('API client handles disconnected server, invalid JSON and timeout', async () => {
  const oldFetch = globalThis.fetch;
  const oldWindow = globalThis.window;
  globalThis.window = globalThis as any;
  try {
    globalThis.fetch = async () => { throw new TypeError('connection failed'); };
    await assert.rejects(request('/health'), (e: ApiError) => e.status === 0 && /local backend/.test(e.message));
    globalThis.fetch = async () => new Response('not json', { status: 200 });
    await assert.rejects(request('/health'), (e: ApiError) => e.status === 502);
    globalThis.fetch = (_url, init) => new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))));
    await assert.rejects(request('/health', {}, 5), (e: ApiError) => e.status === 504);
  } finally { globalThis.fetch = oldFetch; globalThis.window = oldWindow; }
});

test('errors from both backend envelopes become readable ApiErrors', async () => {
  const oldFetch = globalThis.fetch, oldWindow = globalThis.window;
  globalThis.window = globalThis as any;
  try {
    // The analysis engine's own envelope: {error: {code, message}}.
    globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: 'MODEL_NOT_INSTALLED', message: 'Install it with: ollama pull qwen2.5-coder:3b' } }), { status: 503 });
    await assert.rejects(request('/projects/p/analysis'), (e: ApiError) => e.status === 503 && e.code === 'MODEL_NOT_INSTALLED' && /ollama pull/.test(e.message));
    // The session host's envelope: {code, detail}.
    globalThis.fetch = async () => new Response(JSON.stringify({ code: 'AI_BUSY', detail: 'Local AI is busy.' }), { status: 429 });
    await assert.rejects(request('/projects/p/analysis'), (e: ApiError) => e.status === 429 && e.code === 'AI_BUSY' && e.message === 'Local AI is busy.');
    // Neither: a generic message that still carries the status.
    globalThis.fetch = async () => new Response(JSON.stringify({ unexpected: true }), { status: 500 });
    await assert.rejects(request('/x'), (e: ApiError) => e.status === 500 && /500/.test(e.message));
  } finally { globalThis.fetch = oldFetch; globalThis.window = oldWindow; }
});

test('analysis requests map the UI vocabulary to the backend contract', () => {
  const file = { file_id: 'f-1' };
  assert.deepEqual(analysisBody('explain', 'experienced', file, { start_line: 3, end_line: 9 }), { intent: 'explain', depth: 'advanced', file_id: 'f-1', start_line: 3, end_line: 9 });
  assert.deepEqual(analysisBody('explain', 'beginner', file, null), { intent: 'explain', depth: 'beginner', file_id: 'f-1' });
  assert.deepEqual(analysisBody('debug', 'intermediate', file, null), { intent: 'debug', depth: 'intermediate', file_id: 'f-1' });
  // A project overview never carries a file or a selection, even if the UI has one.
  assert.deepEqual(analysisBody('overview', 'beginner', file, { start_line: 1, end_line: 2 }), { intent: 'overview', depth: 'beginner' });
  assert.ok(ANALYSIS_TIMEOUT_MS > 120000, 'analysis may need several local model calls');
});

test('analyzeProject posts JSON to the project analysis route', async () => {
  const oldFetch = globalThis.fetch, oldWindow = globalThis.window;
  globalThis.window = globalThis as any;
  try {
    let seen: { url: string; init?: RequestInit } | null = null;
    globalThis.fetch = async (url, init) => { seen = { url: String(url), init }; return new Response(JSON.stringify({ summary: 'ok', selection: { scope: 'file' } }), { status: 200 }); };
    const result = await analyzeProject('a b/c', analysisBody('explain', 'beginner', { file_id: 'f-1' }, { start_line: 2, end_line: 4 }));
    assert.equal((result as any).summary, 'ok');
    assert.equal(seen!.url, '/api/projects/a%20b%2Fc/analysis');
    assert.equal(seen!.init?.method, 'POST');
    assert.deepEqual(JSON.parse(seen!.init?.body as string), { intent: 'explain', depth: 'beginner', file_id: 'f-1', start_line: 2, end_line: 4 });
    assert.ok(seen!.init?.signal, 'a timeout signal is attached');
  } finally { globalThis.fetch = oldFetch; globalThis.window = oldWindow; }
});
