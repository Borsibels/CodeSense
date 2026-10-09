"""Scoring, capture and offline replay for the Phase 4.5 evaluation (no model needed to import it).

Used by ``check_analysis_live.py`` (live runs) and by ``tests/test_analysis_eval.py``.

Principles
----------
* Deterministic checks wherever possible; the model never grades itself.
* Valid JSON is not correctness, and a ``source_verified`` citation is not a confirmed bug: debug
  findings are scored against planted ground truth by LINE SPAN and by FUNCTION.
* Works on Phase 4 responses (no ``tier``/``debug_outcome``) and Phase 4.5 responses, so a Phase 4
  baseline and a Phase 4.5 run are measured by the same code.
* Recorded raw drafts can be replayed through the real ``CodeAnalysisService`` without calling the
  model (:class:`ReplayStructured`), so deterministic changes can be re-scored in seconds.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
for extra in (BACKEND, BACKEND / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from analysis_fixtures import BY_NAME, FIXTURES, Fixture  # noqa: E402
from app.config import ProjectSettings  # noqa: E402
from app.services import analysis_models  # noqa: E402
from app.services.ollama_service import OllamaGeneration  # noqa: E402
from app.services.project_models import ProjectAnalysis  # noqa: E402
from app.services.project_pipeline import analyze_project  # noqa: E402
from app.services.structured import StructuredResult  # noqa: E402
from project_fixtures import make_zip  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def heldout_fixtures() -> tuple[Fixture, ...]:
    from heldout_fixtures import HELDOUT_FIXTURES  # noqa: PLC0415

    return HELDOUT_FIXTURES


def find_fixture(name: str) -> Fixture:
    if name in BY_NAME:
        return BY_NAME[name]
    for fixture in heldout_fixtures():
        if fixture.name == name:
            return fixture
    raise KeyError(name)


def fixtures_for_split(split: str) -> tuple[Fixture, ...]:
    if split == "dev":
        return tuple(f for f in FIXTURES if f.split == "dev")
    if split == "heldout":
        return heldout_fixtures()
    if split == "all":
        return tuple(FIXTURES) + heldout_fixtures()
    raise ValueError(f"unknown split {split!r}")


def project_of(fixture: Fixture) -> ProjectAnalysis:
    return analyze_project(make_zip(fixture.files), ProjectSettings())


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def function_range(project: ProjectAnalysis, path: str, qualified_name: str | None) -> tuple[int, int] | None:
    """Line range of a function/method by qualified (or plain) name, from the Phase 3 symbols."""
    if not qualified_name or path not in project.analyses:
        return None
    symbols = project.analyses[path].symbols
    for key in ("qualified_name", "name"):
        for symbol in symbols:
            if getattr(symbol, key) == qualified_name and symbol.kind != "variable":
                return (symbol.start_line, symbol.end_line)
    return None


# --------------------------------------------------------------------------- #
# Debug scoring
# --------------------------------------------------------------------------- #
def score_debug(body: dict, fixture: Fixture, project: ProjectAnalysis | None = None) -> dict:
    """Score one debug response against the fixture's ground truth.

    Every finding is classified ``tp`` (overlaps the planted bug), ``acceptable`` (a real, unplanted
    observation listed in the fixture) or ``fp``. ``tier`` is read when present; a Phase 4 response
    has none, which is reported as ``untiered`` (the old behaviour: every finding is presented
    equally).
    """
    project = project or project_of(fixture)
    findings = body.get("findings", [])
    fn_range = function_range(project, fixture.bug[0], fixture.bug_function) if fixture.bug else None
    rows = []
    for finding in findings:
        located = bool(finding.get("file_path")) and finding.get("start_line")
        span = (finding["start_line"], finding["end_line"]) if located else None
        path = finding.get("file_path")
        hits_bug = bool(span and fixture.bug and path == fixture.bug[0] and overlaps(span, fixture.bug[1:]))
        in_function = bool(span and fn_range and path == fixture.bug[0] and overlaps(span, fn_range))
        acceptable = bool(span and any(path == a[0] and overlaps(span, a[1:]) for a in fixture.acceptable))
        rows.append(
            {
                "id": finding.get("id"),
                "tier": finding.get("tier", "untiered"),
                "reasons": finding.get("tier_reasons", []),
                "verification": finding.get("verification"),
                "category": finding.get("category"),
                "kind": "tp" if hits_bug else ("acceptable" if acceptable else "fp"),
                "in_bug_function": in_function,
                "span": list(span) if span else None,
            }
        )
    outcome = body.get("debug_outcome")  # None for Phase 4 responses
    possible = [r for r in rows if r["tier"] == "possible_problem"]
    score: dict[str, Any] = {
        "fixture": fixture.name,
        "has_bug": fixture.has_bug,
        "findings": rows,
        "n_findings": len(rows),
        "debug_outcome": outcome,
        # Phase 4 behaviour: any finding at all means the user is shown a problem.
        "untiered_reports_problem": bool(rows),
        # Phase 4.5 behaviour: the outcome field (falls back to "any finding" for a Phase 4 response).
        "reports_problem": (outcome == "possible_problems") if outcome else bool(rows),
        "bug_cited_any": any(r["kind"] == "tp" for r in rows),
        "bug_cited_possible_tier": any(r["kind"] == "tp" for r in possible),
        "bug_function_cited": any(r["in_bug_function"] or r["kind"] == "tp" for r in rows),
        "fp_total": sum(1 for r in rows if r["kind"] == "fp"),
        "fp_in_possible_tier": sum(1 for r in possible if r["kind"] == "fp"),
        "acceptable_total": sum(1 for r in rows if r["kind"] == "acceptable"),
        "pattern_checks": [
            {"rule": c.get("rule"), "strength": c.get("strength"), "lines": [c.get("start_line"), c.get("end_line")]}
            for c in body.get("pattern_checks", [])
        ],
    }
    if fixture.bug:
        hit_lines = [c for c in body.get("pattern_checks", []) if c.get("file_path") == fixture.bug[0]
                     and c.get("start_line") and overlaps((c["start_line"], c["end_line"]), fixture.bug[1:])]
        score["rule_hit_on_bug"] = bool(hit_lines)
    if fixture.ambiguous:
        score["overclaim"] = score["reports_problem"]
    if fixture.bug_hidden:
        score["claims_about_hidden_lines"] = sum(1 for r in rows if r["kind"] == "tp")
    return score


def aggregate_debug(scores: list[dict]) -> dict:
    """Roll per-run debug scores up into the headline numbers (clean runs and bug runs apart)."""
    clean = [s for s in scores if not s["has_bug"]]
    buggy = [s for s in scores if s["has_bug"]]
    out: dict[str, Any] = {"runs": len(scores), "clean_runs": len(clean), "bug_runs": len(buggy)}
    if clean:
        silent = sum(1 for s in clean if not s["reports_problem"])
        out.update(
            clean_no_clear_problem=silent,
            clean_no_clear_problem_rate=round(silent / len(clean), 3),
            clean_untiered_with_findings=sum(1 for s in clean if s["untiered_reports_problem"]),
            clean_nonbug_items_in_possible_tier_per_run=round(sum(s["fp_in_possible_tier"] for s in clean) / len(clean), 2),
            clean_findings_per_run=round(sum(s["n_findings"] for s in clean) / len(clean), 2),
        )
    if buggy:
        out.update(
            bug_cited_any=sum(1 for s in buggy if s["bug_cited_any"]),
            bug_cited_possible_tier=sum(1 for s in buggy if s["bug_cited_possible_tier"]),
            bug_function_cited=sum(1 for s in buggy if s["bug_function_cited"]),
            bug_rule_hit=sum(1 for s in buggy if s.get("rule_hit_on_bug")),
            buggy_outcome_possible_problems=sum(1 for s in buggy if s["reports_problem"]),
        )
    ambiguous = [s for s in scores if "overclaim" in s]
    if ambiguous:
        out["ambiguous_overclaims"] = f"{sum(1 for s in ambiguous if s['overclaim'])}/{len(ambiguous)}"
    return out


# --------------------------------------------------------------------------- #
# Beginner wording: the CORRECTED jargon metric
# --------------------------------------------------------------------------- #
# Phase 4 counted every word below as "jargon" and reported 6.6-8.9 undefined technical words per
# 100. 56 of 145 hits were the single word "list", which a non-programmer understands. The metric is
# split so the headline measures words that actually need a definition:
#   TRUE_JARGON         words with no everyday meaning that gives the reader the right idea.
#   EVERYDAY_TECHNICAL  ordinary words that also carry a technical sense; reported, not headlined.
TRUE_JARGON = (
    "iterate", "iterates", "iterated", "iterating", "iteration", "initialize", "initializes", "initialized",
    "initializing", "initialise", "initialised", "initialises", "initialising", "initialization",
    "parameter", "parameters", "argument", "arguments", "boolean", "booleans", "syntax", "instantiate",
    "instantiates", "instantiated", "operator", "operators", "modulo", "index", "indexes", "indices",
    "conditional", "conditionals", "callback", "callbacks", "integer", "integers", "array", "arrays",
    "module", "modules", "attribute", "attributes", "recursion", "recursive", "concatenate", "concatenation",
    "dereference", "iterable", "iterator", "polymorphism", "closure", "runtime", "variable", "variables",
    "function", "functions", "method", "methods", "class", "classes", "object", "objects", "string", "strings",
    "exception", "exceptions", "parse", "parses", "parsed", "parsing", "compile", "scope", "dictionary",
)
EVERYDAY_TECHNICAL = (
    "list", "lists", "loop", "loops", "return", "returns", "returned", "returning", "condition", "conditions",
)
# A word counts as "explained" when a definition marker follows it straight away: brackets, a dash, a
# colon, "means", "is a ...". A rough proxy, reviewed by hand in the report.
_DEFINED = re.compile(r"^[\s\"'`)]*(\(|—|-|:|,\s*(which|a|an|the)\b|\s*(means|is a|is an|is like|are)\b)")


def _undefined(text: str, words: Iterable[str]) -> list[str]:
    low = text.lower()
    hits = []
    for term in words:
        for match in re.finditer(rf"\b{re.escape(term)}\b", low):
            if not _DEFINED.match(low[match.end() : match.end() + 40]):
                hits.append(term)
    return hits


def jargon_report(text: str) -> dict:
    """Undefined technical words in ``text``: the corrected metric next to the old one."""
    words = len(re.findall(r"[A-Za-z']+", text))
    true_hits = _undefined(text, TRUE_JARGON)
    everyday_hits = _undefined(text, EVERYDAY_TECHNICAL)
    per100 = lambda n: round(100 * n / words, 2) if words else 0.0  # noqa: E731
    return {
        "words": words,
        "true_jargon": true_hits,
        "true_jargon_per_100_words": per100(len(true_hits)),
        "everyday_technical": everyday_hits,
        # What Phase 4 headlined (true jargon + everyday technical words together), kept for comparison.
        "phase4_style_per_100_words": per100(len(true_hits) + len(everyday_hits)),
    }


# --------------------------------------------------------------------------- #
# Explanation accuracy checks (deterministic proxies; the human rubric covers the rest)
# --------------------------------------------------------------------------- #
_BACKTICKED = re.compile(r"`([A-Za-z_][\w.]*)`")
_CODE_WORD = re.compile(r"\b([a-z]+(?:_[a-z0-9]+)+|[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b")
_SPECULATIVE_ROLE = re.compile(
    r"\b(could be used|might be used|may be used|can be used|such as|various|different (?:tasks|applications|programs)|"
    r"a (?:wide )?variety|data (?:analysis|filtering)|statistical|games?)\b",
    re.I,
)


def prose_fields(body: dict) -> dict[str, str]:
    fields = {"summary": body.get("summary") or "", "role_in_app": body.get("role_in_app") or ""}
    fields["sections"] = " ".join(f"{e['title']}. {e['description']}" for e in body.get("explanations", []))
    concept = body.get("concept_to_learn")
    fields["concept"] = f"{concept['name']}. {concept['explanation']}" if concept else ""
    return fields


def all_prose(body: dict) -> str:
    parts = [body.get("summary") or "", body.get("analogy") or "", body.get("role_in_app") or ""]
    parts += [f"{e['title']}. {e['description']}" for e in body.get("explanations", [])]
    concept = body.get("concept_to_learn")
    if concept:
        parts.append(f"{concept['name']}. {concept['explanation']}")
    parts += [f"{f['problem']} {f['what_could_happen']} {f['likely_cause']} {f['suggestion']}" for f in body.get("findings", [])]
    return " ".join(parts)


def _project_text(project: ProjectAnalysis) -> str:
    return "\n".join(f.text for f in project.files) + "\n" + "\n".join(f.path for f in project.files)


def invented_identifiers(body: dict, project: ProjectAnalysis) -> list[str]:
    """Code-looking names in the AI's prose that appear nowhere in the uploaded project."""
    prose = " ".join(prose_fields(body).values())
    source = _project_text(project)
    names = {m.group(1) for m in _BACKTICKED.finditer(prose)} | {m.group(1) for m in _CODE_WORD.finditer(prose)}
    out = []
    for name in sorted(names):
        parts = [p for p in name.split(".") if p]
        if parts and all(re.search(rf"\b{re.escape(p)}\b", source) for p in parts):
            continue
        out.append(name)
    return out


