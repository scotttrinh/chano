"""File-system routing: ``app/**/route.py`` files become routes.

Conventions borrowed from the Next.js App Router:

- ``app/route.py``                      -> ``/``
- ``app/api/hello/route.py``            -> ``/api/hello``
- ``app/api/items/[item_id]/route.py``  -> ``/api/items/{item_id}``
- ``app/files/[...path]/route.py``      -> ``/files/{path:path}``
- ``app/(group)/about/route.py``        -> ``/about``

With ``base_path = "/v1"`` in ``[tool.chano]``, every URL and function path
gains the prefix: ``app/route.py`` -> ``/v1`` (function ``v1``).

A route module exports functions named after HTTP methods (``GET``, ``POST``,
...). Its signature declares its inputs: path and query parameters, a
model for the JSON body, and ``Depends`` (see ``chano.params``).

Locally, every route is served by one app (``create_app``). When
deployed, each route is its own function running a single-route app
(``create_route_app``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from chano.app import AfterBackend, Chano, ErrorHandler, Middleware
from chano.config import normalize_base_path, project_base_path, project_config
from chano.params import is_generator_callable
from chano.requests import Request
from chano.responses import Response
from chano.status import NotFound

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ROUTE_FILE = "route.py"
MIDDLEWARE_FILE = "middleware.py"
ERROR_FILE = "error.py"
DEPENDENCIES_FILE = "dependencies.py"


def load_module(path: Path) -> ModuleType:
    """Import a route or segment file once per process, keyed only by its
    resolved path: files from different projects never share a
    ``sys.modules`` entry, and one file reached from several routes or
    prefixes reuses its module. A failed import leaves no partial module
    behind, so a retry runs the file again."""
    digest = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]
    name = f"chano_routes._{digest}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@dataclass(frozen=True)
class Route:
    url_path: str
    """URL path template, e.g. ``/api/items/{item_id}``."""
    file: Path
    function: str
    """Function path in the build output, e.g. ``api/items/[item_id]``."""
    pattern: str | None
    """Regex matching this route's URLs, or None if the path is static."""
    rank: tuple[int, ...]
    segments: tuple[Path, ...]
    """Directories from ``app/`` down to the route's own, root to leaf.
    Route groups and dynamic directories count."""

    def load(self) -> ModuleType:
        """Import the route module via ``load_module``."""
        return load_module(self.file)

    def segment_files(self, *filenames: str) -> list[Path]:
        """Existing ``filenames`` in this route's segments, root to leaf and in
        the given order within each segment. Sibling directories are never
        touched."""
        return [
            segment / filename
            for segment in self.segments
            for filename in filenames
            if (segment / filename).is_file()
        ]

    def load_segments(self, filename: str) -> list[ModuleType]:
        """Load ``filename`` from each of this route's segments that has one,
        root to leaf, via ``load_module``."""
        return [load_module(path) for path in self.segment_files(filename)]


def _route(app_dir: Path, route_file: Path, base_path: str = "") -> Route:
    """``base_path`` must already be normalized."""
    url, function, regex, rank = [], [], [], []
    parts = route_file.parent.relative_to(app_dir).parts
    segments = [app_dir]
    for part in parts:
        segments.append(segments[-1] / part)
    for part in parts:
        if part.startswith("(") and part.endswith(")"):
            continue  # route group: organizational only
        function.append(part)
        if part.startswith("[...") and part.endswith("]"):
            url.append(f"{{{part[4:-1]}:path}}")
            regex.append("(.+)")
            rank.append(2)
        elif part.startswith("[") and part.endswith("]"):
            url.append(f"{{{part[1:-1]}}}")
            regex.append("([^/]+)")
            rank.append(1)
        else:
            url.append(part)
            regex.append(re.escape(part))
            rank.append(0)
    url_path = "/".join([base_path, *url]) if url else base_path
    function = [*base_path.split("/")[1:], *function]
    return Route(
        url_path=url_path or "/",
        file=route_file,
        function="/".join(function) or "index",
        pattern=f"^{re.escape(base_path)}/{'/'.join(regex)}$" if any(rank) else None,
        rank=tuple(rank),
        segments=tuple(segments),
    )


