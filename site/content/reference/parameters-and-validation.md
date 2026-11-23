---
title: Parameters and validation
description: Parameter source inference, Annotated markers, msgspec and Pydantic validation, dependency injection, lazy singletons, security schemes, and validation errors.
lede: How Chano resolves handler and dependency arguments from Python type annotations, validates inputs with msgspec or Pydantic, and reports errors.
section: reference
order: 2
nav_title: Parameters and validation
---

## Source inference order

For each parameter of a handler or dependency callable, Chano selects one of seven sources in this order:

1. **`Depends(fn)`** inside `Annotated[T, Depends(fn)]`: resolves `fn` recursively and injects its return value.
2. **`Request` annotation**: injects the current `chano.Request`.
3. **Explicit markers** (`Query()`, `Header()`, `Cookie()`, `Body()`) inside `Annotated[...]`: forces the named source.
4. **Path parameter**: if the parameter name matches a `[segment]` in the route path, reads from URL path parameters.
5. **Body model**: if the type is a `msgspec.Struct`, `dataclass`, `attrs` class, or `pydantic.BaseModel`, decodes from the JSON request body.
6. **Query parameter**: all other parameters default to query string values.

## Parameter markers

Markers must be placed inside `Annotated[...]` (`q: Annotated[str, Query()]`). Passing `Query()`, `Header()`, `Cookie()`, `Body()`, or `Depends()` as a parameter default value raises `TypeError`. Default values use standard Python syntax (`limit: Annotated[int, Query()] = 10`).

| Marker | Reads from | Notes |
| --- | --- | --- |
| `Query(*, alias=None)` | URL query string | Non-list types take the last value when repeated; `list[T]` collects all values |
| `Header(*, alias=None)` | HTTP request headers | Underscores convert to hyphens (`x_trace_id` -> `x-trace-id`) unless `alias` is set; matched case-insensitively |
| `Cookie(*, alias=None)` | `Cookie` request headers | Unquotes `http.cookies` quoting; first occurrence wins on duplicates |
| `Body(*, alias=None)` | JSON request body | Forces a scalar or container to be read from the JSON body; `alias` sets the key when embedded |

```python
from typing import Annotated

from chano import Body, Cookie, Header, Query


def POST(
    item_id: int,  # path parameter from [item_id]
    q: Annotated[str | None, Query(alias="search")] = None,
    tags: Annotated[list[str], Query()] = [],
    x_trace_id: Annotated[str | None, Header()] = None,
    session: Annotated[str | None, Cookie(alias="sid")] = None,
    count: Annotated[int, Body()] = 1,
):
    return {
        "item_id": item_id,
        "q": q,
        "tags": tags,
        "trace": x_trace_id,
        "session": session,
        "count": count,
    }
```

## Validation with `msgspec.Meta` and models

Chano validates every parameter against its full `Annotated` type using `msgspec` (or `pydantic.TypeAdapter` when a `pydantic.BaseModel` is present). Use `msgspec.Meta` to attach constraints to path, query, header, cookie, or body parameters—and combine `Meta(...)` with a marker in the same `Annotated[...]`:

```python
from typing import Annotated

from msgspec import Meta, Struct

from chano import Header, Query


class Item(Struct):
    name: Annotated[str, Meta(min_length=1, max_length=100)]
    price: Annotated[float, Meta(gt=0)]
    sku: Annotated[str, Meta(pattern=r"^[A-Z0-9-]+$")]


def GET(
    item_id: Annotated[int, Meta(ge=1)],
    limit: Annotated[int, Meta(ge=1, le=100), Query()] = 20,
    x_client_id: Annotated[str, Meta(min_length=3), Header()] = "web",
):
    return {"item_id": item_id, "limit": limit, "client": x_client_id}
```

Common `msgspec.Meta` constraints include:

- **Numeric bounds**: `gt`, `ge`, `lt`, `le`, `multiple_of`
- **String / bytes / collection length**: `min_length`, `max_length`
- **String format**: `pattern` (regular expression)
- **OpenAPI metadata**: `title`, `description`, `examples` (published in `openapi.json`)

