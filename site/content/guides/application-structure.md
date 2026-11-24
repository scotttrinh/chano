---
title: Application structure
description: Structure shared authentication, middleware, error boundaries, representations, lazy singletons, and background work in Chano.
lede: Because each route compiles into its own function, Chano replaces central app wiring with file-system conventions and lazy initialization.
section: guides
order: 3
nav_title: Application structure
---

## Dependencies and authentication

`Annotated[T, Depends(fn)]` resolves dependencies recursively and caches results **per request** by callable identity. If two dependencies both request `get_user`, it runs once for that request.

Dependencies can declare their own validated parameters (queries, headers, cookies, bodies) and other dependencies. For Bearer authentication, combine `HTTPBearer()` with `@raises`:

```python
from typing import Annotated

from chano import Depends, HTTPBearer, HTTPException, raises

bearer = HTTPBearer()


@raises(401, "Not authenticated")
def current_user(token: Annotated[str, Depends(bearer)]) -> str:
    if token != "secret":
        raise HTTPException(401, "Not authenticated")
    return "alice"


def GET(user: Annotated[str, Depends(current_user)]):
    return {"user": user}
```

- `Annotated[str, Depends(bearer)]` reads `Authorization: Bearer <token>`, returns `401` with `WWW-Authenticate: Bearer` when missing, and registers an HTTP Bearer security scheme in `openapi.json`.
- `@raises(status, description)` documents error responses in `openapi.json` for every handler or dependency that uses it. It is documentation-only; your code still raises `HTTPException`.

You can also extract reusable, validated inputs into a dependency:

```python
from typing import Annotated

from msgspec import Meta, Struct

from chano import Depends, Query


class Pagination(Struct):
    skip: int
    limit: int


def pagination(
    skip: Annotated[int, Meta(ge=0), Query()] = 0,
    limit: Annotated[int, Meta(ge=1, le=100), Query()] = 20,
) -> Pagination:
    return Pagination(skip=skip, limit=limit)


def GET(page: Annotated[Pagination, Depends(pagination)]):
    return {"skip": page.skip, "limit": page.limit}
```

## Folder-scoped `dependencies.py`

To protect a subtree of routes without repeating `Depends(...)` in every handler, add a `dependencies.py` file to any folder under `app/`. Export a `dependencies` list or tuple of callables:

```python
# app/(admin)/dependencies.py
from typing import Annotated

from chano import Depends, HTTPBearer, HTTPException, raises

bearer = HTTPBearer()


@raises(403, "Admin access required")
def require_admin(token: Annotated[str, Depends(bearer)]) -> None:
    if token != "admin-secret":
        raise HTTPException(403, "Admin access required")


dependencies = [require_admin]
```

Every `route.py` at or below `app/(admin)/` runs `require_admin` before resolving handler arguments. Because `(admin)` is a route group, `app/(admin)/users/route.py` is served at `/users` without adding `/admin` to the URL. Inherited checks share the same per-request cache as `Depends(...)`, so if both `dependencies.py` and a handler depend on `current_user`, it runs once per request.

## Folder-scoped `middleware.py`

Add a `middleware.py` exporting `async def middleware(request, call_next) -> Response` to wrap every route at or below that directory:

```python
# app/middleware.py
import time

from chano import Request, Response


async def middleware(request: Request, call_next) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["x-response-time"] = f"{elapsed_ms:.2f}ms"
    return response
```

Multiple `middleware.py` files along a route's path run **root-to-leaf** (outermost to innermost). Middleware can short-circuit by returning a `Response` without awaiting `call_next`.

## Folder-scoped `error.py`

Add an `error.py` exporting `handle_error(request, exc) -> Response` (sync or async) to customize error responses for a subtree of routes:

```python
# app/api/error.py
from chano import HTTPException, JSONResponse, Request, Response


def handle_error(request: Request, exc: Exception) -> Response:
    if isinstance(exc, HTTPException):
        return JSONResponse(
            {"error": exc.detail, "path": request.path},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    raise exc
```

Error boundaries run **leaf-to-root** (nearest wins). If `handle_error` re-raises, the exception bubbles up to the next ancestor `error.py`, then to the route's `representation.py` (when present) or Chano's default error mapping.

## Folder-scoped `representation.py`

Middleware and `error.py` can rewrite response bytes on the wire, but they do not update `openapi.json`. To keep runtime output and OpenAPI schemas in agreement — for example, RFC 9457 Problem JSON errors or a response envelope like `Document[T]` — place a `representation.py` in any folder under `app/` and export `representation`:

