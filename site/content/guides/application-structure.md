---
title: Application structure
description: Structure shared authentication, middleware, error boundaries, lazy singletons, and background work in Chano.
lede: Because each route compiles into its own function, Chano replaces central app wiring with file-system conventions and lazy initialization.
section: guides
order: 3
nav_title: Application structure
---

## Dependencies and authentication

`user: User = Depends(fn)` resolves dependencies recursively, checks the parameter type against `fn`'s return type, and caches results **per request** by callable identity. If two dependencies both request `get_user`, it runs once for that request.

Dependencies can declare their own validated parameters (queries, headers, cookies, bodies), other dependencies, and failure or redirect outcomes in their return annotation. For Bearer authentication, combine `HTTPBearer()` with `chano.status`:

```python
from chano import Depends, HTTPBearer
from chano.status import Unauthorized

bearer = HTTPBearer()


def current_user(token: str = Depends(bearer)) -> str | Unauthorized:
    if token != "secret":
        return Unauthorized("Not authenticated")
    return "alice"


def GET(user: str = Depends(current_user)):
    return {"user": user}
```

- `token: str = Depends(bearer)` reads `Authorization: Bearer <token>`, returns `Unauthorized` (`401` with `WWW-Authenticate: Bearer`) when missing, registers an HTTP Bearer security scheme in `openapi.json`, and documents `401` automatically.
- Returning any `Response` (such as `Unauthorized(...)` or `SeeOther("/login")`) from a dependency short-circuits the request before later dependencies or the handler run.

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


def GET(page: Pagination = Depends(pagination)):
    return {"skip": page.skip, "limit": page.limit}
```

## Folder-scoped `boundary.py`

To share guards, error recovery, and HTTP wrappers across a subtree of routes without repeating wiring in every handler, add a `boundary.py` file to any folder under `app/` exporting `boundary = Boundary(...)`:

```python
# app/(admin)/boundary.py
import time

from chano import (
    Boundary,
    CallNext,
    Depends,
    ErrorDetail,
    Failure,
    HTTPBearer,
    JSONResponse,
    Request,
    Response,
)
from chano.status import Forbidden

bearer = HTTPBearer()


class AdminRequired(Forbidden):
    """Admin access required"""


def require_admin(token: str = Depends(bearer)) -> None | AdminRequired:
    if token != "admin-secret":
        return AdminRequired("Admin access required")
    return None


def format_failure(request: Request, exc: Failure[ErrorDetail]) -> Response:
    return JSONResponse(
        {"error": exc.content.detail, "path": request.path},
        status_code=exc.status_code,
        headers=exc.headers,
    )


async def timing(request: Request, call_next: CallNext) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["x-response-time"] = f"{elapsed_ms:.2f}ms"
    return response


boundary = Boundary(
    guards=[require_admin],
    recover=[format_failure],
    wrap=[timing],
)
```

- **`guards`** run **root-to-leaf** before handler arguments resolve, sharing the per-request `Depends(...)` cache. A guard returning any `Response` short-circuits the request. Because `(admin)` is a route group, `app/(admin)/users/route.py` is served at `/users` without adding `/admin` to the URL.
- **`recover`** functions are selected by their second parameter's type annotation (`C` matches `isinstance(error, C)`; `C[B]` on an `Outcome` also checks `isinstance(error.content, B)`), running **leaf-to-root** then in list order. Returning a `Response` is final; raising continues outward to remaining candidates or Chano's default error mapping.
- **`wrap`** functions run **root-to-leaf** (outermost to innermost) on HTTP routes only. `call_next` always returns a `Response`, and returning without awaiting `call_next` short-circuits inner wrappers and the handler.

## `lazy()` instead of startup hooks

With Vercel Functions, there is no global app startup. Use `lazy(factory)` to initialize a process-wide client or pool on first call and reuse it across warm invocations:

```python
import httpx

from chano import Depends, lazy


def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


get_http_client = lazy(create_http_client)


async def GET(client: httpx.AsyncClient = Depends(get_http_client)):
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

Add a `queue.py` file anywhere under `app/` exporting `topic` and a `CONSUME` handler (sync or async). Typed parameters bind from the JSON message payload, and `Depends()`, `after()`, and `boundary.py` (`guards` and `recover`, never `wrap`) work just like HTTP routes:

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
