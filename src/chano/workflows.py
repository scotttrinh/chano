"""Vercel Workflows support: ``[[tool.vercel.workflows]]`` build emission and
optional ``vercel-workflow`` integration.

``vercel-workflow`` is strictly optional and is never required to install or
import Chano. When ``[[tool.vercel.workflows]]`` is configured in
``pyproject.toml``:

- ``chano build`` introspects the live workflow registries and their registered
  ``vercel.queue`` subscriptions (failing with an actionable error if
  ``vercel-workflow`` / ``vercel-queue`` is missing or no subscriptions are
  registered), emitting ``.well-known/workflow/v1/flow.func`` (and
  ``.well-known/workflow/v1/step.func`` only when step subscriptions are
  actually registered), tracing imports from each configured entrypoint module.
- Deployed workflow functions serve deliveries through
  ``vercel_runtime.workers.create_queue_service_app()`` (using the
  ``vercel.queue`` subscriptions registered when ``Workflows`` is imported)
  or adapt ``Workflows.http_handler`` directly.
- ``create_app`` / ``chano dev`` mounts ``Workflows.http_handler`` at
  ``/.well-known/workflow/v1/flow`` and ``Workflows.manifest_handler`` at
  ``/.well-known/workflow/v1/manifest.json`` (outside OpenAPI) when
  ``vercel-workflow`` is installed.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import os
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from chano.app import ASGIApp
from chano.config import WorkflowConfig
from chano.requests import Receive, Scope, Send

if TYPE_CHECKING:
    from chano.app import Chano

WORKFLOW_FLOW_FUNCTION = ".well-known/workflow/v1/flow"
WORKFLOW_STEP_FUNCTION = ".well-known/workflow/v1/step"
WORKFLOW_FLOW_PATH = f"/{WORKFLOW_FLOW_FUNCTION}"
WORKFLOW_MANIFEST_PATH = "/.well-known/workflow/v1/manifest.json"

WORKFLOW_CONSUMER = "default"
WORKFLOW_FLOW_MAX_DURATION = 60
WORKFLOW_STEP_MAX_DURATION = "max"
WORKFLOW_RETRY_AFTER_SECONDS = 5
WORKFLOW_INITIAL_DELAY_SECONDS = 0
WORKFLOW_SEQUENTIAL_REPLAYS_ENV = "WORKFLOW_SEQUENTIAL_REPLAYS"

_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*$")

WorkflowKind = Literal["flow", "step"]


def validate_workflow_namespace(namespace: str | None) -> None:
    """Validate an optional workflow queue namespace (matching ``vercel-workflow``)."""
    if namespace is not None and not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(
            f'Invalid queue namespace "{namespace}": must be lowercase alphanumeric, '
            "starting with a letter"
        )


def get_workflow_topic_prefix(namespace: str | None = None) -> str:
    """Return the workflow queue topic prefix for ``namespace`` (delegating to
    ``vercel.workflow``'s helper when installed)."""
    validate_workflow_namespace(namespace)
    with contextlib.suppress(ImportError):
        world_mod = importlib.import_module("vercel.workflow._internal.world")
        helper = getattr(world_mod, "get_queue_topic_prefix", None)
        if callable(helper):
            return str(helper(namespace))
    if namespace is not None:
        return f"__{namespace}_wkf_workflow_"
    return "__wkf_workflow_"


def get_step_topic_prefix(namespace: str | None = None) -> str:
    """Return the step queue topic prefix for ``namespace``."""
    validate_workflow_namespace(namespace)
    if namespace is not None:
        return f"__{namespace}_wkf_step_"
    return "__wkf_step_"


def is_sequential_replays_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether ``WORKFLOW_SEQUENTIAL_REPLAYS=1`` is set."""
    source = os.environ if env is None else env
    return source.get(WORKFLOW_SEQUENTIAL_REPLAYS_ENV) == "1"


def workflow_flow_trigger(
    *,
    namespace: str | None = None,
    topic: str | None = None,
    consumer: str = WORKFLOW_CONSUMER,
    retry_after: int | None = WORKFLOW_RETRY_AFTER_SECONDS,
    initial_delay: int | None = WORKFLOW_INITIAL_DELAY_SECONDS,
    max_concurrency: int | None = None,
    sequential_replays: bool | None = None,
) -> dict[str, object]:
    """Build a ``queue/v2beta`` trigger for ``.well-known/workflow/v1/flow.func``."""
    enabled = (
        is_sequential_replays_enabled()
        if sequential_replays is None
        else sequential_replays
    )
    resolved_topic = topic or f"{get_workflow_topic_prefix(namespace)}*"
    trigger: dict[str, object] = {
        "type": "queue/v2beta",
        "topic": resolved_topic,
        "consumer": consumer,
    }
    if retry_after is not None:
        trigger["retryAfterSeconds"] = retry_after
    if initial_delay is not None:
        trigger["initialDelaySeconds"] = initial_delay
    if enabled:
        trigger["maxConcurrency"] = 1
    elif max_concurrency is not None:
        trigger["maxConcurrency"] = max_concurrency
    return trigger


def workflow_step_trigger(
    *,
    namespace: str | None = None,
    topic: str | None = None,
    consumer: str = WORKFLOW_CONSUMER,
    retry_after: int | None = WORKFLOW_RETRY_AFTER_SECONDS,
    initial_delay: int | None = WORKFLOW_INITIAL_DELAY_SECONDS,
) -> dict[str, object]:
    """Build a ``queue/v2beta`` trigger for ``.well-known/workflow/v1/step.func``."""
    resolved_topic = topic or f"{get_step_topic_prefix(namespace)}*"
    trigger: dict[str, object] = {
        "type": "queue/v2beta",
        "topic": resolved_topic,
        "consumer": consumer,
    }
    if retry_after is not None:
        trigger["retryAfterSeconds"] = retry_after
    if initial_delay is not None:
        trigger["initialDelaySeconds"] = initial_delay
    return trigger


def resolve_workflow_trace_roots(
    workflows: Sequence[WorkflowConfig], project_dir: Path
) -> tuple[Path, ...]:
    """Locate local Python source files for configured workflow entrypoints so
    build-time import tracing vendors all workflow and step dependencies."""
    project_root = project_dir.resolve()
    search_roots = [project_root]
    src_dir = project_root / "src"
    if src_dir.is_dir():
        search_roots.append(src_dir)

    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            found.append(resolved)

    for workflow in workflows:
        parts = workflow.module_name.split(".")
        for root in search_roots:
            pkg_dir = root.joinpath(*parts)
            init_file = pkg_dir / "__init__.py"
            py_file = root.joinpath(*parts[:-1], f"{parts[-1]}.py")
            if init_file.is_file() or py_file.is_file():
                for idx in range(1, len(parts)):
                    ancestor_init = root.joinpath(*parts[:idx], "__init__.py")
                    if ancestor_init.is_file():
                        add(ancestor_init)
                if init_file.is_file():
                    add(init_file)
                if py_file.is_file():
                    add(py_file)
                break
    return tuple(found)


def _normalize_workflow(item: str | WorkflowConfig) -> WorkflowConfig:
    return item if isinstance(item, WorkflowConfig) else WorkflowConfig(entrypoint=item)


def load_workflow_entrypoint(entrypoint: str | WorkflowConfig) -> Any:
    """Import ``module:object`` and return the referenced ``Workflows`` registry."""
    config = _normalize_workflow(entrypoint)
    module = importlib.import_module(config.module_name)
    obj: Any = module
    for attr in config.object_name.split("."):
        if not hasattr(obj, attr):
            raise AttributeError(
                f"workflow entrypoint {config.entrypoint!r}: "
                f"attribute {attr!r} not found on {obj!r}"
            )
        obj = getattr(obj, attr)
    return obj


def _is_optional_vercel_missing(exc: ModuleNotFoundError) -> bool:
    name = exc.name or ""
    return (
        name == "vercel"
        or name.startswith("vercel.")
        or name == "vercel_workflow"
        or name.startswith("vercel_workflow.")
        or name == "vercel_runtime"
        or name.startswith("vercel_runtime.")
    )


def _trigger_from_subscription(sub: Any, *, kind: WorkflowKind) -> dict[str, object]:
    consumer = (
        str(sub.consumer_group)
        if getattr(sub, "consumer_group", None)
        else WORKFLOW_CONSUMER
    )
    retry_after = (
        sub.retry_after_seconds
        if getattr(sub, "retry_after_seconds", None) is not None
        else WORKFLOW_RETRY_AFTER_SECONDS
    )
    initial_delay = (
        sub.initial_delay_seconds
        if getattr(sub, "initial_delay_seconds", None) is not None
        else WORKFLOW_INITIAL_DELAY_SECONDS
    )
    if kind == "step":
        return workflow_step_trigger(
            topic=str(sub.topic),
            consumer=consumer,
            retry_after=retry_after,
            initial_delay=initial_delay,
        )
    max_concurrency = (
        sub.max_concurrency
        if getattr(sub, "max_concurrency", None) is not None
        else None
    )
    return workflow_flow_trigger(
        topic=str(sub.topic),
        consumer=consumer,
        retry_after=retry_after,
        initial_delay=initial_delay,
        max_concurrency=max_concurrency,
    )


def _subscription_owner(sub: Any) -> Any | None:
    """Extract the owning ``Workflows`` registry from a ``vercel.queue``
    ``Subscription`` created by ``vercel-workflow``, or ``None`` if unavailable."""
    func = getattr(sub, "func", None)
    closure = getattr(func, "__closure__", None)
    if not closure:
        return None
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if isinstance(value, functools.partial) and "registry" in (
            value.keywords or {}
        ):
            return value.keywords["registry"]
    return None


def _subscriptions_for_registry(candidates: Sequence[Any], registry: Any) -> list[Any]:
    """Return subscriptions from ``candidates`` whose verified owner is ``registry``."""
    return [sub for sub in candidates if _subscription_owner(sub) is registry]


def introspect_workflow_triggers(
    workflows: Sequence[WorkflowConfig],
    project_dir: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Derive ``(flow_triggers, step_triggers)`` from live ``Workflows``
    registries and their actual registered ``vercel.queue`` subscriptions.

    Verifies that each configured registry actually serves queue deliveries
    (via ``http_handler``) and owns its registered subscription(s), failing
    with an actionable ``RuntimeError`` when ``vercel-workflow`` /
    ``vercel-queue`` is unavailable or a registry does not serve queue
    deliveries. Never manufactures triggers that are not backed by actual
    SDK subscriptions or attributes another registry's subscriptions.
    """
    project_root = str(project_dir.resolve())
    added_path = project_root not in sys.path
    if added_path:
        sys.path.insert(0, project_root)

    loaded_entries: list[tuple[WorkflowConfig, Any, str | None]] = []
    try:
        try:
            queue_mod = importlib.import_module("vercel.queue")
        except ModuleNotFoundError as exc:
            if _is_optional_vercel_missing(exc):
                raise RuntimeError(
                    "Cannot build [[tool.vercel.workflows]]: 'vercel-workflow' "
                    "and 'vercel-queue' must be installed in the build environment."
                ) from exc
            raise

        get_subs = getattr(queue_mod, "get_subscriptions", None)
        if not callable(get_subs):
            raise RuntimeError(
                "Cannot build [[tool.vercel.workflows]]: installed 'vercel.queue' "
                "does not expose get_subscriptions()."
            )

        for wf in workflows:
            try:
                registry = load_workflow_entrypoint(wf)
            except ModuleNotFoundError as exc:
                if _is_optional_vercel_missing(exc):
                    raise RuntimeError(
                        f"Cannot build workflow entrypoint {wf.entrypoint!r}: "
                        "'vercel-workflow' is not installed in the build environment. "
                        "Install 'vercel-workflow' (or 'vercel>=0.10.0') to build "
                        "[[tool.vercel.workflows]]."
                    ) from exc
                raise
            if _registry_http_handler(registry) is None:
                raise RuntimeError(
                    f"Workflow entrypoint {wf.entrypoint!r} does not serve queue "
                    "deliveries (its `http_handler` is unavailable). Ensure it "
                    "exports a vercel.workflow.Workflows registry with "
                    "as_vercel_job=True."
                )
            ns = getattr(registry, "namespace", None)
            if ns is not None and not isinstance(ns, str):
                raise ValueError(
                    f"invalid namespace on workflow {wf.entrypoint!r}: {ns!r}"
                )
            validate_workflow_namespace(ns)
            loaded_entries.append((wf, registry, ns))

        all_subs = cast("Sequence[Any]", get_subs())
    finally:
        if added_path:
            with contextlib.suppress(ValueError):
                sys.path.remove(project_root)

    subs_by_topic: dict[str, list[Any]] = {}
    for sub in all_subs:
        topic = getattr(sub, "topic", None)
        if isinstance(topic, str):
            subs_by_topic.setdefault(topic, []).append(sub)

    flow_triggers: list[dict[str, object]] = []
    step_triggers: list[dict[str, object]] = []
    seen_flow: set[tuple[str, str]] = set()
    seen_step: set[tuple[str, str]] = set()

    for wf, registry, ns in loaded_entries:
        flow_topic = f"{get_workflow_topic_prefix(ns)}*"
        step_topic = f"{get_step_topic_prefix(ns)}*"
        matched_flow = _subscriptions_for_registry(
            subs_by_topic.get(flow_topic, ()), registry
        )
        matched_step = _subscriptions_for_registry(
            subs_by_topic.get(step_topic, ()), registry
        )

        if not matched_flow and not matched_step:
            raise RuntimeError(
                f"Workflow entrypoint {wf.entrypoint!r} did not register any "
                f"vercel.queue subscription for {flow_topic!r}. Ensure it exports "
                "a vercel.workflow.Workflows registry with as_vercel_job=True."
            )

        for sub in matched_flow:
            trigger = _trigger_from_subscription(sub, kind="flow")
            key = (str(trigger["topic"]), str(trigger["consumer"]))
            if key not in seen_flow:
                seen_flow.add(key)
                flow_triggers.append(trigger)

        for sub in matched_step:
            trigger = _trigger_from_subscription(sub, kind="step")
            key = (str(trigger["topic"]), str(trigger["consumer"]))
            if key not in seen_step:
                seen_step.add(key)
                step_triggers.append(trigger)

    return flow_triggers, step_triggers


class _ASGIHTTPRequest:
    """Adapter implementing ``vercel.workflow.HTTPRequest`` over ASGI."""

    def __init__(self, scope: Scope, receive: Receive) -> None:
        self._scope = scope
        self._receive = receive
        path = scope.get("path", "/")
        qs = scope.get("query_string", b"")
        self._url = f"{path}?{qs.decode('latin-1')}" if qs else path
        self._headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", ())
        }

    @property
    def method(self) -> str:
        return str(self._scope.get("method", "GET"))

    @property
    def url(self) -> str:
        return self._url

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    async def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        while True:
            message = await self._receive()
            msg_type = message.get("type")
            if msg_type == "http.disconnect":
                return
            if msg_type != "http.request":
                raise ValueError(f"unexpected ASGI message: {msg_type!r}")
            body = cast("bytes", message.get("body", b""))
            if body:
                if chunk_size is not None and chunk_size > 0:
                    for idx in range(0, len(body), chunk_size):
                        yield body[idx : idx + chunk_size]
                else:
                    yield body
            if not message.get("more_body", False):
                return


