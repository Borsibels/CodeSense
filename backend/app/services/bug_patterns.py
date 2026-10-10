"""Deterministic bug-pattern rules for the file (or symbol) being debugged.

Why this exists
---------------
The 3B model both misses real bugs and invents false ones, and nothing in the backend judges whether a
claim is true. A few classic mistakes CAN be recognised exactly from structure, so they are checked
here with the standard-library ``ast`` module (Python) and the Phase 3 token stream (JavaScript), never
with a text or regular-expression search over the source: a pattern inside a string or comment can never
match, and every rule states what it needs to be true.

What a hit means
----------------
A hit is NOT proof of a bug. Every hit carries

* a ``strength``:
    ``problem_if_assumptions_hold``  the code is a bug *provided the listed assumptions are true*;
    ``worth_checking``               the code is suspicious but depends on what the author intended;
* its ``assumptions``, in plain words, shown to the user next to the hit.

Nothing is executed, imported or evaluated. Rules only read the target file (or the selected
symbol's lines); results never enter the model prompt, so the debug prompt stays exactly what was
measured, and they never mix into the AI ``findings`` list.

Precision gate
--------------
Each rule has positive tests, negative tests (guarded or idiomatic variants that must stay silent) and
is run over the clean fixtures and this backend's own code, where it must fire zero times
(``tests/test_bug_patterns.py``). A rule that cannot be made safe is not added: intent-dependent bugs
(a min/max mix-up, a flipped comparison with no comment to compare against) are deliberately NOT here.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from app.services.js_analysis import Token, match_brackets, tokenize
from app.services.project_models import ProjectAnalysis

logger = logging.getLogger(__name__)

Strength = Literal["problem_if_assumptions_hold", "worth_checking"]
Range = tuple[int, int]

MAX_HITS_PER_RULE = 5
# Work bound: a rule examines at most this many candidate loops per file, so a freak file with thousands of
# look-alike loops cannot make a request slow. Ordinary files have a handful.
MAX_CANDIDATES = 300
_MUTATORS = frozenset(
    {"append", "extend", "insert", "add", "update", "setdefault", "pop", "popitem", "remove", "discard", "clear", "sort", "reverse"}
)
_LENGTH_CHANGERS = frozenset({"append", "extend", "insert", "pop", "remove", "clear", "popitem", "add", "discard", "update", "setdefault"})
_INDEX_GUARD_EXCEPTIONS = frozenset({"IndexError", "LookupError", "Exception", "BaseException"})


@dataclass(frozen=True)
class RuleInfo:
    rule: str
    language: Literal["python", "javascript", "any"]
    strength: Strength
    title: str
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class PatternHit:
    rule: str
    language: str
    strength: Strength
    path: str
    start_line: int
    end_line: int
    focus_lines: tuple[int, ...]  # the exact lines the rule is about (used to corroborate AI findings)
    title: str
    beginner: str
    technical: str
    assumptions: tuple[str, ...]
    parser: Literal["confirmed", "heuristic"]  # Python ast = confirmed; JS token scan = heuristic

    def explanation(self, depth: str) -> str:
        return self.beginner if depth == "beginner" else self.technical


# --------------------------------------------------------------------------- #
# Catalogue: what each rule is, and what it assumes (documented once, used by tests and the README)
# --------------------------------------------------------------------------- #
RULES: dict[str, RuleInfo] = {
    "PY_INDEX_PAST_END": RuleInfo(
        "PY_INDEX_PAST_END", "python", "problem_if_assumptions_hold",
        "Loop reads one item past the end",
        ("The name being indexed is a list, tuple or string.", "The loop is not stopped early (break/return) before its last pass."),
    ),
    "PY_SKIPS_FIRST_ITEM": RuleInfo(
        "PY_SKIPS_FIRST_ITEM", "python", "worth_checking",
        "Loop starts at position 1, so the first item is never visited",
        ("The loop is meant to process every item, including the first.", "Nothing else in this function handles the first item."),
    ),
    "PY_IS_LITERAL": RuleInfo(
        "PY_IS_LITERAL", "python", "problem_if_assumptions_hold",
        "`is` used to compare with a text or number",
        ("The intent is to compare values for equality, not to test that two names are the very same object.",),
    ),
    "PY_MUTABLE_DEFAULT": RuleInfo(
        "PY_MUTABLE_DEFAULT", "python", "problem_if_assumptions_hold",
        "A default list/dictionary is changed by the function",
        (
            "The function is called more than once in the same run without passing that argument.",
            "Sharing the changed default between calls is not intended (for example, a deliberate cache).",
        ),
    ),
    "PY_SYNTAX_ERROR": RuleInfo(
        "PY_SYNTAX_ERROR", "python", "problem_if_assumptions_hold",
        "Python cannot read this file",
        ("The file is meant to be Python 3 code that this server's Python can read (Python 2 code and newer syntax report the same way).",),
    ),
    "JS_INDEX_PAST_END": RuleInfo(
        "JS_INDEX_PAST_END", "javascript", "problem_if_assumptions_hold",
        "Loop reads one item past the end",
        ("The name being indexed is an array or a string.", "The loop is not stopped early (break/return) before its last pass."),
    ),
    "JS_ASSIGN_IN_CONDITION": RuleInfo(
        "JS_ASSIGN_IN_CONDITION", "javascript", "problem_if_assumptions_hold",
        "A fixed value is assigned inside an if condition",
        ("The `=` is a typing mistake for `==` or `===`, not a deliberate assignment.",),
    ),
    "JS_NAN_COMPARE": RuleInfo(
        "JS_NAN_COMPARE", "javascript", "problem_if_assumptions_hold",
        "Comparing a value with NaN",
        ("The comparison is meant to detect whether a value is NaN (not a number).",),
    ),
    "MISSING_LOCAL_FILE": RuleInfo(
        "MISSING_LOCAL_FILE", "any", "worth_checking",
        "Refers to a file that is not in the upload",
        ("The upload is the whole project.", "No build step or server creates that file."),
    ),
}


def _hit(rule: str, path: str, focus: Iterable[int], beginner: str, technical: str, parser: str, title: str | None = None,
         assumptions: Sequence[str] | None = None) -> PatternHit:
    info = RULES[rule]
    lines = tuple(sorted({n for n in focus if n and n > 0})) or (1,)
    return PatternHit(
        rule=rule,
        language=info.language,
        strength=info.strength,
        path=path,
        start_line=lines[0],
        end_line=lines[-1],
        focus_lines=lines,
        title=title or info.title,
        beginner=beginner,
        technical=technical,
        assumptions=tuple(assumptions) if assumptions is not None else info.assumptions,
        parser=parser,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Python (ast)
# --------------------------------------------------------------------------- #
def _scope_walk(nodes: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Walk statements/expressions but do not descend into nested functions, lambdas or classes."""
    stack = [n for n in nodes if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                stack.append(child)


def _bound_names(tree: ast.AST) -> set[str]:
    """Every name the file binds anywhere (so `range`/`len`/`list` can be recognised as the builtins)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _plain_chain(node: ast.AST) -> str | None:
    """`items`, `self.items`, `a.b.c` as text; None for anything that is not a plain name chain."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _len_of(node: ast.AST) -> str | None:
    """`items` for `len(items)` (builtin form only)."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len" and len(node.args) == 1 and not node.keywords:
        return _plain_chain(node.args[0])
    return None


def _int_value(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    return None


def _subscripts(nodes: Iterable[ast.AST], seq: str) -> list[ast.Subscript]:
    return [n for n in _scope_walk(nodes) if isinstance(n, ast.Subscript) and _plain_chain(n.value) == seq]


def _slice_is_name(sub: ast.Subscript, name: str) -> bool:
    return isinstance(sub.slice, ast.Name) and sub.slice.id == name


def _mentions_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def _changes_length(nodes: Sequence[ast.AST], seq: str) -> bool:
    """Does the code rebind or resize ``seq``?  Then ``len(seq)`` is not stable across the loop."""
    for n in _scope_walk(nodes):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _LENGTH_CHANGERS and _plain_chain(n.func.value) == seq:
            return True
        targets: list[ast.AST] = []
        if isinstance(n, ast.Assign):
            targets = list(n.targets)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            targets = [n.target]
        elif isinstance(n, ast.Delete):
            targets = list(n.targets)
        for target in targets:
            if _plain_chain(target) == seq:
                return True
            if isinstance(target, ast.Subscript) and _plain_chain(target.value) == seq and isinstance(n, ast.Delete):
                return True
    return False


def _guarded_by_try(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Is the node inside a ``try`` body whose handler would swallow an IndexError?"""
    child, current = node, parents.get(node)
    while current is not None:
        if isinstance(current, ast.Try) and child in current.body:
            for handler in current.handlers:
                if handler.type is None:
                    return True
                kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                if any(isinstance(k, ast.Name) and k.id in _INDEX_GUARD_EXCEPTIONS for k in kinds):
                    return True
        child, current = current, parents.get(current)
    return False


def _enclosing_scope_body(node: ast.AST, parents: dict[ast.AST, ast.AST], tree: ast.Module) -> list[ast.stmt]:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.body
        current = parents.get(current)
    return tree.body


def _range_args(node: ast.For) -> list[ast.expr] | None:
    it = node.iter
    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range" and not it.keywords and 1 <= len(it.args) <= 2:
        return it.args
    return None


def _len_plus_constant(node: ast.AST) -> tuple[str, int] | None:
    """`len(x) + k` or `k + len(x)` with a whole-number constant k."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        for a, b in ((node.left, node.right), (node.right, node.left)):
            seq, k = _len_of(a), _int_value(b)
            if seq and k is not None:
                return seq, k
    return None


def _py_loops(tree: ast.Module, path: str, shadowed: set[str], parents: dict[ast.AST, ast.AST]) -> list[PatternHit]:
    hits: list[PatternHit] = []
    if {"range", "len"} & shadowed:
        return hits  # `range`/`len` are not the builtins here: the rules' meaning does not hold
    candidates = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.For) and isinstance(node.target, ast.Name)):
            continue
        args = _range_args(node)
        if not args:
            continue
        candidates += 1
        if candidates > MAX_CANDIDATES or len(hits) >= MAX_HITS_PER_RULE * 2:
            break
        index = node.target.id
        stop = args[-1]
        start_value = _int_value(args[0]) if len(args) == 2 else 0

        # --- PY_INDEX_PAST_END: range(len(x) + k), k >= 1, and x[i] in the body ------------------
        plus = _len_plus_constant(stop)
        if plus and plus[1] >= 1 and (len(args) == 1 or start_value is not None):
            seq = plus[0]
            reads = [s for s in _subscripts(node.body, seq) if _slice_is_name(s, index)]
            if reads and not _changes_length(node.body, seq) and not _guarded_by_try(node, parents):
                guarded = any(
                    isinstance(c, ast.Compare) and _mentions_name(c, index) and any(_len_of(x) == seq for x in ast.walk(c))
                    for c in _scope_walk(node.body)
                )
                if not guarded:
                    first = min(reads, key=lambda s: s.lineno)
                    extra = plus[1]
                    hits.append(
                        _hit(
                            "PY_INDEX_PAST_END", path, (node.lineno, first.lineno),
                            f"This loop counts one place too far: it goes up to {extra} more than the last position in `{seq}`, "
                            f"so on its final pass `{seq}[{index}]` asks for an item that is not there. That normally stops the "
                            "program with an IndexError.",
                            f"`range(len({seq}) + {extra})` yields indices up to len({seq}) + {extra} - 1, so `{seq}[{index}]` is out of range "
                            f"on the last {extra} iteration(s) (IndexError for a list, tuple or string).",
                            "confirmed",
                        )
                    )
        # --- PY_SKIPS_FIRST_ITEM: range(1, len(x)) and x[i], with nothing else touching x[0] -------
        if len(args) == 2 and start_value == 1 and _len_of(args[1]):
            seq = _len_of(args[1])
            assert seq is not None
            body_reads = _subscripts(node.body, seq)
            reads = [s for s in body_reads if _slice_is_name(s, index)]
            everything = [n for n in _scope_walk(node.body) if isinstance(n, ast.Subscript)]
            # Looking at the previous/next entry of ANY sequence (`table[i - 1]`), or filling a derived table at
            # [i], means the loop is deliberately an "start at the second element" algorithm (e.g. a KMP prefix
            # table in the standard library), not a skipped first item.
            neighbour_reads = any(isinstance(s.slice, ast.BinOp) and _mentions_name(s.slice, index) for s in everything)
            fills_table = any(
                _slice_is_name(s, index) and isinstance(s.ctx, ast.Store) and _plain_chain(s.value) != seq for s in everything
            )
            if reads and not neighbour_reads and not fills_table and not _first_item_handled(node, seq, parents, tree):
                first = min(reads, key=lambda s: s.lineno)
                hits.append(
                    _hit(
                        "PY_SKIPS_FIRST_ITEM", path, (node.lineno, first.lineno),
                        f"This loop starts counting at 1, but the first item of `{seq}` is at position 0, so that first item is never "
                        "looked at. If you wanted every item, start at 0 (or loop over the items directly).",
                        f"`range(1, len({seq}))` never yields index 0, and nothing in this scope reads `{seq}[0]`, so the first element is skipped.",
                        "confirmed",
                    )
                )
    return hits[:MAX_HITS_PER_RULE * 2]


def _first_item_handled(loop: ast.For, seq: str, parents: dict[ast.AST, ast.AST], tree: ast.Module) -> bool:
    """Does the enclosing scope deal with ``seq[0]`` (the classic `best = x[0]; for i in range(1, ...)`)?"""
    body = _enclosing_scope_body(loop, parents, tree)
    for sub in _subscripts(body, seq):
        sl = sub.slice
        if _int_value(sl) == 0:
            return True
        if isinstance(sl, ast.Slice) and (sl.lower is None or _int_value(sl.lower) == 0):
            return True
    for n in _scope_walk(body):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and _plain_chain(n.func.value) == seq and n.func.attr == "pop":
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in {"iter", "next", "enumerate", "zip", "sum", "min", "max", "sorted"}:
            if any(_plain_chain(a) == seq for a in n.args):
                return True
    return False


def _py_is_literal(tree: ast.Module, path: str) -> list[PatternHit]:
    hits: list[PatternHit] = []

    def literal_kind(node: ast.AST) -> str | None:
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            node = node.operand
        if isinstance(node, ast.Constant) and type(node.value) in (str, bytes, int, float, complex):
            return type(node.value).__name__
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for op, left, right in zip(node.ops, operands, operands[1:]):
            if isinstance(op, (ast.Is, ast.IsNot)):
                kind = literal_kind(left) or literal_kind(right)
                if kind:
                    word = "is not" if isinstance(op, ast.IsNot) else "is"
                    text = {"str": "a piece of text", "bytes": "bytes", "int": "a whole number", "float": "a number", "complex": "a number"}[kind]
                    hits.append(
                        _hit(
                            "PY_IS_LITERAL", path, (node.lineno,),
                            f"`{word}` asks whether two names point at the very same object, not whether they hold the same value. "
                            f"To compare with {text}, use `==` (or `!=`). Python itself warns about this line.",
                            f"`{word}` tests object identity; comparing with a {kind} literal depends on interning and raises a SyntaxWarning "
                            "in CPython. Use `==` / `!=` for value comparison.",
                            "confirmed",
                        )
                    )
                    break
    return hits[:MAX_HITS_PER_RULE]


def _positional_defaults(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, ast.expr]]:
    positional = [*fn.args.posonlyargs, *fn.args.args]
    pairs = list(zip(positional[len(positional) - len(fn.args.defaults):], fn.args.defaults)) if fn.args.defaults else []
    pairs += [(a, d) for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is not None]
    return [(a.arg, d) for a, d in pairs]


def _is_mutable_literal(node: ast.expr, shadowed: set[str]) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"list", "dict", "set"}
        and node.func.id not in shadowed and not node.args and not node.keywords
    )


def _py_mutable_default(tree: ast.Module, path: str, shadowed: set[str]) -> list[PatternHit]:
    hits: list[PatternHit] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for name, default in _positional_defaults(fn):
            if not _is_mutable_literal(default, shadowed):
                continue
            body_nodes = list(_scope_walk(fn.body))
            # `tags += [x]` changes the shared list in place, so an augmented target is not a rebinding.
            augmented = {id(n.target) for n in body_nodes if isinstance(n, ast.AugAssign)}
            rebinds = any(
                isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store) and id(n) not in augmented
                for n in body_nodes
            ) or any(isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names for n in body_nodes)
            if rebinds:  # `tags = tags or []`, `tags = list(tags)`: the call gets its own copy
                continue
            mutation = _first_mutation(body_nodes, name)
            if mutation is None or _used_as_state(body_nodes, name):
                continue
            hits.append(
                _hit(
                    "PY_MUTABLE_DEFAULT", path, (default.lineno, mutation.lineno),
                    f"`{name}` starts as one list/dictionary that is created ONCE, when the function is defined, and every call that does not "
                    f"pass its own `{name}` shares it. Because the function changes it, items from earlier calls will still be there on later calls.",
                    f"The default for `{name}` is evaluated once at definition time and mutated in the body, so state persists across calls "
                    "that rely on the default.",
                    "confirmed",
                )
            )
    return hits[:MAX_HITS_PER_RULE]


def _first_mutation(nodes: Sequence[ast.AST], name: str) -> ast.AST | None:
    """The first in-place change of the parameter: a method call (`tags.append(x)`) or `tags += [x]`.

    Writing into a default container by subscript (`cache[key] = value`, `seen[x] += 1`) is deliberately
    NOT counted. Measured over the Python standard library, every such hit was a cache, a "seen" set or a
    counter kept on purpose; the accident the rule exists for is the method call that grows a list.
    """
    best: ast.AST | None = None
    for n in nodes:
        found = False
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _MUTATORS:
            found = isinstance(n.func.value, ast.Name) and n.func.value.id == name
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            found = True  # `tags += [x]` extends the shared list in place
        if found and (best is None or n.lineno < best.lineno):  # type: ignore[attr-defined]
            best = n
    return best


def _used_as_state(nodes: Sequence[ast.AST], name: str) -> bool:
    """Does the function test membership in (or read an item of) the default container?  Then it is state."""
    for n in nodes:
        if isinstance(n, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops):
            if any(isinstance(c, ast.Name) and c.id == name for c in n.comparators):
                return True
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and isinstance(n.value, ast.Name) and n.value.id == name:
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and isinstance(n.func.value, ast.Name) and n.func.value.id == name:
            return True
    return False


def _py_syntax(project: ProjectAnalysis, path: str) -> list[PatternHit]:
    analysis = project.analyses[path]
    for d in analysis.diagnostics:
        if d.severity == "error" and d.code == "syntax_error":
            return [
                _hit(
                    "PY_SYNTAX_ERROR", path, (d.line or 1,),
                    f"Python could not read this file, so it cannot run as written. Python's own message: \"{d.message}\".",
                    f"ast.parse fails: {d.message}" + (f" (line {d.line})" if d.line else "") + ".",
                    "confirmed",
                )
            ]
    return []


# --------------------------------------------------------------------------- #
# JavaScript (Phase 3 token stream; heuristic, so every hit says so)
# --------------------------------------------------------------------------- #
class _Js:
    def __init__(self, text: str, scan=None) -> None:
        scan = scan if scan is not None else tokenize(text)
        self.tokens: list[Token] = scan.tokens
        self.pairs, self.balanced = match_brackets(self.tokens)

    def val(self, k: int) -> str | None:
        return self.tokens[k].value if 0 <= k < len(self.tokens) else None

    def is_punct(self, k: int, value: str) -> bool:
        return 0 <= k < len(self.tokens) and self.tokens[k].kind == "punct" and self.tokens[k].value == value

    def is_id(self, k: int, value: str | None = None) -> bool:
        return 0 <= k < len(self.tokens) and self.tokens[k].kind == "id" and (value is None or self.tokens[k].value == value)

    def adjacent(self, a: int, b: int) -> bool:
        return self.tokens[a].end == self.tokens[b].start

    def keyword_at(self, k: int, word: str) -> bool:
        return self.is_id(k, word) and not self.is_punct(k - 1, ".")

    def chain(self, tokens: Sequence[Token]) -> str | None:
        """`items` / `this.items` from id(.id)* tokens; None for anything else."""
        if not tokens or len(tokens) % 2 == 0:
            return None
        for n, t in enumerate(tokens):
            if n % 2 == 0 and t.kind != "id":
                return None
            if n % 2 == 1 and not (t.kind == "punct" and t.value == "."):
                return None
        return "".join(t.value for t in tokens)


def _p(token: Token, value: str) -> bool:
    """Is ``token`` the punctuation ``value``?  (A string literal "[" must never count as a bracket.)"""
    return token.kind == "punct" and token.value == value


def _split_top_level(tokens: list[Token], sep: str) -> list[list[Token]]:
    parts: list[list[Token]] = [[]]
    depth = 0
    for t in tokens:
        if t.kind == "punct" and t.value in "([{":
            depth += 1
        elif t.kind == "punct" and t.value in ")]}":
            depth -= 1
        if depth == 0 and t.kind == "punct" and t.value == sep:
            parts.append([])
        else:
            parts[-1].append(t)
    return parts


def _js_index_past_end(js: _Js, path: str) -> list[PatternHit]:
    hits: list[PatternHit] = []
    toks = js.tokens
    candidates = 0
    for k in range(len(toks)):
        if not (js.keyword_at(k, "for") and js.is_punct(k + 1, "(") and (k + 1) in js.pairs):
            continue
        close = js.pairs[k + 1]
        header = toks[k + 2 : close]
        parts = _split_top_level(header, ";")
        if len(parts) != 3:
            continue
        init, test, update = parts
        if len(hits) >= MAX_HITS_PER_RULE:
            break
        if init and init[0].kind == "id" and init[0].value in {"let", "var", "const"}:
            init = init[1:]
        if len(init) < 3 or init[0].kind != "id" or not _p(init[1], "=") or _p(init[2], "="):
            continue
        index = init[0].value
        # test:  i <= <chain>.length
        if not (len(test) >= 6 and test[0].kind == "id" and test[0].value == index and _p(test[1], "<") and _p(test[2], "=")
                and test[1].end == test[2].start and _p(test[-2], ".") and test[-1].kind == "id" and test[-1].value == "length"):
            continue
        seq = js.chain(test[3:-2])
        if not seq:
            continue
        # update: i++ / ++i / i += 1
        text_update = "".join(t.value for t in update)
        if text_update not in {f"{index}++", f"++{index}", f"{index}+=1"}:
            continue
        candidates += 1
        if candidates > MAX_CANDIDATES:
            break
        body_start = close + 1
        if js.is_punct(body_start, "{") and body_start in js.pairs:
            body = toks[body_start + 1 : js.pairs[body_start]]
        else:
            end = body_start
            while end < len(toks) and not js.is_punct(end, ";"):
                end += 1
            body = toks[body_start:end]
        if any(t.kind == "id" and t.value == "undefined" for t in body):
            continue  # the body looks at undefined explicitly
        if _mentions_length(body, seq):
            continue  # the body looks at the length itself: probably guarded
        read = _find_read(body, seq, index)
        if read is None:
            continue
        hits.append(
            _hit(
                "JS_INDEX_PAST_END", path, (toks[k].line, read),
                f"This loop uses `<=` where it should use `<`. Positions in `{seq}` go from 0 to {seq}.length - 1, so on the final pass "
                f"`{seq}[{index}]` is past the end and gives `undefined`, which usually turns a total into NaN or crashes.",
                f"`{index} <= {seq}.length` runs one iteration too many: `{seq}[{seq}.length]` is `undefined`.",
                "heuristic",
            )
        )
    return hits[:MAX_HITS_PER_RULE]


def _mentions_length(body: Sequence[Token], seq: str) -> bool:
    parts = seq.split(".")
    n = len(parts)
    for k in range(len(body)):
        window = body[k : k + 2 * n + 1]
        if len(window) == 2 * n + 1 and "".join(t.value for t in window) == seq + ".length":
            if k == 0 or body[k - 1].value != ".":
                return True
    return False


def _find_read(body: Sequence[Token], seq: str, index: str) -> int | None:
    parts = seq.split(".")
    n = 2 * len(parts) - 1
    for k in range(len(body)):
        window = body[k : k + n + 3]
        if len(window) != n + 3:
            continue
        if "".join(t.value for t in window[:n]) != seq or (k > 0 and body[k - 1].value == "."):
            continue
        if _p(window[n], "[") and window[n + 1].kind == "id" and window[n + 1].value == index and _p(window[n + 2], "]"):
            return window[0].line
    return None


_LITERAL_IDS = frozenset({"true", "false", "null", "undefined"})


def _js_assign_in_condition(js: _Js, path: str) -> list[PatternHit]:
    """`if (x = 5)`: the condition is exactly `<name> = <constant>`, so it is fixed whatever `x` was.

    Deliberately narrower than "any single `=` in an if": over real code (node_modules) every such hit
    was the deliberate `if (match = re.exec(s))` idiom, which cannot be told from a typo by structure.
    A constant on the right-hand side has no idiomatic use, so only that form is reported.
    """
    hits: list[PatternHit] = []
    toks = js.tokens
    for k in range(len(toks)):
        if not (js.keyword_at(k, "if") and js.is_punct(k + 1, "(") and (k + 1) in js.pairs):
            continue
        cond = toks[k + 2 : js.pairs[k + 1]]
        eq = next((n for n, t in enumerate(cond) if _p(t, "=")), None)
        if eq is None or eq == 0:
            continue
        target = js.chain(cond[:eq])
        rhs = cond[eq + 1 :]
        if rhs and _p(rhs[0], "-"):
            rhs = rhs[1:]
        is_constant = len(rhs) == 1 and (rhs[0].kind in {"num", "str"} or (rhs[0].kind == "id" and rhs[0].value in _LITERAL_IDS))
        lone = not _p(cond[eq - 1], "=") and not (eq + 1 < len(cond) and _p(cond[eq + 1], "="))  # not ==, ===, !=, <=, >=
        compound = cond[eq - 1].kind == "punct" and cond[eq - 1].value in "!<>+-*/%&|^?" and cond[eq - 1].end == cond[eq].start
        if target and is_constant and lone and not compound:
            hits.append(
                _hit(
                    "JS_ASSIGN_IN_CONDITION", path, (cond[eq].line,),
                    f"Inside this `if (...)` there is a single `=`: it STORES the fixed value into `{target}` instead of comparing, so the "
                    "condition is the same every time and `" + target + "` is overwritten. To compare, use `===` (or `==`).",
                    f"`if ({target} = <constant>)` assigns the constant and then tests it, so the branch is always taken (or never taken) "
                    "and the variable is overwritten.",
                    "heuristic",
                )
            )
    return hits[:MAX_HITS_PER_RULE]


def _js_nan_compare(js: _Js, path: str) -> list[PatternHit]:
    hits: list[PatternHit] = []
    toks = js.tokens
    for k, t in enumerate(toks):
        if not (t.kind == "id" and t.value == "NaN"):
            continue
        if js.is_punct(k - 1, ".") and not (js.is_id(k - 2, "Number")):
            continue
        # operator tokens just before / just after NaN: `x === NaN`, `NaN !== x`
        before = []
        m = k - 1
        while m >= 0 and toks[m].kind == "punct" and toks[m].value in "=!" and (not before or toks[m].end == toks[m + 1].start):
            before.append(toks[m].value)
            m -= 1
        after = []
        m = k + 1
        while m < len(toks) and toks[m].kind == "punct" and toks[m].value in "=!" and (not after or toks[m].start == toks[m - 1].end):
            after.append(toks[m].value)
            m += 1
        ops = {"==", "===", "!=", "!=="}
        if "".join(reversed(before)) in ops or "".join(after) in ops:
            hits.append(
                _hit(
                    "JS_NAN_COMPARE", path, (t.line,),
                    "NaN (\"not a number\") is never equal to anything, not even to itself, so this comparison always gives the same answer "
                    "whatever the value is. To test for NaN, use `Number.isNaN(value)`.",
                    "`NaN === x` and `NaN !== x` are constant (false / true) for every x, including NaN. Use Number.isNaN(x).",
                    "heuristic",
                )
            )
    return hits[:MAX_HITS_PER_RULE]


# --------------------------------------------------------------------------- #
# Dependency graph
# --------------------------------------------------------------------------- #
def _missing_local_files(project: ProjectAnalysis, path: str) -> list[PatternHit]:
    hits = []
    for edge in project.graph.edges:
        if edge.source == path and edge.status == "missing":
            hits.append(
                _hit(
                    "MISSING_LOCAL_FILE", path, (edge.line,),
                    f"This file refers to `{edge.specifier}`, but no such file is in the uploaded project, so that part cannot work unless the "
                    "file exists somewhere that was not uploaded.",
                    f"Unresolved local reference `{edge.specifier}` ({edge.kind}): the project graph has no matching file"
                    + (f" ({edge.reason})" if edge.reason else "") + ".",
                    "heuristic" if edge.confidence == "heuristic" else "confirmed",
                )
            )
    return hits[:MAX_HITS_PER_RULE]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _guarded(name: str, rule, *args) -> list[PatternHit]:
    """Run one rule; a bug in a rule (or a freak input) skips that rule instead of failing the whole analysis."""
    try:
        return rule(*args)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: rules are advisory and must never take a request down
        logger.warning("Pattern rule %s failed (%s) and was skipped", name, type(exc).__name__)  # no source text in the log
        return []



def run_pattern_checks(project: ProjectAnalysis, path: str, ranges: Sequence[Range] | None = None) -> list[PatternHit]:
    """All rule hits in ``path``; when ``ranges`` is given, only hits whose focus lines fall inside them.

    Pure and deterministic. Never raises on odd input: a file a rule cannot read yields no hits.
    """
    file = project.by_path.get(path)
    if file is None:
        return []
    hits: list[PatternHit] = []
    if file.language == "python":
        hits += _guarded("PY_SYNTAX_ERROR", _py_syntax, project, path)
        if not project.analyses[path].has_syntax_error:
            try:
                tree = ast.parse(file.text)
                shadowed = _bound_names(tree)
                parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            except (RecursionError, SyntaxError, ValueError, MemoryError):
                tree = None  # a pathological file: the rules stay silent rather than guess
            if tree is not None:
                hits += _guarded("PY_LOOPS", _py_loops, tree, path, shadowed, parents)
                hits += _guarded("PY_IS_LITERAL", _py_is_literal, tree, path)
                hits += _guarded("PY_MUTABLE_DEFAULT", _py_mutable_default, tree, path, shadowed)
    elif file.language == "javascript" and not path.lower().endswith((".jsx", ".tsx")):
        try:
            js: _Js | None = _Js(file.text, file.javascript_scan)
        except (RecursionError, ValueError, MemoryError):
            js = None
        if js is not None and js.balanced:  # unbalanced brackets usually mean JSX or a misread regex: do not trust the tokens
            hits += _guarded("JS_INDEX_PAST_END", _js_index_past_end, js, path)
            hits += _guarded("JS_ASSIGN_IN_CONDITION", _js_assign_in_condition, js, path)
            hits += _guarded("JS_NAN_COMPARE", _js_nan_compare, js, path)
    hits += _guarded("MISSING_LOCAL_FILE", _missing_local_files, project, path)
    if ranges:
        hits = [h for h in hits if any(a <= line <= b for line in h.focus_lines for a, b in ranges)]
    return sorted(hits, key=lambda h: (h.start_line, h.rule))
