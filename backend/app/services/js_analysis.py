"""JavaScript structure extraction with a conservative, dependency-free scanner.

This is **not** a JavaScript parser. It tokenizes the file (skipping comments,
strings, template literals and regex literals so their contents are never
mistaken for code), matches brackets, and recognises a handful of statement
shapes: ES ``import``/``export``, CommonJS ``require``/``module.exports``,
dynamic ``import()``, and top-level function/class/variable declarations.

Everything it reports is ``heuristic``. It never claims a file is syntactically
valid or invalid; scan problems (unterminated strings, unbalanced brackets) are
warnings. Known blind spots: JSX and TypeScript syntax, regex-vs-division
ambiguity (decided from the previous token), automatic semicolon insertion
(approximated), getters/setters in object literals, and anything computed at
run time (``require(name)``, ``import(path)``). Nothing is ever executed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from app.services.project_models import (
    Diagnostic,
    ExportItem,
    FileAnalysis,
    ProjectFile,
    Reference,
    Symbol,
)

_MAX_SIGNATURE = 200
_MAX_TEMPLATE_DEPTH = 12
_MAX_IMPORT_TOKENS = 400
_ASCII_IDENTIFIER = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
# A statement is scanned for at most this many top-level steps (bracketed groups count as one),
# so adversarial input such as thousands of comma-joined declarations cannot go quadratic.
_MAX_STATEMENT_STEPS = 80

_REGEX_AFTER_KEYWORDS = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else", "yield", "await"}
)
# A line ending in one of these (or a next line starting with one) continues the statement.
_CONTINUES = frozenset("=+-*/%&|^!~<>?:,.([") | {"=>", "..."}
_CONTINUED_BY = frozenset("=+-*/%&|^<>?:,.([)]}") | {"=>"}
_CONTINUE_KEYWORDS = frozenset({"new", "typeof", "instanceof", "in", "of", "void", "delete", "await", "yield", "extends"})
_OPENERS = {"{": "}", "(": ")", "[": "]"}
_CLOSERS = {"}": "{", ")": "(", "]": "["}
_DECL_KEYWORDS = frozenset({"const", "let", "var"})
_DECLARATION_STARTS = _DECL_KEYWORDS | {"function", "class", "async"}


@dataclass(slots=True)
class Token:
    kind: str  # id | punct | str | num | tmpl | regex
    value: str
    line: int
    start: int
    end: int


class _Scan:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens: list[Token] = []
        self.diagnostics: list[Diagnostic] = []
        self.line = 1

    def warn(self, code: str, message: str, line: int) -> None:
        self.diagnostics.append(Diagnostic("warning", code, message, line, confidence="heuristic"))


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch in "_$" or (ord(ch) > 127 and ch.isidentifier())


def _is_ident_part(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or (ord(ch) > 127 and ("a" + ch).isidentifier())


def _skip_template(text: str, i: int, depth: int) -> int:
    """``i`` is just after the opening backtick; return the index after the closing one."""
    n = len(text)
    if depth > _MAX_TEMPLATE_DEPTH:
        return n
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif c == "$" and text.startswith("${", i):
            i = _skip_braces(text, i + 2, depth + 1)
        else:
            i += 1
    return n


def _skip_braces(text: str, i: int, depth: int) -> int:
    """``i`` is just after ``${``; return the index after its matching ``}``."""
    n = len(text)
    level = 1
    while i < n:
        c = text[i]
        if c in "'\"":
            i += 1
            while i < n and text[i] != c and text[i] != "\n":
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif c == "`":
            i = _skip_template(text, i + 1, depth)
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif c == "{":
            level += 1
            i += 1
        elif c == "}":
            level -= 1
            i += 1
            if level == 0:
                return i
        else:
            i += 1
    return n


def _regex_allowed(prev: Token | None) -> bool:
    if prev is None:
        return True
    if prev.kind == "punct":
        return prev.value not in (")", "]", "}")
    if prev.kind == "id":
        return prev.value in _REGEX_AFTER_KEYWORDS
    return False


def _scan_regex(text: str, i: int) -> int | None:
    """Return the index after a regex literal starting at ``i`` (a ``/``), or None."""
    n = len(text)
    j = i + 1
    in_class = False
    while j < n:
        c = text[j]
        if c == "\n":
            return None
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            return j
        j += 1
    return None


def tokenize(text: str) -> _Scan:
    scan = _Scan(text)
    tokens = scan.tokens
    n = len(text)
    i = 0
    line = 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r\f\v﻿":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                scan.warn("unterminated_comment", "A block comment is never closed.", line)
                end = n
            else:
                end += 2
            line += text.count("\n", i, end)
            i = end
        elif c in "'\"":
            j = i + 1
            terminated = False
            while j < n:
                if text[j] == "\\":
                    if j + 1 < n and text[j + 1] == "\n":
                        line += 1
                    j += 2
                    continue
                if text[j] == c:
                    terminated = True
                    break
                if text[j] == "\n":
                    break
                j += 1
            if not terminated:
                scan.warn("unterminated_string", "A string literal is not closed on its line.", line)
            tokens.append(Token("str", text[i + 1 : min(j, n)], line, i, min(j + 1, n)))
            i = min(j + 1, n)
        elif c == "`":
            end = _skip_template(text, i + 1, 0)
            if end >= n and not text.endswith("`"):
                scan.warn("unterminated_template", "A template literal is never closed.", line)
            tokens.append(Token("tmpl", "", line, i, end))
            line += text.count("\n", i, end)
            i = end
        elif _is_ident_start(c):
            # Scan common ASCII names in C; retain the existing Unicode continuation rules.
            match = _ASCII_IDENTIFIER.match(text, i)
            j = match.end() if match else i + 1
            while j < n and _is_ident_part(text[j]):
                j += 1
            tokens.append(Token("id", text[i:j], line, i, j))
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "._"):
                j += 1
            tokens.append(Token("num", text[i:j], line, i, j))
            i = j
        elif c == "/" and _regex_allowed(tokens[-1] if tokens else None) and (end := _scan_regex(text, i)):
            tokens.append(Token("regex", "", line, i, end))
            i = end
        elif text.startswith("=>", i):
            tokens.append(Token("punct", "=>", line, i, i + 2))
            i += 2
        elif text.startswith("...", i):
            tokens.append(Token("punct", "...", line, i, i + 3))
            i += 3
        elif text.startswith(("++", "--"), i):
            tokens.append(Token("punct", text[i : i + 2], line, i, i + 2))
            i += 2
        else:
            tokens.append(Token("punct", c, line, i, i + 1))
            i += 1
    return scan


def _match_brackets(tokens: list[Token], diagnostics: list[Diagnostic]) -> dict[int, int]:
    """Map each opener's token index to its closer's. Unbalanced input is reported, not fatal."""
    match: dict[int, int] = {}
    stack: list[int] = []
    reported = False
    for index, tok in enumerate(tokens):
        if tok.kind != "punct":
            continue
        if tok.value in _OPENERS:
            stack.append(index)
        elif tok.value in _CLOSERS:
            while stack and tokens[stack[-1]].value != _CLOSERS[tok.value]:
                stack.pop()  # an unclosed opener of another type: drop it
                reported = True
            if stack:
                match[stack.pop()] = index
            else:
                reported = True
    if stack or reported:
        line = tokens[stack[-1]].line if stack else None
        diagnostics.append(
            Diagnostic(
                "warning",
                "unbalanced_brackets",
                "Brackets are not balanced (or the scanner misread JSX/regex); "
                "symbol ranges in this file may be inaccurate.",
                line,
                confidence="heuristic",
            )
        )
    return match


def match_brackets(tokens: list[Token]) -> tuple[dict[int, int], bool]:
    """Public wrapper for rule modules: ``(opener index -> closer index, balanced)``.

    ``balanced`` is False when the scanner met brackets that do not pair up (often JSX or a regex it
    misread); callers that need certainty should then skip the file.
    """
    problems: list[Diagnostic] = []
    pairs = _match_brackets(tokens, problems)
    return pairs, not problems


class _Extractor:
    def __init__(self, file: ProjectFile, scan: _Scan) -> None:
        self.file = file
        self.text = file.text
        self.tokens = scan.tokens
        self.diagnostics = scan.diagnostics
        self.match = _match_brackets(self.tokens, self.diagnostics)
        self.symbols: list[Symbol] = []
        self.references: list[Reference] = []
        self.exports: list[ExportItem] = []
        self.last_line = max(file.line_count, 1)

    # ----- small helpers ------------------------------------------------- #
    def tok(self, index: int) -> Token | None:
        return self.tokens[index] if 0 <= index < len(self.tokens) else None

    def is_punct(self, index: int, value: str) -> bool:
        t = self.tok(index)
        return t is not None and t.kind == "punct" and t.value == value

    def is_id(self, index: int, value: str | None = None) -> bool:
        t = self.tok(index)
        return t is not None and t.kind == "id" and (value is None or t.value == value)

    def snippet(self, first: int, last: int) -> str:
        """Source text from token ``first`` through token ``last`` (inclusive), collapsed."""
        text = " ".join(self.text[self.tokens[first].start : self.tokens[last].end].split())
        return text if len(text) <= _MAX_SIGNATURE else text[: _MAX_SIGNATURE - 3] + "..."

    def end_line_of(self, closer_index: int | None, fallback: int) -> int:
        return self.tokens[closer_index].line if closer_index is not None else fallback

    def statement_end(self, start: int) -> int:
        """Index of the last token of the statement starting at ``start`` (ASI-aware, approximate)."""
        k = start
        n = len(self.tokens)
        steps = 0
        while k < n:
            steps += 1
            if steps > _MAX_STATEMENT_STEPS:
                return min(k, n - 1)
            t = self.tokens[k]
            if t.kind == "punct":
                if t.value in _OPENERS:
                    closer = self.match.get(k)
                    if closer is None:
                        return n - 1
                    k = closer
                    t = self.tokens[k]
                elif t.value == ";":
                    return k
                elif t.value == "}":
                    return max(k - 1, start)
            nxt = self.tok(k + 1)
            if nxt is None:
                return k
            if nxt.line > t.line:
                prev_continues = (t.kind == "punct" and t.value in _CONTINUES) or (
                    t.kind == "id" and t.value in _CONTINUE_KEYWORDS
                )
                next_continues = (nxt.kind == "punct" and nxt.value in _CONTINUED_BY) or (
                    nxt.kind == "id" and nxt.value in ("instanceof", "in", "of")
                )
                if not prev_continues and not next_continues:
                    return k
            k += 1
        return n - 1

    # ----- references ---------------------------------------------------- #
    def scan_calls(self, i: int) -> None:
        """``require('x')`` / ``import('x')`` anywhere in the file."""
        t = self.tokens[i]
        prev = self.tok(i - 1)
        if prev and prev.kind == "punct" and prev.value == "." or (prev and prev.kind == "id" and prev.value == "function"):
            return
        if not self.is_punct(i + 1, "("):
            return
        arg = self.tok(i + 2)
        closer = self.tok(i + 3)
        literal = arg is not None and arg.kind == "str" and closer is not None and closer.kind == "punct" and closer.value in (")", ",")
        kind = "require" if t.value == "require" else "dynamic_import"
        if literal:
            self.references.append(Reference(kind, arg.value, t.line, confidence="heuristic"))
        else:
            what = "require()" if kind == "require" else "import()"
            self.diagnostics.append(
                Diagnostic(
                    "info",
                    "dynamic_reference",
                    f"{what} with a non-literal argument: the target is decided at run time and is not in the dependency graph.",
                    t.line,
                    confidence="heuristic",
                )
            )

    def parse_import(self, i: int) -> None:
        """``import ... from 'x'`` / ``import 'x'`` at top level (token ``i`` is ``import``)."""
        line = self.tokens[i].line
        nxt = self.tok(i + 1)
        if nxt is None:
            return
        if nxt.kind == "str":
            self.references.append(Reference("import", nxt.value, line, confidence="heuristic"))
            return
        names: list[str] = []
        k = i + 1
        limit = min(len(self.tokens), i + _MAX_IMPORT_TOKENS)
        while k < limit:
            t = self.tokens[k]
            if t.kind == "id" and t.value == "from" and self.tok(k + 1) and self.tokens[k + 1].kind == "str":
                self.references.append(
                    Reference("import", self.tokens[k + 1].value, line, tuple(names), confidence="heuristic")
                )
                return
            if t.kind == "punct" and t.value == ";":
                return
            if t.kind == "punct" and t.value == "{":
                closer = self.match.get(k)
                if closer is None:
                    return
                j = k + 1
                while j < closer:
                    if self.tokens[j].kind == "id":
                        if self.is_id(j, "type") and self.is_id(j + 1) and not self.is_id(j + 1, "as"):
                            j += 1  # TypeScript `import { type X }`
                            continue
                        names.append(self.tokens[j].value)
                        j += 3 if self.is_id(j + 1, "as") else 1
                        continue
                    j += 1
                k = closer
            elif t.kind == "punct" and t.value == "*":
                names.append("*")
            elif t.kind == "id" and t.value not in ("as", "type") and not self.is_id(k - 1, "as") and not self.is_punct(k - 1, "*"):
                names.append("default")
            k += 1

    def parse_export(self, i: int) -> int:
        """Handle ``export ...`` at top level; return the index to resume scanning from."""
        line = self.tokens[i].line
        k = i + 1
        t = self.tok(k)
        if t is None:
            return k
        if t.kind == "id" and t.value == "default":
            nxt = k + 1
            if self.is_id(nxt, "async") and self.is_id(nxt + 1, "function"):
                nxt += 1
            name = None
            if self.is_id(nxt, "function") or self.is_id(nxt, "class"):
                probe = nxt + 1 + (1 if self.is_punct(nxt + 1, "*") else 0)
                if self.is_id(probe) and not self.is_punct(probe, "("):
                    name = self.tokens[probe].value
                before = len(self.symbols)
                end = self.declaration(nxt, exported=False, async_index=nxt - 1 if nxt != k + 1 else None)
                for index in range(before, len(self.symbols)):
                    if self.symbols[index].parent is None:
                        self.symbols[index] = replace(self.symbols[index], exported=True)
                self.exports.append(ExportItem(name or "default", "default", line))
                return max(end, k)
            self.exports.append(ExportItem("default", "default", line))
            return k
        if t.kind == "punct" and t.value == "*":
            alias = None
            j = k + 1
            if self.is_id(j, "as") and self.is_id(j + 1):
                alias = self.tokens[j + 1].value
                j += 2
            if self.is_id(j, "from") and self.tok(j + 1) and self.tokens[j + 1].kind == "str":
                source = self.tokens[j + 1].value
                self.references.append(Reference("reexport", source, line, ("*",), confidence="heuristic"))
                self.exports.append(ExportItem(alias or "*", "star", line, source))
                return j + 1
            return j
        if t.kind == "punct" and t.value == "{":
            closer = self.match.get(k)
            if closer is None:
                return k
            exported: list[str] = []
            local: list[str] = []
            j = k + 1
            while j < closer:
                if self.tokens[j].kind == "id":
                    if self.is_id(j + 1, "as") and self.is_id(j + 2):
                        local.append(self.tokens[j].value)
                        exported.append(self.tokens[j + 2].value)
                        j += 3
                        continue
                    local.append(self.tokens[j].value)
                    exported.append(self.tokens[j].value)
                j += 1
            source = None
            if self.is_id(closer + 1, "from") and self.tok(closer + 2) and self.tokens[closer + 2].kind == "str":
                source = self.tokens[closer + 2].value
                self.references.append(Reference("reexport", source, line, tuple(local), confidence="heuristic"))
            for name in exported:
                self.exports.append(ExportItem(name, "reexport" if source else "named", line, source))
            return closer
        # export function / class / const ... : declaration() records the symbol and the export
        return self.declaration(k, exported=True) if t.kind == "id" else k

    # ----- declarations -------------------------------------------------- #
    def declaration(self, i: int, exported: bool = False, async_index: int | None = None) -> int:
        """Record a top-level declaration starting at token ``i``; return resume index.

        Returns the index of the last token consumed for the *header* only: the
        main loop keeps scanning the body so nested ``require()`` calls are seen.
        """
        t = self.tokens[i]
        start_index = i
        is_async = False
        if t.kind == "id" and t.value == "async" and self.is_id(i + 1, "function"):
            is_async = True
            i += 1
            t = self.tokens[i]
        elif async_index is not None:
            is_async = True
            start_index = async_index

        if t.kind == "id" and t.value == "function":
            j = i + 1 + (1 if self.is_punct(i + 1, "*") else 0)
            name_token = self.tok(j)
            if name_token is None or name_token.kind != "id":
                return i  # anonymous (export default function () {})
            params = j + 1
            if not self.is_punct(params, "("):
                return i
            params_close = self.match.get(params)
            if params_close is None:
                return i
            body = params_close + 1
            if not self.is_punct(body, "{"):
                body = next((k for k in range(params_close + 1, min(params_close + 12, len(self.tokens))) if self.is_punct(k, "{")), -1)
            end_line = self.end_line_of(self.match.get(body) if body > 0 else None, name_token.line)
            self.add_symbol(
                name_token.value,
                "async_function" if is_async else "function",
                self.tokens[start_index].line,
                end_line,
                self.snippet(start_index, params_close),
                exported,
            )
            return params_close
        if t.kind == "id" and t.value == "class":
            name_token = self.tok(i + 1)
            if name_token is None or name_token.kind != "id" or name_token.value == "extends":
                return i
            brace = next((k for k in range(i + 2, min(i + 40, len(self.tokens))) if self.is_punct(k, "{")), -1)
            if brace == -1:
                return i
            closer = self.match.get(brace)
            end_line = self.end_line_of(closer, name_token.line)
            self.add_symbol(
                name_token.value,
                "class",
                t.line,
                end_line,
                self.snippet(i, brace - 1),
                exported,
            )
            if closer is not None:
                self.class_members(name_token.value, brace, closer)
            return brace - 1
        if t.kind == "id" and t.value in _DECL_KEYWORDS:
            return self.variable_declaration(i, exported)
        return i

    def add_symbol(self, name: str, kind: str, start: int, end: int, signature: str | None, exported: bool, parent: str | None = None) -> None:
        end = max(end, start)
        qualified = f"{parent}.{name}" if parent else name
        self.symbols.append(
            Symbol(
                name=name,
                qualified_name=qualified,
                kind=kind,
                start_line=start,
                end_line=min(end, self.last_line),
                signature=signature,
                parent=parent,
                exported=True if exported else None,
                confidence="heuristic",
            )
        )
        if exported and not parent:
            self.exports.append(ExportItem(name, "named", start))

    def variable_declaration(self, i: int, exported: bool) -> int:
        keyword = self.tokens[i].value
        stmt_end = self.statement_end(i)
        k = i + 1
        while k <= stmt_end:
            bound = self.binding_names(k)
            if bound is None:
                break
            names, after = bound
            first_name = names[0] if names else "{...}"
            start_line = self.tokens[i].line if k == i + 1 else self.tokens[k].line
            # declarator end: next top-level comma or the statement end
            decl_end = after
            while decl_end <= stmt_end:
                tok = self.tokens[decl_end]
                if tok.kind == "punct" and tok.value in _OPENERS:
                    closer = self.match.get(decl_end)
                    if closer is None:
                        decl_end = stmt_end
                        break
                    decl_end = closer + 1
                    continue
                if tok.kind == "punct" and tok.value == ",":
                    break
                decl_end += 1
            last = min(decl_end - 1, stmt_end)
            end_line = self.tokens[min(last, len(self.tokens) - 1)].line
            kind, signature = "variable", f"{keyword} {first_name}"
            if self.is_punct(after, "="):
                kind, signature, end_line = self.classify_rhs(keyword, first_name, after + 1, last)
            for name in names:
                self.add_symbol(name, kind, start_line, end_line, signature, exported)
            k = decl_end + 1
        return i

    def binding_names(self, k: int) -> tuple[list[str], int] | None:
        """Names bound at token ``k`` (identifier or simple destructuring) and the index after them."""
        t = self.tok(k)
        if t is None:
            return None
        if t.kind == "id":
            return [t.value], k + 1
        if t.kind == "punct" and t.value in ("{", "["):
            closer = self.match.get(k)
            if closer is None:
                return None
            names = []
            for j in range(k + 1, closer):
                tok = self.tokens[j]
                if tok.kind == "id" and not self.is_punct(j + 1, ":") and not self.is_punct(j - 1, "."):
                    prev_is_default = self.is_punct(j - 1, "=")
                    if not prev_is_default:
                        names.append(tok.value)
            return names, closer + 1
        return None

    def classify_rhs(self, keyword: str, name: str, rhs: int, last: int) -> tuple[str, str, int]:
        """Is the initialiser a function/class/arrow? -> (kind, signature, end_line)."""
        j = rhs
        is_async = False
        if self.is_id(j, "async") and not self.is_punct(j + 1, "=>"):
            is_async = True
            j += 1
        t = self.tok(j)
        end_line = self.tokens[min(last, len(self.tokens) - 1)].line
        if t is None:
            return "variable", f"{keyword} {name}", end_line
        if t.kind == "id" and t.value == "function":
            paren = next((k for k in range(j + 1, min(j + 4, len(self.tokens))) if self.is_punct(k, "(")), -1)
            close = self.match.get(paren) if paren != -1 else None
            signature = f"{keyword} {name} = {'async ' if is_async else ''}function" + (
                self.snippet(paren, close) if close is not None else "()"
            )
            return ("async_function" if is_async else "function"), signature[:_MAX_SIGNATURE], end_line
        if t.kind == "id" and t.value == "class":
            return "class", f"{keyword} {name} = class", end_line
        # arrow: ident => ...   or   (params) => ...
        arrow = None
        if t.kind == "id" and self.is_punct(j + 1, "=>"):
            arrow = j + 1
        elif t.kind == "punct" and t.value == "(":
            close = self.match.get(j)
            if close is not None and self.is_punct(close + 1, "=>"):
                arrow = close + 1
        if arrow is not None:
            signature = f"{keyword} {name} = " + ("async " if is_async else "") + self.snippet(j, arrow)
            return ("async_function" if is_async else "function"), signature[:_MAX_SIGNATURE], end_line
        text = " ".join(self.text[self.tokens[rhs].start : self.tokens[min(last, rhs + 12)].end].split())
        text = text if len(text) <= 60 else text[:57] + "..."
        return "variable", f"{keyword} {name} = {text}"[:_MAX_SIGNATURE], end_line

    def class_members(self, class_name: str, open_index: int, close_index: int) -> None:
        j = open_index + 1
        while j < close_index:
            t = self.tokens[j]
            if t.kind == "punct" and t.value in (";", ","):
                j += 1
                continue
            start = j
            is_async = False
            while self.is_id(j) and self.tokens[j].value in ("static", "async", "get", "set") and j + 1 < close_index and not (
                self.is_punct(j + 1, "(") or self.is_punct(j + 1, "=") or self.is_punct(j + 1, ";")
            ):
                if self.tokens[j].value == "async":
                    is_async = True
                j += 1
            if self.is_punct(j, "*"):
                j += 1
            if self.is_punct(j, "#"):
                j += 1
            name_token = self.tok(j)
            if name_token is None or j >= close_index:
                break
            if name_token.kind == "punct" and name_token.value == "[":
                closer = self.match.get(j)
                name = "[computed]"
                j = (closer if closer is not None else j)
            elif name_token.kind in ("id", "str", "num"):
                name = name_token.value
            elif name_token.kind == "punct" and name_token.value == "{":  # static { ... } block
                closer = self.match.get(j)
                j = (closer if closer is not None else close_index) + 1
                continue
            else:
                j += 1
                continue
            if self.is_punct(j + 1, "("):
                params_close = self.match.get(j + 1)
                body = (params_close + 1) if params_close is not None else -1
                body_close = self.match.get(body) if body != -1 and self.is_punct(body, "{") else None
                if params_close is not None and body_close is not None:
                    self.add_symbol(
                        name,
                        "async_method" if is_async else "method",
                        self.tokens[start].line,
                        self.tokens[body_close].line,
                        self.snippet(start, params_close),
                        False,
                        parent=class_name,
                    )
                    j = body_close + 1
                    continue
            # a field or something unrecognised: skip its statement
            j = max(self.statement_end(j), j) + 1

    # ----- CommonJS exports ---------------------------------------------- #
    def scan_commonjs_export(self, i: int) -> None:
        """``module.exports = ...`` / ``exports.x = ...`` (token ``i`` is ``module`` or ``exports``)."""
        t = self.tokens[i]
        if self.is_punct(i - 1, "."):
            return
        line = t.line
        j = i
        if t.value == "module":
            if not (self.is_punct(i + 1, ".") and self.is_id(i + 2, "exports")):
                return
            j = i + 2
        if self.is_punct(j + 1, ".") and self.is_id(j + 2) and self.is_punct(j + 3, "="):
            self.exports.append(ExportItem(self.tokens[j + 2].value, "commonjs", line))
            return
        if self.is_punct(j + 1, "=") and not self.is_punct(j + 2, "="):
            rhs = j + 2
            names: list[str] = []
            if self.is_punct(rhs, "{"):
                closer = self.match.get(rhs)
                if closer is not None:
                    depth_end = closer
                    k = rhs + 1
                    while k < depth_end:
                        tok = self.tokens[k]
                        if tok.kind in ("id", "str") and (
                            self.is_punct(k + 1, ",") or self.is_punct(k + 1, ":") or self.is_punct(k + 1, "}") or self.is_punct(k + 1, "(")
                        ) and (self.is_punct(k - 1, ",") or k == rhs + 1 or self.is_punct(k - 1, "{")):
                            names.append(tok.value)
                        if tok.kind == "punct" and tok.value in _OPENERS:
                            k = self.match.get(k, k)
                        k += 1
            elif self.is_id(rhs) and not self.is_punct(rhs + 1, "("):
                names.append(self.tokens[rhs].value)
            if names:
                for name in names:
                    self.exports.append(ExportItem(name, "commonjs", line))
            else:
                self.exports.append(ExportItem("module.exports", "commonjs", line))

    # ----- main pass ------------------------------------------------------ #
    def run(self) -> None:
        depth = 0
        i = 0
        n = len(self.tokens)
        while i < n:
            t = self.tokens[i]
            if t.kind == "punct":
                if t.value == "{":
                    depth += 1
                elif t.value == "}":
                    depth = max(depth - 1, 0)
                i += 1
                continue
            if t.kind != "id":
                i += 1
                continue
            prev = self.tok(i - 1)
            member = prev is not None and prev.kind == "punct" and prev.value == "."
            value = t.value
            if value in ("require", "import") and not member:
                if value == "import" and not self.is_punct(i + 1, "(") and not self.is_punct(i + 1, "."):
                    if depth == 0:
                        self.parse_import(i)
                else:
                    self.scan_calls(i)
            elif value == "export" and depth == 0 and not member:
                i = max(self.parse_export(i), i)
            elif depth == 0 and not member and value in _DECLARATION_STARTS:
                at_statement_start = prev is None or (prev.kind == "punct" and prev.value in (";", "}", "{")) or prev.line < t.line
                if at_statement_start:
                    i = max(self.declaration(i), i)
            elif value in ("module", "exports") and not member:
                self.scan_commonjs_export(i)
            i += 1

        self.finalise()

    def finalise(self) -> None:
        # Mark exported flags for CommonJS/ESM names declared separately (export { a }, module.exports = { a }).
        exported_names = {e.name for e in self.exports if e.kind in ("named", "default", "commonjs", "reexport")}
        has_exports = bool(self.exports)
        fixed: list[Symbol] = []
        for symbol in self.symbols:
            flag = symbol.exported
            if flag is None and has_exports and symbol.parent is None:
                flag = symbol.name in exported_names
            fixed.append(symbol if flag == symbol.exported else replace(symbol, exported=flag))
        self.symbols = fixed


def analyze_javascript(file: ProjectFile) -> FileAnalysis:
    scan = file.javascript_scan
    extractor = _Extractor(file, scan)
    extractor.run()
    diagnostics = list(extractor.diagnostics)
    if file.path.lower().endswith(".jsx"):
        # Apostrophes in JSX text ("Don't") look like unterminated strings to this scanner.
        diagnostics = [d for d in diagnostics if d.code != "unterminated_string"]
        diagnostics.append(
            Diagnostic(
                "info",
                "jsx_heuristic",
                "JSX is scanned with the plain-JavaScript heuristic; markup is ignored and symbol ranges may be inaccurate.",
                confidence="heuristic",
            )
        )
    # Deduplicate exact repeats (a name listed twice) while keeping order.
    exports = list(dict.fromkeys(extractor.exports))
    return FileAnalysis(
        path=file.path,
        language="javascript",
        parser="js-scanner",
        confidence="heuristic",
        line_count=file.line_count,
        symbols=tuple(sorted(extractor.symbols, key=lambda s: (s.start_line, s.qualified_name))),
        references=tuple(sorted(extractor.references, key=lambda r: (r.line, r.specifier, r.kind))),
        exports=tuple(sorted(exports, key=lambda e: (e.line, e.name, e.kind))),
        diagnostics=tuple(sorted(diagnostics, key=lambda d: (d.line or 0, d.code))),
    )
