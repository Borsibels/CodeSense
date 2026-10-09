"""Python static analysis with the standard-library ``ast`` module.

The source is only *parsed*, never compiled or run. Everything extracted from
the syntax tree is reported as ``confirmed``; the dynamic-import notes are
``heuristic`` because what a dynamic-import call such as ``import_module(x)``
loads is not knowable statically.
"""

from __future__ import annotations

import ast
import warnings

from app.services.project_models import (
    Diagnostic,
    FileAnalysis,
    ProjectFile,
    Reference,
    Symbol,
)

_MAX_SIGNATURE = 200
_MAX_VALUE = 60
_MAX_DOC = 120
_MAX_VARIABLES = 200

_COMPOUND_BODIES = ("body", "orelse", "finalbody")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _unparse(node: ast.AST | None, limit: int) -> str | None:
    if node is None:
        return None
    try:
        return _clip(ast.unparse(node), limit)
    except (RecursionError, ValueError, AttributeError):
        return None


def _doc_line(node: ast.AST) -> str | None:
    try:
        doc = ast.get_docstring(node, clean=True)  # type: ignore[arg-type]
    except TypeError:
        return None
    if not doc:
        return None
    first = next((line.strip() for line in doc.splitlines() if line.strip()), "")
    return _clip(first, _MAX_DOC) or None


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = _unparse(node.args, _MAX_SIGNATURE)
    args = "..." if args is None else args
    returns = _unparse(node.returns, 60)
    suffix = f" -> {returns}" if returns else ""
    return _clip(f"{prefix} {node.name}({args}){suffix}", _MAX_SIGNATURE)


def _class_signature(node: ast.ClassDef) -> str:
    bases = [b for b in (_unparse(base, 60) for base in node.bases) if b]
    bases += [
        f"{kw.arg}={v}" if kw.arg else f"**{v}"
        for kw in node.keywords
        if (v := _unparse(kw.value, 60))
    ]
    return _clip(f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}", _MAX_SIGNATURE)


def _start_line(node: ast.AST) -> int:
    lines = [node.lineno]  # type: ignore[attr-defined]
    lines += [d.lineno for d in getattr(node, "decorator_list", ())]
    return min(lines)


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, *test.comparators]
    has_name = any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
    has_main = any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)
    return has_name and has_main


