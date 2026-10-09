import assert from "node:assert/strict";
import { test } from "node:test";
import { uploadError, sourceUploadError, folderFiles } from "../src/upload.ts";

test("upload selection accepts one nonempty ZIP and rejects invalid selections", () => {
  const zip = { name: "project.ZIP", size: 1024 };
  assert.equal(uploadError([zip]), null);
  assert.match(uploadError([])!, /one ZIP/);
  assert.match(uploadError([zip, zip])!, /one ZIP/);
  assert.match(uploadError([{ name: "project.zip.exe", size: 1024 }])!, /\.zip archive/);
  assert.match(uploadError([{ name: "empty.zip", size: 0 }])!, /empty/);
  assert.match(uploadError([{ name: "huge.zip", size: 10 * 1024 * 1024 + 1 }])!, /10 MB/);
});
test('source and folder selection enforce limits and exclude generated and environment files', () => {
  assert.equal(sourceUploadError([{name:'main.py',size:12},{name:'app.js',size:20}]), null);
  assert.match(sourceUploadError([])!, /at least/);
  assert.match(sourceUploadError([{name:'notes.txt',size:10}])!, /source files/);
  assert.match(sourceUploadError([{name:'main.py',size:100*1024+1}])!, /100 KB/);
  assert.equal(sourceUploadError([{name:'notes.txt',size:10}], true), null);
  assert.match(sourceUploadError([{name:'main.py',size:10*1024*1024+1}], true)!, /10 MB/);
  const files = ['demo/main.py','demo/node_modules/a.js','demo/.env','demo/dist/app.js','demo/src/app.js'].map(path => ({name:path.split('/').at(-1)!,webkitRelativePath:path}) as File);
  assert.deepEqual(folderFiles(files).map(f => f.webkitRelativePath), ['demo/main.py','demo/src/app.js']);
});
