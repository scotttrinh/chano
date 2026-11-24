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
| `Response(content=b"", status_code=200, headers=None, media_type=None)` | `200` | `media_type` | Base ASGI response (`bytes` or `str`) |
| `JSONResponse(content, status_code=200, headers=None)` | `200` | `application/json` | Encodes with `msgspec` (and Pydantic `model_dump`) |
| `HTMLResponse(content, status_code=200, headers=None)` | `200` | `text/html; charset=utf-8` | HTML body |
| `PlainTextResponse(content, status_code=200, headers=None)` | `200` | `text/plain; charset=utf-8` | Plain text body |
| `RedirectResponse(url, status_code=307, headers=None)` | `307` | None | Sets `location` header |

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
from chano import JSONRepresentation, Representation
```

`Representation` is a `typing.Protocol` that pairs runtime response and error rendering with the matching OpenAPI schema transformations for a route subtree (via [`representation.py`](/reference/routing#representation-py)) or a single route (via `Chano.add_route(..., representation=...)`).

| Member | Type / Signature | Role |
| --- | --- | --- |
| `media_type` | `str` | OpenAPI content key for `200` success responses |
| `error_media_type` | `str` | OpenAPI content key for `422` and `@raises` error responses |
| `success_type(returns)` | `(Any) -> Any` | Maps a handler's return annotation to the wire body annotation used for the `200` OpenAPI schema |
| `success(request, value)` | `(Request, Any) -> Response \| Awaitable[Response]` | Renders a non-`Response` handler return value into a `Response` |
| `error_type(status_code)` | `(int) -> Any` | Returns the error body annotation for `status_code`, or `None` for a bodyless OpenAPI response |
| `error(request, exc)` | `(Request, Exception) -> Response \| Awaitable[Response]` | Renders an uncaught endpoint exception into a `Response` |

### Sync and async calls

`success` and `error` may be sync or async: Chano calls them directly and awaits the result if it is awaitable. Returning anything other than a `Response` raises `TypeError` naming the representation.

`error` receives every endpoint exception, including unexpected ones. `JSONRepresentation.error` logs their traceback to `stderr`; an `error` that handles them itself must log them, or they are lost.

### `JSONRepresentation`

`JSONRepresentation` is a concrete default implementation matching Chano's built-in wire format (`JSONResponse` for success, `error_response` for errors) so you can subclass it and override only the success half or only the error half:

- `media_type` and `error_media_type` are `"application/json"`.
- `success_type(returns)` returns `returns` unchanged; `success(request, value)` returns `JSONResponse(value)`.
- `error_type(status_code)` returns `ValidationErrorDetail` (`{"detail": [{"loc": [...], "msg": "..."}]}`) for `422` and `ErrorDetail` (`{"detail": Any}`) for other statuses; `error(request, exc)` delegates to `error_response(exc)`.

### Runtime and OpenAPI behavior

- **Explicit `Response` bypass**: if a handler returns a `Response` instance directly, `representation.success` is skipped. In OpenAPI, `200` only uses `media_type` and `success_type(returns)` when the handler has a return annotation that is neither `None` nor a `Response` subclass.
- **Error order and fallback**: [`error.py`](/reference/routing#error-py) handlers run first, leaf-to-root. `representation.error` replaces `error_response` only as the final fallback past the root `error.py`. Exceptions raised by `representation.success` flow through `error.py` like any handler error. If `representation.error` raises or returns a non-`Response`, Chano falls back to `error_response` for that new exception so a broken representation still produces a response.
- **OpenAPI error responses**: for routes with a representation, `422` (when the endpoint has request inputs) and every status declared with `@raises` use `error_media_type` and `error_type(status)`, keeping each `@raises` description. Returning `None` from `error_type(status)` leaves that status without a response body.
- **Unchanged default**: routes without a representation keep the built-in wire format and exact default OpenAPI output.

### Limitations

- Unmatched-path `404` responses and exceptions raised inside `middleware.py` still use Chano's default error mapping rather than a route's representation.
- `@raises` continues to declare `(status, description)` pairs rather than exception classes, and return-union error types are not modeled.