def _http_handler_to_asgi(handler: Callable[[Any], Awaitable[Any]]) -> ASGIApp:
    """Wrap a ``vercel.workflow.HTTPHandler`` (``HTTPRequest -> HTTPResponse``)
    as a standard ASGI application."""

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                await send({"type": f"{msg['type']}.complete"})
                if msg["type"] == "lifespan.shutdown":
                    return
        if scope["type"] != "http":
            return
        req = _ASGIHTTPRequest(scope, receive)
        resp = await handler(req)
        raw_headers = [
            (str(k).encode("latin-1"), str(v).encode("latin-1"))
            for k, v in getattr(resp, "headers", {}).items()
        ]
        body = getattr(resp, "body", b"")
        if isinstance(body, str):
            body = body.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": int(resp.status),
                "headers": raw_headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

    return asgi


def _registry_http_handler(registry: Any) -> Callable[[Any], Awaitable[Any]] | None:
    try:
        handler = getattr(registry, "http_handler", None)
    except RuntimeError:
        return None
    return handler if callable(handler) else None


def _registry_manifest_handler(
    registry: Any,
) -> Callable[[Any], Awaitable[Any]] | None:
    try:
        handler = getattr(registry, "manifest_handler", None)
    except RuntimeError:
        return None
    return handler if callable(handler) else None