def role_checks(body: dict, project: ProjectAnalysis, target: str | None) -> dict:
    """Is ``role_in_app`` grounded in the dependency graph, or generic / about unrelated files?"""
    role = body.get("role_in_app") or ""
    result: dict[str, Any] = {"role_present": bool(role)}
    if not role:
        return result
    neighbours = set(project.graph.dependencies.get(target or "", ())) | set(project.graph.dependents.get(target or "", ()))
    mentioned = []
    for file in project.files:
        stem = file.path.rsplit("/", 1)[-1]
        if re.search(rf"\b{re.escape(stem)}\b", role) or re.search(rf"\b{re.escape(stem.rsplit('.', 1)[0])}\b", role) and stem.rsplit(".", 1)[0] not in ("index", "main"):
            mentioned.append(file.path)
    result["files_mentioned"] = mentioned
    result["mentions_non_neighbour"] = [p for p in mentioned if p != target and p not in neighbours]
    result["speculative_phrases"] = sorted({m.group(0).lower() for m in _SPECULATIVE_ROLE.finditer(role)})
    # "Generic" = says nothing about this project's files or symbols and leans on speculation.
    names = {s.name for a in project.analyses.values() for s in a.symbols if s.kind != "variable"}
    mentions_symbol = any(re.search(rf"\b{re.escape(n)}\b", role) for n in names)
    result["generic"] = bool(result["speculative_phrases"]) and not mentioned and not mentions_symbol
    # "Degenerate": the model echoed the context's data lines instead of writing a sentence (Phase 4.5 explain-v2 failure
    # mode: `depends on: None, is used by: None`, or just a file name). Not speculative, but no use to a beginner.
    stripped = role.strip().strip(".").strip()
    words = re.findall(r"[A-Za-z']+", stripped)
    is_bare_path = bool(re.fullmatch(r"[\w./-]+\.\w+(?:\s*,\s*[\w./-]+\.\w+)*", stripped))
    echoes_lines = bool(re.match(r"^(?:depends on|is used by)\s*:", stripped, re.I))
    result["degenerate"] = echoes_lines or is_bare_path or len(words) < 5
    result["useful"] = not result["generic"] and not result["degenerate"]
    return result


