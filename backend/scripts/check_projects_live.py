"""Live verification of the project-analysis endpoints against a real uvicorn process.

Starts the actual FastAPI app on a free loopback port, sends real HTTP requests with
the synthetic fixture projects from ``tests/project_fixtures.py`` (no downloads), and
checks the results. Ollama is replaced by a *fake listener that only counts connection
attempts*: the checks assert the project endpoints never touch it.

Run from anywhere in PowerShell::

    python backend\\scripts\\check_projects_live.py

Exit code 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import io
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

import httpx  # noqa: E402

import project_fixtures as fx  # noqa: E402
from app.services import TokenBudget  # noqa: E402

RESULTS: list[tuple[bool, str]] = []
BUDGET = TokenBudget(total_context=4096, reserved_generation=1024, safety_margin=256)
ZIP = {"Content-Type": "application/zip"}


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((ok, label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""), flush=True)
    return ok


def section(title: str) -> None:
    print(f"\n==> {title}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeOllama:
    """Accepts TCP connections and counts them. Anything that connects is a bug here."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            conn.close()


def working_set_mb(pid: int) -> float | None:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {pid}).WorkingSet64"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return int(out) / (1024 * 1024)
    except Exception:  # noqa: BLE001 - measurement is optional
        return None


def zip_of(directory: Path, only: tuple[str, ...]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in only and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(directory.parent).as_posix())
    return buffer.getvalue()


def timed_post(client: httpx.Client, path: str, data: bytes, **kwargs) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    response = client.post(path, content=data, headers=ZIP, **kwargs)
    return response, (time.perf_counter() - started) * 1000


