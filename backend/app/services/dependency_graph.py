"""Lightweight dependency graph over the analysed files (plain dicts, no graph library).

Every reference found by the analysers becomes one :class:`GraphEdge` with a
resolution **status**, so the graph never pretends to know more than it does:

``resolved``    the target is an analysed file in the project
``excluded``    the target exists in the archive but was not analysed (reason given)
``missing``     an explicit relative path / relative import that matches nothing in
                the archive (a confirmed dangling reference)
``external``    a package, standard-library module, Node builtin or URL (not local)
``unresolved``  cannot be decided statically (aliases, root-relative web paths,
                namespace packages, targets outside the project)

Resolution only inspects the list of paths in the archive. No package manager,
interpreter, ``sys.path`` or filesystem is consulted and nothing is imported.
"""

from __future__ import annotations

import posixpath
import sys
from collections import deque
from urllib.parse import unquote

from app.services.project_models import (
    Cycle,
    Deadline,
    DependencyGraph,
    EntryPoint,
    ExcludedFile,
    ExternalDependency,
    FileAnalysis,
    GraphEdge,
    GraphNode,
    ProjectFile,
    Reference,
)

_STDLIB = getattr(sys, "stdlib_module_names", frozenset())
_NODE_BUILTINS = frozenset(
    "assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns domain "
    "events fs http http2 https inspector module net os path perf_hooks process punycode querystring readline repl "
    "stream string_decoder sys timers tls trace_events tty url util v8 vm wasi worker_threads zlib".split()
)
_JS_EXTENSIONS = (".js", ".mjs", ".cjs", ".jsx", ".json", ".ts", ".tsx")
_JS_INDEX_EXTENSIONS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
_IGNORED_SCHEMES = ("data:", "javascript:", "mailto:", "tel:", "about:", "blob:", "#")
_ALIAS_PREFIXES = ("@/", "~/", "#", "$", "~")
_ENTRY_NAMES = frozenset({"main", "app", "index", "server", "run", "manage", "wsgi", "asgi", "cli"})


