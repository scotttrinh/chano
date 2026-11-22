---
title: Routing and special files
description: File-system routing conventions, HTTP method exports, match precedence, folder-scoped hooks, and route-level options.
lede: Folders under app/ define URLs, route.py exports HTTP handlers, and sibling special files scope middleware, errors, and dependencies to a subtree.
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

A `route.py` may define module-level variables to configure its deployed Vercel function and OpenAPI visibility:

| Variable | Type | Default | Effect |
| --- | --- | --- | --- |
| `max_duration` | `int` | Platform default | Sets `maxDuration` (seconds) in `.vc-config.json` |
| `memory` | `int` | Platform default | Sets `memory` (MB) in `.vc-config.json` |
| `regions` | `list[str]` | Platform default | Sets `regions` in `.vc-config.json` |
| `include` | `Sequence[str]` | `()` | Extra distribution names bundled into this function |
| `include_in_schema` | `bool` | `True` | When `False`, omits all methods in this file from `/openapi.json` and `/docs` |

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
- Re-raising (a bare `raise` or a new exception) passes the exception to the next ancestor `error.py`, then to Chano's default error mapping (`422` for validation, `HTTPException` JSON, or `500` plain text).
- Exceptions raised by middleware bypass `error.py` and use Chano's default error mapping.

### `dependencies.py`

Exports `dependencies: list[Callable] | tuple[Callable, ...]`:

- Executes **root-to-leaf** in declared order before any handler argument is resolved.
- Each item is a bare callable (not wrapped in `Depends(...)`) and must not be a generator or async generator.
- Shares the per-request dependency cache with handler `Depends(...)`, so a callable listed in `dependencies.py` and requested via `Depends()` runs once per request.
- Any `HTTPBearer` or `@raises` declarations on inherited checks are included in generated OpenAPI schemas.