_OWNERSHIP = (
    re.compile(r"`?(\w+)`?\s+(?:method|function)\s+(?:of|in|on|inside)\s+(?:the\s+)?(?:class\s+)?`?(\w+)`?", re.I),
    re.compile(r"(?:class\s+)?`?(\w+)`?\s+(?:class\s+)?(?:has|contains|defines)\s+(?:a\s+|an\s+|the\s+)?(?:method|function)\s+`?(\w+)`?", re.I),
)


def relationship_contradictions(body: dict, project: ProjectAnalysis) -> list[str]:
    """Explicit ownership claims ("X method of Y") that the parser's symbols contradict.

    Catches only explicit phrasing. Everything subtler (a summary crediting one file with another's
    behaviour) needs the human rubric.
    """
    prose = " ".join(prose_fields(body).values())
    symbols = [s for a in project.analyses.values() for s in a.symbols if s.kind != "variable"]
    classes = {s.name for s in symbols if s.kind == "class"}
    by_name: dict[str, list] = {}
    for s in symbols:
        by_name.setdefault(s.name, []).append(s)
    problems: list[str] = []
    for pattern, order in ((_OWNERSHIP[0], (0, 1)), (_OWNERSHIP[1], (1, 0))):
        for match in pattern.finditer(prose):
            member, owner = match.group(order[0] + 1), match.group(order[1] + 1)
            if owner not in classes or member not in by_name:
                continue
            parents = {s.parent.split(".")[-1] if s.parent else None for s in by_name[member]}
            if owner not in parents:
                problems.append(f"{member} is not a member of {owner} (parser: {sorted(p or 'top-level' for p in parents)})")
    return problems


