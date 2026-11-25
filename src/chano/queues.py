"""Vercel Queues support: ``app/**/queue.py`` files become private consumers.

Each ``queue.py`` module exports a ``CONSUME`` handler (sync or async) and
module-level ``topic`` (required), plus optional queue tuning:

- ``consumer`` (str, default ``"default"``)
- ``retry_after`` (int >= 0 -> ``retryAfterSeconds``)
- ``initial_delay`` (int >= 0 -> ``initialDelaySeconds``)
- ``max_concurrency`` (int >= 1 -> ``maxConcurrency``)

Plus the standard Vercel function options (``max_duration``, ``memory``,
``regions``, ``include``). Queue consumers are never exposed as public HTTP
routes or documented in OpenAPI.

In deployed functions, ``create_queue_app`` registers the consumer with
``vercel.queue`` and serves push callbacks through
``vercel_runtime.workers.create_queue_service_app()`` (or
``vercel.queue.accept_and_handle``), so deliveries are accepted, leases are
renewed while running, messages are acknowledged on success, and failures
preserve retry semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import msgspec

from chano._pydantic import enc_hook as _pydantic_enc_hook
from chano.app import (
    AfterBackend,
    ASGIApp,
    EndpointASGIApp,
    _after_state,
    _AfterState,
)
from chano.params import Endpoint, is_generator_callable
from chano.requests import Message, Receive, Request, Scope, Send
from chano.responses import Failure, PlainTextResponse, Response
from chano.routing import load_module

QUEUE_FILE = "queue.py"
QUEUE_FUNCTION_PREFIX = "_queues"
QUEUE_TRIGGER_TYPE = "queue/v2beta"
QUEUE_MAX_DURATION_SECONDS = 86_400

VQS_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_queue_log = logging.getLogger("chano.queues")


class QueueNotFoundError(LookupError):
    """No queue consumer matched the requested topic/consumer."""


@dataclass(frozen=True)
class QueueConsumer:
    """One discovered ``queue.py`` module under ``app/``."""

    file: Path
    function: str
    """Function path in the build output, e.g. ``_queues/orders``."""

    def load(self) -> ModuleType:
        return load_module(self.file)


@dataclass(frozen=True)
class QueueTrigger:
    """Validated Vercel Queue trigger configuration from a ``queue.py`` module."""

    topic: str
    consumer: str = "default"
    retry_after: int | None = None
    initial_delay: int | None = None
    max_concurrency: int | None = None

    def to_vc_trigger(self) -> dict[str, object]:
        trigger: dict[str, object] = {
            "type": QUEUE_TRIGGER_TYPE,
            "topic": self.topic,
            "consumer": self.consumer,
        }
        if self.retry_after is not None:
            trigger["retryAfterSeconds"] = self.retry_after
        if self.initial_delay is not None:
            trigger["initialDelaySeconds"] = self.initial_delay
        if self.max_concurrency is not None:
            trigger["maxConcurrency"] = self.max_concurrency
        return trigger


@dataclass(frozen=True)
class LoadedQueue:
    """A discovered queue consumer with validated config and compiled ``Endpoint``."""

    spec: QueueConsumer
    trigger: QueueTrigger
    endpoint: Endpoint
    module: ModuleType


def _queue_consumer(app_dir: Path, queue_file: Path) -> QueueConsumer:
    parts = queue_file.parent.relative_to(app_dir).parts
    function_parts: list[str] = []
    for part in parts:
        if part.startswith("(") and part.endswith(")"):
            continue
        if part.startswith("[") and part.endswith("]"):
            raise ValueError(
                f"invalid queue path {queue_file}: queue.py cannot live inside "
                f"dynamic segment {part!r}"
            )
        function_parts.append(part)
    suffix = "/".join(function_parts) or "index"
    return QueueConsumer(
        file=queue_file,
        function=f"{QUEUE_FUNCTION_PREFIX}/{suffix}",
    )


def discover_queues(app_dir: str | Path) -> list[QueueConsumer]:
    """Find all ``queue.py`` consumers under ``app_dir``, sorted deterministically."""
    app_path = Path(app_dir).resolve()
    if not app_path.is_dir():
        raise FileNotFoundError(f"app directory not found: {app_path}")

    project_root = str(app_path.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    queues = [_queue_consumer(app_path, file) for file in app_path.rglob(QUEUE_FILE)]
    return sorted(queues, key=lambda q: (q.function, q.file))


def sanitize_queue_name(name: str) -> str:
    """Normalize a plain consumer group string identically to
    ``vercel.queue.sanitize_name`` and ``@vercel/build-utils``."""
    if not name:
        raise ValueError("consumer name must be a non-empty string")
    result: list[str] = []
    for char in name:
        if char == "_":
            result.append("__")
        elif char == ".":
            result.append("_D")
        elif char == "/":
            result.append("_S")
        elif char.isascii() and (char.isalnum() or char == "-"):
            result.append(char)
        else:
            result.append(f"_{ord(char):02X}")
    return "".join(result)


def validate_queue_topic(topic: object, file: Path) -> str:
    """Validate a Vercel Queues topic name or wildcard pattern."""
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError(
            f"invalid `topic` in {file}: expected a non-empty string, got {topic!r}"
        )
    cleaned = topic.strip()
    if cleaned != "*":
        prefix = cleaned.removesuffix("*")
        if not prefix or not VQS_NAME_PATTERN.fullmatch(prefix):
            raise ValueError(
                f"invalid `topic` in {file}: {cleaned!r} must contain only "
                "ASCII letters, digits, '_' and '-' (optionally ending with '*')"
            )
    return cleaned


def _read_optional_int(
    module: ModuleType,
    name: str,
    file: Path,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int | None:
    if not hasattr(module, name):
        return None
    value = getattr(module, name)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        bound = (
            f"between {minimum} and {maximum}"
            if maximum is not None
            else f">= {minimum}"
        )
        raise ValueError(
            f"invalid `{name}` in {file}: expected an integer {bound}, got {value!r}"
        )
    return value


def read_queue_trigger(module: ModuleType, file: Path) -> QueueTrigger:
    """Validate and return the ``QueueTrigger`` declared by ``module``."""
    topic = validate_queue_topic(getattr(module, "topic", None), file)
    consumer = getattr(module, "consumer", "default")
    if not isinstance(consumer, str) or not consumer.strip():
        raise ValueError(
            f"invalid `consumer` in {file}: expected a non-empty string, "
            f"got {consumer!r}"
        )
    normalized_consumer = sanitize_queue_name(consumer.strip())
    retry_after = _read_optional_int(
        module, "retry_after", file, minimum=1, maximum=QUEUE_MAX_DURATION_SECONDS
    )
    initial_delay = _read_optional_int(
        module, "initial_delay", file, minimum=0, maximum=QUEUE_MAX_DURATION_SECONDS
    )
    max_concurrency = _read_optional_int(module, "max_concurrency", file, minimum=1)
    return QueueTrigger(
        topic=topic,
        consumer=normalized_consumer,
        retry_after=retry_after,
        initial_delay=initial_delay,
        max_concurrency=max_concurrency,
    )


def load_queue(queue: QueueConsumer) -> LoadedQueue:
    """Import ``queue`` and validate its ``CONSUME`` handler and trigger config."""
    module = queue.load()
    handler = getattr(module, "CONSUME", None)
    if not callable(handler):
        raise TypeError(
            f"invalid `CONSUME` in {queue.file}: expected a callable handler, "
            f"got {handler!r}"
        )
    if is_generator_callable(handler):
        raise TypeError(
            f"invalid `CONSUME` in {queue.file}: generator functions are not "
            "supported as queue consumers"
        )
    trigger = read_queue_trigger(module, queue.file)
    endpoint = Endpoint(handler, set(), default_source="body")
    return LoadedQueue(
        spec=queue,
        trigger=trigger,
        endpoint=endpoint,
        module=module,
    )


def _topic_matches_pattern(pattern: str, topic: str) -> bool:
    """Return whether a concrete ``topic`` matches exact or wildcard ``pattern``."""
    if pattern == "*":
        return bool(topic)
    if pattern.endswith("*"):
        return topic.startswith(pattern[:-1])
    return pattern == topic


def _topics_overlap(first: str, second: str) -> bool:
    """Return whether two exact/wildcard topic patterns can match a common topic."""
    first_wild = first.endswith("*")
    second_wild = second.endswith("*")
    if not first_wild and not second_wild:
        return first == second
    if first_wild and not second_wild:
        return second.startswith(first[:-1])
    if second_wild and not first_wild:
        return first.startswith(second[:-1])
    p1, p2 = first[:-1], second[:-1]
    return p1.startswith(p2) or p2.startswith(p1)


def load_all_queues(app_dir: str | Path) -> list[LoadedQueue]:
    """Discover and load every ``queue.py`` under ``app_dir``, rejecting
    duplicate or overlapping topic registrations for the same consumer group."""
    loaded: list[LoadedQueue] = []
    by_consumer: dict[str, list[LoadedQueue]] = {}
    for queue in discover_queues(app_dir):
        item = load_queue(queue)
        consumer = item.trigger.consumer
        for existing in by_consumer.get(consumer, ()):
            if existing.trigger.topic == item.trigger.topic:
                raise ValueError(
                    f"duplicate queue consumer for topic {item.trigger.topic!r} "
                    f"and consumer {consumer!r}: {existing.spec.file} and {queue.file}"
                )
            if _topics_overlap(existing.trigger.topic, item.trigger.topic):
                raise ValueError(
                    f"overlapping queue topics {existing.trigger.topic!r} and "
                    f"{item.trigger.topic!r} for consumer {consumer!r}: "
                    f"{existing.spec.file} and {queue.file}"
                )
        by_consumer.setdefault(consumer, []).append(item)
        loaded.append(item)
    return loaded


def resolve_queues(
    queues: Sequence[LoadedQueue],
    topic: str,
    *,
    consumer: str | None = None,
) -> list[LoadedQueue]:
    """Resolve matching ``LoadedQueue`` consumers without invoking any handler.

    Supports both exact and wildcard (``prefix*`` / ``*``) topic patterns.
    When ``consumer`` is provided, matches deterministically against
    ``sanitize_queue_name(consumer)`` (the same reversible normalization applied
    to each ``queue.py``'s ``consumer`` declaration).
    """
    normalized_consumer = (
        sanitize_queue_name(consumer) if consumer is not None else None
    )
    matches = [
        q
        for q in queues
        if _topic_matches_pattern(q.trigger.topic, topic)
        and (normalized_consumer is None or q.trigger.consumer == normalized_consumer)
    ]
    if not matches:
        detail = f"topic {topic!r}" + (
            f" and consumer {consumer!r}" if consumer is not None else ""
        )
        raise QueueNotFoundError(f"no queue consumer registered for {detail}")
    return matches


def _encode_payload(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray | memoryview):
        return bytes(payload)
    return msgspec.json.encode(payload, enc_hook=_pydantic_enc_hook)


def _synthetic_queue_request(
    loaded: LoadedQueue,
    body: bytes,
    headers: Mapping[str, str] | None = None,
    *,
    topic: str | None = None,
) -> Request:
    merged: dict[str, str] = {
        "content-type": "application/json",
        "ce-type": "com.vercel.queue.v2beta",
        "ce-vqsqueuename": topic or loaded.trigger.topic,
        "ce-vqsconsumergroup": loaded.trigger.consumer,
    }
    if headers:
        for key, value in headers.items():
            merged[key.lower()] = value
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/{loaded.spec.function}",
        "raw_path": f"/{loaded.spec.function}".encode(),
        "query_string": b"",
        "headers": [
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in merged.items()
        ],
    }
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


async def invoke_loaded_queue(
    loaded: LoadedQueue,
    payload: Any,
    *,
    topic: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Invoke a ``LoadedQueue`` with ``payload`` and local ``after()`` support."""
    body = _encode_payload(payload)
    request = _synthetic_queue_request(loaded, body, headers, topic=topic)
    after_state = _AfterState("local")
    token = _after_state.set(after_state)
    cancelled = False
    try:
        return await loaded.endpoint.call(request)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        tasks = after_state.close_and_take()
        _after_state.reset(token)
        if not cancelled:
            for task in tasks:
                await task()


async def invoke_resolved_queues(
    matches: Sequence[LoadedQueue],
    payload: Any,
    *,
    topic: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Invoke already-resolved queue consumer(s)."""
    if len(matches) == 1:
        return await invoke_loaded_queue(
            matches[0], payload, topic=topic, headers=headers
        )
    results: dict[str, Any] = {}
    for item in matches:
        results[item.trigger.consumer] = await invoke_loaded_queue(
            item, payload, topic=topic, headers=headers
        )
    return results


async def dispatch_loaded_queues(
    queues: Sequence[LoadedQueue],
    topic: str,
    payload: Any,
    *,
    consumer: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Resolve and dispatch ``payload`` to matching ``queues`` for ``topic``."""
    matches = resolve_queues(queues, topic, consumer=consumer)
    return await invoke_resolved_queues(matches, payload, topic=topic, headers=headers)


async def invoke_queue(
    app_dir: str | Path,
    topic: str,
    payload: Any,
    *,
    consumer: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Discover queues under ``app_dir`` and invoke consumer(s) for ``topic``."""
    return await dispatch_loaded_queues(
        load_all_queues(app_dir),
        topic,
        payload,
        consumer=consumer,
        headers=headers,
    )


def _headers_from_message_metadata(metadata: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if metadata is None:
        return headers
    if content_type := getattr(metadata, "content_type", None):
        headers["content-type"] = str(content_type)
    if topic := getattr(metadata, "topic", None):
        headers["ce-vqsqueuename"] = str(topic)
    if consumer_group := getattr(metadata, "consumer_group", None):
        headers["ce-vqsconsumergroup"] = str(consumer_group)
    if message_id := getattr(metadata, "message_id", None):
        headers["ce-vqsmessageid"] = str(message_id)
    if (delivery_count := getattr(metadata, "delivery_count", None)) is not None:
        headers["ce-vqsdeliverycount"] = str(delivery_count)
    if receipt_handle := getattr(metadata, "receipt_handle", None):
        headers["ce-vqsreceipthandle"] = str(receipt_handle)
    if region := getattr(metadata, "region", None):
        headers["ce-vqsregion"] = str(region)
    if (created_at := getattr(metadata, "created_at", None)) is not None:
        headers["ce-vqscreatedat"] = (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        )
    return headers


def _make_queue_subscriber(
    loaded: LoadedQueue,
    after_backend: AfterBackend,
    *,
    message_annotation: Any = Any,
) -> Callable[[Any], Awaitable[None]]:
    """Wrap a Chano ``LoadedQueue`` as an async ``vercel.queue`` subscriber.

    Annotates ``message`` with ``vercel.queue.Message[bytes]`` when available
    so the SDK passes full ``Message`` objects (payload bytes + ``MessageMetadata``)
    instead of raw payload only. Returning normally lets ``vercel.queue``
    acknowledge the delivery; raising preserves lease/retry semantics.
    """

    async def subscriber(message: Any) -> None:
        payload = getattr(message, "payload", message)
        metadata = getattr(message, "metadata", None)
        headers = _headers_from_message_metadata(metadata)
        body = _encode_payload(payload)
        request = _synthetic_queue_request(loaded, body, headers)
        after_state = _AfterState(after_backend)
        token = _after_state.set(after_state)
        cancelled = False
        try:
            result = await loaded.endpoint.call(request)
            if isinstance(result, Failure):
                raise result
            if isinstance(result, Response) and result.status_code >= 400:
                raise RuntimeError(
                    f"queue consumer {loaded.spec.function!r} returned error "
                    f"status {result.status_code}"
                )
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            tasks = after_state.close_and_take()
            _after_state.reset(token)
            if not cancelled:
                for task in tasks:
                    await task()

    subscriber.__name__ = f"chano_queue_{loaded.spec.function.replace('/', '_')}"
    subscriber.__annotations__ = {"message": message_annotation, "return": None}
    return subscriber


def _register_vercel_queue_subscriber(
    queue_module: Any, loaded: LoadedQueue, after_backend: AfterBackend
) -> Callable[[Any], Awaitable[None]]:
    """Register ``loaded`` with ``vercel.queue.subscribe`` using raw bytes transport
    so Chano validates the payload against the ``CONSUME`` signature.

    Returns the registered subscriber callable; callers must retain a strong
    reference because ``vercel.queue`` stores weak references in its registry.
    """
    trigger = loaded.trigger
    byte_transport_cls = getattr(queue_module, "ByteBufferTransport", None)
    is_pattern = trigger.topic.endswith("*")
    topic_cls = getattr(queue_module, "TopicPattern" if is_pattern else "Topic", None)
    if callable(topic_cls) and callable(byte_transport_cls):
        topic_arg: Any = topic_cls(trigger.topic, transport=byte_transport_cls())
    else:
        topic_arg = trigger.topic

    sanitized_cls = getattr(queue_module, "SanitizedName", None)
    consumer_arg: Any = (
        sanitized_cls(trigger.consumer) if callable(sanitized_cls) else trigger.consumer
    )

    kwargs: dict[str, Any] = {
        "topic": topic_arg,
        "consumer_group": consumer_arg,
    }
    if trigger.retry_after is not None:
        kwargs["retry_after"] = trigger.retry_after
    if trigger.initial_delay is not None:
        kwargs["initial_delay"] = trigger.initial_delay
    if trigger.max_concurrency is not None:
        kwargs["max_concurrency"] = trigger.max_concurrency

    msg_cls = getattr(queue_module, "Message", None)
    message_annotation: Any = Any
    if msg_cls is not None:
        try:
            message_annotation = msg_cls[bytes]
        except TypeError:
            message_annotation = msg_cls

    subscriber = _make_queue_subscriber(
        loaded, after_backend, message_annotation=message_annotation
    )
    queue_module.subscribe(**kwargs)(subscriber)
    return subscriber


def _headers_from_scope(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_k, raw_v in scope.get("headers", ()):
        headers[raw_k.decode("latin-1").lower()] = raw_v.decode("latin-1")
    return headers


def _is_vercel_queue_delivery(headers: Mapping[str, str]) -> bool:
    ce_type = headers.get("ce-type", "")
    return (
        "vercel.queue" in ce_type
        or "ce-vqsmessageid" in headers
        or "ce-vqsreceipthandle" in headers
    )


async def _iter_asgi_body(receive: Receive) -> AsyncIterator[bytes]:
    while True:
        message = await receive()
        msg_type = message.get("type")
        if msg_type == "http.disconnect":
            raise ValueError("request body disconnected before completion")
        if msg_type != "http.request":
            raise ValueError(f"unexpected ASGI message: {msg_type!r}")
        body = cast("bytes", message.get("body", b""))
        if body:
            yield body
        if not message.get("more_body", False):
            return


async def _send_status(send: Send, status: int) -> None:
    headers = [(b"allow", b"POST")] if status == 405 else []
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


class _VercelQueueCallbackASGIApp:
    """ASGI adapter delegating push callbacks to ``vercel.queue.accept_and_handle``
    when ``vercel_runtime.workers`` is not installed alongside ``vercel-queue``."""

    def __init__(self, queue_module: Any) -> None:
        self._accept_and_handle = queue_module.accept_and_handle
        protocol_error = getattr(queue_module, "ProtocolError", None)
        bad_req: list[type[BaseException]] = [TypeError, ValueError]
        if isinstance(protocol_error, type) and issubclass(
            protocol_error, BaseException
        ):
            bad_req.insert(0, protocol_error)
        self._bad_request_exceptions = tuple(bad_req)
        self._headers_module: Any = None
        with contextlib.suppress(ImportError):
            self._headers_module = importlib.import_module("vercel.headers")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                await send({"type": f"{msg['type']}.complete"})
                if msg["type"] == "lifespan.shutdown":
                    return
        if scope["type"] != "http":
            return
        if scope.get("method") != "POST":
            await _send_status(send, 405)
            return

        if self._headers_module is not None and hasattr(
            self._headers_module, "headers_from_asgi_scope"
        ):
            headers = self._headers_module.headers_from_asgi_scope(scope)
        else:
            headers = _headers_from_scope(scope)

        ctx = (
            self._headers_module.HeadersContext(headers).use()
            if self._headers_module is not None
            and hasattr(self._headers_module, "HeadersContext")
            else contextlib.nullcontext()
        )
        try:
            with ctx:
                await self._accept_and_handle(_iter_asgi_body(receive), headers)
        except self._bad_request_exceptions as exc:
            _queue_log.warning("Vercel Queue push callback rejected: %s", exc)
            await _send_status(send, 400)
        except Exception:
            _queue_log.exception("Vercel Queue push callback failed")
            await _send_status(send, 500)
        else:
            await _send_status(send, 204)


class _LocalQueueASGIApp:
    """Fallback ASGI app when ``vercel-queue`` is not installed locally.

    Refuses platform CloudEvent push deliveries with 500 rather than faking
    HTTP 200 without acknowledging the queue lease, while still serving plain
    local JSON POST requests for unit testing.
    """

    def __init__(self, inner: EndpointASGIApp) -> None:
        self._inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _is_vercel_queue_delivery(
            _headers_from_scope(scope)
        ):
            response = PlainTextResponse(
                "vercel-queue is required to accept and acknowledge Vercel Queue "
                "push deliveries",
                status_code=500,
            )
            await response(receive, send)
            return
        await self._inner(scope, receive, send)


def _try_import_optional(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        top = name.split(".", 1)[0]
        if missing == top or missing.startswith(f"{top}."):
            return None
        raise


class _BoundQueueASGIApp:
    """Retains a strong reference to the registered ``vercel.queue`` subscriber
    (which the SDK tracks via weakref) for the lifetime of the ASGI app."""

    def __init__(
        self, inner: ASGIApp, subscriber: Callable[[Any], Awaitable[None]]
    ) -> None:
        self._inner = inner
        self._subscriber = subscriber

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._inner(scope, receive, send)


def create_queue_app(
    app_dir: str | Path,
    function: str,
    *,
    after_backend: AfterBackend = "local",
) -> ASGIApp:
    """Build the single-consumer ASGI app that runs inside a deployed queue function.

    When ``vercel-queue`` is installed (always true in Vercel builds), registers
    the consumer via ``vercel.queue.subscribe`` and returns the platform queue
    callback ASGI app (``vercel_runtime.workers.create_queue_service_app()`` or
    an ``accept_and_handle`` adapter) so deliveries are accepted, leases are
    renewed, successes are acknowledged, and failures trigger retries.
    """
    for queue in discover_queues(app_dir):
        if queue.function == function:
            loaded = load_queue(queue)
            queue_module = _try_import_optional("vercel.queue")
            if queue_module is not None and hasattr(queue_module, "subscribe"):
                subscriber = _register_vercel_queue_subscriber(
                    queue_module, loaded, after_backend
                )
                workers_module = _try_import_optional("vercel_runtime.workers")
                if workers_module is not None and hasattr(
                    workers_module, "create_queue_service_app"
                ):
                    return _BoundQueueASGIApp(
                        cast("ASGIApp", workers_module.create_queue_service_app()),
                        subscriber,
                    )
                if hasattr(queue_module, "accept_and_handle"):
                    return _BoundQueueASGIApp(
                        _VercelQueueCallbackASGIApp(queue_module), subscriber
                    )
            return _LocalQueueASGIApp(
                EndpointASGIApp(
                    loaded.endpoint,
                    methods=("POST",),
                    after_backend=after_backend,
                )
            )
    raise QueueNotFoundError(
        f"no queue consumer for function {function!r} in {app_dir}"
    )