def _registries_flow_asgi(registries: Sequence[Any]) -> ASGIApp | None:
    """Build an ASGI app serving ``Workflows.http_handler`` for one or more
    registries, routing multi-namespace requests by ``x-vqs-queue-name``."""
    entries: list[tuple[tuple[str, str], Callable[[Any], Awaitable[Any]]]] = []
    for registry in registries:
        handler = _registry_http_handler(registry)
        if handler is not None:
            ns = getattr(registry, "namespace", None)
            prefixes = (get_workflow_topic_prefix(ns), get_step_topic_prefix(ns))
            entries.append((prefixes, handler))
    if not entries:
        return None
    if len(entries) == 1:
        return _http_handler_to_asgi(entries[0][1])

    async def dispatch(request: _ASGIHTTPRequest) -> Any:
        queue_name = request.headers.get("x-vqs-queue-name", "")
        if queue_name:
            for prefixes, handler in entries:
                if queue_name.startswith(prefixes):
                    return await handler(request)
        return await entries[0][1](request)

    return _http_handler_to_asgi(dispatch)


def _try_create_queue_service_app() -> ASGIApp | None:
    """Use ``vercel_runtime.workers.create_queue_service_app()`` when the
    runtime and ``vercel-queue`` have registered workflow subscriptions."""
    try:
        workers_mod = importlib.import_module("vercel_runtime.workers")
        queue_mod = importlib.import_module("vercel.queue")
    except ModuleNotFoundError as exc:
        if _is_optional_vercel_missing(exc):
            return None
        raise
    get_subs = getattr(queue_mod, "get_subscriptions", None)
    create_app_fn = getattr(workers_mod, "create_queue_service_app", None)
    if callable(get_subs) and callable(create_app_fn) and get_subs():
        return cast("ASGIApp", create_app_fn())
    return None


