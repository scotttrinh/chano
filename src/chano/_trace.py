from __future__ import annotations

import ast
import sys
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from chano._report import FunctionExplanation
from chano._store import StoreIndex, normalize_dist_name
from chano._vendor import ENTRYPOINT
from chano.config import ChanoConfig
from chano.routing import Route

# Distributions (and closures) always bundled when present in the store.
ALWAYS_INCLUDED: tuple[str, ...] = (
    "chano",
    "vercel-runtime",
    "vercel",
)


@dataclass(frozen=True)
class FunctionSpec:
    """One Vercel function and everything emitted beside its payload."""

    name: str
    """Function path under ``functions/``, without ``.func``."""
    route: Route | None
    entrypoint: str
    """Source of the function's ``index.py``."""
    vc_config: dict[str, object]
    """Content of the function's ``.vc-config.json``."""
    trace_roots: tuple[Path, ...] = ()
    """Project source files traced for this function."""
    include: tuple[str, ...] = ()
    """PEP 503 normalized distribution names from module-level ``include``."""
    required_distributions: tuple[str, ...] = ()
    """Distributions that MUST exist in the build store for this function."""
    source_file: Path | None = None
    """Primary source file for display in ``--explain``."""


@dataclass(frozen=True)
class _ImportRef:
    module: str | None
    level: int
    names: tuple[str, ...] = ()
    package: tuple[str, ...] | None = None


def _is_type_checking_guard(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute):
        return node.attr == "TYPE_CHECKING"
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return any(_is_type_checking_guard(value) for value in node.values)
    return False


def _call_arg(call: ast.Call, pos: int, kw: str) -> ast.expr | None:
    if pos < len(call.args):
        return call.args[pos]
    for keyword in call.keywords:
        if keyword.arg == kw:
            return keyword.value
    return None


def _str_literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _ImportCollector(ast.NodeVisitor):
    """Collect runtime imports from one module AST, skipping ``TYPE_CHECKING``."""

    def __init__(self) -> None:
        self.imports: list[_ImportRef] = []
        self.calls: list[ast.Call] = []
        self.importlib_aliases: set[str] = {"importlib"}
        self.import_module_aliases: set[str] = set()
        self.builtins_aliases: set[str] = {"builtins"}
        self.dunder_import_aliases: set[str] = {"__import__"}

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_guard(node.test):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        if (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and _is_type_checking_guard(node.test.operand)
        ):
            for stmt in node.body:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(_ImportRef(module=alias.name, level=0))
            if alias.name == "importlib":
                self.importlib_aliases.add(alias.asname or "importlib")
            elif alias.name == "builtins":
                self.builtins_aliases.add(alias.asname or "builtins")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = tuple(alias.name for alias in node.names if alias.name != "*")
        self.imports.append(
            _ImportRef(module=node.module, level=node.level, names=names)
        )
        if node.level == 0 and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    self.import_module_aliases.add(alias.asname or "import_module")
                elif alias.name == "*":
                    self.import_module_aliases.add("import_module")
        elif node.level == 0 and node.module == "builtins":
            for alias in node.names:
                if alias.name == "__import__":
                    self.dunder_import_aliases.add(alias.asname or "__import__")

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def resolve_calls(self) -> None:
        for call in self.calls:
            func = call.func
            is_import_module = (
                isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.importlib_aliases
            ) or (isinstance(func, ast.Name) and func.id in self.import_module_aliases)
            if is_import_module:
                ref = _parse_import_module_call(call)
                if ref is not None:
                    self.imports.append(ref)
                continue

            is_dunder_import = (
                isinstance(func, ast.Name) and func.id in self.dunder_import_aliases
            ) or (
                isinstance(func, ast.Attribute)
                and func.attr == "__import__"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.builtins_aliases
            )
            if is_dunder_import:
                ref = _parse_dunder_import_call(call)
                if ref is not None:
                    self.imports.append(ref)


def _parse_import_module_call(call: ast.Call) -> _ImportRef | None:
    name = _str_literal(_call_arg(call, 0, "name"))
    if not name:
        return None
    if name.startswith("."):
        level = len(name) - len(name.lstrip("."))
        mod = name[level:] or None
        pkg_arg = _call_arg(call, 1, "package")
        if isinstance(pkg_arg, ast.Name) and pkg_arg.id == "__package__":
            return _ImportRef(module=mod, level=level)
        pkg_str = _str_literal(pkg_arg)
        if pkg_str:
            pkg_parts = tuple(part for part in pkg_str.split(".") if part)
            if pkg_parts:
                return _ImportRef(module=mod, level=level, package=pkg_parts)
        return None
    return _ImportRef(module=name, level=0)