def code(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["code"]
    except Exception:  # noqa: BLE001
        return f"<no error envelope: HTTP {response.status_code}>"


def run(client: httpx.Client, fake: FakeOllama, server_pid: int) -> None:
    temp_before = set(os.listdir(tempfile.gettempdir()))
    backend_before = {p.name for p in BACKEND.iterdir()}
    marker = Path(tempfile.gettempdir()) / "codesense_live_marker.txt"
    marker.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    section("1. Python multi-file project")
    r, ms = timed_post(client, "/api/projects/inspect", fx.make_zip(fx.PYTHON_PROJECT))
    body = r.json()
    check(r.status_code == 200, "inspect returns 200", r.text[:200])
    check([f["path"] for f in body["files"]] == ["app/__init__.py", "app/main.py", "app/models.py", "app/utils.py"], "files discovered in sorted order")
    check([e["path"] for e in body["excluded"]] == ["README.md"] and body["excluded"][0]["reason"] == "unsupported_extension", "README.md excluded with a reason")
    edges = {(e["source"], e["specifier"]): (e["status"], e["target"]) for e in body["graph"]["edges"]}
    check(edges[("app/main.py", "app.utils")] == ("resolved", "app/utils.py"), "app/main.py -> app/utils.py resolved")
    check(edges[("app/models.py", "app.utils")] == ("resolved", "app/utils.py"), "app/models.py -> app/utils.py resolved")
    check(edges[("app/main.py", "requests")][0] == "external" and edges[("app/main.py", "os")][0] == "external", "requests/os are external")
    main = next(f for f in body["files"] if f["path"] == "app/main.py")
    check({s["name"] for s in main["symbols"]} == {"run"} and main["has_main_guard"], "symbols and __main__ guard extracted")
    r2, _ = timed_post(client, "/api/projects/inspect", fx.make_zip(fx.PYTHON_PROJECT))
    check(r2.text == r.text, "identical archive -> byte-identical response")

    r, _ = timed_post(client, "/api/projects/context", fx.make_zip(fx.PYTHON_PROJECT), params={"file": "app/main.py", "symbol": "run", "intent": "debug"})
    ctx = r.json()
    check(r.status_code == 200 and 'return helper(user.greet())' in ctx["context"], "debug context contains the selected symbol")
    check(
        "def helper(text):" in ctx["context"] and 'return re.compile("x")' not in ctx["context"],
        "debug context pulls only the dependency code the symbol uses (unused_name appears as an outline signature, not as code)",
    )
    check(BUDGET.count(ctx["context"]).tokens == ctx["budget"]["estimated_tokens"] <= ctx["budget"]["context_limit"] == 2116, "reported tokens match an independent recount and respect the limit")

    # ------------------------------------------------------------------ #
    section("2. HTML/CSS/JS project")
    r, _ = timed_post(client, "/api/projects/inspect", fx.make_zip(fx.WEB_PROJECT))
    body = r.json()
    edges = {(e["source"], e["specifier"]): (e["status"], e["target"]) for e in body["graph"]["edges"]}
    check(edges[("index.html", "css/styles.css")] == ("resolved", "css/styles.css"), "index.html -> styles.css")
    check(edges[("index.html", "js/app.js")] == ("resolved", "js/app.js"), "index.html -> app.js")
    check(edges[("css/styles.css", "base.css")] == ("resolved", "css/base.css"), "styles.css @import base.css")
    check(edges[("js/app.js", "./util.js")] == ("resolved", "js/util.js"), "app.js -> util.js")
    check(edges[("index.html", "js/missing.js")][0] == "missing" and edges[("js/app.js", "./config")][0] == "missing", "dangling references reported as missing")
    check(edges[("index.html", "img/logo.png")][0] == "excluded", "an existing but unanalysed image is 'excluded', not 'missing'")
    app_js = next(f for f in body["files"] if f["path"] == "js/app.js")
    check(app_js["confidence"] == "heuristic" and {"render", "Store"} <= {s["name"] for s in app_js["symbols"]}, "JS symbols found and labelled heuristic")

    # ------------------------------------------------------------------ #
    section("3. Mixed-language project")
    r, _ = timed_post(client, "/api/projects/inspect", fx.make_zip(fx.MIXED_PROJECT))
    body = r.json()
    check(body["summary"]["languages"] == {"css": 2, "html": 1, "javascript": 2, "python": 4}, "four languages counted")
    check([e["path"] for e in body["graph"]["entry_points"]] == ["app/main.py", "web/index.html"], "entry points: main.py and index.html")

    # ------------------------------------------------------------------ #
    section("4. Ignored directories")
    r, _ = timed_post(client, "/api/projects/inspect", fx.make_zip(fx.IGNORED_PROJECT))
    body = r.json()
    reasons = {e["path"]: e["reason"] for e in body["excluded"]}
    check([f["path"] for f in body["files"]] == ["main.py"], "only main.py analysed")
    check(all(reasons[p] == "ignored_directory" for p in ("node_modules/lib/index.js", "dist/bundle.js", ".git/config", "venv/lib/site.py", "build/out.js")), "node_modules/dist/.git/venv/build ignored with reason")
    check(reasons["static/app.min.js"] == "minified" and reasons["data.bin"] == "unsupported_extension", "minified and binary files explained")
    check(len(reasons) + len(body["files"]) == len(fx.IGNORED_PROJECT), "every archive file is accounted for")

    # ------------------------------------------------------------------ #
    section("5. Project larger than the context budget")
    big = fx.make_zip(fx.big_python_project())
    for intent, params in (
        ("explain", {"file": "pipeline.py", "intent": "explain"}),
        ("debug", {"file": "main.py", "symbol": "main", "intent": "debug"}),
        ("overview", {"intent": "overview"}),
    ):
        r, ms = timed_post(client, "/api/projects/context", big, params=params)
        body = r.json()
        tokens = BUDGET.count(body["context"]).tokens
        check(r.status_code == 200 and tokens <= body["budget"]["context_limit"], f"{intent}: within budget ({tokens}/{body['budget']['context_limit']} est. tokens, {ms:.0f} ms)")
    r, _ = timed_post(client, "/api/projects/context", big, params={"file": "pipeline.py", "intent": "explain"})
    body = r.json()
    pipeline = next(f for f in body["files"] if f["path"] == "pipeline.py")
    check(pipeline["status"] == "partial" and pipeline["omitted_ranges"], "oversized target reported as PARTIAL with omitted ranges")
    check(body["omitted_region_total"] > 0 and "not included" in body["context"], "omissions are listed and marked in the text")
    check(any("partially included" in w for w in body["warnings"]), "warning says the file is not fully analysed")
    included = {n for rng in pipeline["included_ranges"] for n in range(rng["start_line"], rng["end_line"] + 1)}
    numbered = [line for line in body["context"].split("\n") if line.lstrip()[:1].isdigit() and " | " in line]
    shown = {int(line.split("|")[0]) for line in numbered}
    check(included <= shown and shown >= included, "coverage ranges agree with the numbered lines in the text")

    # ------------------------------------------------------------------ #
    section("6. Malicious and invalid archives are rejected")
    evil = {
        "path traversal (../)": (fx.make_zip({"../evil.py": "x = 1\n"}), 422, "UNSAFE_PATH"),
        "nested traversal (a/../../)": (fx.make_zip({"a/../../evil.py": "x"}), 422, "UNSAFE_PATH"),
        "absolute path": (fx.make_zip({"/etc/evil.py": "x"}), 422, "UNSAFE_PATH"),
        "windows drive path": (fx.make_zip({"C:/evil.py": "x"}), 422, "UNSAFE_PATH"),
        "windows backslash traversal": (fx.patch_names(fx.make_zip({"..|..|evil.py": "x"}), {b"|": b"\\"}), 422, "UNSAFE_PATH"),
        "symlink": (fx.make_symlink_zip(), 422, "UNSUPPORTED_ENTRY"),
        "duplicate path": (fx.make_duplicate_zip(), 422, "DUPLICATE_PATH"),
        "case collision": (fx.make_zip({"A.py": "x", "a.py": "y"}), 422, "DUPLICATE_PATH"),
        "encrypted entry": (fx.make_encrypted_zip(), 422, "ENCRYPTED_ARCHIVE"),
        "zip bomb (4 MiB of zeros)": (fx.make_zip_bomb(4), 422, "SUSPICIOUS_COMPRESSION"),
        "501 entries": (fx.make_zip({f"f{i}.py": "" for i in range(501)}), 422, "TOO_MANY_ENTRIES"),
        "not a zip": (b"MZ this is not a zip", 422, "INVALID_ARCHIVE"),
        "empty body": (b"", 422, "INVALID_ARCHIVE"),
        "truncated zip": (fx.make_zip(fx.PYTHON_PROJECT)[:150], 422, "INVALID_ARCHIVE"),
        "6 MiB single entry": (fx.make_zip({"blob.bin": os.urandom(6 * 1024 * 1024)}, compression=zipfile.ZIP_STORED), 422, "ENTRY_TOO_LARGE"),
    }
    for label, (data, status, expected) in evil.items():
        for path in ("/api/projects/inspect", "/api/projects/context"):
            r, _ = timed_post(client, path, data)
            check(r.status_code == status and code(r) == expected, f"{label} -> {status} {expected} ({path.rsplit('/', 1)[1]})", f"got {r.status_code} {code(r)}")

    big_zip = fx.make_zip({"a.bin": os.urandom(5 * 1024 * 1024 - 1024), "b.bin": os.urandom(5 * 1024 * 1024 - 1024), "c.bin": os.urandom(1_000_000)}, compression=zipfile.ZIP_STORED)
    r, _ = timed_post(client, "/api/projects/inspect", big_zip)
    check(len(big_zip) > 10 * 1024 * 1024 and r.status_code == 413 and code(r) == "REQUEST_TOO_LARGE", f"{len(big_zip) / 1048576:.1f} MiB upload -> 413 REQUEST_TOO_LARGE", f"got {r.status_code} {code(r)}")
    r = client.post("/api/projects/inspect", content=fx.make_zip(fx.PYTHON_PROJECT), headers={"Content-Type": "application/json"})
    check(r.status_code == 415 and code(r) == "UNSUPPORTED_MEDIA_TYPE", "JSON content type on a ZIP route -> 415")
    r = client.post("/api/projects/inspect", content=fx.make_zip(fx.PYTHON_PROJECT), headers={**ZIP, "Host": "evil.example"})
    check(r.status_code == 400 and code(r) == "INVALID_HOST", "foreign Host header -> 400 INVALID_HOST")

    # ------------------------------------------------------------------ #
    section("7. Request-size policy is route specific")
    fair = fx.make_zip({"data.bin": os.urandom(4 * 1024 * 1024)}, compression=zipfile.ZIP_STORED)
    before = working_set_mb(server_pid)
    r, ms = timed_post(client, "/api/projects/inspect", fair)
    after = working_set_mb(server_pid)
    check(r.status_code == 200 and len(fair) > 4_000_000, f"a {len(fair) / 1048576:.1f} MiB ZIP is accepted ({ms:.0f} ms)", f"got {r.status_code}")
    r = client.post("/api/ai/generate", json={"prompt": "x" * 200_000})
    check(r.status_code == 413 and code(r) == "REQUEST_TOO_LARGE", "the 128 KiB limit still protects /api/ai/generate")
    r = client.post("/api/ai/generate", content=fx.make_zip(fx.PYTHON_PROJECT), headers=ZIP)
    check(r.status_code == 415, "/api/ai/generate still refuses ZIP bodies")
    if before and after:
        print(f"      server working set: {before:.0f} MiB before, {after:.0f} MiB after the {len(fair) / 1048576:.1f} MiB upload")

    # ------------------------------------------------------------------ #
    section("8. Nothing is executed, written or retained")
    hostile_py = f"open({str(marker)!r}, 'w').write('pwned')\nimport os; os.system('echo pwned > {marker.name}')\nraise SystemExit(7)\n"
    hostile_js = f"require('fs').writeFileSync({str(marker)!r}, 'pwned'); process.exit(7);\n"
    hostile_html = f"<script>fetch('http://127.0.0.1:{fake.port}/beacon')</script><img src='http://127.0.0.1:{fake.port}/pixel.png'>"
    bundle = fx.make_zip({"evil.py": hostile_py, "evil.js": hostile_js, "evil.html": hostile_html})
    r1, _ = timed_post(client, "/api/projects/inspect", bundle)
    r2, _ = timed_post(client, "/api/projects/context", bundle, params={"file": "evil.py", "intent": "debug"})
    check(r1.status_code == 200 and r2.status_code == 200, "hostile source is analysed normally")
    check(not marker.exists(), "no uploaded Python/JS ran (marker file was never created)")
    check(fake.connections == 0, "no outbound request was made for URLs found in uploaded HTML")
    # PowerShell (used above only to read the server's memory) drops transient policy-test scripts in %TEMP%.
    appeared = {
        name
        for name in set(os.listdir(tempfile.gettempdir())) - temp_before - {marker.name}
        if not name.startswith("__PSScriptPolicyTest")
    }
    check(not appeared, "no files appeared in the temp directory", f"new entries: {sorted(appeared)[:5]}")
    check({p.name for p in BACKEND.iterdir()} - backend_before - {"__pycache__"} == set(), "no files appeared in the backend directory")

    # ------------------------------------------------------------------ #
    section("9. Ollama is not required")
    r = client.get("/api/ai/health")
    check(r.status_code == 200 and r.json()["status"] == "unavailable", "AI health reports Ollama as unavailable (nothing real is listening)")
    r, _ = timed_post(client, "/api/projects/context", fx.make_zip(fx.MIXED_PROJECT), params={"file": "app/main.py", "intent": "explain"})
    check(r.status_code == 200, "context endpoint works while Ollama is unavailable")
    check(fake.connections == 1, "the only connection to the fake Ollama was the /api/ai/health probe", f"{fake.connections} connections")

    # ------------------------------------------------------------------ #
    section("10. Documentation and CORS")
    spec = client.get("/openapi.json").json()
    check({"/api/projects/inspect", "/api/projects/context"} <= set(spec["paths"]), "OpenAPI lists both project routes")
    page = client.get("/docs")
    check(page.status_code == 200 and "cdn" not in page.text.lower(), "offline Swagger UI still served without CDN references")
    origin = "http://127.0.0.1:5173"
    pre = client.options("/api/projects/inspect", headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    check(pre.status_code == 200 and pre.headers.get("access-control-allow-origin") == origin, "CORS preflight allowed for the Vite origin")

    # ------------------------------------------------------------------ #
    section("11. Representative timings (end to end over HTTP, localhost)")
    corpus = {
        "5-file Python project": fx.make_zip(fx.PYTHON_PROJECT),
        "9-file mixed project": fx.make_zip(fx.MIXED_PROJECT),
        "this backend (app/*.py)": zip_of(BACKEND / "app", (".py",)),
        "40-function oversized module": big,
    }
    for label, data in corpus.items():
        inspect_ms, context_ms = [], []
        for _ in range(15):
            inspect_ms.append(timed_post(client, "/api/projects/inspect", data)[1])
            context_ms.append(timed_post(client, "/api/projects/context", data, params={"intent": "overview"})[1])
        print(
            f"      {label:<32} {len(data) / 1024:7.1f} KiB zip   inspect median {statistics.median(inspect_ms):6.1f} ms (max {max(inspect_ms):6.1f})"
            f"   context median {statistics.median(context_ms):6.1f} ms (max {max(context_ms):6.1f})"
        )
    check(True, "timings recorded (informational)")


def main() -> int:
    fake = FakeOllama()
    port = free_port()
    env = {**os.environ, "OLLAMA_BASE_URL": f"http://127.0.0.1:{fake.port}", "PYTHONPATH": str(BACKEND)}
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=60) as client:
            for _ in range(100):
                try:
                    if client.get("/openapi.json").status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                print("server did not start; log:\n" + Path(log.name).read_text())
                return 1
            print(f"server up on {base} (pid {server.pid}); fake Ollama on port {fake.port}")
            run(client, fake, server.pid)
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        leaked = [line for line in Path(log.name).read_text(errors="replace").splitlines() if "SECRET" in line]
        Path(log.name).unlink(missing_ok=True)
        if leaked:
            RESULTS.append((False, "server log contained marker text"))

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    print("RESULT: SUCCESS" if passed == len(RESULTS) else "RESULT: FAILURE")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
