---
title: Requests, responses, and background work
description: Reference for Request, Geo, Response classes, Representation, cookies, StreamingResponse, SSEResponse, and after().
lede: Inspect incoming HTTP requests and Vercel edge metadata, return buffered or streaming responses, customize representations, manage cookies, and schedule post-response tasks.
section: reference
order: 3
nav_title: Requests and responses
---

## `Request`

Annotate any handler or dependency parameter with `request: Request` to receive the ASGI request wrapper.

| Attribute or method | Type | Description |
| --- | --- | --- |
| `request.method` | `str` | HTTP method (for example `"GET"`) |
| `request.path` | `str` | Full public path including `base_path` |
| `request.path_params` | `dict[str, str]` | Matched dynamic route segments |
| `request.query_params` | `dict[str, list[str]]` | Parsed query string; each key maps to all values |
| `request.headers` | `dict[str, str]` | Request headers keyed by lower-case name |
| `request.cookies` | `dict[str, str]` | Parsed cookies; unquoted, first occurrence wins |
| `await request.body()` | `bytes` | Cached raw request body bytes |
| `await request.json()` | `Any` | Parsed JSON request body |
| `request.id` | `str` | `x-vercel-id` header, or a generated UUID4 hex string locally |
| `request.ip` | `str \| None` | `x-real-ip`, else first `x-forwarded-for`, else ASGI client host |
| `request.geo` | `Geo` | Frozen struct populated from Vercel `x-vercel-ip-*` headers |

### `Geo` fields

`request.geo` returns a frozen `Geo` struct with optional `str | None` fields:

- `country` (`x-vercel-ip-country`)
- `region` (`x-vercel-ip-country-region`, ISO 3166-2 region code)
- `city` (`x-vercel-ip-city`, percent-decoded)
- `latitude` (`x-vercel-ip-latitude`)
- `longitude` (`x-vercel-ip-longitude`)
- `postal_code` (`x-vercel-ip-postal-code`)

Outside Vercel, `x-vercel-*` and `x-forwarded-for` headers are client-supplied and must not be trusted for authorization.

## Buffered responses

| Class | Default status | Default `Content-Type` | Notes |
| --- | --- | --- | --- |
| `Response[T](content=b"", status_code=200, headers=None, media_type=None)` | `200` | `media_type` | Base generic ASGI response; stores `content` and renders `body` lazily |
| `JSONResponse[T](content, status_code=200, headers=None)` | `200` | `application/json` | Generic JSON response encoded with `msgspec` (and Pydantic `model_dump`) |
| `HTMLResponse[T](content, status_code=200, headers=None)` | `200` | `text/html; charset=utf-8` | HTML body |
| `PlainTextResponse[T](content, status_code=200, headers=None)` | `200` | `text/plain; charset=utf-8` | Plain text body |
| `RedirectResponse(url, status_code=307, headers=None)` | `307` | None | Sets `location` header |

Generic `JSONResponse[T]` keeps the payload type visible to type checkers and OpenAPI while allowing custom status codes, headers, and cookies:

```python
from typing import Annotated

from chano import JSONResponse, Status


def POST(item: Item) -> Annotated[JSONResponse[Item], Status(201)]:
    response = JSONResponse(item, status_code=201, headers={"X-Request-Id": "1"})
    response.set_cookie("session", "abc123")
    return response
```

## Cookies

Every `Response` class (including `StreamingResponse` and `SSEResponse`) supports setting and deleting cookies:

```python
response = JSONResponse({"ok": True})
response.set_cookie("session", "abc123", max_age=3600)
response.delete_cookie("old_session")
return response
```

- `set_cookie(key, value="", max_age=None, expires=None, path="/", domain=None, secure=True, httponly=True, samesite="lax")`: defaults to `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`. Validates names, Latin-1 values, and attributes eagerly (raising `ValueError` on invalid inputs).
- `delete_cookie(key, path="/", domain=None)`: expires the cookie with `Max-Age=0` while preserving `Secure; HttpOnly; SameSite=Lax`. `path` and `domain` must match the original cookie.

## `StreamingResponse`

```python
from chano import StreamingResponse
```

`StreamingResponse(content, status_code=200, headers=None, media_type=None)` streams an async or sync iterable of `bytes` or `str` chunks without a `content-length` header:

