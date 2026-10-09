"""Small synthetic projects and hostile ZIP builders shared by the project tests and live script.

Everything is generated in memory; nothing is downloaded and no uploaded code is ever run.
"""

from __future__ import annotations

import io
import stat
import zipfile
from collections.abc import Mapping

FileMap = Mapping[str, str | bytes]


def make_zip(files: FileMap, *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """ZIP of ``{path: content}``. Paths ending in ``/`` become directory entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, content in files.items():
            if name.endswith("/"):
                archive.writestr(zipfile.ZipInfo(name), b"")
                continue
            data = content.encode("utf-8") if isinstance(content, str) else content
            archive.writestr(zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), data, compress_type=compression)
    return buffer.getvalue()


def patch_names(data: bytes, replacements: Mapping[bytes, bytes]) -> bytes:
    """Rewrite same-length entry names inside raw ZIP bytes (local header + central directory).

    zipfile on Windows rewrites ``\\`` when *writing*, so hostile names such as
    ``..\\evil.py`` are created by writing a placeholder and patching the bytes.
    """
    for old, new in replacements.items():
        assert len(old) == len(new)
        data = data.replace(old, new)
    return data


def make_special_zip(name: str, mode: int, content: bytes = b"target.py") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
        info.external_attr = mode << 16
        info.create_system = 3  # Unix
        archive.writestr(info, content)
    return buffer.getvalue()


def make_symlink_zip(name: str = "link.py") -> bytes:
    return make_special_zip(name, stat.S_IFLNK | 0o777)


def make_encrypted_zip() -> bytes:
    """A ZIP whose only entry carries the "encrypted" flag (general-purpose bit 0)."""
    data = bytearray(make_zip({"secret.py": "x = 1"}))
    data[6] |= 0x1  # local file header flags
    data[data.rindex(b"PK" + bytes([1, 2])) + 8] |= 0x1  # central directory flags
    return bytes(data)


def make_zip_bomb(megabytes: int = 4) -> bytes:
    return make_zip({"zeros.py": b"\x00" * (megabytes * 1024 * 1024)})


def make_duplicate_zip(first: str = "a.py", second: str = "a.py") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(first, "x = 1\n")
        with_warning = zipfile.ZipInfo(second)
        import warnings  # noqa: PLC0415

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns about duplicate names
            archive.writestr(with_warning, "x = 2\n")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Representative projects
# --------------------------------------------------------------------------- #
PYTHON_PROJECT: dict[str, str | bytes] = {
    "app/__init__.py": "",
    "app/main.py": '''"""Entry point."""
import os
import json
from app.utils import helper, unused_name
from app.models import User
import requests


def run(name):
    user = User(name)
    return helper(user.greet())


if __name__ == "__main__":
    print(run("world"))
''',
    "app/utils.py": '''import re


def helper(text):
    """Shout the text."""
    return text.upper() + "!"


def unused_name():
    return re.compile("x")
''',
    "app/models.py": '''from app.utils import helper


class User:
    """A person."""

    def __init__(self, name):
        self.name = name

    def greet(self):
        return helper("hello " + self.name)

    async def save(self):
        return None
''',
    "README.md": "# not code\n",
}

WEB_PROJECT: dict[str, str | bytes] = {
    "index.html": '''<!DOCTYPE html>
<html>
<head>
  <title>Todo app</title>
  <link rel="stylesheet" href="css/styles.css">
  <script src="js/app.js" type="module"></script>
  <script src="js/missing.js"></script>
</head>
<body>
  <div id="root"></div>
  <img src="img/logo.png">
  <script>console.log("inline")</script>
</body>
</html>
''',
    "css/styles.css": '''@import "base.css";
.todo { color: red; background: url(../img/logo.png); }
#root > .item:hover { margin: 0 }
''',
    "css/base.css": "body { margin: 0; }\n",
    "js/app.js": '''import { add } from './util.js';
import config from "./config";
const root = document.getElementById("root");

export function render(items) {
  root.textContent = items.map((i) => add(i, 1)).join(",");
}

class Store {
  constructor() { this.items = []; }
  push(item) { this.items.push(item); }
}
''',
    "js/util.js": '''export const add = (a, b) => a + b;
export function sub(a, b) {
  return a - b;
}
''',
    "img/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(64)),
}

MIXED_PROJECT: dict[str, str | bytes] = {**PYTHON_PROJECT, **{f"web/{k}": v for k, v in WEB_PROJECT.items()}}

IGNORED_PROJECT: dict[str, str | bytes] = {
    "main.py": "print('hi')\n",
    "node_modules/lib/index.js": "module.exports = 1;\n",
    "dist/bundle.js": "var a=1;\n",
    ".git/config": "[core]\n",
    "venv/lib/site.py": "x = 1\n",
    "__pycache__/main.cpython-312.pyc": b"\x00\x01\x02",
    "build/out.js": "var b=2;\n",
    "static/app.min.js": "var c=3;\n",
    "data.bin": b"\x00\x01\x02\x03",
    "notes.txt": "plain text\n",
}

CYCLE_PROJECT: dict[str, str | bytes] = {
    "a.py": "import b\n\ndef fa():\n    return b.fb()\n",
    "b.py": "import c\n\ndef fb():\n    return c.fc()\n",
    "c.py": "import a\n\ndef fc():\n    return a.fa()\n",
    "self_ref.py": "import self_ref\n",
    "js/x.js": "import { y } from './y.js';\nexport const x = 1;\n",
    "js/y.js": "import { x } from './x.js';\nexport const y = 2;\n",
}


def big_python_project(functions: int = 40, lines_each: int = 14) -> dict[str, str | bytes]:
    """A project far larger than the model's context budget."""
    body = []
    for i in range(functions):
        lines = [f"def compute_{i}(value, factor={i}):", f'    """Step {i} of the pipeline."""']
        for j in range(lines_each):
            lines.append(f"    value = (value * factor + {j}) % 1000003  # stage {j}")
        lines.append("    return value")
        body.append("\n".join(lines))
    calls = "\n".join(f"    total += compute_{i}(total)" for i in range(functions))
    return {
        "pipeline.py": "\n\n\n".join(body) + "\n",
        "main.py": f"from pipeline import *\n\n\ndef main():\n    total = 1\n{calls}\n    return total\n\n\nif __name__ == \"__main__\":\n    main()\n",
        "helpers.py": "def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n",
    }
