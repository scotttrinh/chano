---
title: Response annotations
description: Describe status codes, error responses, headers, cookies, and representations on route return types.
lede: Use Python Annotated return types—or thin decorator sugar—to keep runtime behavior, type checking, and OpenAPI schemas in sync.
section: guides
order: 35
---

Just as Chano uses `Annotated[T, Query()]`, `Annotated[T, Header()]`, and `Annotated[T, Depends(...)]` to describe request inputs, it uses `Annotated` metadata on **return types** to describe HTTP outcomes. Status codes, documented error responses, and per-handler representation overrides all live in one place, keeping runtime behavior, static type checking (`ty`), and generated OpenAPI schemas aligned.

## Status codes with `chano.status` and `Status`

Import standard Python 3.12+ status aliases from `chano.status`—or attach `Status(code, description=...)` directly with `Annotated` when you want a custom OpenAPI description:

```python
import msgspec
from chano.status import Created, NoContent


class CreateItem(msgspec.Struct):
    name: str


class Item(msgspec.Struct):
    id: str
    name: str


def post(body: CreateItem) -> Created[Item]:
    return Item(id="1", name=body.name)


def delete(item_id: str) -> NoContent:
    return None
```

When a return annotation has a single unambiguous `< 400` `Status(...)`, Chano uses that status code at runtime for plain return values and documents it in OpenAPI. Bodyless statuses (`204`, `205`, `304`) omit both the wire body and OpenAPI `content`. `Status(...)` accepts final HTTP status codes (`200`–`599`) and rejects `1xx` informational statuses.

## Multi-status unions (`Payload -> Status`)

When an endpoint can return different payloads or statuses depending on the branch, use a union of status-annotated types. OpenAPI generates an entry for each status with its matching payload schema (and combines multiple schemas sharing the same status and media type with `anyOf`). Because Chano cannot infer which status a plain non-`Response` return value is meant to send when multiple `< 400` statuses are declared, multi-status handlers must return an explicit `Response` (such as `JSONResponse(..., status_code=...)`); returning a plain value raises `TypeError`:

```python
import msgspec
from chano import JSONResponse
from chano.status import OK, Accepted


class Item(msgspec.Struct):
    id: str


class Job(msgspec.Struct):
    job_id: str


def post(async_mode: bool = False) -> OK[JSONResponse[Item]] | Accepted[JSONResponse[Job]]:
    if async_mode:
        return JSONResponse(Job(job_id="j1"), status_code=202)
    return JSONResponse(Item(id="1"), status_code=200)
```

## Headers, cookies, and `JSONResponse[T]`

When a route uses a `Representation` (such as an envelope wrapper from `representation.py`), returning `JSONResponse(item, status_code=201, headers=...)` does **not** bypass the representation:

- `Representation`s are keyed by `response_type` (`JSONResponse` by default on `JSONRepresentation`).
- Chano passes `(request, result.content, result.status_code)` to `representation.render(...)` and preserves your `status_code`, custom headers, and `set_cookie` / `delete_cookie` calls.
- Generic `JSONResponse[Item]` lets OpenAPI unwrap `Item` and run `representation.body_type(status, Item)` so the envelope schema stays accurate.
- Non-matching `Response` subclasses (`RedirectResponse`, `HTMLResponse`, `PlainTextResponse`, `StreamingResponse`, raw `Response`) pass through untouched.

```python
from chano import JSONResponse
from chano.status import Created


def post(body: CreateItem) -> Created[JSONResponse[Item]]:
    item = Item(id="1", name=body.name)
    res = JSONResponse(item, status_code=201, headers={"Location": f"/items/{item.id}"})
    res.set_cookie("recent_item", item.id)
    return res
```

## Documenting errors with `Status` and `chano.status`

`Status(status_code, description=...)` with `status_code >= 400`—and the corresponding `chano.status` aliases like `Unauthorized[T]`, `Forbidden[T]`, `NotFound[T]`, and `Conflict[T]`—are additive error metadata on return types, `Depends(...)` callables, and folder-scoped `dependencies.py` checks. They never replace the `< 400` return status (`NotFound[Item]` means `200 Item` + `404`; `Created[NotFound[Item]]` means `201 Item` + `404`), and only `>= 400` statuses propagate from dependencies. When a route has an active `Representation`, OpenAPI renders each declared error status with `representation.media_type(status)` and `representation.body_type(status, None)`:

```python
from typing import Annotated
from chano import Depends, HTTPException, Header, Status
from chano.status import Created, NotFound

type Authed[T] = Annotated[T, Status(401, description="Missing or invalid token")]


def require_user(authorization: Annotated[str, Header()]) -> Authed[str]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid token")
    return authorization.removeprefix("Bearer ")


def get(
    item_id: str,
    user: Annotated[str, Depends(require_user)],
) -> NotFound[Item]:
    raise HTTPException(404, "Item not found")


def post(body: CreateItem) -> Created[NotFound[Item]]:
    return Item(id="1", name=body.name)
```

## Per-handler overrides with `Representation` and `Raw`

Folder-scoped `representation.py` sets the default representation for a subtree. To override it for a single handler—or opt out back to raw JSON—put a `Representation` instance or `Raw` in the return annotation:

```python
from typing import Annotated
from chano import JSONRepresentation, Raw


def webhook(payload: dict[str, object]) -> Annotated[dict[str, bool], Raw]:
    return {"ok": True}


def custom(payload: dict[str, object]) -> Annotated[dict[str, bool], JSONRepresentation()]:
    return {"ok": True}
```

## Decorator sugar: `@status_code`, `@raises`, `@representation`

For simple endpoints and unannotated `dependencies.py` checks, `@status_code`, `@raises`, and `@representation` provide concise decorator sugar. Under the hood they wrap the function's return annotation in `Annotated[...]`, so both styles share one implementation:

```python
from chano import JSONRepresentation, raises, representation, status_code


@representation(JSONRepresentation())
@status_code(201)
@raises(409, "Item already exists")
def post(body: CreateItem) -> Item:
    return Item(id="1", name=body.name)
```

Use `@openapi(...)` alongside them for operation-level documentation (`summary`, `description`, `tags`, `deprecated`, `response_description`, `include_in_schema`, `operation_id`, `openapi_extra`).
