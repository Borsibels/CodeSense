import assert from 'node:assert/strict';
import { test } from 'node:test';
import { detectLanguage } from '../src/detectLanguage.ts';

test('detects distinctive source syntax without treating ambiguous snippets as certain', () => {
  for (const [code, expected] of [
    ['def greet():\n    return "hi"', 'python'], ['print("hello")', 'python'],
    ['from math import sqrt', 'python'], ['function greet() { return "hi"; }', 'javascript'],
    ['const options = {color: "red"};', 'javascript'], ['const template = "<div>Hello</div>";', 'javascript'],
    ['<!DOCTYPE html><html><body>hi</body></html>', 'html'], ['<div class="notice">hi</div>', 'html'],
    ['.notice { color: red; }', 'css'], ['@media screen { .card { padding: 2px; } }', 'css'],
    ['', null], ['x = 1', null], ['return value', null], ['just some text', null],
  ] as const) assert.equal(detectLanguage(code), expected, code);
});

test('supported filename extensions resolve ambiguous snippets and unsupported extensions do not guess', () => {
  assert.equal(detectLanguage('x = 1', 'main.PY'), 'python');
  assert.equal(detectLanguage('x = 1;', 'app.js'), 'javascript');
  assert.equal(detectLanguage('/* empty */', 'style.css'), 'css');
  assert.equal(detectLanguage('hello', 'index.htm'), 'html');
  assert.equal(detectLanguage('print(1)', 'notes.txt'), null);
});
