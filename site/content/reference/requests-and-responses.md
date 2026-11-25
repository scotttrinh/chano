---
title: Requests, responses, and background work
description: Reference for Request, Geo, Response classes, cookies, StreamingResponse, SSEResponse, and after().
lede: Inspect incoming HTTP requests and Vercel edge metadata, return buffered or streaming responses, manage cookies, and schedule post-response tasks.
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

## Outcomes and `chano.status`

`Outcome[T]`, `Failure[T = ErrorDetail]`, and `ErrorDetail` are exported from `chano`. `Outcome` is a `Response` whose status is fixed by its class and whose payload is JSON-encoded (`None` and bodyless statuses `1xx`, `204`, `304` send no body). `Failure` is both an `Outcome` and an `Exception`—return it for static type checking or raise it from deep code.

Named statuses in `chano.status`:

| Category | Classes |
| --- | --- |
| Success / redirect | `OK` (200), `Created` (201), `Accepted` (202), `NoContent` (204), `SeeOther` (303), `NotModified` (304) |
| Failures | `BadRequest` (400), `Unauthorized` (401), `Forbidden` (403), `NotFound` (404), `MethodNotAllowed` (405), `Conflict` (409), `Gone` (410), `UnprocessableContent` (422), `TooManyRequests` (429), `InternalServerError` (500), `ServiceUnavailable` (503) |

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
