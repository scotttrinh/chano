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

1. **`Depends(fn)`** as a default value (`user: User = Depends(fn)`): resolves `fn` recursively, checks the parameter type against `fn`'s non-`Response` return type, and injects its value (short-circuiting if `fn` returns a `Response`).
2. **`Request` annotation**: injects the current `chano.Request`.
3. **Explicit markers** (`Query()`, `Header()`, `Cookie()`, `Body()`) inside `Annotated[...]`: forces the named source.
4. **Path parameter**: if the parameter name matches a `[segment]` in the route path, reads from URL path parameters.
5. **Body model**: if the type is a `msgspec.Struct`, `dataclass`, `attrs` class, or `pydantic.BaseModel`, decodes from the JSON request body.
6. **Query parameter**: all other parameters default to query string values.

## Parameter markers

Source markers must be placed inside `Annotated[...]` (`q: Annotated[str, Query()]`). Passing `Query()`, `Header()`, `Cookie()`, or `Body()` as a parameter default value raises `TypeError`. Default values use standard Python syntax (`limit: Annotated[int, Query()] = 10`).

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

When multiple consumers (such as an inherited guard in `boundary.py`, a `Depends(...)` helper, and the handler) read the same input, OpenAPI merges their schemas with `allOf` so every constraint is documented.

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
from chano import Depends
```

- Pass `Depends(fn)` as a parameter default value: `user: User = Depends(current_user)`. Its static type strips `Response` branches from `fn`'s return type, so type checkers verify the parameter annotation.
- Resolves `fn`'s own signature using the same inference rules as route handlers, including nested `Depends(...)` calls and `Query`/`Header`/`Cookie`/`Body` inputs.
- If `fn` returns any `Response` (such as `Unauthorized(...)` or `SeeOther("/login")`), Chano short-circuits immediately and sends that response without running later dependencies or the handler.
- Results are cached **per request** by callable identity, so a dependency referenced multiple times in one request executes once.
- Async dependencies are awaited on the event loop; sync dependencies run via `asyncio.to_thread`.
- Inherited guards in `boundary.py` run root-to-leaf before handler arguments resolve, may short-circuit by returning a `Response`, and share the same per-request cache as `Depends(fn)` (note: generator functions are rejected as guards).

```python
from typing import Annotated

from msgspec import Meta, Struct

from chano import Depends, Header, Query
from chano.status import Forbidden


class Pagination(Struct):
    skip: int
    limit: int


def get_pagination(
    skip: Annotated[int, Meta(ge=0), Query()] = 0,
    limit: Annotated[int, Meta(ge=1, le=100), Query()] = 20,
) -> Pagination:
    return Pagination(skip=skip, limit=limit)


def require_tenant(x_tenant: Annotated[str, Header()]) -> str | Forbidden:
    if x_tenant not in ("acme", "globex"):
        return Forbidden("Unknown tenant")
    return x_tenant


def GET(
    tenant: str = Depends(require_tenant),
    page: Pagination = Depends(get_pagination),
):
    return {"tenant": tenant, "skip": page.skip, "limit": page.limit}
```

## `lazy(factory)`

```python
from chano import Depends, lazy
```

Wraps a zero-argument `factory` (sync or async) into a process-wide singleton callable that initializes on first call and caches the result for subsequent warm invocations:

- Sync factories initialize under a `threading.Lock` with double-checked locking.
- Async factories share a single in-flight creation task across concurrent callers, even across event loops, and survive cancellation of the initiating caller.
- Failed attempts are never cached; the next call retries.
- Safe to pass to `client: Client = Depends(lazy(factory))` without adding request parameters to OpenAPI.

## `HTTPBearer`

### `HTTPBearer(name="HTTPBearer")`

```python
from chano import Depends, HTTPBearer

bearer = HTTPBearer()


def GET(token: str = Depends(bearer)):
    return {"token": token}
```

- Parses `Authorization: Bearer <token>` (case-insensitive scheme).
- Annotated `-> str | Unauthorized`: returns `Unauthorized("Not authenticated", headers={"www-authenticate": "Bearer"})` when missing or malformed, so its `401` response is documented automatically in OpenAPI without any decorator.
- Adds an HTTP bearer entry under `components.securitySchemes` and attaches a `security` requirement to the OpenAPI operation.

## Validation errors

### `RequestValidationError(errors)`

A `Failure` subclass (`UnprocessableContent[ValidationErrorDetail]`) raised automatically with status `422` when parameter decoding or validation fails, keeping `.errors`. The default response body is:

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
