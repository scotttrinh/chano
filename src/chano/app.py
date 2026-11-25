from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import inspect
import mimetypes
import re
import sys
import traceback
import types
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from chano.config import WorkflowConfig, normalize_base_path
from chano.openapi import openapi_info, openapi_schema, swagger_html
from chano.params import Endpoint
from chano.requests import Receive, Request, Scope, Send
from chano.responses import (
    Failure,
    HTMLResponse,
    JSONResponse,
    Outcome,
    PlainTextResponse,
    Response,
    _unwrap_type,
)
from chano.status import MethodNotAllowed, NotFound

_PATH_PARAM = re.compile(r"\{(\w+)(:path)?\}")

type CallNext = Callable[[Request], Awaitable[Response]]
type Guard = Callable[..., object]
type Recover = Callable[[Request, Any], Response | Awaitable[Response]]
type Wrap = Callable[[Request, CallNext], Awaitable[Response]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
AfterBackend = Literal["local", "wait_until"]

_AFTER_BACKENDS = ("local", "wait_until")


@dataclass(frozen=True)
class Boundary:
    """Folder-scoped guards, error recovery, and HTTP wrappers."""

    guards: Sequence[Guard] = ()
    recover: Sequence[Recover] = ()
    wrap: Sequence[Wrap] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "guards", tuple(self.guards))
        object.__setattr__(self, "recover", tuple(self.recover))
        object.__setattr__(self, "wrap", tuple(self.wrap))


class _AfterState:
    """Per-request ``after()`` state; closing it blocks detached or nested calls."""

    __slots__ = ("backend", "closed", "tasks")

    def __init__(self, backend: AfterBackend) -> None:
        self.backend = backend
        self.closed = False
        self.tasks: list[Callable[[], Awaitable[None]]] = []

    def schedule(self, task: Callable[[], Awaitable[None]]) -> None:
        if self.closed:
            raise RuntimeError("after() can only be called while handling a request")
        if self.backend == "wait_until":
            functions = importlib.import_module("vercel.functions")
            functions.wait_until(task())
        else:
            self.tasks.append(task)

    def close_and_take(self) -> list[Callable[[], Awaitable[None]]]:
        self.closed = True
        tasks = self.tasks
        self.tasks = []
        return tasks


_after_state: ContextVar[_AfterState | None] = ContextVar(
    "chano_after_state", default=None
)


