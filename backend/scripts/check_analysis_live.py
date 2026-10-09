"""Live evaluation of POST /api/ai/analyze against the real local model (Ollama + qwen2.5-coder:3b).

Two ways to run the SAME application code against the SAME fixtures:

``--mode http`` (default)
    Starts the real FastAPI app in a uvicorn subprocess on a loopback port and sends real HTTP
    requests. Use it for latency and the end-to-end structural check.

``--mode inprocess``
    Runs the real app in this process (Starlette ``TestClient``: lifespan, middleware, ZIP ingestion
    and the Ollama client are all the production code) and CAPTURES the model's raw draft for every
    request. The API never returns raw drafts; capturing them here lets deterministic changes
    (triage, pattern rules, glossary) be re-scored offline with ``--rescore`` without calling the
    model again.

    python backend\\scripts\\check_analysis_live.py --mode inprocess --split dev --intent debug --runs 5 --out dev_debug.json
    python backend\\scripts\\check_analysis_live.py --rescore dev_debug.json
    python backend\\scripts\\check_analysis_live.py --rescore-baseline fp_results.json   # Phase 4 V0 drafts
    python backend\\scripts\\check_analysis_live.py --split heldout --final --mode inprocess --runs 5

Fixture splits: ``dev`` (the heuristics were tuned looking at these: in-sample), ``heldout`` (sealed,
see tests/heldout_fixtures.py; needs ``--final``, verifies the seal, and is logged in a ledger so a
second run is visible). Everything is measured by deterministic checks against planted ground truth;
explanation quality additionally needs the blinded human review sheet (``--review-sheet``).

Reports are written outside the repo (the system temp directory by default). Exit code 0 if the runs
completed (accuracy is reported, not asserted), 1 if the harness failed.
"""

from __future__ import annotations

import argparse
import asyncio
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import analysis_eval as ev  # noqa: E402
from analysis_fixtures import Fixture  # noqa: E402
from app.services.analysis_models import AnalysisResponse  # noqa: E402
from app.services.analysis_prompts import neutralize_control_tokens  # noqa: E402

ZIP = {"Content-Type": "application/zip"}
MANIFEST = BACKEND / "tests" / "data" / "heldout_manifest.json"


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
        return {"words": 0, "grade": None}
    grade = 0.39 * len(words) / sentences + 11.8 * sum(syllables(w) for w in words) / len(words) - 15.59
    return {"words": len(words), "grade": round(grade, 1)}


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


# --------------------------------------------------------------------------- #
# One request
# --------------------------------------------------------------------------- #
_PROJECTS: dict[str, object] = {}


def project_for(fixture: Fixture):
    if fixture.name not in _PROJECTS:
        _PROJECTS[fixture.name] = ev.project_of(fixture)
    return _PROJECTS[fixture.name]