- Sync iterables advance in a worker thread so blocking producers do not stall the event loop.
- On `HEAD` requests, sends headers and an empty body without starting the iterator.
- Watches ASGI `receive` for `http.disconnect`, cancels the producer, and closes the iterator (`aclose()` or `close()`).

## `SSEResponse` and `ServerSentEvent`

```python
from chano import SSEResponse, ServerSentEvent
```

`SSEResponse(events, status_code=200, headers=None, ping=None)` streams Server-Sent Events with `content-type: text/event-stream` and `cache-control: no-cache`:

- Yielded items may be `ServerSentEvent(data=..., event=None, id=None, retry=None)`, a raw `str`, or any JSON-serializable value (encoded with `msgspec`).
- `ping` (positive finite number of seconds, or `None`) interleaves `: ping` keepalive comment lines to prevent idle proxy timeouts.
- Newlines in `event` or `id` raise `ValueError` at construction.

## `after(fn, /, *args, **kwargs)`

```python
from chano import after
```

Schedules a sync or async callable to run after the HTTP response finishes sending:

- Must be called during an active request; calling outside a request or inside another `after` callback raises `RuntimeError`.
- Sync callbacks run via `asyncio.to_thread`; async callbacks are awaited on the event loop.
- Exceptions raised inside callbacks are logged to `stderr` without affecting the HTTP response or other queued callbacks.
- In `chano dev` and tests, callbacks drain in the request's `finally` block. In built Vercel functions, Chano delegates to `vercel.functions.wait_until`.

## `Representation`

```python
from chano import (
    JSONRepresentation,
    Raw,
    Representation,
    Status,
    StatusRepresentation,
    raises,
    representation,
    status_code,
)
from chano.status import Created, NoContent, NotFound
```