class _Resolver:
    def __init__(self, files: tuple[ProjectFile, ...], excluded: tuple[ExcludedFile, ...]) -> None:
        self.analysed = {f.path for f in files}
        self.excluded = {e.path: e for e in excluded}
        known = set(self.analysed) | set(self.excluded)
        self.known = known
        self.dirs: set[str] = {""}
        for path in known:
            parts = path.split("/")
            for depth in range(1, len(parts)):
                self.dirs.add("/".join(parts[:depth]))
        self.top_level = {p.split("/")[0].rsplit(".", 1)[0] for p in known}

    # ----- shared -------------------------------------------------------- #
    def target_edge(self, edge: dict, path: str) -> GraphEdge:
        """Edge to a known path: resolved if analysed, otherwise 'excluded' with the reason."""
        if path in self.analysed:
            return GraphEdge(target=path, status="resolved", **edge)
        record = self.excluded[path]
        return GraphEdge(status="excluded", target=None, reason=f"Target exists but was not analysed ({record.reason}).", **edge)

    # ----- web (HTML / CSS) ---------------------------------------------- #
    def web(self, source: str, ref: Reference) -> GraphEdge | None:
        spec = ref.specifier.strip()
        lowered = spec.lower()
        if not spec or lowered.startswith(_IGNORED_SCHEMES):
            return None
        edge = dict(source=source, specifier=spec, kind=ref.kind, line=ref.line, confidence=ref.confidence)
        if lowered.startswith(("http://", "https://", "//")):
            return GraphEdge(status="external", external_kind="url", **edge)
        if spec.startswith("~"):
            return GraphEdge(status="unresolved", reason="Bundler alias (~); cannot be resolved statically.", **edge)
        clean = unquote(spec.split("#", 1)[0].split("?", 1)[0])
        if not clean:
            return None
        if clean.startswith("/"):
            path = posixpath.normpath(clean.lstrip("/"))
            if path in self.known:
                return self.target_edge(edge, path)
            return GraphEdge(
                status="unresolved",
                reason="Root-relative path: the web server's document root is unknown.",
                **edge,
            )
        path = posixpath.normpath(posixpath.join(posixpath.dirname(source), clean))
        if path == ".." or path.startswith("../"):
            return GraphEdge(status="unresolved", reason="Points outside the uploaded project.", **edge)
        if clean.endswith("/"):
            index = posixpath.join(path, "index.html")
            if index in self.known:
                return self.target_edge(edge, index)
        if path in self.known:
            return self.target_edge(edge, path)
        return GraphEdge(status="missing", reason="No matching file in the archive.", **edge)

    # ----- JavaScript ---------------------------------------------------- #
    def javascript(self, source: str, ref: Reference) -> GraphEdge | None:
        spec = ref.specifier.strip()
        if not spec:
            return None
        edge = dict(source=source, specifier=spec, kind=ref.kind, line=ref.line, confidence=ref.confidence)
        lowered = spec.lower()
        if lowered.startswith(("http://", "https://", "//")):
            return GraphEdge(status="external", external_kind="url", **edge)
        if lowered.startswith("node:") or spec in _NODE_BUILTINS or spec.split("/")[0] in _NODE_BUILTINS:
            return GraphEdge(status="external", external_kind="builtin", **edge)
        is_relative = spec in (".", "..") or spec.startswith(("./", "../"))
        if not is_relative and not spec.startswith("/"):
            if spec.startswith(_ALIAS_PREFIXES) or spec.split("/")[0] in self.top_level:
                return GraphEdge(
                    status="unresolved",
                    reason="Possible path alias / base-URL import; cannot be resolved statically.",
                    **edge,
                )
            return GraphEdge(status="external", external_kind="package", **edge)
        if spec.startswith("/"):
            base = posixpath.normpath(spec.lstrip("/"))
        else:
            base = posixpath.normpath(posixpath.join(posixpath.dirname(source), spec))
        if base == ".." or base.startswith("../"):
            return GraphEdge(status="unresolved", reason="Points outside the uploaded project.", **edge)
        if base == ".":
            base = ""
        candidates = [base] if base else []
        candidates += [base + ext for ext in _JS_EXTENSIONS] if base else []
        candidates += [posixpath.join(base, "index" + ext) for ext in _JS_INDEX_EXTENSIONS]
        for candidate in candidates:
            if candidate in self.known:
                return self.target_edge(edge, candidate)
        if spec.startswith("/"):
            return GraphEdge(status="unresolved", reason="Root-relative path; the base directory is unknown.", **edge)
        return GraphEdge(status="missing", reason="No matching file in the archive.", **edge)

    # ----- Python -------------------------------------------------------- #
    def _module_file(self, base: str, parts: list[str]) -> str | None:
        """File for dotted ``parts`` under ``base`` ('' = root): module, package __init__, or None."""
        prefix = posixpath.join(base, *parts) if parts else base
        if parts:
            for candidate in (prefix + ".py", posixpath.join(prefix, "__init__.py")):
                if candidate in self.known:
                    return candidate
            return None
        init = posixpath.join(base, "__init__.py")
        return init if init in self.known else None

    def _dir_exists(self, base: str, parts: list[str]) -> bool:
        return (posixpath.join(base, *parts) if parts else base) in self.dirs

    def python(self, source: str, ref: Reference) -> list[GraphEdge]:
        parts = [p for p in ref.specifier.split(".") if p] if ref.specifier else []
        names = [n for n in ref.names if n != "*"]
        dotted = ("." * ref.level) + ref.specifier
        edge = dict(source=source, specifier=dotted, kind=ref.kind, line=ref.line, confidence=ref.confidence)
        here = posixpath.dirname(source)

        if ref.level > 0:
            base = here
            for _ in range(ref.level - 1):
                if not base:
                    return [GraphEdge(status="missing", reason="Relative import goes above the project root.", **edge)]
                base = posixpath.dirname(base)
            roots = [base]
        else:
            # Nearest enclosing folder that contains the module wins: script mode (the importer's
            # own folder), then each parent up to the project root, then a conventional src/ layout.
            roots = []
            folder = here
            while True:
                roots.append(folder)
                if not folder:
                    break
                folder = posixpath.dirname(folder)
            if "src" in self.dirs:
                roots.append("src")

        partial = False  # the first dotted component exists locally, but the full module does not
        for root in roots:
            module = self._module_file(root, parts) if (parts or ref.level > 0) else None
            subs = []
            for name in names:
                sub = self._module_file(root, [*parts, name])
                if sub is not None and sub != module:
                    joined = dotted if dotted.endswith(".") else dotted + "."
                    subs.append((f"{joined}{name}", sub))
            if module is not None or subs:
                found = [self.target_edge(edge, module)] if module is not None else []
                found += [self.target_edge({**edge, "specifier": spec}, path) for spec, path in subs]
                return found
            if parts and self._dir_exists(root, parts):
                return [
                    GraphEdge(
                        status="unresolved",
                        reason="Namespace package (a folder without __init__.py); cannot be resolved to a file.",
                        **edge,
                    )
                ]
            if ref.level > 0:  # a relative import has exactly one place to look
                reason = (
                    "Package has no __init__.py and no such submodule."
                    if not parts
                    else "No matching module in the archive."
                )
                return [GraphEdge(status="missing", reason=reason, **edge)]
            if parts and (self._module_file(root, parts[:1]) is not None or self._dir_exists(root, parts[:1])):
                partial = True

        if partial:
            return [
                GraphEdge(
                    status="missing",
                    reason=f"'{parts[0]}' exists in the project but this submodule does not.",
                    **edge,
                )
            ]
        if parts and parts[0] in _STDLIB:
            return [GraphEdge(status="external", external_kind="stdlib", **edge)]
        return [
            GraphEdge(
                status="external",
                external_kind="package",
                reason="Not found in the project; assumed to be an installed package.",
                **edge,
            )
        ]