When multiple consumers (such as an inherited check in `dependencies.py`, a `Depends(...)` helper, and the handler) read the same input, OpenAPI merges their schemas with `allOf` so every constraint is documented.

## Body decoding and coercion

- **Single body parameter**: decodes raw request bytes directly into the annotated type in a single `msgspec.json.decode` pass (or `pydantic.TypeAdapter.validate_json` for Pydantic-backed annotations).
- **Multiple body parameters**: when a handler or dependency has more than one body parameter, Chano decodes the body once as a JSON object and validates each field by parameter name or `alias` in strict mode (equivalent to `embed=True`):

```python
from typing import Annotated

from msgspec import Meta, Struct

from chano import Body


class Item(Struct):
    name: str
    price: Annotated[float, Meta(gt=0)]


def PUT(
    item: Item,
    note: Annotated[str, Meta(max_length=200), Body()] = "",
):
    # Expects JSON body: {"item": {"name": "...", "price": 1.5}, "note": "..."}
    return {"item": item, "note": note}
```

- **Path, query, header, and cookie parameters**: values arrive as strings and are coerced leniently (`strict=False`, for example `"42"` to `int`, `"true"` to `bool`).
- **Optional Pydantic support**: Chano inspects `sys.modules.get("pydantic")` lazily and caches a `TypeAdapter` at endpoint construction time. Projects that do not import `pydantic` pay zero import cost.

## `Depends(fn)`

```python
from typing import Annotated

from chano import Depends
```

- Place `Depends(fn)` inside `Annotated[T, Depends(fn)]`. Passing `Depends(fn)` as a default value raises `TypeError`.
- Resolves `fn`'s own signature using the same inference rules as route handlers, including nested `Depends(...)` calls and `Query`/`Header`/`Cookie`/`Body` inputs.
- Results are cached **per request** by callable identity, so a dependency referenced multiple times in one request executes once.
- Async dependencies are awaited on the event loop; sync dependencies run via `asyncio.to_thread`.
- Inherited checks in `dependencies.py` run root-to-leaf before handler arguments resolve, and share the same per-request cache as `Depends(fn)` (note: generator functions are rejected in `dependencies.py`).

```python
from typing import Annotated

from msgspec import Meta, Struct

from chano import Depends, Header, HTTPException, Query


class Pagination(Struct):
    skip: int
    limit: int


def get_pagination(
    skip: Annotated[int, Meta(ge=0), Query()] = 0,
    limit: Annotated[int, Meta(ge=1, le=100), Query()] = 20,
) -> Pagination:
    return Pagination(skip=skip, limit=limit)


def require_tenant(x_tenant: Annotated[str, Header()]) -> str:
    if x_tenant not in ("acme", "globex"):
        raise HTTPException(403, "Unknown tenant")
    return x_tenant


def GET(
    tenant: Annotated[str, Depends(require_tenant)],
    page: Annotated[Pagination, Depends(get_pagination)],
):
    return {"tenant": tenant, "skip": page.skip, "limit": page.limit}
```

## `lazy(factory)`

```python
from typing import Annotated

from chano import Depends, lazy
```

Wraps a zero-argument `factory` (sync or async) into a process-wide singleton callable that initializes on first call and caches the result for subsequent warm invocations:

- Sync factories initialize under a `threading.Lock` with double-checked locking.
- Async factories share a single in-flight creation task across concurrent callers, even across event loops, and survive cancellation of the initiating caller.
- Failed attempts are never cached; the next call retries.
- Safe to pass to `Annotated[T, Depends(lazy(factory))]` without adding request parameters to OpenAPI.

## `HTTPBearer` and `@raises`

### `HTTPBearer(name="HTTPBearer")`

```python
from typing import Annotated

from chano import Depends, HTTPBearer

bearer = HTTPBearer()


def GET(token: Annotated[str, Depends(bearer)]):
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