def _sub_bodies(node: ast.AST) -> list[list[ast.stmt]]:
    """Statement lists nested in a compound statement (not inside functions/classes)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return []
    bodies = [getattr(node, name) for name in _COMPOUND_BODIES if isinstance(getattr(node, name, None), list)]
    for handler in getattr(node, "handlers", ()):
        bodies.append(handler.body)
    for case in getattr(node, "cases", ()):
        bodies.append(case.body)
    return bodies


class _Collector:
    def __init__(self) -> None:
        self.symbols: list[Symbol] = []
        self.variable_count = 0
        self.variables_truncated = False

    def visit(self, body: list[ast.stmt], parent: str | None, in_class: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_async = isinstance(node, ast.AsyncFunctionDef)
                if in_class:
                    kind = "async_method" if is_async else "method"
                else:
                    kind = "async_function" if is_async else "function"
                qualified = f"{parent}.{node.name}" if parent else node.name
                self.symbols.append(
                    Symbol(
                        name=node.name,
                        qualified_name=qualified,
                        kind=kind,
                        start_line=_start_line(node),
                        end_line=node.end_lineno or node.lineno,
                        signature=_function_signature(node),
                        parent=parent,
                        doc=_doc_line(node),
                    )
                )
            elif isinstance(node, ast.ClassDef):
                qualified = f"{parent}.{node.name}" if parent else node.name
                self.symbols.append(
                    Symbol(
                        name=node.name,
                        qualified_name=qualified,
                        kind="class",
                        start_line=_start_line(node),
                        end_line=node.end_lineno or node.lineno,
                        signature=_class_signature(node),
                        parent=parent,
                        doc=_doc_line(node),
                    )
                )
                self.visit(node.body, qualified, True)
            elif not in_class and isinstance(node, (ast.Assign, ast.AnnAssign)):
                self._variables(node)
            else:
                for sub in _sub_bodies(node):
                    self.visit(sub, parent, in_class)

    def _variables(self, node: ast.Assign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = _unparse(node.value, _MAX_VALUE)
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if self.variable_count >= _MAX_VARIABLES:
                self.variables_truncated = True
                return
            self.variable_count += 1
            self.symbols.append(
                Symbol(
                    name=target.id,
                    qualified_name=target.id,
                    kind="variable",
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    signature=_clip(f"{target.id} = {value}", _MAX_SIGNATURE) if value else target.id,
                )
            )


def _references(tree: ast.Module) -> tuple[list[Reference], list[Diagnostic]]:
    refs: list[Reference] = []
    notes: list[Diagnostic] = []
    top_level = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                refs.append(
                    Reference("import", alias.name, node.lineno, top_level=id(node) in top_level)
                )
        elif isinstance(node, ast.ImportFrom):
            names = tuple(alias.name for alias in node.names)
            refs.append(
                Reference(
                    "import",
                    node.module or "",
                    node.lineno,
                    names=names,
                    level=node.level,
                    top_level=id(node) in top_level,
                )
            )
            if "*" in names:
                notes.append(
                    Diagnostic(
                        "info",
                        "star_import",
                        "'from ... import *' - the imported names are not tracked.",
                        node.lineno,
                    )
                )
        elif isinstance(node, ast.Call):
            func = node.func
            dynamic = (isinstance(func, ast.Name) and func.id == "__import__") or (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            if dynamic:
                notes.append(
                    Diagnostic(
                        "info",
                        "dynamic_import",
                        "Dynamic import: the loaded module is decided at run time and is not in the dependency graph.",
                        node.lineno,
                        confidence="heuristic",
                    )
                )
    refs.sort(key=lambda r: (r.line, r.level, r.specifier, r.names))
    return refs, notes


def analyze_python(file: ProjectFile) -> FileAnalysis:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. invalid escape sequences: not our concern
            tree = ast.parse(file.text, filename=file.path)
    except SyntaxError as exc:
        return FileAnalysis(
            path=file.path,
            language="python",
            parser="python-ast",
            confidence="confirmed",
            line_count=file.line_count,
            diagnostics=(
                Diagnostic(
                    "error",
                    "syntax_error",
                    f"{type(exc).__name__}: {exc.msg}",
                    exc.lineno,
                    exc.offset,
                ),
            ),
            has_syntax_error=True,
        )
    except (RecursionError, MemoryError, ValueError, OverflowError) as exc:
        return FileAnalysis(
            path=file.path,
            language="python",
            parser="python-ast",
            confidence="confirmed",
            line_count=file.line_count,
            diagnostics=(
                Diagnostic(
                    "error",
                    "unparseable",
                    f"The file could not be parsed ({type(exc).__name__}); it may be too deeply nested.",
                ),
            ),
            has_syntax_error=True,
        )

    collector = _Collector()
    refs, notes = _references(tree)
    try:
        collector.visit(tree.body, None, False)
    except RecursionError:
        notes.append(
            Diagnostic(
                "warning",
                "too_deeply_nested",
                "Statements are nested too deeply; the symbol list may be incomplete.",
            )
        )
    if collector.variables_truncated:
        notes.append(
            Diagnostic(
                "info",
                "variables_truncated",
                f"Only the first {_MAX_VARIABLES} top-level variables are listed.",
            )
        )
    notes.sort(key=lambda d: (d.line or 0, d.code))

    return FileAnalysis(
        path=file.path,
        language="python",
        parser="python-ast",
        confidence="confirmed",
        line_count=file.line_count,
        symbols=tuple(sorted(collector.symbols, key=lambda s: (s.start_line, s.qualified_name))),
        references=tuple(refs),
        diagnostics=tuple(notes),
        doc=_doc_line(tree),
        has_main_guard=any(_is_main_guard(node) for node in _walk_top_blocks(tree.body)),
    )


def _walk_top_blocks(body: list[ast.stmt]):
    """Module-level statements, including those nested in try/if blocks (iterative)."""
    pending = [body]
    while pending:
        for node in pending.pop():
            yield node
            pending.extend(_sub_bodies(node))
