---
title: Parameters and validation
description: Parameter source inference, marker classes, dependency injection, lazy singletons, security schemes, and validation errors.
lede: How Chano resolves handler and dependency arguments from Python type annotations, validates inputs with msgspec or Pydantic, and reports errors.
section: reference
order: 2
nav_title: Parameters and validation
---

## Source inference order

For each parameter of a handler or dependency callable, Chano selects one of seven sources in this order:

1. **`Depends(fn)`** (as a default value or inside `Annotated`): resolves `fn` recursively and injects its return value.
2. **`Request` annotation**: injects the current `chano.Request`.
3. **Explicit markers** (`Query()`, `Header()`, `Cookie()`, `Body()`): forces the named source.
4. **Path parameter**: if the parameter name matches a `[segment]` in the route path, reads from URL path parameters.
5. **Body model**: if the type is a `msgspec.Struct`, `dataclass`, `attrs` class, or `pydantic.BaseModel`, decodes from the JSON request body.
6. **Query parameter**: all other parameters default to query string values.

## Parameter markers

Markers can be used as defaults (`q: str = Query()`) or inside `Annotated` (`q: Annotated[str, Query()]`):

| Marker | Reads from | Notes |
| --- | --- | --- |
| `Query(default=..., alias=None)` | URL query string | Non-list types take the last value when repeated |
| `Header(default=..., alias=None)` | HTTP request headers | Underscores convert to hyphens (`x_trace_id` -> `x-trace-id`) unless `alias` is set |
| `Cookie(default=..., alias=None)` | `Cookie` request headers | Unquotes `http.cookies` quoting; first occurrence wins on duplicates |
| `Body(default=..., alias=None)` | JSON request body | Forces a scalar or container to be read from the JSON body |

## Body decoding and coercion

- **Single body parameter**: decodes raw request bytes directly into the annotated type in a single `msgspec.json.decode` pass (or `pydantic.TypeAdapter.validate_json` for Pydantic-backed annotations).
- **Multiple body parameters**: decodes the body as a JSON object and validates each field by parameter name or `alias` in strict mode.
- **Path, query, header, and cookie parameters**: values arrive as strings and are coerced leniently (`strict=False`, for example `"42"` to `int`).
- **Optional Pydantic support**: Chano inspects `sys.modules.get("pydantic")` lazily and caches a `TypeAdapter` at endpoint construction time. Projects that do not import `pydantic` pay zero import cost.

## `Depends(fn)`

```python
from chano import Depends
```

- Resolves `fn`'s own signature using the same inference rules as route handlers.
- Results are cached **per request** by callable identity, so a dependency referenced multiple times in one request executes once.
- Async dependencies are awaited on the event loop; sync dependencies run via `asyncio.to_thread`.

## `lazy(factory)`

```python
from chano import lazy
```

Wraps a zero-argument `factory` (sync or async) into a process-wide singleton callable that initializes on first call and caches the result for subsequent warm invocations:

- Sync factories initialize under a `threading.Lock` with double-checked locking.
- Async factories share a single in-flight creation task across concurrent callers, even across event loops, and survive cancellation of the initiating caller.
- Failed attempts are never cached; the next call retries.
- Safe to pass to `Depends(lazy(factory))` without adding request parameters to OpenAPI.

## `HTTPBearer` and `@raises`

### `HTTPBearer(name="HTTPBearer")`

```python
from chano import Depends, HTTPBearer

bearer = HTTPBearer()

def GET(token: str = Depends(bearer)):
    return {"token": token}
```

- Parses `Authorization: Bearer <token>` (case-insensitive scheme).
- Raises `HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})` when missing or malformed.
- Adds an HTTP bearer entry under `components.securitySchemes` and attaches a `security` requirement to the OpenAPI operation.

### `@raises(status_code, description)`

```python
from chano import HTTPException, raises

@raises(404, "Item not found")
def GET(item_id: int):
    raise HTTPException(404, "Item not found")
```

- Documents an error response (`400` to `599`) in `openapi.json` for a handler or dependency.
- Documentation-only: runtime code must still raise `HTTPException`.
- When stacked or inherited from dependencies, handler declarations take precedence over direct dependencies, which take precedence over inherited checks.

## Error types

### `HTTPException(status_code, detail=None, headers=None)`

Raises an HTTP error rendered by default as JSON `{"detail": ...}` with the given `status_code` and optional `headers`.

### `RequestValidationError(errors)`

Raised automatically with status `422` when parameter decoding or validation fails. The default response body is:

```json
{
  "detail": [
    {
      "loc": ["body", "price"],
      "msg": "Expected `float` >= 0 - at `$.price`"
    }
  ]
}
```