def _external_label(edge: GraphEdge) -> str:
    spec = edge.specifier
    if edge.external_kind == "url":
        return spec
    if edge.external_kind in ("package", "builtin") and edge.source.lower().endswith((".js", ".jsx", ".mjs", ".cjs")):
        parts = spec.removeprefix("node:").split("/")
        return "/".join(parts[:2]) if spec.startswith("@") else parts[0]
    return spec.lstrip(".").split(".")[0]


def _strongly_connected(nodes: list[str], adjacency: dict[str, tuple[str, ...]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative (no recursion limit issues). Deterministic for sorted input."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child = work.pop()
            if child == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = adjacency.get(node, ())
            advanced = False
            for position in range(child, len(children)):
                other = children[position]
                if other not in index_of:
                    work.append((node, position + 1))
                    work.append((other, 0))
                    advanced = True
                    break
                if other in on_stack:
                    low[node] = min(low[node], index_of[other])
            if advanced:
                continue
            if low[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return components


def _example_loop(start: str, members: set[str], adjacency: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Shortest loop through ``start`` that stays inside the component (BFS)."""
    if start in adjacency.get(start, ()):
        return (start, start)
    previous: dict[str, str] = {}
    queue = deque([start])
    seen = {start}
    while queue:
        node = queue.popleft()
        for other in adjacency.get(node, ()):
            if other not in members:
                continue
            if other == start:
                loop = [node]
                while loop[-1] != start:
                    loop.append(previous[loop[-1]])
                loop.reverse()
                return (*loop, start)
            if other not in seen:
                seen.add(other)
                previous[other] = node
                queue.append(other)
    return (start, start)


def _entry_points(files: tuple[ProjectFile, ...], analyses: dict[str, FileAnalysis], dependents: dict[str, tuple[str, ...]]) -> tuple[EntryPoint, ...]:
    found: dict[str, EntryPoint] = {}
    for file in files:
        analysis = analyses[file.path]
        stem = posixpath.splitext(posixpath.basename(file.path))[0].lower()
        reason = None
        if file.language == "python":
            if analysis.has_main_guard:
                reason = "Has an `if __name__ == \"__main__\"` block."
            elif posixpath.basename(file.path).lower() == "__main__.py":
                reason = "Is a package's __main__.py."
            elif stem in _ENTRY_NAMES and not dependents.get(file.path):
                reason = f"Conventional entry-point name ('{stem}') and no project file imports it."
        elif file.language == "html":
            if stem == "index":
                reason = "Conventional web entry page (index.html)."
            elif not dependents.get(file.path):
                reason = "HTML page that no project file references."
        elif file.language == "javascript" and stem in _ENTRY_NAMES and not dependents.get(file.path):
            reason = f"Conventional entry-point name ('{stem}') and no project file imports it."
        if reason:
            found[file.path] = EntryPoint(file.path, reason)
    return tuple(sorted(found.values(), key=lambda e: (e.path.count("/"), e.path)))


def build_graph(
    files: tuple[ProjectFile, ...],
    analyses: dict[str, FileAnalysis],
    excluded: tuple[ExcludedFile, ...],
    deadline: Deadline | None = None,
) -> DependencyGraph:
    resolver = _Resolver(files, excluded)
    edges: list[GraphEdge] = []
    for file in files:
        if deadline:
            deadline.check()
        for ref in analyses[file.path].references:
            if file.language == "python":
                edges.extend(resolver.python(file.path, ref))
            elif file.language == "javascript":
                edge = resolver.javascript(file.path, ref)
                if edge:
                    edges.append(edge)
            else:
                edge = resolver.web(file.path, ref)
                if edge:
                    edges.append(edge)

    unique = dict.fromkeys(edges)  # frozen dataclasses: order-preserving dedupe
    ordered = sorted(
        unique,
        key=lambda e: (e.source, e.line, e.specifier, e.kind, e.target or "", e.status),
    )

    forward: dict[str, set[str]] = {f.path: set() for f in files}
    reverse: dict[str, set[str]] = {f.path: set() for f in files}
    for edge in ordered:
        if edge.status == "resolved" and edge.target:
            forward[edge.source].add(edge.target)
            reverse[edge.target].add(edge.source)
    dependencies = {path: tuple(sorted(targets)) for path, targets in forward.items()}
    dependents = {path: tuple(sorted(sources)) for path, sources in reverse.items()}

    paths = sorted(forward)
    cycles: list[Cycle] = []
    for component in _strongly_connected(paths, dependencies):
        if len(component) > 1 or component[0] in dependencies.get(component[0], ()):
            cycles.append(Cycle(tuple(component), _example_loop(component[0], set(component), dependencies)))
    cycles.sort(key=lambda c: c.files)

    externals: dict[tuple[str, str], set[str]] = {}
    for edge in ordered:
        if edge.status == "external" and edge.external_kind:
            externals.setdefault((edge.external_kind, _external_label(edge)), set()).add(edge.source)
    external_dependencies = tuple(
        ExternalDependency(name=name, kind=kind, files=tuple(sorted(users)))
        for (kind, name), users in sorted(externals.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    )

    nodes = tuple(
        GraphNode(
            path=f.path,
            language=f.language,
            dependency_count=len(dependencies[f.path]),
            dependent_count=len(dependents[f.path]),
        )
        for f in files
    )
    return DependencyGraph(
        nodes=nodes,
        edges=tuple(ordered),
        cycles=tuple(cycles),
        entry_points=_entry_points(files, analyses, dependents),
        external_dependencies=external_dependencies,
        dependencies=dependencies,
        dependents=dependents,
    )