`Representation` is a `typing.Protocol` that pairs status- and type-keyed wire rendering with matching OpenAPI schema transformations for a route subtree (via [`representation.py`](/reference/routing#representation-py)), a single route (via `Chano.add_route(..., representation=...)`), or an individual handler (via `Annotated[T, MyRep()]` / `@representation(MyRep())`).

| Member | Type / Signature | Role |
| --- | --- | --- |
| `response_type` | `type[Response] \| tuple[type[Response], ...]` | `Response` subclass(es) handled by this representation |
| `media_type(status_code)` | `(int) -> str` | OpenAPI content key for responses with `status_code` |
| `body_type(status_code, returns)` | `(int, Any) -> Any` | Maps an unwrapped payload annotation (for `< 400`) or `None` (for `>= 400`) to the wire body annotation used in OpenAPI, or `None` for a bodyless response |
| `render(request, value, status_code)` | `(Request, Any, int) -> Response \| Awaitable[Response]` | Renders a matching payload value or uncaught endpoint exception into a `Response` for `status_code` |

### Sync and async calls

`render` may be sync or async: Chano calls it directly and awaits the result if it is awaitable. Returning anything other than a `Response` raises `TypeError` naming the representation.

When an uncaught endpoint exception reaches the fallback past `error.py`, Chano derives `status_code` (`exc.status_code` for `HTTPException`, `422` for `RequestValidationError`, `500` otherwise) and calls `render(request, exc, status_code)`. `JSONRepresentation.render` logs unexpected exception tracebacks to `stderr`; a custom `render` that handles unexpected exceptions itself must log them, or they are lost.

### `JSONRepresentation`

`JSONRepresentation` is a concrete default implementation matching Chano's built-in wire format (`JSONResponse` for payload values, `error_response` for exceptions):

- `response_type` defaults to `JSONResponse`.
- `media_type(status_code)` returns `"application/json"`.
- `body_type(status_code, returns)` returns `returns` for `status_code < 400`, `ValidationErrorDetail` (`{"detail": [{"loc": [...], "msg": "..."}]}`) for `422`, and `ErrorDetail` (`{"detail": Any}`) for other `>= 400` statuses.
- `render(request, value, status_code)` delegates `Exception` values to `error_response(value)` (logging unexpected tracebacks to `stderr`) and returns `JSONResponse(value, status_code=status_code)` for non-exception values.

### `StatusRepresentation`

`StatusRepresentation(routes, *, default=JSONRepresentation())` composes separate representations by HTTP status code without coupling them:

```python
from chano import StatusRepresentation

representation = StatusRepresentation(
    {
        range(200, 400): Envelope(),
        range(400, 600): ProblemJSON(),
    }
)
```

- Keys in `routes` may be `int`, `range`, or any `Container[int]` (such as `frozenset` or `tuple`), checked in insertion order.
- `response_type` is the union tuple of all child `response_type`s (or `JSONResponse` if empty); runtime and OpenAPI matching check the resolved child's `response_type` for each `status_code`.
- Unmatched statuses delegate to `default` (`JSONRepresentation()` by default, or `None` to fall back to Chano's default wire/OpenAPI behavior).

### Runtime and OpenAPI behavior

- **Type-keyed matching and passthrough**: non-`Response` handler returns are treated as implicit `JSONResponse` (matching any representation whose `response_type` for that status includes `JSONResponse`, or falling back to `JSONResponse` otherwise). When a handler returns an explicit `Response` matching the active representation's `response_type` (for example `JSONResponse(item, status_code=201, headers=...)` with cookies), Chano passes `(request, result.content, result.status_code)` to `representation.render` without eager JSON encoding, preserving `status_code`, custom headers (without clobbering representation-owned `content-length` or default `content-type`), and `set-cookie` headers. Non-matching `Response` subclasses (`RedirectResponse`, `HTMLResponse`, `PlainTextResponse`, `StreamingResponse`, `SSEResponse`, raw `Response`) pass through untouched. In OpenAPI, generic `JSONResponse[T]` / `Response[T]` unwrap `T` before `body_type(status, T)` runs.
- **Unified `Annotated` return metadata, `chano.status`, and decorators**: `Status(code, *, description=None)`, a `Representation` instance, and the `Raw` opt-out sentinel all live in return `Annotated[T, ...]` annotations—supporting unions (such as `Annotated[Item, Status(201)] | Annotated[Job, Status(202)]`), Python 3.12 `TypeAliasType` aliases from `chano.status` (`OK[T]`, `Created[T]`, `Accepted[T]`, `NoContent`, `NotModified`, redirects, `BadRequest[T]`, `Unauthorized[T]`, `Forbidden[T]`, `NotFound[T]`, `Conflict[T]`, `Gone[T]`, `UnprocessableContent[T]`, `TooManyRequests[T]`, `InternalServerError[T]`, `BadGateway[T]`, `ServiceUnavailable[T]`, etc.), and bodyless statuses (`204`, `205`, `304` with `None`, which omit response bodies and OpenAPI `content`). `Status` requires a final HTTP status code (`200`–`599`, rejecting `1xx`). `Status` with `code < 400` binds to the return branch (an unambiguous single `< 400` `Status` sets the runtime status code for plain non-`Response` returns, while explicit `Response.status_code` overrides still win; returning a plain non-`Response` value when multiple distinct `< 400` statuses are declared raises `TypeError`). `Status` with `code >= 400` is additive error metadata (so `NotFound[Item]` means `200 Item` + `404`, and `Created[NotFound[Item]]` means `201 Item` + `404`), and only `>= 400` statuses propagate from `Depends(...)` callables and `dependencies.py` checks. `@status_code(...)`, `@raises(code, description)` (attaching `Status(code, description=description)`), and `@representation(...)` are thin decorator sugar wrapping the function's return annotation in `Annotated[...]` (with `@representation(None)` or `@representation(Raw)` opting out of `representation.py`).
- **Error order and fallback**: [`error.py`](/reference/routing#error-py) handlers run first, leaf-to-root. The handler's active representation (`@representation(...)` / `Annotated[..., Rep()]`, or `None` for `Raw`, else `representation.py`) runs `render(request, exc, status_code)` only as the final fallback past the root `error.py`. Exceptions raised by `representation.render` during normal return rendering or body serialization flow through `error.py` like any handler error. If error `representation.render` raises or returns a non-`Response`, Chano falls back to `error_response` for that new exception so a broken representation still produces a response.
- **OpenAPI error responses**: for routes with an active representation, `422` (when the endpoint has request inputs) and every `>= 400` status declared with `Status` / `chano.status` / `@raises` call `media_type(status)` and `body_type(status, None)`, keeping each description. Returning `None` from `body_type(status, None)` leaves that status without a response body.
- **Unchanged default**: routes without an active representation keep the built-in wire format and exact default OpenAPI output.

### Limitations

- Unmatched-path `404` responses and exceptions raised inside `middleware.py` still use Chano's default error mapping rather than a route's representation.
- `>= 400` `Status` / `@raises` declare `(status, description)` pairs rather than exception classes.
