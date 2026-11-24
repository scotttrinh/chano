---
title: Routing and special files
description: File-system routing conventions, HTTP method exports, match precedence, folder-scoped hooks, and route-level options.
lede: Folders under app/ define URLs, route.py exports HTTP handlers, and sibling special files scope middleware, errors, dependencies, and representations to a subtree.
section: reference
order: 1
nav_title: Routing and special files
---

## File-system routing

Chano discovers every `route.py` file under `app/` and maps its directory path to a URL pattern:

| File path | URL path | Function name | Notes |
| --- | --- | --- | --- |
| `app/route.py` | `/` | `index` | Root endpoint |
| `app/health/route.py` | `/health` | `health` | Static segment (rank `0`) |
| `app/items/[item_id]/route.py` | `/items/{item_id}` | `items/[item_id]` | Dynamic segment (rank `1`, matches `[^/]+`) |
| `app/files/[...path]/route.py` | `/files/{path:path}` | `files/[...path]` | Catch-all segment (rank `2`, matches `.+`) |
| `app/(admin)/users/route.py` | `/users` | `users` | Route group; stripped from URL and function name |

Static files in `public/` are served at `/` (or `{base_path}/` when `base_path` is configured).

## Match precedence

Both `chano dev` and the generated Vercel Build Output `config.json` resolve paths in this order:

1. **Literal routes** (routes with no `[param]` segments)
2. **Generated docs** (`/openapi.json`, `/docs.html`, `/docs`) and **`public/` static files**
3. **Dynamic routes** (`[param]`), sorted by specificity rank then path
4. **Catch-all routes** (`[...param]`)

First matching route wins. If a matched route does not define the requested HTTP method, Chano returns `405 Method Not Allowed` with a sorted `Allow` header rather than falling through to a less specific route.

## HTTP method handlers

Each `route.py` exports top-level functions named after supported HTTP methods:

```text
GET  POST  PUT  PATCH  DELETE  OPTIONS  HEAD
```

- Handlers may be `def` (executed in a worker thread via `asyncio.to_thread`) or `async def` (awaited on the event loop).
- If `GET` is defined without `HEAD`, `HEAD` requests run `GET` and suppress the response body.
- Returning a `dict`, `list`, `msgspec.Struct`, or other JSON-serializable value produces a `200` `JSONResponse`. Returning a `Response` instance sends it directly.

## Route-level module options

A `route.py` may define module-level variables to configure its deployed Vercel function:

| Variable | Type | Default | Effect |
| --- | --- | --- | --- |
| `max_duration` | `int` | Platform default | Sets `maxDuration` (seconds) in `.vc-config.json` |
| `memory` | `int` | Platform default | Sets `memory` (MB) in `.vc-config.json` |
| `regions` | `list[str]` | Platform default | Sets `regions` in `.vc-config.json` |
| `include` | `Sequence[str]` | `()` | Extra distribution names bundled into this function |

## OpenAPI operation metadata

Decorate individual HTTP method handlers with `@openapi(...)` to customize or hide them in `/openapi.json` and `/docs` without changing runtime dispatch:

```python
from chano import openapi

@openapi(tags=["items"], summary="List items", deprecated=False)
def GET() -> list[dict]:
    return []

@openapi(include_in_schema=False)
def DELETE() -> dict[str, bool]:
    return {"deleted": True}
```

| Parameter | Type | Default | Effect |
| --- | --- | --- | --- |
| `include_in_schema` | `bool` | `True` | When `False`, omits this method from `/openapi.json` and `/docs` |
| `summary` | `str` | Docstring first line | Overrides the operation `summary` |
| `description` | `str` | Docstring body | Overrides the operation `description` |
| `operation_id` | `str` | Generated | Overrides the operation `operationId` |
| `tags` | `Sequence[str]` | None | Adds OpenAPI `tags` to group the operation |
| `deprecated` | `bool` | `False` | Emits `"deprecated": true` when `True` |
| `response_description` | `str` | `"Successful Response"` | Overrides the `200` response description |
| `openapi_extra` | `Mapping[str, Any]` | None | Deep-merged into the generated operation object |

Stacking `@openapi(...)` decorators lets the outermost decorator override each specified field and deep-merge `openapi_extra` with outer keys winning.

## Folder-scoped special files

Special files placed in any folder under `app/` apply to every `route.py` in that folder and all nested subfolders, including inside route groups `(group)` and dynamic segments `[param]`. Sibling folders are never affected.

### `middleware.py`

Exports `async def middleware(request: Request, call_next) -> Response`:

- Executes **root-to-leaf** (outermost directory first).
- Only runs after a route matches (including `405` responses). Does not run for `404` responses, `/docs`, or `public/` files.
- `call_next(request)` always returns a `Response`, because endpoint errors pass through `error.py` before returning to middleware.
- Returning without calling `call_next` short-circuits inner middleware and the handler.
- Non-async or missing `middleware` raises `TypeError` at load time.