def create_workflow_app(
    entrypoints: Sequence[str | WorkflowConfig],
) -> ASGIApp:
    """Load configured workflow ``entrypoints`` and return their ASGI app.

    On Vercel, prefers ``vercel_runtime.workers.create_queue_service_app()``
    backed by the ``vercel.queue`` subscriptions registered when ``Workflows``
    is imported, and falls back to adapting ``Workflows.http_handler``.
    """
    registries = [load_workflow_entrypoint(ep) for ep in entrypoints]
    if queue_app := _try_create_queue_service_app():
        return queue_app
    if flow_app := _registries_flow_asgi(registries):
        return flow_app
    raise RuntimeError(
        "vercel-workflow is required to serve workflow handlers; "
        "install `vercel-workflow` in your project"
    )


async def _start_workflow_world() -> None:
    """Eagerly start ``LocalWorld``'s embedded queue in the ASGI lifespan task
    so ``_close_workflow_world`` exits its anyio cancel scope in the same task."""
    with contextlib.suppress(ImportError):
        world_mod = importlib.import_module("vercel.workflow._internal.world")
        get_world = getattr(world_mod, "get_world", None)
        if callable(get_world):
            world = get_world()
            get_client = getattr(world, "_get_queue_client", None)
            if callable(get_client) and callable(getattr(world, "aclose", None)):
                await cast("Awaitable[Any]", get_client())


