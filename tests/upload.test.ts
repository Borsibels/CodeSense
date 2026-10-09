import assert from "node:assert/strict";
import { test } from "node:test";
import { uploadError } from "../src/upload.ts";

test("upload selection accepts one nonempty ZIP and rejects invalid selections", () => {
  const zip = { name: "project.ZIP", size: 1024 };
  assert.equal(uploadError([zip]), null);
  assert.match(uploadError([])!, /one ZIP/);
  assert.match(uploadError([zip, zip])!, /one ZIP/);
  assert.match(uploadError([{ name: "project.zip.exe", size: 1024 }])!, /\.zip archive/);
  assert.match(uploadError([{ name: "empty.zip", size: 0 }])!, /empty/);
});