def discover_routes(app_dir: str | Path, base_path: str = "") -> list[Route]:
    """Find all routes, most specific first, with URLs and function paths
    under ``base_path``.

    Static segments sort ahead of dynamic ones, and catch-alls go last, so
    ``/items/new`` wins over ``/items/{item_id}``.
    """
    base_path = normalize_base_path(base_path)
    app_dir = Path(app_dir).resolve()
    if not app_dir.is_dir():
        raise FileNotFoundError(f"app directory not found: {app_dir}")

    # Let route modules import sibling packages from the project root.
    project_root = str(app_dir.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    routes = [_route(app_dir, file, base_path) for file in app_dir.rglob(ROUTE_FILE)]
    return sorted(routes, key=lambda route: (route.rank, route.url_path, route.file))


def _middleware(route: Route) -> list[Middleware]:
    """Each ancestor ``middleware.py``'s ``middleware``, root to leaf."""
    chain = []
    for module in route.load_segments(MIDDLEWARE_FILE):
        middleware = getattr(module, "middleware", None)
        if not inspect.iscoroutinefunction(middleware):
            raise TypeError(
                f"invalid `middleware` in {module.__file__}: expected "
                f"`async def middleware(request, call_next)`, got {middleware!r}"
            )
        chain.append(middleware)
    return chain


def load_segment_error_handlers(modules: Sequence[ModuleType]) -> list[ErrorHandler]:
    """Validate and return ``handle_error`` from each ``error.py`` module."""
    handlers = []
    for module in modules:
        handler = getattr(module, "handle_error", None)
        if not callable(handler):
            raise TypeError(
                f"invalid `handle_error` in {module.__file__}: expected "
                f"`def handle_error(request, exc)`, got {handler!r}"
            )
        handlers.append(handler)
    return handlers


def _error_handlers(route: Route) -> list[ErrorHandler]:
    """Each ancestor ``error.py``'s ``handle_error``, root to leaf."""
    return load_segment_error_handlers(route.load_segments(ERROR_FILE))


def load_segment_dependencies(
    modules: Sequence[ModuleType],
) -> list[list[Callable[..., Any]]]:
    """Validate and return ``dependencies`` from each ``dependencies.py`` module."""
    groups: list[list[Callable[..., Any]]] = []
    for module in modules:
        declared = getattr(module, "dependencies", None)
        if not isinstance(declared, list | tuple) or not all(
            callable(check) for check in declared
        ):
            raise TypeError(
                f"invalid `dependencies` in {module.__file__}: expected a list "
                f"or tuple of dependency callables, got {declared!r}"
            )
        if generators := [check for check in declared if is_generator_callable(check)]:
            raise TypeError(
                f"invalid `dependencies` in {module.__file__}: generator "
                f"functions never run as checks, got {generators!r}"
            )
        groups.append(list(declared))
    return groups


def _dependencies(route: Route) -> list[list[Callable[..., Any]]]:
    """Each ancestor ``dependencies.py``'s ``dependencies``, one group per
    file, root to leaf and in declared order within each."""
    return load_segment_dependencies(route.load_segments(DEPENDENCIES_FILE))


def _add_route(app: Chano, route: Route) -> None:
    middleware = _middleware(route)
    error_handlers = _error_handlers(route)
    dependencies = _dependencies(route)
    module = route.load()
    for method in HTTP_METHODS:
        handler = getattr(module, method, None)
        if callable(handler):
            app.add_route(
                route.url_path,
                handler,
                methods=[method],
                middleware=middleware,
                error_handlers=error_handlers,
                dependencies=dependencies,
            )


def create_app(
    app_dir: str | Path = "app",
    *,
    public_dir: str | Path | None = None,
    title: str = "Chano",
    base_path: str | None = None,
) -> Chano:
    """Build one app serving every route. Used for local development and for
    generating the OpenAPI schema at build time.

    ``public_dir`` is only for local development: in a deployment, static
    files are served by the platform. ``base_path=None`` reads it from
    ``[tool.chano]`` in the ``pyproject.toml`` beside ``app_dir``.

    Queue consumers (``queue.py``), private schedules (``schedule.py``) and
    configured ``[[tool.vercel.workflows]]`` endpoints are wired for local
    invocation without exposing them in OpenAPI.
    """
    from chano.queues import (
        QueueNotFoundError,
        dispatch_loaded_queues,
        invoke_resolved_queues,
        load_all_queues,
        resolve_queues,
    )
    from chano.schedules import (
        ScheduleNotFoundError,
        invoke_loaded_schedule,
        load_all_schedules,
        resolve_schedule,
        run_loaded_schedule,
    )
    from chano.workflows import mount_workflows

    config = project_config(app_dir)
    if base_path is None:
        base_path = config.base_path
    app = Chano(title=title, public_dir=public_dir, base_path=base_path)
    for route in discover_routes(app_dir, app.base_path):
        _add_route(app, route)

    queues = tuple(load_all_queues(app_dir))
    app.queues = queues

    async def _dispatch_queue(
        topic: str,
        payload: Any,
        *,
        consumer: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return await dispatch_loaded_queues(
            queues, topic, payload, consumer=consumer, headers=headers
        )

    app._queue_dispatcher = _dispatch_queue

    if queues:

        async def _dev_queue_trigger(
            topic: str, request: Request, consumer: str | None = None
        ) -> Any:
            try:
                matches = resolve_queues(queues, topic, consumer=consumer)
            except QueueNotFoundError as exc:
                raise NotFound(str(exc)) from None
            body = await request.body()
            result = await invoke_resolved_queues(
                matches,
                body,
                topic=topic,
                headers=request.headers,
            )
            if isinstance(result, Response):
                return result
            if result is None:
                return Response(status_code=200)
            return result

        app.add_route(
            f"{app.base_path}/_chano/queues/{{topic}}",
            _dev_queue_trigger,
            methods=["POST"],
            include_in_schema=False,
        )

    schedules = tuple(load_all_schedules(app_dir))
    app.schedules = schedules

    async def _run_schedule(
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return await run_loaded_schedule(
            schedules,
            target,
            headers=headers,
        )

    app._schedule_runner = _run_schedule

    if schedules:

        async def _dev_schedule_trigger(target: str, request: Request) -> Any:
            try:
                resolved = resolve_schedule(schedules, target)
            except ScheduleNotFoundError as exc:
                raise NotFound(str(exc)) from None
            result = await invoke_loaded_schedule(resolved, headers=request.headers)
            if isinstance(result, Response):
                return result
            if result is None:
                return Response(status_code=200)
            return result

        app.add_route(
            f"{app.base_path}/_chano/schedules/{{target:path}}",
            _dev_schedule_trigger,
            methods=["POST"],
            include_in_schema=False,
        )

    app.workflows = config.workflows
    if config.workflows:
        mount_workflows(
            app,
            config.workflows,
            project_root=Path(app_dir).resolve().parent,
        )

    return app


def create_route_app(
    app_dir: str | Path,
    function: str,
    *,
    base_path: str | None = None,
    after_backend: AfterBackend = "local",
) -> Chano:
    """Build the single-route app that runs inside one deployed function.

    The docs endpoints are off: the build emits them as static files instead.
    ``base_path=None`` reads it from ``[tool.chano]`` as in ``create_app``.
    """
    if base_path is None:
        base_path = project_base_path(app_dir)
    app = Chano(docs=False, base_path=base_path, after_backend=after_backend)
    for route in discover_routes(app_dir, app.base_path):
        if route.function == function:
            _add_route(app, route)
            return app
    raise LookupError(f"no route for function {function!r} in {app_dir}")