def _parse_dunder_import_call(call: ast.Call) -> _ImportRef | None:
    name = _str_literal(_call_arg(call, 0, "name"))
    if name is None:
        return None
    level_arg = _call_arg(call, 4, "level")
    if level_arg is None:
        level = 0
    elif (
        isinstance(level_arg, ast.Constant)
        and type(level_arg.value) is int
        and level_arg.value >= 0
    ):
        level = level_arg.value
    else:
        return None

    names: tuple[str, ...] = ()
    fromlist_arg = _call_arg(call, 3, "fromlist")
    if isinstance(fromlist_arg, (ast.List, ast.Tuple)):
        names = tuple(
            elt.value
            for elt in fromlist_arg.elts
            if isinstance(elt, ast.Constant)
            and isinstance(elt.value, str)
            and elt.value
            and elt.value != "*"
        )
    if level == 0 and not name:
        return None
    return _ImportRef(module=name or None, level=level, names=names)


def _extract_imports(source: str | bytes, filename: str) -> list[_ImportRef]:
    tree = ast.parse(source, filename=filename)
    collector = _ImportCollector()
    collector.visit(tree)
    collector.resolve_calls()
    return collector.imports


def _package_parts(file_path: Path, project_dir: Path) -> tuple[str, ...]:
    try:
        return file_path.resolve().parent.relative_to(project_dir).parts
    except ValueError:
        return ()


def _resolve_project_modules(parts: tuple[str, ...], project_dir: Path) -> list[Path]:
    """Resolve dotted module ``parts`` to project files inside ``project_dir``,
    including intermediate package ``__init__.py`` files."""
    if not parts or any(
        not part or part in (".", "..") or "/" in part or "\\" in part for part in parts
    ):
        return []
    py_file = project_dir.joinpath(*parts[:-1], f"{parts[-1]}.py")
    init_file = project_dir.joinpath(*parts, "__init__.py")
    if py_file.is_file():
        target = py_file.resolve()
    elif init_file.is_file():
        target = init_file.resolve()
    else:
        return []
    if not target.is_relative_to(project_dir):
        return []

    files: list[Path] = []
    for i in range(1, len(parts)):
        parent_init = project_dir.joinpath(*parts[:i], "__init__.py")
        if parent_init.is_file():
            resolved_parent = parent_init.resolve()
            if resolved_parent.is_relative_to(project_dir):
                files.append(resolved_parent)
    files.append(target)
    return files


def _resolve_import_ref(
    ref: _ImportRef,
    current_package: tuple[str, ...],
    project_dir: Path,
    store: StoreIndex,
) -> tuple[list[Path], tuple[str, ...]]:
    """Resolve one ``_ImportRef`` to project files to recurse into and/or
    distribution names from ``store``."""
    mod_parts = (
        tuple(part for part in ref.module.split(".") if part) if ref.module else ()
    )
    if ref.level > 0:
        base_pkg = ref.package if ref.package is not None else current_package
        up = ref.level - 1
        if up > len(base_pkg):
            return [], ()
        target_parts = (*base_pkg[: len(base_pkg) - up], *mod_parts)
        allow_store = ref.package is not None
    else:
        if not mod_parts:
            return [], ()
        target_parts = mod_parts
        allow_store = True

    project_files: list[Path] = []
    if target_parts:
        project_files.extend(_resolve_project_modules(target_parts, project_dir))
    for name in ref.names:
        project_files.extend(
            _resolve_project_modules((*target_parts, name), project_dir)
        )
    if project_files or not allow_store or not target_parts:
        return project_files, ()
    top = target_parts[0]
    if top in sys.stdlib_module_names:
        return [], ()
    return [], store.by_import.get(top, ())


