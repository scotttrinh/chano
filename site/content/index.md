---
title: Building serverless web applications with Chano
description: A guide to serverless web architecture and building Python APIs with Chano on Vercel.
lede: This guide introduces serverless web architecture and explains how to build and deploy Python APIs with Chano. It is written for developers comfortable with FastAPI and Python type hints, with little or no prior experience with Next.js or Vercel.
---

## Serverless architecture: the mental model

If you have built APIs with FastAPI, you are used to a **long-lived server process**:

1. You create a single `app = FastAPI()` instance.
2. You attach all your routes, middleware, and startup hooks to that object.
3. You run `uvicorn main:app` on a machine or container that stays alive 24/7, listening on a port.

**Serverless** (specifically Function-as-a-Service, or FaaS) flips this model around. Instead of one always-on process handling every endpoint, your application is split into **isolated functions** that a cloud platform (like Vercel) runs on demand.

### Key serverless concepts

- **One function per route**: When a request arrives for `GET /api/items/42`, the platform routes it to the function built specifically for that route. Other routes (`POST /api/orders`, `GET /api/users`) live in separate functions with their own memory, timeouts, and scaling.
- **Cold starts vs. warm invocations**: If no instance of a function is currently running, the platform boots a fresh Python runtime, imports your route's code, and runs the handler. This is a **cold start**. Subsequent requests arriving shortly after reuse that already-warm process (**warm invocations**), skipping startup overhead.
- **Bundle size drives cold-start speed**: Because each cold start loads the function's code and dependencies from disk or network, smaller packages mean faster cold starts. Keeping heavy unused libraries out of a route directly improves latency.
- **Ephemeral execution**: Once a response is sent, the platform may freeze or tear down the function container at any moment. Background threads or `asyncio.create_task()` calls started without telling the platform will be paused or killed unless you use a platform-aware hook like `wait_until` (which Chano wraps for you via `after()`).
- **CDNs handle static files**: Files that do not change per request (images, text files, even API documentation) should be served directly from the edge CDN rather than waking up a Python function.

## How Chano bridges FastAPI and serverless

Chano keeps the parts of FastAPI that make Python APIs pleasant to write: typed handler signatures, dependency injection with `Depends()`, automatic request validation, and OpenAPI/Swagger docs. At the same time, it redesigns the application structure around **one-function-per-route** serverless deployments.

| Concept | FastAPI | Chano |
| --- | --- | --- |
| App entry point | `app = FastAPI()` | No central app object; routes live in `app/**/route.py` |
| Route registration | `@app.get("/items/{id}")` | `def GET(id: int):` inside `app/items/[id]/route.py` |
| Validation and JSON | Pydantic | `msgspec` (with optional `pydantic.BaseModel` support) |
| Dependencies | `Depends(fn)` | `Depends(fn)` (per-request cached) + `lazy(factory)` |
| Router-level checks | `APIRouter(dependencies=[...])` | `dependencies.py` in any route folder |
| Middleware | `@app.middleware("http")` | `middleware.py` in any route folder |
| Error handlers | `@app.exception_handler(Exc)` | `error.py` in any route folder |
| Startup and lifespan | `@asynccontextmanager lifespan` | `lazy(factory)` (initializes on first use per warm instance) |
| Background tasks | `BackgroundTasks` | `after(fn, *args, **kwargs)` (`wait_until` in production) |
| OpenAPI and `/docs` | Generated at runtime in Python | Pre-rendered at build time to static CDN files |
| Deployment unit | One container running `uvicorn` | One isolated `.func` per route in `.vercel/output` |

### Why `msgspec` instead of Pydantic?

In a traditional server, Pydantic's import time (~100-200ms) and memory footprint only happen once when `uvicorn` boots. In serverless functions, every cold start pays that cost.