def after(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
    """Schedule ``fn(*args, **kwargs)`` to run after the response is sent.

    ``fn`` may be sync or async; sync callables run in a worker thread via
    ``asyncio.to_thread``. Must be called while handling a request, otherwise
    raises ``RuntimeError``. Exceptions are logged to ``stderr`` and never
    reach the client.
    """
    state = _after_state.get()
    if state is None or state.closed:
        raise RuntimeError("after() can only be called while handling a request")
    if not callable(fn):
        raise TypeError(f"after() requires a callable, got {fn!r}")
    state.schedule(_make_after_task(fn, args, kwargs))


def _make_after_task(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Callable[[], Awaitable[None]]:
    """Bind ``fn(*args, **kwargs)`` into an isolated coroutine factory."""
    call = functools.partial(fn, *args, **kwargs) if (args or kwargs) else fn
    is_async = _is_async(fn)

    async def run() -> None:
        outer_cancelled = False

        async def invoke() -> None:
            try:
                if is_async:
                    await call()
                else:
                    result = await asyncio.to_thread(call)
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError as exc:
                if not outer_cancelled:
                    traceback.print_exception(exc, file=sys.stderr)
            except Exception as exc:
                traceback.print_exception(exc, file=sys.stderr)

        token = _after_state.set(None)
        try:
            child = asyncio.create_task(invoke())
            try:
                await asyncio.shield(child)
            except asyncio.CancelledError:
                outer_cancelled = True
                child.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await child
                raise
        finally:
            _after_state.reset(token)

    return run


def error_response(exc: Exception) -> Response:
    """The default mapping: ``Failure`` is sent as-is, anything else is logged
    and becomes a plain-text 500."""
    if isinstance(exc, Failure):
        return exc
    traceback.print_exception(exc, file=sys.stderr)
    return PlainTextResponse("Internal Server Error", 500)


def _call_sync_handler(handler: Recover, request: Request, exc: Exception) -> Any:
    """Run a sync recover function with ``exc`` active so a bare ``raise`` works."""
    tb = exc.__traceback__
    try:
        raise exc
    except Exception:
        exc.__traceback__ = tb
        return handler(request, exc)


def _is_async(handler: Callable[..., Any]) -> bool:
    """An ``async def`` function or an instance with ``async def __call__``."""
    return inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        type(handler).__call__
    )


def _recover_annotation(fn: Recover) -> Any:
    params = list(inspect.signature(fn, eval_str=True).parameters.values())
    if len(params) < 2 or params[1].annotation is inspect.Parameter.empty:
        return Exception
    return params[1].annotation


def _matches_recover(error: Exception, annotation: Any) -> bool:
    annotation = _unwrap_type(annotation)
    if annotation is Any or annotation is object:
        return True
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        return any(_matches_recover(error, arg) for arg in get_args(annotation))
    if isinstance(origin, type):
        if not isinstance(error, origin):
            return False
        args = get_args(annotation)
        if issubclass(origin, Outcome) and len(args) == 1:
            content_type = _unwrap_type(args[0])
            if content_type is not Any and isinstance(content_type, type):
                return isinstance(error.content, content_type)
        return True
    return isinstance(annotation, type) and isinstance(error, annotation)


def _recover_candidates(boundaries: Sequence[Boundary]) -> tuple[Recover, ...]:
    return tuple(fn for b in reversed(boundaries) for fn in b.recover)


async def _run_recover(
    candidates: Sequence[Recover], request: Request, exc: Exception
) -> Response:
    """Run matching recover functions (nearest folder first, list order)."""
    for fn in candidates:
        if not _matches_recover(exc, _recover_annotation(fn)):
            continue
        tb = exc.__traceback__
        try:
            try:
                raise exc
            except Exception:
                exc.__traceback__ = tb
                if _is_async(fn):
                    response = fn(request, exc)
                else:
                    response = await asyncio.to_thread(
                        _call_sync_handler, fn, request, exc
                    )
                if inspect.isawaitable(response):
                    response = await response
                if not isinstance(response, Response):
                    name = getattr(fn, "__qualname__", repr(fn))
                    raise TypeError(
                        f"recover {fn.__module__}.{name} "
                        f"returned {response!r}, not a Response"
                    ) from None
                return response
        except Exception as raised:
            exc = raised
    return error_response(exc)


def _wrap(wrapper: Wrap, call_next: CallNext) -> CallNext:
    async def call(request: Request) -> Response:
        response = await wrapper(request, call_next)
        if not isinstance(response, Response):
            raise TypeError(
                f"wrap {wrapper.__module__}.{wrapper.__qualname__} "
                f"returned {response!r}, not a Response"
            )
        return response

    return call


def _compile_path(path: str) -> re.Pattern[str]:
    """``/items/{item_id}`` -> regex; ``{name:path}`` also matches slashes."""
    regex, position = "", 0
    for match in _PATH_PARAM.finditer(path):
        regex += re.escape(path[position : match.start()])
        regex += f"(?P<{match[1]}>{'.+' if match[2] else '[^/]+'})"
        position = match.end()
    return re.compile(regex + re.escape(path[position:]))


class Chano:
    """A small ASGI application: routes, request validation and OpenAPI.

    ``docs`` and ``public_dir`` exist for local development. In a deployment
    the docs are pre-rendered and static files are served by the platform.
    ``base_path`` prefixes the docs and static URLs; route paths passed to
    ``add_route`` already include it.
    """

    def __init__(
        self,
        *,
        title: str = "Chano",
        version: str = "0.1.0",
        docs: bool = True,
        public_dir: str | Path | None = None,
        base_path: str = "",
        after_backend: AfterBackend = "local",
    ) -> None:
        if after_backend not in _AFTER_BACKENDS:
            raise ValueError(
                f"unknown after_backend {after_backend!r}: "
                f"expected one of {_AFTER_BACKENDS}"
            )
        self.title = title
        self.base_path = normalize_base_path(base_path)
        self.version = version
        self.docs = docs
        self.public_dir = Path(public_dir).resolve() if public_dir else None
        self.after_backend: AfterBackend = after_backend
        self.routes: list[tuple[str, re.Pattern[str], dict[str, Endpoint]]] = []
        self.hidden_operations: set[tuple[str, str]] = set()
        """``(path, method)`` pairs served but left out of the OpenAPI schema."""
        self.boundaries: dict[str, tuple[Boundary, ...]] = {}
        """Each path's ancestor boundaries, root to leaf."""
        self._mounted_apps: dict[str, ASGIApp] = {}
        """Internal ASGI sub-apps mounted by exact path (excluded from OpenAPI)."""
        self.queues: tuple[Any, ...] = ()
        """Discovered queue consumers attached by ``create_app``."""
        self.schedules: tuple[Any, ...] = ()
        """Discovered schedule jobs attached by ``create_app``."""
        self.workflows: tuple[WorkflowConfig, ...] = ()
        """Configured workflow entrypoints from ``[[tool.vercel.workflows]]``."""
        self.workflows_mounted: bool = False
        """Whether LocalWorld workflow endpoints are mounted on this app."""
        self._queue_dispatcher: Callable[..., Awaitable[Any]] | None = None
        self._schedule_runner: Callable[..., Awaitable[Any]] | None = None
        self._startup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._shutdown_hooks: list[Callable[[], Awaitable[None]]] = []

    def on_startup(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register an async hook invoked on ASGI ``lifespan.startup``."""
        self._startup_hooks.append(hook)

    def on_shutdown(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register an async hook invoked on ASGI ``lifespan.shutdown``."""
        self._shutdown_hooks.append(hook)

    def mount_asgi(self, path: str, asgi_app: ASGIApp) -> None:
        """Mount a raw ASGI callable at exact ``path``, outside OpenAPI."""
        if not path.startswith("/"):
            raise ValueError(f"mount path must start with '/': {path!r}")
        self._mounted_apps[path] = asgi_app

    async def dispatch_queue(
        self,
        topic: str,
        payload: Any,
        *,
        consumer: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Dispatch ``payload`` to local queue consumer(s) subscribed to ``topic``."""
        if self._queue_dispatcher is None:
            raise LookupError(f"no queue consumer registered for topic {topic!r}")
        return await self._queue_dispatcher(
            topic, payload, consumer=consumer, headers=headers
        )

    async def run_schedule(
        self,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Run a local schedule job matching ``target``."""
        if self._schedule_runner is None:
            raise LookupError(f"no schedule registered matching {target!r}")
        return await self._schedule_runner(target, headers=headers)

    def add_route(
        self,
        path: str,
        handler: Callable[..., Any],
        methods: list[str],
        *,
        include_in_schema: bool | None = None,
        boundaries: Sequence[Boundary] = (),
    ) -> None:
        """Register ``handler`` for ``path``; ``include_in_schema=False``
        (or ``@openapi(include_in_schema=False)`` on ``handler``) serves it
        without documenting it in OpenAPI.

        ``boundaries`` (root to leaf) supply guards, recover functions, and
        wrappers for ``path``. Each call for one path must pass the same chain."""
        if include_in_schema is None:
            info = openapi_info(handler)
            include_in_schema = (
                True
                if info is None or info.include_in_schema is None
                else info.include_in_schema
            )
        path_params = {m[1] for m in _PATH_PARAM.finditer(path)}
        chain = tuple(boundaries)
        endpoint = Endpoint(handler, path_params, [b.guards for b in chain])
        endpoints = next((e for p, _, e in self.routes if p == path), None)
        if endpoints is None:
            endpoints = {}
            self.routes.append((path, _compile_path(path), endpoints))
            self.boundaries[path] = chain
        elif self.boundaries[path] != chain:
            raise ValueError(f"conflicting boundaries for route {path!r}")
        for method in map(str.upper, methods):
            endpoints[method] = endpoint
            if include_in_schema:
                self.hidden_operations.discard((path, method))
            else:
                self.hidden_operations.add((path, method))

    def openapi(self) -> dict[str, Any]:
        return openapi_schema(self)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    for hook in self._startup_hooks:
                        await hook()
                elif message["type"] == "lifespan.shutdown":
                    for hook in reversed(self._shutdown_hooks):
                        await hook()
                await send({"type": f"{message['type']}.complete"})
                if message["type"] == "lifespan.shutdown":
                    return
        if scope["type"] != "http":
            return

        if mounted := self._mounted_apps.get(scope["path"]):
            await mounted(scope, receive, send)
            return

        request = Request(scope, receive)
        after_state = _AfterState(self.after_backend)
        token = _after_state.set(after_state)
        cancelled = False
        try:
            try:
                response = await self._handle(request)
            except Exception as exc:  # unmatched paths and middleware errors
                response = error_response(exc)
            await response(receive, send, include_body=request.method != "HEAD")
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            tasks = after_state.close_and_take()
            _after_state.reset(token)
            if not cancelled:
                for task in tasks:
                    await task()

    async def _handle(self, request: Request) -> Response:
        """Match in Build Output order: literal routes, then docs and
        ``public_dir``, then dynamic routes."""
        literal = [route for route in self.routes if not route[1].groups]
        if response := await self._dispatch(request, literal):
            return response
        if response := self._dev_file(request):
            return response
        dynamic = [route for route in self.routes if route[1].groups]
        if response := await self._dispatch(request, dynamic):
            return response
        raise NotFound("Not Found")

    async def _dispatch(
        self,
        request: Request,
        routes: list[tuple[str, re.Pattern[str], dict[str, Endpoint]]],
    ) -> Response | None:
        """Serve the request with the first of ``routes`` whose regex matches
        the path, as the platform sends it to that route's function, or
        return None if none matches.

        The route's middleware wraps its endpoint, or the ``405`` when no
        endpoint supports the method. Inside the innermost middleware, the
        route's error boundary has already turned endpoint errors into a
        ``Response``."""
        for path, pattern, endpoints in routes:
            if match := pattern.fullmatch(request.path):
                request.path_params = match.groupdict()
                boundaries = self.boundaries.get(path, ())
                call = self._endpoint_call(endpoints, _recover_candidates(boundaries))
                for wrapper in reversed([w for b in boundaries for w in b.wrap]):
                    call = _wrap(wrapper, call)
                return await call(request)
        return None

    @staticmethod
    def _endpoint_call(
        endpoints: dict[str, Endpoint], recover: Sequence[Recover]
    ) -> CallNext:
        async def call(request: Request) -> Response:
            method = "GET" if request.method == "HEAD" else request.method
            endpoint = endpoints.get(request.method) or endpoints.get(method)
            try:
                if endpoint is None:
                    headers = {"allow": ", ".join(sorted(endpoints))}
                    raise MethodNotAllowed("Method Not Allowed", headers=headers)
                result = await endpoint.call(request)
                if isinstance(result, Failure):
                    raise result
                if isinstance(result, Response):
                    return result
                return JSONResponse(result)
            except Exception as exc:
                return await _run_recover(recover, request, exc)

        return call

    def _dev_file(self, request: Request) -> Response | None:
        """The docs, ``openapi.json`` or a ``public_dir`` file at the path,
        with Build Output precedence; ``405`` for methods other than
        ``GET``/``HEAD``.

        The build writes ``openapi.json`` and ``docs.html`` over ``public/``,
        but ``public/docs`` wins over the ``docs`` -> ``docs.html`` rewrite.
        """
        path, openapi_url = request.path, f"{self.base_path}/openapi.json"
        docs_html = f"{self.base_path}/docs.html"
        if self.docs and path == openapi_url:
            self._require_get(request)
            return JSONResponse(self.openapi())
        if self.docs and path == docs_html:
            self._require_get(request)
            return HTMLResponse(swagger_html(self.title, openapi_url))
        if response := self._static_file(path, request.method):
            return response
        if self.docs and path == f"{self.base_path}/docs":
            self._require_get(request)
            return HTMLResponse(swagger_html(self.title, openapi_url))
        return None

    @staticmethod
    def _require_get(request: Request) -> None:
        if request.method not in ("GET", "HEAD"):
            raise MethodNotAllowed("Method Not Allowed", headers={"allow": "GET, HEAD"})

    def _static_file(self, path: str, method: str = "GET") -> Response | None:
        if self.public_dir is None or path.endswith("/"):
            return None
        if self.base_path:
            # Only paths strictly under the prefix: not /apiary or /api itself.
            if not path.startswith(f"{self.base_path}/"):
                return None
            path = path[len(self.base_path) :]
        try:
            file = (self.public_dir / path.lstrip("/")).resolve()
            if not file.is_relative_to(self.public_dir) or not file.is_file():
                return None
        except (OSError, RuntimeError, ValueError):  # symlink loops, NUL bytes
            return None
        if method not in ("GET", "HEAD"):
            raise MethodNotAllowed("Method Not Allowed", headers={"allow": "GET, HEAD"})
        media_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        return Response(file.read_bytes(), media_type=media_type)


class EndpointASGIApp:
    """Single-endpoint ASGI app for private Vercel functions (queues, schedules).

    Accepts any path on ``methods``, runs the ``Endpoint`` with Chano's
    ``after()`` context, maps raised exceptions with ``error_response()``, and
    serializes non-``Response`` return values as JSON (or empty 200 for
    ``None``).
    """

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        methods: Sequence[str] = ("POST",),
        after_backend: AfterBackend = "local",
    ) -> None:
        if after_backend not in _AFTER_BACKENDS:
            raise ValueError(
                f"unknown after_backend {after_backend!r}: "
                f"expected one of {_AFTER_BACKENDS}"
            )
        self.endpoint = endpoint
        self.methods = tuple(dict.fromkeys(m.upper() for m in methods))
        self.after_backend: AfterBackend = after_backend

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                await send({"type": f"{message['type']}.complete"})
                if message["type"] == "lifespan.shutdown":
                    return
        if scope["type"] != "http":
            return

        request = Request(scope, receive)
        after_state = _AfterState(self.after_backend)
        token = _after_state.set(after_state)
        cancelled = False
        try:
            try:
                if request.method not in self.methods:
                    headers = {"allow": ", ".join(sorted(self.methods))}
                    raise MethodNotAllowed("Method Not Allowed", headers=headers)
                result = await self.endpoint.call(request)
                if isinstance(result, Failure):
                    raise result
                if isinstance(result, Response):
                    response = result
                elif result is None:
                    response = Response(status_code=200)
                else:
                    response = JSONResponse(result)
            except Exception as exc:
                response = error_response(exc)
            await response(receive, send, include_body=request.method != "HEAD")
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            tasks = after_state.close_and_take()
            _after_state.reset(token)
            if not cancelled:
                for task in tasks:
                    await task()