def _relative_display(path: Path, project_root: Path) -> str:
    """Format ``path`` relative to ``project_root`` in POSIX style when possible."""
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _trace_function_chains(
    spec: FunctionSpec, project_dir: Path, store: StoreIndex
) -> dict[str, tuple[str, ...]]:
    """Statically trace ``spec.trace_roots`` and ``spec.entrypoint``, returning
    each matched distribution mapped to the shortest file chain that imports it."""
    project_root = project_dir.resolve()
    visited: dict[Path, tuple[str, ...]] = {}
    queue: deque[Path] = deque()
    chains: dict[str, tuple[str, ...]] = {}

    def enqueue_files(paths: Iterable[Path], parent_chain: tuple[str, ...]) -> None:
        for path in paths:
            resolved = path.resolve()
            if resolved not in visited and resolved.is_file():
                visited[resolved] = (
                    *parent_chain,
                    _relative_display(resolved, project_root),
                )
                queue.append(resolved)

    def process_refs(
        refs: list[_ImportRef],
        current_package: tuple[str, ...],
        current_chain: tuple[str, ...],
    ) -> None:
        for ref in refs:
            files, dists = _resolve_import_ref(
                ref, current_package, project_root, store
            )
            enqueue_files(files, current_chain)
            for dist in dists:
                chains.setdefault(dist, current_chain)

    def drain_queue() -> None:
        while queue:
            current = queue.popleft()
            package = _package_parts(current, project_root)
            process_refs(
                _extract_imports(current.read_bytes(), str(current)),
                package,
                visited[current],
            )

    enqueue_files(spec.trace_roots, ())
    drain_queue()

    # Trace generated entrypoint (imports chano) after file trace roots.
    process_refs(
        _extract_imports(spec.entrypoint, str(project_root / ENTRYPOINT)),
        (),
        (ENTRYPOINT,),
    )
    drain_queue()

    return chains


def _format_closure_reason(
    name: str,
    seed_reasons: Mapping[str, str],
    parents: Mapping[str, str],
) -> str:
    if name in seed_reasons:
        return seed_reasons[name]
    chain: list[str] = []
    current = name
    while current in parents:
        parent = parents[current]
        chain.append(parent)
        current = parent
    return f"required by {' <- '.join(chain)} <- {seed_reasons[current]}"


def _resolve_closure_with_reasons(
    seeds: Iterable[tuple[str, str]], store: StoreIndex
) -> dict[str, str]:
    """Return sorted ``distribution -> reason`` for the transitive closure of
    ``seeds`` over ``Requires-Dist`` for distributions present in ``store``."""
    seed_reasons: dict[str, str] = {}
    parents: dict[str, str] = {}
    queue: deque[str] = deque()
    for raw, reason in seeds:
        name = normalize_dist_name(raw)
        if name in store.distributions and name not in seed_reasons:
            seed_reasons[name] = reason
            queue.append(name)
    visited = set(seed_reasons)
    while queue:
        current = queue.popleft()
        for req in sorted(store.distributions[current].requires):
            if req in store.distributions and req not in visited:
                visited.add(req)
                parents[req] = current
                queue.append(req)
    return {
        name: _format_closure_reason(name, seed_reasons, parents)
        for name in sorted(visited)
    }


def _resolve_shaken_reasons(
    spec: FunctionSpec,
    project_dir: Path,
    config: ChanoConfig,
    store: StoreIndex,
) -> dict[str, str]:
    """Compute sorted ``distribution -> reason`` for a tree-shaken function."""
    project_root = project_dir.resolve()
    traced = _trace_function_chains(spec, project_root, store)
    source_file = spec.source_file or (spec.route.file if spec.route else None)
    route_label = (
        _relative_display(source_file, project_root) if source_file else spec.name
    )

    seeds: list[tuple[str, str]] = [
        (name, "always included") for name in ALWAYS_INCLUDED
    ]
    seeds.extend(
        (name, f"traced from {' -> '.join(chain)}") for name, chain in traced.items()
    )
    seeds.extend(
        (name, f"listed in route include ({route_label})") for name in spec.include
    )
    seeds.extend((name, "listed in [tool.chano] include") for name in config.include)
    return _resolve_closure_with_reasons(seeds, store)


def explain_function(
    spec: FunctionSpec,
    project_dir: Path,
    config: ChanoConfig,
    store: StoreIndex,
) -> FunctionExplanation:
    """Compute included distributions with reason chains and excluded
    distributions for ``spec``."""
    if not config.tree_shake:
        included = {
            name: "tree_shake = false (full store)" for name in store.distributions
        }
        return FunctionExplanation(name=spec.name, included=included, excluded=())

    included = _resolve_shaken_reasons(spec, project_dir, config, store)
    excluded = tuple(name for name in store.distributions if name not in included)
    return FunctionExplanation(name=spec.name, included=included, excluded=excluded)


def resolve_distributions(
    spec: FunctionSpec,
    project_dir: Path,
    config: ChanoConfig,
    store: StoreIndex,
) -> tuple[str, ...]:
    """Compute the final sorted distributions to bundle for ``spec``."""
    return tuple(_resolve_shaken_reasons(spec, project_dir, config, store))