```python
# app/api/representation.py
from typing import Any

import msgspec

from chano import (
    HTTPException,
    JSONRepresentation,
    Request,
    RequestValidationError,
    Response,
)


class ProblemDetail(msgspec.Struct):
    title: str
    status: int
    detail: Any = None
    instance: str | None = None


class ProblemJSON(JSONRepresentation):
    error_media_type = "application/problem+json"

    def error_type(self, status_code: int) -> Any:
        return ProblemDetail

    def error(self, request: Request, exc: Exception) -> Response:
        if isinstance(exc, HTTPException):
            status, title, detail, headers = (
                exc.status_code,
                "HTTP Error",
                exc.detail,
                exc.headers,
            )
        elif isinstance(exc, RequestValidationError):
            status, title, detail, headers = (
                422,
                "Validation Error",
                exc.errors,
                None,
            )
        else:
            # Unexpected errors keep the default: a logged traceback and a plain 500.
            return super().error(request, exc)
        body = ProblemDetail(
            title=title,
            status=status,
            detail=detail,
            instance=request.path,
        )
        return Response(
            msgspec.json.encode(body),
            status_code=status,
            headers=headers,
            media_type=self.error_media_type,
        )


representation = ProblemJSON()
```

Subclassing `JSONRepresentation` lets you override only the error half or only the success half (`media_type`, `success_type`, and `success`). The nearest `representation.py` to a route wins (they do not chain or merge), `error.py` boundaries still run first, and any handler that returns a `Response` directly bypasses the representation. See [Representation](/reference/requests-and-responses#representation) and [representation.py](/reference/routing#representation-py) for the full contract.

## `lazy()` instead of startup hooks

With Vercel Functions, there is no global app startup. Use `lazy(factory)` to initialize a process-wide client or pool on first call and reuse it across warm invocations:

```python
from typing import Annotated

import httpx

from chano import Depends, lazy


def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


get_http_client = lazy(create_http_client)


async def GET(client: Annotated[httpx.AsyncClient, Depends(get_http_client)]):
    resp = await client.get("https://httpbin.org/uuid")
    return resp.json()
```

`lazy()` is thread-safe for sync factories and task-safe for async factories. If `factory` raises, nothing is cached and the next request retries.

## Background work with `after()`

Use `after(fn, /, *args, **kwargs)` to run sync or async work after the HTTP response has been sent:

```python
from chano import after


async def send_audit_log(user_id: str, action: str) -> None:
    ...


def POST(user_id: str):
    after(send_audit_log, user_id, "created_item")
    return {"status": "accepted"}
```

- In `chano dev` and tests, queued callbacks drain in the request's `finally` block.
- In built Vercel functions, `after()` delegates to `vercel.functions.wait_until` so the platform keeps the function alive until the callback completes.

## Streaming and Server-Sent Events

For token streams or live updates, return `StreamingResponse` or `SSEResponse`:

```python
import asyncio

from chano import SSEResponse, ServerSentEvent


async def event_stream():
    for i in range(3):
        yield ServerSentEvent(data={"step": i}, event="progress")
        await asyncio.sleep(0.5)


async def GET():
    return SSEResponse(event_stream(), ping=15)
```

`SSEResponse` sets `content-type: text/event-stream` and `cache-control: no-cache`, JSON-encodes structured payloads automatically, and sends `: ping` keepalive comments when `ping` is set. On client disconnect, Chano cancels the producer and closes the iterator cleanly.

## Queues, schedules, and workflows

Beyond HTTP `route.py` endpoints, Chano compiles Vercel Queues, Schedules, and Workflows into private Vercel Functions that stay out of `/openapi.json` and `/docs`.

### Queue consumers (`queue.py`)

Add a `queue.py` file anywhere under `app/` exporting `topic` and a `CONSUME` handler (sync or async). Typed parameters bind from the JSON message payload, and `Depends()`, `after()`, `dependencies.py`, and `error.py` work just like HTTP routes:

```python
# app/orders/queue.py
from msgspec import Struct

topic = "orders-created"
consumer = "billing"
retry_after = 30
max_concurrency = 5


class OrderEvent(Struct):
    order_id: str
    amount: int


async def CONSUME(event: OrderEvent):
    ...
```

Test consumers locally with `uv run chano queue list` and `uv run chano queue send orders-created '{"order_id": "o_1", "amount": 4200}'`.

### Scheduled jobs (`schedule.py`)

Add a `schedule.py` file exporting a 5-field cron `schedule` and a `RUN` handler for a private scheduled function (with optional `timezone` and `name`):

```python
# app/reports/daily/schedule.py
schedule = "0 9 * * *"
timezone = "America/New_York"


async def RUN():
    ...
```

Inspect and trigger scheduled jobs locally with `uv run chano schedule list` and `uv run chano schedule run reports-daily`.

### Durable workflows (`[[tool.vercel.workflows]]`)

If your project installs the optional `vercel-workflow` package, point `[[tool.vercel.workflows]]` in `pyproject.toml` at your `Workflows` registry:

```toml
[[tool.vercel.workflows]]
entrypoint = "workflows.orders:wf"
```

In `chano dev`, Chano mounts `/.well-known/workflow/v1/flow` and `/.well-known/workflow/v1/manifest.json` automatically when `vercel-workflow` is installed. During `chano build`, Chano introspects registered `vercel.queue` subscriptions and emits `.well-known/workflow/v1/flow.func` (plus `step.func` when step subscriptions are registered). See [Routing and special files](/reference/routing) and [Configuration and CLI](/reference/configuration-and-cli) for all options.