def glossary_coverage(jargon: dict, body: dict) -> int:
    """How many undefined true-jargon hits the response's own glossary defines (the reader's real experience)."""
    from app.services.glossary import find_terms  # noqa: PLC0415

    shown = {g["term"] for g in body.get("glossary", [])}
    return sum(1 for term in jargon["true_jargon"] if {t for t, _ in find_terms([term])} & shown)


def score_explain(body: dict, fixture: Fixture, project: ProjectAnalysis | None = None) -> dict:
    project = project or project_of(fixture)
    text = all_prose(body)
    target = body.get("target_file")
    jargon = jargon_report(text)
    jargon["covered_by_glossary"] = glossary_coverage(jargon, body)
    return {
        "fixture": fixture.name,
        "jargon": jargon,
        "invented_identifiers": invented_identifiers(body, project),
        "role": role_checks(body, project, target),
        "relationship_contradictions": relationship_contradictions(body, project),
        "analogy_present": bool(body.get("analogy")),
        "relationships_returned": len(body.get("relationships", [])),
    }


def aggregate_explain(scores: list[dict]) -> dict:
    if not scores:
        return {}
    words = sum(s["jargon"]["words"] for s in scores)
    true_n = sum(len(s["jargon"]["true_jargon"]) for s in scores)
    every_n = sum(len(s["jargon"]["everyday_technical"]) for s in scores)
    roles = [s["role"] for s in scores if s["role"].get("role_present")]
    return {
        "answers": len(scores),
        "words": words,
        "true_jargon_per_100_words": round(100 * true_n / max(words, 1), 2),
        "everyday_technical_per_100_words": round(100 * every_n / max(words, 1), 2),
        "phase4_style_per_100_words": round(100 * (true_n + every_n) / max(words, 1), 2),
        "invented_identifiers_per_answer": round(sum(len(s["invented_identifiers"]) for s in scores) / len(scores), 2),
        "relationship_contradictions": sum(len(s["relationship_contradictions"]) for s in scores),
        "generic_role_answers": f"{sum(1 for r in roles if r.get('generic'))}/{len(roles)}",
        "degenerate_role_answers": f"{sum(1 for r in roles if r.get('degenerate'))}/{len(roles)}",
        "useful_role_answers": f"{sum(1 for r in roles if r.get('useful'))}/{len(roles)}",
        "true_jargon_covered_by_glossary": f"{sum(s['jargon'].get('covered_by_glossary', 0) for s in scores)}/{sum(len(s['jargon']['true_jargon']) for s in scores)}",
        "role_mentions_non_neighbour_files": f"{sum(1 for r in roles if r.get('mentions_non_neighbour'))}/{len(roles)}",
        "analogies": sum(1 for s in scores if s["analogy_present"]),
    }