async def _close_workflow_world() -> None:
    with contextlib.suppress(ImportError):
        world_mod = importlib.import_module("vercel.workflow._internal.world")
        get_world = getattr(world_mod, "get_world", None)
        if callable(get_world):
            world = get_world()
            aclose = getattr(world, "aclose", None)
            if callable(aclose):
                await cast("Awaitable[None]", aclose())


def mount_workflows(
    app: Chano,
    workflows: Sequence[WorkflowConfig],
    *,
    project_root: Path | None = None,
) -> bool:
    """Mount ``vercel-workflow`` HTTP endpoints on ``app`` when configured and
    ``vercel-workflow`` is installed.

    Mounts ``Workflows.http_handler`` at ``/.well-known/workflow/v1/flow``
    (matching ``vercel.workflow.ENDPOINT_PATH``) and ``Workflows.manifest_handler``
    at ``/.well-known/workflow/v1/manifest.json`` (``MANIFEST_PATH``). Returns
    ``True`` if mounted, ``False`` if ``workflows`` is empty or the optional
    ``vercel-workflow`` package is not installed.
    """
    if not workflows:
        return False
    if project_root is not None:
        root_str = str(project_root.resolve())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

    try:
        registries = [load_workflow_entrypoint(wf) for wf in workflows]
    except ModuleNotFoundError as exc:
        if _is_optional_vercel_missing(exc):
            return False
        raise

    flow_app = _registries_flow_asgi(registries)
    if flow_app is None:
        return False

    endpoint_path = WORKFLOW_FLOW_PATH
    manifest_path = WORKFLOW_MANIFEST_PATH
    with contextlib.suppress(ImportError):
        vw = importlib.import_module("vercel.workflow")
        endpoint_path = str(getattr(vw, "ENDPOINT_PATH", WORKFLOW_FLOW_PATH))
        manifest_path = str(getattr(vw, "MANIFEST_PATH", WORKFLOW_MANIFEST_PATH))

    app.mount_asgi(endpoint_path, flow_app)
    for registry in registries:
        manifest_handler = _registry_manifest_handler(registry)
        if manifest_handler is not None:
            app.mount_asgi(manifest_path, _http_handler_to_asgi(manifest_handler))
            break

    app.on_startup(_start_workflow_world)
    app.on_shutdown(_close_workflow_world)
    app.workflows_mounted = True
    return True