### `error.py`

Exports `def handle_error(request: Request, exc: Exception) -> Response` or `async def handle_error(...)`:

- Executes **leaf-to-root** (nearest directory first) inside the innermost middleware.
- Handles `HTTPException`, `RequestValidationError`, `405` method mismatches, and unhandled endpoint exceptions.
- Re-raising (a bare `raise` or a new exception) passes the exception to the next ancestor `error.py`, then to the route's `representation.error` (when configured) or Chano's default error mapping (`422` for validation, `HTTPException` JSON, or `500` plain text).
- Exceptions raised by middleware bypass `error.py` and use Chano's default error mapping.

### `dependencies.py`

Exports `dependencies: list[Callable] | tuple[Callable, ...]`:

- Executes **root-to-leaf** in declared order before any handler argument is resolved.
- Each item is a bare callable (not wrapped in `Depends(...)`) and must not be a generator or async generator.
- Shares the per-request dependency cache with handler `Depends(...)`, so a callable listed in `dependencies.py` and requested via `Depends()` runs once per request.
- Any `HTTPBearer` or `@raises` declarations on inherited checks are included in generated OpenAPI schemas.

## Private Vercel primitive files

Queue consumers (`queue.py`) and private schedules (`schedule.py`) live under `app/` alongside routes, inherit ancestor `dependencies.py` and `error.py` (never `middleware.py`), support `max_duration`, `memory`, `regions`, and `include`, and are excluded from public HTTP routes and OpenAPI. Route groups `(group)` are stripped from function names; dynamic `[param]` segments are rejected.

### `queue.py` (Vercel Queues)

Each `app/**/queue.py` compiles to `functions/_queues/<path>.func/` (`app/queue.py` -> `_queues/index`) with a `queue/v2beta` trigger in `.vc-config.json` and requires `vercel-queue` in the build dependency store.

| Export | Type | Required / Default | Description |
| --- | --- | --- | --- |
| `CONSUME` | sync or async callable | Required | Handler invoked per message (non-generator; parameters default to JSON body fields) |
| `topic` | `str` | Required | Topic name (`[A-Za-z0-9_-]+`), wildcard prefix (`prefix*`), or `*` |
| `consumer` | `str` | `"default"` | Consumer group name (reversibly sanitized to match `vercel.queue`) |
| `retry_after` | `int` | `None` | Seconds before retry (`1`–`86400` -> `retryAfterSeconds`) |
| `initial_delay` | `int` | `None` | Initial delivery delay in seconds (`0`–`86400` -> `initialDelaySeconds`) |
| `max_concurrency` | `int` | `None` | Maximum concurrent deliveries (`>= 1` -> `maxConcurrency`) |

Duplicate or overlapping wildcard topics within the same consumer group raise `ValueError`. Returning normally acknowledges the message; raising or returning a `>= 400` `Response` triggers retries.

### `schedule.py` (Vercel Schedules)

Each `app/**/schedule.py` compiles to `functions/_schedules/<path>.func/` (`app/schedule.py` -> `_schedules/index`) with `experimentalTriggers: [{"type": "schedule/v1beta"}]` and a `config.json["schedules"][name]` entry.

| Export | Type | Required / Default | Description |
| --- | --- | --- | --- |
| `RUN` | sync or async callable | Required | Handler invoked on schedule (non-generator; supports `Depends()` and `after()`) |
| `schedule` | `str` | Required | 5-field cron expression (cannot constrain both day-of-month and day-of-week) |
| `timezone` | `str` | `None` | Optional IANA timezone name (for example `"UTC"` or `"America/New_York"`) |
| `name` | `str` | Path with `/` -> `-` | Schedule key (`1`–`256` chars, alnum start, `[A-Za-z0-9._-]`) |

Duplicate logical paths or colliding schedule names raise `ValueError`.

### `representation.py`

Exports `representation: Representation` (an object implementing the [`Representation`](/reference/requests-and-responses#representation) protocol, such as a [`JSONRepresentation`](/reference/requests-and-responses#representation) subclass):

- **Nearest wins**: the closest `representation.py` to a route replaces any ancestor `representation.py`; representations do not chain or merge. Routes with no ancestor `representation.py` keep Chano's default wire format and OpenAPI output unchanged.
- **Validation and conflicts**: missing `representation` or an object missing required protocol members (or an uninstantiated class) raises `TypeError` naming the file at load time. Two route files mapping to the same URL path with different representations raise `ValueError`.
- **Order relative to `error.py`**: `error.py` boundaries run first, leaf-to-root. `representation.error` runs only as the final fallback past the root `error.py` (replacing Chano's default `error_response`). If `representation.success` raises, the exception enters `error.py` like any handler error.
- **Bundling**: `chano build` includes ancestor `representation.py` files in each function's trace roots so their imports are vendored into deployed functions.
