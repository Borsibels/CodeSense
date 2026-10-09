import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ApiError, post, request, uploadProject } from '../src/api.ts';

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