Chano uses [`msgspec`](https://jcristharif.com/msgspec/) for request decoding, validation, JSON serialization, and OpenAPI 3.1 schema generation. `msgspec` is a compact C extension that decodes and validates JSON in a single pass. If you already use `pydantic.BaseModel`, Chano supports it automatically when Pydantic is imported, without adding Pydantic as a dependency for projects that do not need it.

## Project setup and layout

Chano targets **Python 3.12+** and works with [`uv`](https://docs.astral.sh/uv/).

A minimal project looks like this:

```text
my-app/
├── pyproject.toml
├── app/
│   ├── route.py
│   └── api/
│       └── items/
│           └── [item_id]/
│               └── route.py
└── public/
    └── hello.txt
```

In `pyproject.toml`, declare `chano` as a runtime dependency and `chano[dev]` in `[dependency-groups].dev`:

```toml
[project]
name = "my-app"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["chano"]

[dependency-groups]
dev = ["chano[dev]"]
```

Why split `chano` and `chano[dev]`?
- `chano[dev]` installs `uvicorn`, which you need locally when running `chano dev`.
- Deployed Vercel functions run on Vercel's own Python runtime (`vercel-runtime`), so `uvicorn` is unnecessary in production. Keeping it in `[dependency-groups].dev` prevents it from being vendored into your serverless functions.

## File-system routing

If you have never used Next.js, its **App Router** convention is simple: **folders define URLs, and special filenames define behavior.**

Chano walks your `app/` directory for files named `route.py`:

| File path | Public URL | Description |
| --- | --- | --- |
| `app/route.py` | `/` | Root endpoint |
| `app/health/route.py` | `/health` | Static segment |
| `app/items/[item_id]/route.py` | `/items/{item_id}` | Dynamic segment (`item_id` passed to handler) |
| `app/files/[...path]/route.py` | `/files/{path:path}` | Catch-all segment (matches `files/a/b/c.png`) |
| `app/(admin)/users/route.py` | `/users` | Route group (`(admin)` organizes files without affecting the URL) |

Static files in `public/` are served at the root URL (`public/hello.txt` -> `/hello.txt`).

### Match precedence

Just like Vercel's production router, Chano resolves paths in a predictable order:

1. **Literal routes** (no `[param]` segments), e.g. `/items/new`
2. **Generated docs** (`/docs`, `/openapi.json`) and **static files** in `public/`
3. **Dynamic routes** (`[param]`), e.g. `/items/{item_id}`
4. **Catch-all routes** (`[...param]`), e.g. `/files/{path:path}`

This guarantees that `/items/new` always wins over `/items/{item_id}`, and a catch-all route never accidentally shadows `/docs` or a file in `public/`.

### Defining HTTP handlers in `route.py`

Export top-level functions named after HTTP methods (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `HEAD`). Handlers can be synchronous (`def`) or asynchronous (`async def`). Chano automatically runs synchronous handlers in a worker thread (`asyncio.to_thread`) so they never block the event loop.

```python
# app/route.py
def GET():
    return {"message": "Hello from Chano"}
```

If a route defines `GET` without `HEAD`, Chano automatically handles `HEAD` requests using `GET` and omits the response body.

## Request parameters, validation, and models

Like FastAPI, Chano reads your handler's signature and type annotations to determine where each argument comes from and how to validate it. Invalid requests automatically receive a `422 Unprocessable Entity` JSON response with field-level error locations.

### Source inference rules

For each parameter in a handler (or a `Depends` function):

1. **`Depends(fn)`** (as a default value or inside `Annotated`) calls `fn`, resolving its own signature recursively.
2. **`Request` annotation** injects the raw `chano.Request` object.
3. **Explicit markers** (`Query()`, `Header()`, `Cookie()`, or `Body()`) override automatic inference.
   - `Header()` automatically maps Python underscores to HTTP hyphens (for example, `x_request_id: Annotated[str | None, Header()] = None` reads `x-request-id`) unless you specify `Header(alias=...)`.
4. **Path parameter**: if the parameter name matches a `[segment]` in the route's directory path, it comes from the URL path.
5. **Request body model**: if the type is a `msgspec.Struct`, `dataclass`, `attrs` class, or `pydantic.BaseModel`, it is decoded from the JSON request body.
6. **Query parameter**: anything else defaults to a query parameter.

### Single vs. multiple body parameters

- **One body parameter**: Chano decodes the raw request bytes directly into your model in a single pass.
- **Multiple body parameters** (e.g. `item: Item, count: Annotated[int, Body()]`): Chano expects a JSON object keyed by parameter name (`{"item": {...}, "count": 3}`), matching FastAPI's `Body(embed=True)` behavior automatically.

### Example with `msgspec.Struct` and `Meta`

```python
# app/api/items/[item_id]/route.py
from typing import Annotated

from msgspec import Meta, Struct

from chano import Cookie, Header


class Item(Struct):
    name: str
    price: Annotated[float, Meta(gt=0)]
    tags: list[str] = []


def GET(item_id: int, verbose: bool = False):
    """Fetch an item."""
    return {"item_id": item_id, "verbose": verbose}


async def PUT(
    item_id: int,
    item: Item,
    x_trace_id: Annotated[str | None, Header()] = None,
    session: Annotated[str | None, Cookie()] = None,
) -> Item:
    """Update an item."""
    return item
```

### Accessing Vercel request metadata

When deployed to Vercel, the edge network attaches request metadata headers. `Request` exposes them directly with safe local fallbacks so your code works unchanged in `chano dev` and tests:

```python
from chano import Request


def GET(request: Request):
    return {
        "request_id": request.id,  # x-vercel-id, or a generated UUID locally
        "ip": request.ip,          # x-real-ip / x-forwarded-for / client host
        "country": request.geo.country,
        "city": request.geo.city,
    }
```

> **Security note**: Outside Vercel, `x-vercel-*` and `x-forwarded-for` headers can be spoofed by clients. Never use `request.ip` or `request.geo` for authorization decisions.

## Chano idioms for application structure

Because Chano deploys each `route.py` as an independent function, there is no central `app` object where you can call `app.include_router()`, `app.add_middleware()`, or `@app.on_event("startup")`. Instead, Chano uses **file-system conventions** and **lazy initialization**.

### `Depends()` and `HTTPBearer()`

`Depends(fn)` works like FastAPI's dependency injection:
- Dependencies can declare their own parameters (headers, queries, other `Depends`).
- Results are cached **per request** by callable identity, so if two dependencies both depend on `get_db`, `get_db` runs once for that request.

For Bearer token authentication, Chano provides `HTTPBearer()` and `@raises`:

```python
from chano import Depends, HTTPBearer, HTTPException, raises

bearer = HTTPBearer()


@raises(401, "Not authenticated")
def current_user(token: str = Depends(bearer)) -> str:
    if token != "secret":
        raise HTTPException(401, "Not authenticated")
    return "alice"


def GET(user: str = Depends(current_user)):
    return {"user": user}
```

- `Depends(bearer)` extracts the token from `Authorization: Bearer <token>`, returns `401` with `WWW-Authenticate: Bearer` if missing, and registers an HTTP Bearer security scheme in `openapi.json`.
- `@raises(status, description)` documents error responses in `openapi.json` for any handler or dependency that uses it (note: it is documentation-only; your code still raises `HTTPException`).

### `lazy()` instead of startup/lifespan hooks

In FastAPI, you create database pools or HTTP clients in a `lifespan` function at server startup. In serverless, **there is no global app startup**: a function only initializes what it needs when it cold-starts.

Use `lazy(factory)` to create a process-wide singleton on first call (thread-safe for sync factories, task-safe for async factories), which is then reused across warm invocations:

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

### Folder-scoped `dependencies.py`

In FastAPI, you protect a group of endpoints with `APIRouter(dependencies=[Depends(verify_admin)])`. In Chano, drop a `dependencies.py` file into any folder under `app/`. Every `route.py` at or below that folder will run those checks before its handler:

```python
# app/(admin)/dependencies.py
from chano import Depends, HTTPBearer, HTTPException, raises

bearer = HTTPBearer()


@raises(403, "Admin access required")
def require_admin(token: str = Depends(bearer)) -> None:
    if token != "admin-secret":
        raise HTTPException(403, "Admin access required")


dependencies = [require_admin]
```

Now `app/(admin)/users/route.py` is automatically protected at `/users`, and because `(admin)` is a route group, the URL stays `/users` without adding `/admin` to the path.

### Folder-scoped `middleware.py`

To wrap requests and responses for a subtree of routes (for timing headers, logging, or CORS), add a `middleware.py` exporting `async def middleware(request, call_next) -> Response`:

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

If multiple `middleware.py` files exist along a route's path (for example, `app/middleware.py` and `app/api/middleware.py`), they run root-to-leaf (outermost to innermost).

### Folder-scoped `error.py`

To customize error responses for a subtree of routes, add an `error.py` exporting `handle_error(request, exc) -> Response` (sync or async):

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
    raise exc  # pass unhandled exceptions to an outer error.py or default 500
```

Unlike middleware, error boundaries run **nearest-to-leaf first**. If `handle_error` re-raises, the exception bubbles up to the next ancestor `error.py`, or falls back to Chano's default error mapping.

## Responses, streaming, and background work

### Standard responses and cookies

Returning a `dict`, `list`, or `Struct` automatically returns a `JSONResponse` with status `200`. When you need custom status codes, headers, or non-JSON formats, return a `Response` subclass:

- `JSONResponse(content, status_code=200, headers=...)`
- `HTMLResponse(html)`
- `PlainTextResponse(text)`
- `RedirectResponse(url, status_code=307)`
- `Response(content, status_code=200, media_type=...)`

All response classes support `set_cookie()` and `delete_cookie()`. `set_cookie()` defaults to secure flags (`Secure=True`, `HttpOnly=True`, `SameSite="lax"`, `Path="/"`), which modern browsers also accept on `localhost` during development.

### Streaming and Server-Sent Events (SSE)

For LLM token streams or live updates, return `StreamingResponse` or `SSEResponse`:

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

- `SSEResponse` sets `content-type: text/event-stream` and `cache-control: no-cache`, automatically encodes `dict` or `Struct` payloads as JSON, and optionally sends `: ping` keepalive comments every `ping` seconds.
- If the client disconnects mid-stream, Chano cancels the producer and closes the iterator cleanly.

### Background work with `after()`

In serverless environments, returning an HTTP response signals to the platform that the invocation is done. If you spawn an unawaited task, it may be frozen before completing.

Chano provides `after(fn, /, *args, **kwargs)` to schedule sync or async work after the response is sent:

```python
from chano import after


async def send_audit_log(user_id: str, action: str) -> None:
    ...  # async HTTP call or DB write


def POST(user_id: str):
    after(send_audit_log, user_id, "created_item")
    return {"status": "accepted"}
```

- In **`chano dev`** and tests, queued callbacks drain cleanly in the request's `finally` block.
- In **built Vercel functions**, `after()` delegates to `vercel.functions.wait_until`, telling Vercel to keep the function alive until the background work finishes.

## Taking advantage of serverless

### Per-route function configuration

Because every `route.py` becomes its own function, you can tune compute resources per endpoint using module-level variables (modeled after Next.js route segment config):

```python
# app/api/reports/route.py
max_duration = 60  # seconds
memory = 2048      # MB
regions = ["iad1"] # pin to a specific Vercel region (e.g. near your database)
```

Other route-level options:
- `include_in_schema = False`: hides all methods in this `route.py` from `/openapi.json` and `/docs` while still serving requests.
- `include = ["some-pkg"]`: explicitly bundles a package if it is loaded dynamically and missed by static import tracing.

### Dependency tree-shaking

When you run `chano build`, Chano performs **static AST import tracing** starting from each `route.py` and its ancestor `middleware.py`, `error.py`, and `dependencies.py` files. Only the third-party distributions that a route actually uses (plus their transitive dependencies) are hard-linked into that function's `_vendor/` directory.

For example, if only `app/api/reports/route.py` imports `pandas` or `pillow`, only that function pays the bundle-size cost. Your `/api/health` function stays minimal and cold-starts quickly.

To inspect why a distribution was included or excluded for a function, pass `--explain`:

```sh
uv run chano build --explain api/items/[item_id]
```

Chano also runs an isolated import check on each built function at build time (when targeting the host platform) and warns or fails if a function approaches Vercel's bundle size limits.

### Project configuration (`[tool.chano]`)

You can configure project-wide settings in `pyproject.toml`:

```toml
[tool.chano]
base_path = "/api"   # mount all routes, static files, and /docs under /api
tree_shake = true    # default: true (set false to bundle all dependencies everywhere)
include = []         # distribution names to bundle into every function
```

Setting `base_path = "/api"` lets you write your `app/` directory without an extra `api/` wrapper folder while serving `/api`, `/api/docs`, `/api/openapi.json`, and `/api/<static>`.

## Local development, building, and deploying

### Local development

Run the local ASGI server with hot reload (reloads on `.py` and `pyproject.toml` changes):

```sh
uv run chano dev
```

- API routes: `http://127.0.0.1:8000`
- Interactive Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI 3.1 schema: `http://127.0.0.1:8000/openapi.json`

### Building for production

Compile the project into Vercel's Build Output API v3 directory (`.vercel/output/`):

```sh
uv run chano build
```

What `chano build` produces:
- `.vercel/output/static/`: `public/` assets plus pre-rendered `openapi.json` and `docs.html`
- `.vercel/output/functions/`: one `<route>.func/` directory per `route.py`, containing a minimal ASGI entrypoint, your project files, `.vc-config.json`, and tree-shaken `_vendor/` dependencies
- `.vercel/output/config.json`: Vercel routing table (`filesystem` check first, then `/docs` rewrite, then dynamic route regex rewrites)

Useful flags:
- `--platform native`: target your local machine's architecture instead of Vercel's default Linux platform
- `--output <dir>`: write to a custom directory
- `--explain <function>`: print the dependency tree-shaking report for a specific function

### Deploying to Vercel

Install the [Vercel CLI](https://vercel.com/docs/cli) and deploy the prebuilt output directly:

```sh
vercel deploy --prebuilt
```

Because `chano build` already assembled `.vercel/output/`, Vercel uploads the static files and functions directly without needing a remote build step.

### Pairing with a frontend (optional)

If you later add a Next.js (or other) frontend in the same repository, you can deploy both as **Vercel Services** using a `vercel.json` at the workspace root:

- Set `base_path = "/api"` in the backend's `pyproject.toml`.
- Configure `vercel.json` to route `/api/:path*` to the Chano service and `/(.*)` to the frontend service.
- Run `vercel dev --local` to develop both services together on `http://localhost:3000`.