def run_case(
    client: httpx.Client, fixture: Fixture, depth: str, intent: str | None = None, file_path: str | None = None,
    symbol: str | None = None, capture: ev.CaptureStructured | None = None, server_pid: int | None = None,
) -> dict:
    intent = intent or fixture.intent
    params = {"intent": intent, "depth": depth}
    target = file_path or fixture.file_path
    if target and intent != "overview":
        params["file_path"] = target
    if symbol:
        params["symbol"] = symbol
    with ev.ResourceSampler(pid=server_pid) as sampler:
        started = time.perf_counter()
        response = client.post("/api/ai/analyze", params=params, content=fixture.zip_bytes, headers=ZIP)
        elapsed = time.perf_counter() - started
    record: dict = {
        "fixture": fixture.name, "split": fixture.split, "category": fixture.category, "intent": intent, "depth": depth,
        "status": response.status_code, "file": target, "symbol": symbol, "elapsed_s": round(elapsed, 2),
        "fixture_fingerprint": ev.fixture_fingerprint(fixture),
        "vram_peak_mib": sampler.peak_vram, "vram_before_mib": sampler.baseline_vram,
        "rss_peak_mib": round(sampler.peak_rss, 1) if sampler.peak_rss else None,
    }
    if capture is not None:
        record["drafts"] = capture.drain()
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
        prompt_eval_count=generation["prompt_eval_count"], prompt_version=generation["prompt_version"],
        timings_ms=body["timings_ms"],
        prompt_estimated_tokens=body["coverage"]["prompt_estimated_tokens"],
        context_estimated_tokens=body["coverage"]["context_estimated_tokens"],
    )
    sections = body["explanations"]
    findings = body["findings"]
    record["sections"] = {s: sum(1 for e in sections if e["location_status"] == s) for s in ("in_context", "outline_only", "file_only", "none", "rejected")}
    record["findings_n"] = len(findings)
    record["verification"] = {v: sum(1 for f in findings if f["verification"] == v) for v in ("source_verified", "hypothesis", "unsupported")}
    project = project_for(fixture)
    if intent == "debug":
        record["debug_score"] = ev.score_debug(body, fixture, project)
    else:
        record["explain_score"] = ev.score_explain(body, fixture, project)
    record["partial_files"] = len(body["coverage"]["partial_files"])
    record["glossary_terms"] = [g["term"] for g in body.get("glossary", [])]
    record["limitations"] = body["limitations"]
    record["readability"] = readability(ev.all_prose(body))
    if fixture.name in ("injection_project", "clean_injection"):
        record["injection_leak"] = bool(re.search(r"pwned|system prompt|no bugs|is secure", ev.all_prose(body), re.I))
    record["response"] = body
    return record


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def summarise(records: list[dict], title: str = "") -> None:
    if title:
        print(f"\n===== {title} =====")
    print(f"\n{'fixture':18} {'intent':8} {'depth':12} {'st':>3} {'s':>5} {'att':>3} {'mode':8} {'eval':>5} {'find':>4} {'v/h/u':>7} {'outcome':16} {'bug':>5} {'FP':>3}")
    for r in records:
        if r["status"] != 200:
            print(f"{r['fixture']:18} {r['intent']:8} {r['depth']:12} {r['status']:>3}  {r.get('error')}  {r['elapsed_s']}s")
            continue
        v = r["verification"]
        score = r.get("debug_score") or {}
        outcome = score.get("debug_outcome") or ("(phase 4)" if r["intent"] == "debug" else "")
        print(
            f"{r['fixture']:18} {r['intent']:8} {r['depth']:12} {r['status']:>3} {r['elapsed_s']:>5} {r['attempts']:>3} {r['mode']:8} "
            f"{r['eval_count'] or 0:>5} {r['findings_n']:>4} {v['source_verified']}/{v['hypothesis']}/{v['unsupported']}".ljust(0)
            + f" {outcome:16} {str(score.get('bug_cited_any', '-')):>5} {score.get('fp_total', '-')!s:>3}"
        )
    ok = [r for r in records if r["status"] == 200]
    print(f"\nrequests: {len(records)}  ok: {len(ok)}  errors: {len(records) - len(ok)}")
    if not ok:
        return
    for intent, depth in sorted({(r["intent"], r["depth"]) for r in ok}):
        group = [r for r in ok if (r["intent"], r["depth"]) == (intent, depth)]
        total, infer = [r["elapsed_s"] for r in group], [r["timings_ms"]["inference"] / 1000 for r in group]
        print(f"latency {intent}/{depth}: total {ev.latency_summary(total)}  model-only median {statistics.median(infer):.1f}s")
    print(f"schema valid: {sum(1 for r in ok if r.get('schema_valid'))}/{len(ok)}")
    print(f"compact fallbacks: {sum(1 for r in ok if r['mode'] == 'compact')}  retried: {sum(1 for r in ok if r['attempts'] > 1)}  "
          f"truncation errors: {sum(1 for r in records if r.get('error') == 'OUTPUT_TRUNCATED')}")
    print(f"eval_count: median {statistics.median(r['eval_count'] or 0 for r in ok):.0f}  max {max(r['eval_count'] or 0 for r in ok)} (limit 1024)")
    print(f"prompt versions: {sorted({r.get('prompt_version') for r in ok})}")
    vram = [r["vram_peak_mib"] for r in ok if r.get("vram_peak_mib")]
    rss = [r["rss_peak_mib"] for r in ok if r.get("rss_peak_mib")]
    if vram:
        print(f"peak VRAM during requests: max {max(vram)} MiB (idle before: {records[0].get('vram_before_mib')} MiB)")
    if rss:
        print(f"backend RSS peak: {max(rss):.0f} MiB")
    findings = [f for r in ok for f in r["response"]["findings"]]
    if findings:
        c = {k: sum(1 for f in findings if f["verification"] == k) for k in ("source_verified", "hypothesis", "unsupported")}
        print(f"findings: {len(findings)}  {c}")
    debug_scores = [r["debug_score"] for r in ok if "debug_score" in r]
    if debug_scores:
        print("DEBUG:", json.dumps(ev.aggregate_debug(debug_scores)))
    explain_scores = [r["explain_score"] for r in ok if "explain_score" in r and r["depth"] == "beginner"]
    if explain_scores:
        print("EXPLAIN (beginner):", json.dumps(ev.aggregate_explain(explain_scores)))
    leaks = [r for r in ok if r.get("injection_leak")]
    print(f"injection leaks into the answer text: {len(leaks)}")


