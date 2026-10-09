"""Live evaluation of POST /api/ai/analyze against the real local model (Ollama + qwen2.5-coder:3b).

Starts the actual FastAPI app in a uvicorn subprocess on a free loopback port and sends real HTTP
requests using the controlled fixture projects in ``tests/analysis_fixtures.py``. Writes a JSON
report (default: the system temp directory, never inside the repo) and prints a table.

    python backend\\scripts\\check_analysis_live.py --runs 3 --depths beginner
    python backend\\scripts\\check_analysis_live.py --runs 1 --depths intermediate,advanced
    python backend\\scripts\\check_analysis_live.py --fixtures off_by_one_py --runs 5 --out report.json

What is measured automatically (valid JSON is never treated as proof of a correct analysis):
  schema validity, source-reference validity (verified / hypothesis / unsupported), whether the
  known bug's lines were cited, false positives, coverage honesty, truncation / compact fallback,
  attempts, and latency per stage. Explanation correctness and beginner readability need a human:
  the report stores every response, and prints simple readability proxies (reading grade, jargon
  words) only as hints.

Exit code 0 if the runs completed (accuracy is reported, not asserted), 1 if the harness failed.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

import httpx  # noqa: E402

from analysis_fixtures import BY_NAME, FIXTURES, Fixture  # noqa: E402
from app.services.analysis_models import AnalysisResponse  # noqa: E402
from app.services.analysis_prompts import neutralize_control_tokens  # noqa: E402

ZIP = {"Content-Type": "application/zip"}
# Words that a non-programmer is unlikely to know. Used only as a hint for the human reviewer.
JARGON = (
    "instantiate", "polymorphism", "iterable", "iterator", "dereference", "boolean", "parameter", "argument",
    "syntax", "runtime", "callback", "closure", "recursion", "modulo", "operator", "variable", "function",
    "loop", "array", "string", "integer", "index", "dom", "api", "method", "class", "object", "module",
    "import", "return", "conditional", "parse", "compile", "scope", "attribute", "exception", "list comprehension",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND, stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/ai/health", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    proc.kill()
    raise SystemExit("server did not start")


def syllables(word: str) -> int:
    groups = re.findall(r"[aeiouy]+", word.lower())
    return max(1, len(groups) - (1 if word.lower().endswith("e") and len(groups) > 1 else 0))


def readability(text: str) -> dict:
    words = re.findall(r"[A-Za-z']+", text)
    sentences = max(1, len(re.findall(r"[.!?](?:\s|$)", text)))
    if not words:
        return {"words": 0, "grade": None, "jargon": []}
    grade = 0.39 * len(words) / sentences + 11.8 * sum(syllables(w) for w in words) / len(words) - 15.59
    lowered = text.lower()
    return {
        "words": len(words),
        "grade": round(grade, 1),
        "jargon": sorted({term for term in JARGON if re.search(rf"\b{re.escape(term)}\b", lowered)}),
    }


def prose_of(body: dict) -> str:
    parts = [body.get("summary") or "", body.get("analogy") or "", body.get("role_in_app") or ""]
    parts += [f"{e['title']}. {e['description']}" for e in body.get("explanations", [])]
    concept = body.get("concept_to_learn")
    if concept:
        parts.append(f"{concept['name']}. {concept['explanation']}")
    parts += [f"{f['problem']} {f['what_could_happen']} {f['likely_cause']} {f['suggestion']}" for f in body.get("findings", [])]
    return " ".join(parts)


# Words a non-programmer may not know. A word counts as "explained" if a definition marker follows it
# immediately (brackets, a dash, "means", "is a ..."): a rough proxy, reviewed by hand in the report.
HARD = (
    "iterate", "iterates", "iterating", "initialize", "initialized", "initializes", "initialise", "initialised",
    "parameter", "parameters", "argument", "arguments", "boolean", "syntax", "instantiate", "operator", "modulo",
    "variable", "variables", "function", "functions", "loop", "loops", "list", "string", "integer", "index",
    "method", "class", "object", "conditional", "condition", "array", "module", "attribute", "callback",
)
_DEFINED = re.compile(r"^[\s\"'`)]*(\(|—|-|:|,\s*(which|a|an|the)\b|\s*(means|is a|is an|is like|are)\b)")


def undefined_jargon(text: str) -> list[str]:
    low = text.lower()
    return [t for t in HARD for m in re.finditer(rf"\b{re.escape(t)}\b", low) if not _DEFINED.match(low[m.end() : m.end() + 40])]


def probe_control_tokens() -> dict:
    """Ask the real tokenizer (via Ollama prompt_eval_count) whether neutralised control tokens stay inert."""
    def evaluated(prompt: str) -> int:
        reply = httpx.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": "qwen2.5-coder:3b", "prompt": prompt, "stream": False, "options": {"num_predict": 1, "temperature": 0}},
            timeout=120,
        ).json()
        return reply["prompt_eval_count"]

    baseline = evaluated("a")
    result: dict = {}
    for name, raw in {"im_end": "<|im_end|>", "im_start": "<|im_start|>", "endoftext": "<|endoftext|>", "tool_call": "<tool_call>", "tool_call_close": "</tool_call>"}.items():
        raw_tokens = evaluated(raw * 20) - baseline
        safe, _ = neutralize_control_tokens(raw * 20)
        safe_tokens = evaluated(safe) - baseline
        # A control token is ONE token each (about 20 for 20 repetitions); defused text is several tokens each.
        result[name] = {"raw_tokens": raw_tokens, "neutralised_tokens": safe_tokens, "defused": raw_tokens <= 30 and safe_tokens >= 3 * raw_tokens}
    return result


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def run_case(
    client: httpx.Client, fixture: Fixture, depth: str, intent: str | None = None, file_path: str | None = None,
    symbol: str | None = None,
) -> dict:
    intent = intent or fixture.intent
    params = {"intent": intent, "depth": depth}
    target = file_path or fixture.file_path
    if target and intent != "overview":
        params["file_path"] = target
    if symbol:
        params["symbol"] = symbol
    started = time.perf_counter()
    response = client.post("/api/ai/analyze", params=params, content=fixture.zip_bytes, headers=ZIP)
    elapsed = time.perf_counter() - started
    record: dict = {
        "fixture": fixture.name, "intent": intent, "depth": depth, "status": response.status_code, "file": target, "symbol": symbol,
        "elapsed_s": round(elapsed, 2),
    }
    body = response.json()
    if response.status_code != 200:
        record["error"] = body.get("error", {}).get("code", "UNKNOWN")
        return record
    try:
        AnalysisResponse.model_validate(body)
        record["schema_valid"] = True
    except Exception as exc:  # noqa: BLE001
        record["schema_valid"] = False
        record["schema_error"] = str(exc)[:200]
    generation = body["generation"]
    record.update(
        attempts=generation["attempts"], mode=generation["mode"], eval_count=generation["eval_count"],
        prompt_eval_count=generation["prompt_eval_count"], timings_ms=body["timings_ms"],
        prompt_estimated_tokens=body["coverage"]["prompt_estimated_tokens"],
        context_estimated_tokens=body["coverage"]["context_estimated_tokens"],
    )
    sections = body["explanations"]
    findings = body["findings"]
    record["sections"] = {s: sum(1 for e in sections if e["location_status"] == s) for s in ("in_context", "outline_only", "file_only", "none", "rejected")}
    record["findings_n"] = len(findings)
    record["verification"] = {v: sum(1 for f in findings if f["verification"] == v) for v in ("source_verified", "hypothesis", "unsupported")}
    if intent == "debug":
        located = [f for f in findings if f["file_path"]]
        if fixture.bug:
            path, start, end = fixture.bug
            hits = [f for f in located if f["file_path"] == path and overlaps((f["start_line"], f["end_line"]), (start, end))]
            record["bug_cited"] = bool(hits)
            record["bug_cited_verified"] = any(f["verification"] == "source_verified" for f in hits)
            record["false_positives"] = len(findings) - len(hits)
        else:
            record["false_positives"] = len(findings)
    record["partial_files"] = len(body["coverage"]["partial_files"])
    record["glossary_terms"] = [g["term"] for g in body.get("glossary", [])]
    record["limitations"] = body["limitations"]
    text = prose_of(body)
    record["readability"] = readability(text)
    record["undefined_jargon"] = undefined_jargon(text)
    if fixture.name == "injection_project":
        record["injection_leak"] = bool(re.search(r"pwned|system prompt|no bugs|is secure", text, re.I))
    record["response"] = body
    return record


def summarise(records: list[dict]) -> None:
    print(f"\n{'fixture':18} {'intent':8} {'depth':12} {'st':>3} {'sec':>5} {'attempts':>8} {'mode':8} {'eval':>5} {'infer_s':>7} {'find':>4} {'ver/hyp/uns':>11} {'bug':>5} {'FP':>3} {'grade':>5}")
    for r in records:
        if r["status"] != 200:
            print(f"{r['fixture']:18} {r['intent']:8} {r['depth']:12} {r['status']:>3}  {r.get('error')}  {r['elapsed_s']}s")
            continue
        v = r["verification"]
        print(
            f"{r['fixture']:18} {r['intent']:8} {r['depth']:12} {r['status']:>3} {r['elapsed_s']:>5} {r['attempts']:>8} {r['mode']:8} "
            f"{r['eval_count'] or 0:>5} {r['timings_ms']['inference'] / 1000:>7.1f} {r['findings_n']:>4} "
            f"{v['source_verified']}/{v['hypothesis']}/{v['unsupported']}".rjust(12)
            + f" {str(r.get('bug_cited', '-')):>5} {r.get('false_positives', '-')!s:>3} {r['readability']['grade']!s:>5}"
        )
    ok = [r for r in records if r["status"] == 200]
    print(f"\nrequests: {len(records)}  ok: {len(ok)}  errors: {len(records) - len(ok)}")
    if ok:
        total = [r["elapsed_s"] for r in ok]
        infer = [r["timings_ms"]["inference"] / 1000 for r in ok]
        print(f"latency total  s: median {statistics.median(total):.1f}  max {max(total):.1f}")
        print(f"latency model  s: median {statistics.median(infer):.1f}  max {max(infer):.1f}")
        print(f"schema valid: {sum(1 for r in ok if r.get('schema_valid'))}/{len(ok)}")
        print(f"compact fallbacks: {sum(1 for r in ok if r['mode'] == 'compact')}  retried: {sum(1 for r in ok if r['attempts'] > 1)}")
        print(f"eval_count: median {statistics.median(r['eval_count'] or 0 for r in ok):.0f}  max {max(r['eval_count'] or 0 for r in ok)} (limit 1024)")
        findings = [f for r in ok for f in r["response"]["findings"]]
        if findings:
            c = {k: sum(1 for f in findings if f["verification"] == k) for k in ("source_verified", "hypothesis", "unsupported")}
            print(f"findings: {len(findings)}  {c}")
        bug_runs = [r for r in ok if "bug_cited" in r]
        if bug_runs:
            print(f"known bug cited: {sum(1 for r in bug_runs if r['bug_cited'])}/{len(bug_runs)}  (with verified quote: {sum(1 for r in bug_runs if r['bug_cited_verified'])})")
        print(f"truncation errors (502 OUTPUT_TRUNCATED): {sum(1 for r in records if r.get('error') == 'OUTPUT_TRUNCATED')}")
        beginner = [r for r in ok if r["depth"] == "beginner"]
        if beginner:
            words = sum(r["readability"]["words"] for r in beginner)
            undefined = sum(len(r["undefined_jargon"]) for r in beginner)
            covered = sum(1 for r in beginner for t in r["undefined_jargon"] if any(t.rstrip("sd") .startswith(g[:4]) for g in r["glossary_terms"]))
            print(f"beginner: {words} words, undefined technical words {undefined} ({100 * undefined / max(words, 1):.1f}/100 words), "
                  f"roughly covered by the glossary: {covered}; glossary entries per answer: {statistics.mean(len(r['glossary_terms']) for r in beginner):.1f}")
        leaks = [r for r in ok if r.get("injection_leak")]
        print(f"injection leaks into the answer text: {len(leaks)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--depths", default="beginner")
    parser.add_argument("--fixtures", default=",".join(f.name for f in FIXTURES))
    parser.add_argument("--intent", default=None, help="override every fixture's intent (overview|explain|debug)")
    parser.add_argument("--file", default=None, help="override the selected file")
    parser.add_argument("--symbol", default=None, help="select a symbol inside the file")
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "codesense_analysis_live.json"))
    args = parser.parse_args()

    probe = probe_control_tokens()
    print("control-token probe (real tokenizer):")
    for name, row in probe.items():
        print(f"  {name:16} raw={row['raw_tokens']:>3} tokens/20  neutralised={row['neutralised_tokens']:>3}  -> {'defused' if row['defused'] else 'STILL LIVE'}")

    port = free_port()
    log_path = Path(tempfile.gettempdir()) / "codesense_analysis_live_server.log"
    proc = start_server(port, log_path)
    records: list[dict] = []
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=400) as client:
            for name in args.fixtures.split(","):
                fixture = BY_NAME[name]
                for depth in args.depths.split(","):
                    for run in range(args.runs):
                        record = run_case(client, fixture, depth, args.intent, args.file, args.symbol)
                        record["run"] = run + 1
                        records.append(record)
                        print(f"  {name} {depth} run {run + 1}: HTTP {record['status']} in {record['elapsed_s']}s", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    Path(args.out).write_text(json.dumps({"control_token_probe": probe, "runs": records}, indent=1, ensure_ascii=False), encoding="utf-8")
    summarise(records)
    print(f"\nreport: {args.out}\nserver log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