# --------------------------------------------------------------------------- #
# Capture and replay of raw model drafts (kept OUTSIDE the repo; the API never returns them)
# --------------------------------------------------------------------------- #
def fixture_fingerprint(fixture: Fixture) -> str:
    payload = json.dumps({p: fixture.files[p] for p in sorted(fixture.files)}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class CaptureStructured:
    """Wraps ``StructuredGenerator.generate`` and remembers every successful draft."""

    def __init__(self, inner_generate) -> None:
        self._inner = inner_generate
        self.captured: list[dict] = []

    async def __call__(self, prompt, schema_model, *, max_retries=None):
        result = await self._inner(prompt, schema_model, max_retries=max_retries)
        self.captured.append(
            {
                "type": type(result.value).__name__,
                "value": result.value.model_dump(mode="json"),
                "attempts": result.attempts,
                "prompt_chars": len(prompt),
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            }
        )
        return result

    def drain(self) -> list[dict]:
        out, self.captured = self.captured, []
        return out


class ReplayStructured:
    """Stands in for the generator and returns recorded drafts, so the REAL service re-runs offline."""

    def __init__(self, drafts: list, model_name: str = "qwen2.5-coder:3b") -> None:
        self.drafts = list(drafts)
        self.max_retries = 1
        self.calls = 0
        self.ollama = SimpleNamespace(settings=SimpleNamespace(model=model_name))

    async def generate(self, prompt, schema_model, *, max_retries=None):
        self.calls += 1
        value = self.drafts.pop(0)
        generation = OllamaGeneration(text="{}", done_reason="stop", truncated=False, prompt_eval_count=None, eval_count=None)
        return StructuredResult(value=value, attempts=1, generation=generation)


@asynccontextmanager
async def _null_slot():
    yield


def draft_from_record(draft: dict):
    model = getattr(analysis_models, draft["type"])
    return model.model_validate(draft["value"])


async def replay_response(fixture: Fixture, draft: dict, *, depth: str = "beginner", intent: str | None = None) -> dict:
    """Run the production ``CodeAnalysisService`` on a recorded draft; return the response as JSON."""
    from app.services import TokenBudget  # noqa: PLC0415
    from app.services.code_analysis_service import AnalysisRequest, CodeAnalysisService  # noqa: PLC0415

    settings = ProjectSettings()
    service = CodeAnalysisService(
        ReplayStructured([draft_from_record(draft)]),
        TokenBudget(4096, 1024, 256),
        reserve=settings.instruction_reserve_tokens,
        inference_slot=_null_slot,
        nonce_factory=lambda: "0123456789ab",
    )
    request = AnalysisRequest(intent or fixture.intent, depth, fixture.file_path, None)  # type: ignore[arg-type]
    response = await service.analyze(project_of(fixture), request)
    return response.model_dump(mode="json", by_alias=True)  # the same keys the HTTP API returns (`from`, not `from_`)


# --------------------------------------------------------------------------- #
# Resource sampling (Windows-friendly, no extra dependencies)
# --------------------------------------------------------------------------- #
def vram_used_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip().splitlines()
        return int(out[0]) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def rss_mib(pid: int) -> float | None:
    """Working set of a process in MiB (``tasklist`` on Windows, ``/proc`` elsewhere)."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5, check=False
            ).stdout.strip()
            if not out or out.startswith("INFO"):
                return None
            digits = re.sub(r"[^\d]", "", out.rsplit('","', 1)[-1])
            return int(digits) / 1024 if digits else None
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


@dataclass
class ResourceSampler:
    """Polls peak VRAM (and optionally one process's RSS) on a background thread."""

    pid: int | None = None
    interval: float = 0.5

    def __post_init__(self) -> None:
        self.peak_vram: int | None = None
        self.baseline_vram: int | None = vram_used_mib()
        self.peak_rss: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            vram = vram_used_mib()
            if vram is not None:
                self.peak_vram = vram if self.peak_vram is None else max(self.peak_vram, vram)
            if self.pid:
                rss = rss_mib(self.pid)
                if rss is not None:
                    self.peak_rss = rss if self.peak_rss is None else max(self.peak_rss, rss)
            self._stop.wait(self.interval)

    def __enter__(self) -> "ResourceSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=3)


# --------------------------------------------------------------------------- #
# Blinded human-review sheet (variant hidden, answers shuffled)
# --------------------------------------------------------------------------- #
RUBRIC = (
    "factual accuracy about what the code does",
    "relationship accuracy (which function/class/file does what)",
    "plain language: terms defined or avoided",
    "correctness of any example",
    "honest uncertainty about the role and about code that was not shown",
)


def write_review_sheet(answers: list[dict], sheet_path: Path, key_path: Path, seed: int = 45) -> None:
    """``answers``: dicts with ``variant``, ``fixture`` and ``response``. The sheet hides the variant."""
    rng = random.Random(seed)
    order = list(range(len(answers)))
    rng.shuffle(order)
    sheet, key = [], {}
    for position, index in enumerate(order, start=1):
        answer = answers[index]
        rid = f"A{position:03d}"
        key[rid] = {"variant": answer["variant"], "fixture": answer["fixture"]}
        body = answer["response"]
        sheet.append(
            {
                "id": rid,
                "task": answer["fixture"],
                "summary": body.get("summary"),
                "sections": [f"{e['title']}: {e['description']}" for e in body.get("explanations", [])],
                "role_in_app": body.get("role_in_app"),
                "concept": body.get("concept_to_learn"),
                "scores_0_to_2": {criterion: None for criterion in RUBRIC},
            }
        )
    sheet_path.write_text(json.dumps(sheet, indent=1, ensure_ascii=False), encoding="utf-8")
    key_path.write_text(json.dumps(key, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Small helpers shared by the CLI
# --------------------------------------------------------------------------- #
def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def latency_summary(values: list[float]) -> dict:
    if not values:
        return {}
    return {
        "median": round(statistics.median(values), 2),
        "p90": round(percentile(values, 90), 2),
        "max": round(max(values), 2),
        "n": len(values),
    }


def now() -> float:
    return time.perf_counter()