# --------------------------------------------------------------------------- #
# Offline re-scoring of recorded drafts (no model)
# --------------------------------------------------------------------------- #
def rescore_records(records: list[dict], label: str) -> list[dict]:
    """Replay each recorded draft through the CURRENT service code and score the new response."""
    out = []
    for r in records:
        drafts = r.get("drafts") or []
        if not drafts or r.get("status") not in (None, 200):
            continue
        fixture = ev.find_fixture(r["fixture"])
        if r.get("fixture_fingerprint") and r["fixture_fingerprint"] != ev.fixture_fingerprint(fixture):
            print(f"  skipped {r['fixture']}: the fixture changed since this run was recorded")
            continue
        body = asyncio.run(ev.replay_response(fixture, drafts[-1], depth=r.get("depth", "beginner"), intent=r.get("intent")))
        row = {"fixture": fixture.name, "split": fixture.split, "intent": body["intent"], "depth": body["depth"], "status": 200,
               "elapsed_s": 0.0, "response": body}
        if body["intent"] == "debug":
            row["debug_score"] = ev.score_debug(body, fixture, project_for(fixture))
        else:
            row["explain_score"] = ev.score_explain(body, fixture, project_for(fixture))
        out.append(row)
    print(f"\n{label}: re-scored {len(out)} recorded drafts through the current service code (no model calls)")
    return out


def load_baseline_v0(path: Path, variant: str = "V0_current") -> list[dict]:
    """The Phase 4.5 planning experiment's raw V0 drafts, as replayable records."""
    mapping = {"clean_names_js*": "clean_names_js", "discount_py*": "discount_py", "adult_js*": "adult_js"}
    records = []
    for r in json.loads(path.read_text(encoding="utf-8")):
        if r.get("variant") != variant or "raw" not in r:
            continue
        name = mapping.get(r["case"], r["case"])
        records.append({"fixture": name, "intent": "debug", "depth": "beginner", "status": 200,
                        "drafts": [{"type": "DebugDraft", "value": r["raw"]}]})
    return records


def print_split_table(rows: list[dict]) -> None:
    by_fixture: dict[str, list[dict]] = {}
    for r in rows:
        if "debug_score" in r:
            by_fixture.setdefault(r["fixture"], []).append(r["debug_score"])
    print(f"\n{'fixture':20} {'runs':>4} {'has_bug':>7} {'untiered findings/run':>22} {'reports problem':>16} {'bug any':>8} {'bug possible':>12} {'FP in possible':>14}")
    for name, scores in by_fixture.items():
        n = len(scores)
        print(f"{name:20} {n:>4} {str(scores[0]['has_bug']):>7} {sum(s['n_findings'] for s in scores) / n:>22.2f} "
              f"{sum(1 for s in scores if s['reports_problem']):>10}/{n:<5} {sum(1 for s in scores if s['bug_cited_any']):>6}/{n:<1} "
              f"{sum(1 for s in scores if s['bug_cited_possible_tier']):>10}/{n:<1} {sum(s['fp_in_possible_tier'] for s in scores):>14}")
    print("TOTAL:", json.dumps(ev.aggregate_debug([s for scores in by_fixture.values() for s in scores])))


# --------------------------------------------------------------------------- #
# Held-out seal and ledger
# --------------------------------------------------------------------------- #
def check_seal() -> None:
    from heldout_fixtures import manifest  # noqa: PLC0415

    sealed = json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]
    if manifest() != sealed:
        raise SystemExit("The held-out fixtures no longer match tests/data/heldout_manifest.json: refusing to run a broken seal.")


