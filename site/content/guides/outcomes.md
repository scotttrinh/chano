---
title: Outcomes and typed errors
description: Declare HTTP statuses, structured error payloads, envelopes, and checked dependencies directly in Python return annotations.
lede: Put status codes and errors in your handler and dependency return types so static type checkers verify every branch and OpenAPI stays in sync automatically.
section: guides
order: 4
nav_title: Outcomes
---

## Why outcomes

In Chano, every step of a request is a typed function, and a `Response` returned or raised from any step ends the request. Rather than pairing a runtime `raise` with a separate documentation decorator, you declare status codes and errors directly in the return annotation using `Outcome`, `Failure`, and the status classes in `chano.status`.

```python
import msgspec

from chano.status import Created, NoContent, NotFound


class Item(msgspec.Struct):
    id: int
    name: str


class Problem(msgspec.Struct):
    title: str


class ItemNotFound(NotFound[Problem]):
    """No item has this ID."""


def GET(item_id: int) -> Item | ItemNotFound:
    item = lookup(item_id)
    if item is None:
        return ItemNotFound(Problem(title="No such item"))
    return item


def POST(item: Item) -> Created[Item]:
    return Created(item, headers={"location": f"/items/{item.id}"})


def DELETE(item_id: int) -> NoContent | NotFound:
    if lookup(item_id) is None:
        raise NotFound("gone")
    return NoContent()
```

- A plain return value (`Item`) is sent as `200 OK` JSON.
- `Created(item)` sends `201 Created` JSON and documents `201` in OpenAPI without inventing a `200`.
- `NoContent()` sends `204 No Content` with no body, no `content-type`, and no `content-length`.
- `NotFound("gone")` wraps the string in `ErrorDetail(detail="gone")` and sends `{"detail": "gone"}` with status `404`.
- A `Failure` is both a `Response` and an `Exception`: return it for static checking against the return union, or raise it from deep helper code. Both flow identically through `error.py` boundaries, middleware, and queue retries.

## Named statuses in `chano.status`

`Outcome`, `Failure`, and `ErrorDetail` are exported from `chano`. Named status classes live in `chano.status`:

- **Success and redirects**: `OK` (`200`), `Created` (`201`), `Accepted` (`202`), `NoContent` (`204`), `SeeOther` (`303`), `NotModified` (`304`).
- **Failures**: `BadRequest` (`400`), `Unauthorized` (`401`), `Forbidden` (`403`), `NotFound` (`404`), `MethodNotAllowed` (`405`), `Conflict` (`409`), `Gone` (`410`), `UnprocessableContent` (`422`), `TooManyRequests` (`429`), `InternalServerError` (`500`), `ServiceUnavailable` (`503`).

Any other status code is a one-line subclass:

```python
from chano import Failure, Outcome


class Teapot(Failure):
    status = 418
```

## Envelopes and custom error formats

An API envelope or RFC 9457 Problem Details format is an ordinary `Outcome` or `Failure` subclass whose constructor builds the wire payload:

```python
import msgspec

from chano.status import Created, NotFound


class Document[T](msgspec.Struct):
    data: T


class Problem(msgspec.Struct):
    title: str
    status: int


class ApiCreated[T](Created[Document[T]]):
    def __init__(self, data: T, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(Document(data), headers=headers)


class ApiNotFound(NotFound[Problem]):
    """Resource not found."""

    media_type = "application/problem+json"

    def __init__(self, title: str) -> None:
        super().__init__(Problem(title=title, status=self.status))
```

When a handler is annotated `-> ApiCreated[Item] | ApiNotFound`, OpenAPI follows the generic base classes to document `201` with `Document[Item]` and `404` with `application/problem+json` and `Problem`—and your type checker rejects returning a raw `Created(item)` there.

## Checked `Depends` and short-circuiting

Dependencies declare their failures or redirects in their own return union, and parameters receive them using default-value `Depends(...)`:

```python
from chano import Depends, HTTPBearer
from chano.status import SeeOther, Unauthorized

bearer = HTTPBearer()


def current_user(token: str = Depends(bearer)) -> User | Unauthorized:
    if token != "secret":
        return Unauthorized("Not authenticated")
    return User(name="alice")


def GET(item_id: int, user: User = Depends(current_user)) -> Item | ItemNotFound:
    ...
```

- `Depends(current_user)` strips `Response` branches from `current_user`'s return type, so `user` has static type `User` and annotating `user: str = Depends(current_user)` is a type error.
- If any dependency or `dependencies.py` check returns a `Response` (such as `Unauthorized(...)` or `SeeOther("/login")`), Chano short-circuits immediately and sends that response without running later dependencies or the handler.
- OpenAPI reads `Outcome` branches from the handler, its dependencies, nested dependencies, and inherited checks automatically—including `HTTPBearer`'s `401`—with no extra decorators.