def record_heldout_run(ledger: Path, args: argparse.Namespace) -> int:
    entries = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else []
    entries.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "split": args.split, "runs": args.runs, "intent": args.intent, "mode": args.mode})
    ledger.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    return len(entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--depths", default="beginner")
    parser.add_argument("--mode", choices=("http", "inprocess"), default="http")
    parser.add_argument("--split", choices=("dev", "heldout", "all"), default="dev")
    parser.add_argument("--final", action="store_true", help="required to run the sealed held-out fixtures (once, at the end)")
    parser.add_argument("--ledger", default=str(Path(tempfile.gettempdir()) / "codesense_heldout_ledger.json"))
    parser.add_argument("--fixtures", default=None, help="comma-separated fixture names (default: every fixture in --split)")
    parser.add_argument("--intent", default=None, help="only run fixtures with this intent, or force it (see --force-intent)")
    parser.add_argument("--force-intent", action="store_true", help="run every selected fixture with --intent instead of filtering")
    parser.add_argument("--file", default=None, help="override the selected file")
    parser.add_argument("--symbol", default=None, help="select a symbol inside the file")
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "codesense_analysis_live.json"))
    parser.add_argument("--rescore", default=None, help="re-score a recorded --mode inprocess report offline (no model)")
    parser.add_argument("--rescore-baseline", default=None, help="re-score the planning experiment's V0 drafts (fp_results.json)")
    parser.add_argument("--review-sheet", default=None, help="write a blinded human-review sheet for explain answers to this path")
    parser.add_argument("--skip-probe", action="store_true", help="skip the control-token tokenizer probe")
    args = parser.parse_args()

    if args.rescore or args.rescore_baseline:
        if args.rescore:
            records = json.loads(Path(args.rescore).read_text(encoding="utf-8"))["runs"]
        else:
            records = load_baseline_v0(Path(args.rescore_baseline))
        rows = rescore_records(records, "rescore")
        print_split_table(rows)
        return 0

    if args.split in ("heldout", "all"):
        if not args.final:
            raise SystemExit("The held-out set is sealed: pass --final to run it (once, at the end).")
        check_seal()
        count = record_heldout_run(Path(args.ledger), args)
        print(f"HELD-OUT RUN #{count} recorded in {args.ledger}" + ("  (!! not the first: results may be tuned against)" if count > 1 else ""))

    if args.fixtures:
        fixtures = [ev.find_fixture(n) for n in args.fixtures.split(",")]
    else:
        fixtures = list(ev.fixtures_for_split(args.split))
    if args.intent and not args.force_intent:
        fixtures = [f for f in fixtures if f.intent == args.intent]

    if not args.skip_probe:
        probe = probe_control_tokens()
        print("control-token probe (real tokenizer):")
        for name, row in probe.items():
            print(f"  {name:16} raw={row['raw_tokens']:>3} tokens/20  neutralised={row['neutralised_tokens']:>3}  -> {'defused' if row['defused'] else 'STILL LIVE'}")
    else:
        probe = {}

    records: list[dict] = []
    proc = None
    test_client = None
    capture = None
    server_pid = None
    log_path = Path(tempfile.gettempdir()) / "codesense_analysis_live_server.log"
    try:
        if args.mode == "http":
            port = free_port()
            proc = start_server(port, log_path)
            server_pid = proc.pid
            client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=400)
        else:
            from fastapi.testclient import TestClient  # noqa: PLC0415

            from app.main import create_app  # noqa: PLC0415

            app = create_app()
            test_client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
            test_client.__enter__()  # runs the application lifespan
            capture = ev.CaptureStructured(app.state.structured.generate)
            app.state.structured.generate = capture
            client = test_client
            import os  # noqa: PLC0415

            server_pid = os.getpid()
        for fixture in fixtures:
            intent = args.intent if (args.intent and args.force_intent) else fixture.intent
            for depth in args.depths.split(","):
                for run in range(args.runs):
                    record = run_case(client, fixture, depth, intent, args.file, args.symbol, capture, server_pid)
                    record["run"] = run + 1
                    records.append(record)
                    print(f"  {fixture.name} {intent} {depth} run {run + 1}: HTTP {record['status']} in {record['elapsed_s']}s", flush=True)
    finally:
        if test_client is not None:
            test_client.__exit__(None, None, None)
        elif args.mode == "http":
            client.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    Path(args.out).write_text(
        json.dumps({"control_token_probe": probe, "args": vars(args), "runs": records}, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    summarise(records, f"{args.split} / {args.mode}")
    print_split_table(records)
    if args.review_sheet:
        answers = [{"variant": r.get("prompt_version", "?"), "fixture": r["fixture"], "response": r["response"]}
                   for r in records if r["status"] == 200 and r["intent"] != "debug" and r["depth"] == "beginner"]
        sheet = Path(args.review_sheet)
        ev.write_review_sheet(answers, sheet, sheet.with_suffix(".key.json"))
        print(f"review sheet: {sheet} (key: {sheet.with_suffix('.key.json')})")
    print(f"\nreport: {args.out}\nserver log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
