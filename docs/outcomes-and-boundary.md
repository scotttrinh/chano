# Outcomes and Boundary: Spec and Plan

> **Status:** Agreed direction, not yet implemented. This replaces the `representation` and
> `representation-metadata` changes, which stay unmerged as reference. Work starts from `main`.
> Chano is unreleased, so this is a hard break: no compatibility shims, deprecation paths, or
> detection of the API it removes.
> As each task ships, its current-state description moves into `lat.md/`.

## Goal

Make a handler's signature the whole contract for what it sends, in a form the type checker can
verify, and do it with less framework than the stack it replaces.

The rule that ties the design together: **every step of a request is a typed function, and a
`Response` returned or raised from any step ends the request.** Handlers, dependencies, guards,
error recovery and wrappers are positions in one pipeline.

Three things follow from that rule:

1. **Outcomes.** The status belongs to the class of the returned value (`Created(item)`), and
   errors are members of the return union (`-> Item | ItemNotFound`).
2. **Checked `Depends`.** A dependency declares its failures in its own return type, and the
   parameter that receives it is checked against it.
3. **`boundary.py`.** One typed object per folder replaces `middleware.py`, `error.py` and
   `dependencies.py`.

There is no `Representation` concept. An envelope or error format is a subclass whose constructor
builds the wire body (§1.5).

## Decisions to confirm

These were recommended in discussion but not explicitly settled. The spec assumes them; changing
one is a local edit.

| # | Decision | Assumed here | Alternative |
| --- | --- | --- | --- |
| D1 | Form of `Depends` | Default value only: `user: User = Depends(current_user)`. Statically checked. Reverses the earlier move to `Annotated`-only for `Depends`. | Keep `Annotated[User, Depends(...)]` and compare the two types when the route loads. |
| D2 | Keyword names on `Boundary` | `guards`, `recover`, `wrap` | Any other names; nothing else depends on them. |
| D3 | Raised failures the signature does not declare | Sent as-is in every environment. | Fail loudly in `chano dev` and tests. |
| D4 | Minimum Python | 3.13 (needed for default type arguments, `class NotFound[T = ErrorDetail]`) | Stay on 3.12 and require `NotFound[ErrorDetail]` in full. |

## What was checked before writing this

A stub version of §1 to §3 was run through `ty` (0.0.104, `--python-version 3.13`):

- Every valid handler, dependency, guard, recover function and wrapper in this document type-checks.
- These mistakes are rejected: returning an undeclared failure, returning the wrong status class,
  pairing a body with the wrong status branch, a bare value where `Created[Item]` is declared, a
  raw `Created(item)` where an envelope subclass is declared, a `Depends` parameter annotated with
  the wrong type, and a wrongly shaped function in `Boundary(...)`.
- A class that is both a `chano.Response` and an `Exception` can be raised and caught on Python
  3.13.9 and 3.15.0b2.

Not checked: the OpenAPI generation, the recover matching rule in §3.3, and behaviour under
`mypy` or `pyright`.

---

## 1. Outcomes

### 1.1 Types

`Outcome[T]` is a `Response` whose status is fixed by its class and whose payload is typed.

```python
class Outcome[T](Response):
    status: ClassVar[int]              # set by each subclass
    media_type = "application/json"
    content: T                         # the unrendered payload

    def __init__(self, content: T, *, headers: dict[str, str] | None = None) -> None: ...


class Created[T](Outcome[T]):
    status = 201


class NoContent(Outcome[None]):
    status = 204
    def __init__(self, *, headers: dict[str, str] | None = None) -> None: ...


class Failure[T = ErrorDetail](Outcome[T], Exception):
    """A 4xx or 5xx outcome. Return it (checked) or raise it (from deep code)."""
    @overload
    def __init__(self: Failure[ErrorDetail], content: str, *, headers=None) -> None: ...
    @overload
    def __init__(self, content: T, *, headers=None) -> None: ...


class NotFound[T = ErrorDetail](Failure[T]):
    status = 404
```

- **Rendering.** `content` is JSON-encoded with `msgspec` and the existing Pydantic hook. `None`
  and bodyless statuses (`1xx`, `204`, `304`) send no body. `Response.body` stays the rendered
  bytes, and `headers`, `set_cookie` and `delete_cookie` work as on any `Response`.
- **Instantiating a class with no `status`** (`Outcome`, `Failure`) raises `TypeError`.
- **`ErrorDetail`** is `msgspec.Struct` with `detail: str`. `NotFound("gone")` sends
  `{"detail": "gone"}`, the same bytes `HTTPException(404, "gone")` sends today.
- **Where they live.** `Outcome`, `Failure` and `ErrorDetail` are exported from `chano`. The named
  statuses are in `chano.status`: `OK`, `Created`, `Accepted`, `NoContent`, `SeeOther`,
  `NotModified`, and the failures `BadRequest`, `Unauthorized`, `Forbidden`, `NotFound`,
  `MethodNotAllowed`, `Conflict`, `Gone`, `UnprocessableContent`, `TooManyRequests`,
  `InternalServerError`, `ServiceUnavailable`. Others are one-line subclasses a user can write.

### 1.2 Handlers

```python
class ItemNotFound(NotFound[Problem]):
    """No item has this ID."""


def GET(item_id: str) -> Item | ItemNotFound:
    item = lookup(item_id)
    if item is None:
        return ItemNotFound(Problem(title="No such item"))
    return item


def POST(item: Item) -> Created[Item]:
    return Created(item, headers={"location": f"/items/{item.id}"})


def DELETE(item_id: str) -> NoContent | NotFound:
    if lookup(item_id) is None:
        raise NotFound("gone")
    return NoContent()
```

- **A plain value is a 200**, JSON-encoded, as today. An unannotated handler works as today.
- **A returned failure and a raised failure are the same thing** to everything downstream
  (§3.3, wrappers, queue retries).
- **Other `Response` classes** (`JSONResponse`, `HTMLResponse`, `PlainTextResponse`,
  `RedirectResponse`, `StreamingResponse`, `SSEResponse`) are unchanged. They are the untyped
  escape hatch: sent as-is and not described in OpenAPI.

### 1.3 Framework failures

The framework's own errors become ordinary failures, so user code and recover functions treat
them like any other.

| Today | Becomes |
| --- | --- |
| `HTTPException` | Removed. Use a `Failure` subclass. |
| `RequestValidationError(errors)` | `RequestValidationError(UnprocessableContent[ValidationErrorDetail])`, keeping `.errors`. Same wire bytes. |
| `HTTPException(404, "Not Found")` for an unmatched path | `NotFound("Not Found")` |
| `HTTPException(405, ...)` with `allow` | `MethodNotAllowed("Method Not Allowed", headers={"allow": ...})` |
| Any other exception | Unchanged: traceback to `stderr`, plain-text `500`. |
| `HTTPBearer.__call__` raising 401 | Returns `str \| Unauthorized`, so its 401 is documented without a decorator. |

### 1.4 OpenAPI

Responses are read from return annotations. `@raises` and `Endpoint.declared_responses` go away.

- **Handler return type.** Each member of the union is one of:
  - an `Outcome` class: documented at its `status`, with its `media_type` and the schema of its
    resolved `content` type;
  - any other `Response` class: no schema, as today;
  - anything else: part of the `200` body. Several such members form one `anyOf` schema.
- **Resolving the content type.** `ApiCreated[Item]` resolves to `Document[Item]` by following
  the generic base classes (§1.5). PEP 695 aliases of unions are expanded. Non-Chano `Annotated`
  metadata (such as `msgspec.Meta`) is kept.
- **Dependencies and guards.** Every `Outcome` member in the return type of a dependency, nested
  dependency or guard is added to the operation, in the order `declared_responses` uses today
  (handler, direct dependencies, guards nearest first, then nested).
- **Same status declared twice** with different schemas and the same media type: `anyOf`.
- **Descriptions.** The class's own docstring if it has one, else the HTTP reason phrase.
  `@openapi(response_description=...)` still sets the `200` description.
- **422.** Added whenever the endpoint has request inputs, from `RequestValidationError`'s
  content type, unless a recover function replaces it (§3.3).

### 1.5 Envelopes and error formats

A format is a set of subclasses. Nothing in the framework knows about it.

```python
class Document[T](msgspec.Struct):
    data: T


class ApiCreated[T](Created[Document[T]]):
    def __init__(self, data: T, *, headers=None) -> None:
        super().__init__(Document(data), headers=headers)


class ApiNotFound(NotFound[Problem]):
    media_type = "application/problem+json"
    def __init__(self, title: str) -> None:
        super().__init__(Problem(title=title, status=self.status))
```

`-> ApiCreated[Item]` documents and sends `{"data": {...}}`, and the checker rejects a raw
`Created(item)` there. What is given up, on purpose: a folder can no longer envelope every route
beneath it from one file. Each handler opts in through its return type.

---

## 2. Dependencies

### 2.1 Declaring failures

```python
def current_user(token: str = Depends(bearer)) -> User | Unauthorized:
    return User(name="scott") if token == "secret" else Unauthorized("Not authenticated")


def GET(item_id: str, user: User = Depends(current_user)) -> Item | ItemNotFound: ...
```

- **Short-circuit.** If a dependency returns a `Response` (any `Response`, so a guard can return
  `SeeOther("/login")`), that is the response. No later dependency or the handler runs.
- **Injection.** Otherwise the value is injected. Per-request caching by callable identity,
  sync-in-thread execution and `lazy()` are unchanged.

### 2.2 The checked form (D1)

```python
@overload
def Depends[T](fn: Callable[..., Awaitable[T | Response]]) -> T: ...
@overload
def Depends[T](fn: Callable[..., T | Response]) -> T: ...
```

- `Depends(current_user)` has static type `User`, with the `Unauthorized` branch removed, for
  sync and async callables and for instances with `__call__`.
- `user: str = Depends(current_user)` is a type error.
- This is the only form. `Annotated[User, Depends(...)]` is not recognised and gets no
  dedicated error.
- `Query`, `Header`, `Cookie` and `Body` stay `Annotated`-only. They carry no type to check.
- **Consequence:** Python requires parameters with defaults to come last, so dependencies follow
  path and body parameters, or the signature uses `*`.

---

## 3. `boundary.py`

### 3.1 The file

A `boundary.py` in any folder under `app/` exports `boundary`, applying to every function at or
below that folder. Route groups count, so `app/(admin)/boundary.py` covers the group without
changing URLs.

```python
# app/api/boundary.py
from chano import Boundary

boundary = Boundary(
    guards=[require_admin],
    recover=[invalid, missing],
    wrap=[request_id],
)
```

```python
type Guard = Callable[..., object]
type Recover = Callable[[Request, Any], Response | Awaitable[Response]]
type Wrap = Callable[[Request, CallNext], Awaitable[Response]]
```

- The constructor's types reject a wrongly shaped entry in the editor. The loader keeps one
  runtime check: `boundary` must be a `Boundary`, else `TypeError` naming the file.
- **Hard break.** `middleware.py`, `error.py` and `dependencies.py` stop being special files.
  Nothing looks for them or warns about them.
- Build trace roots use `boundary.py` in place of the three old names.

### 3.2 Guards

A guard is a dependency whose value is discarded: `def require_admin(user: User =
Depends(current_user)) -> None | Forbidden`. Behaviour is today's `dependencies.py`: root to
leaf, before any handler argument resolves, sharing the per-request cache, generator callables
rejected. A guard that returns a `Response` ends the request (§2.1).

### 3.3 Recover

A recover function turns an error into a response. It is chosen by the annotation of its second
parameter, like an `except` clause.

```python
def invalid(request: Request, error: RequestValidationError) -> ApiUnprocessable: ...
def missing(request: Request, error: LookupError) -> ApiNotFound: ...
```

- **What reaches it:** any exception from a guard, dependency or handler, any `Failure` they
  return, and the method-mismatch `405`.
- **Matching.** An annotation `C` matches `isinstance(error, C)`. An annotation `C[B]`, where `C`
  is an `Outcome` class and `B` is a class, also requires `isinstance(error.content, B)`, so
  `Failure[ErrorDetail]` selects default-format failures and leaves `ApiNotFound` alone.
- **Order.** Nearest folder first, then list order. The first match runs.
- **Result.** A returned `Response` is final. If the function raises, the raised exception
  continues to the remaining candidates outward. Past the root, a `Failure` is sent as-is and
  anything else gets the default `500`.
- **Sync or async**, as `handle_error` is today.
- **OpenAPI.** When both the parameter annotation and the return annotation are `Outcome`
  classes with a fixed status, a documented response matching the parameter is replaced by the
  return type. That covers `RequestValidationError -> ApiUnprocessable`.
- **Limits.** A function that keeps the status and changes only the body (one reformatter for
  every failure) works at runtime by returning a `JSONResponse`, but is not reflected in
  OpenAPI. A mapper from a plain exception (`LookupError`) adds nothing to OpenAPI, because
  nothing says which routes raise it. The explicit route to exact docs is returning the
  format's own failure classes.

### 3.4 Wrap

`async def request_id(request: Request, call_next: CallNext) -> Response`: today's middleware,
root outermost. `call_next` always returns a `Response`, failures included, so a wrapper can
`match` on outcome classes. Wrappers run for HTTP routes only, after a route matches, as
middleware does today. They are opaque to OpenAPI.

### 3.5 Order

Wrappers (root outermost), then guards (root to leaf), then the handler's dependencies and the
handler. An error goes through recover, and the result flows back out through the wrappers. An
error raised by a wrapper skips recover and gets the default mapping, as today.

### 3.6 Queues and schedules

`queue.py` and `schedule.py` inherit `guards` and `recover` from ancestor boundaries and never
`wrap`, matching what they inherit today. Queue acknowledgement is unchanged in meaning: a
returned or raised `Failure`, or any exception, triggers a retry unless a recover function
returns a response below `400`.

---

## 4. What is removed

Relative to `main`:

- `HTTPException`, `@raises`, `Endpoint.declared_responses` and the `__chano_raises__` attribute.
- The `Annotated` form of `Depends`.
- `middleware.py`, `error.py`, `dependencies.py`, their three loaders and their per-file
  validation, and the nested `_error_boundary` chain.

Relative to the replaced stack, none of this is built: `Representation`, `JSONRepresentation`,
`StatusRepresentation`, `Raw`, `Status`, `@status_code`, `@representation`, the `Annotated`
aliases in `chano.status`, `representation.py`, generic `Response[T]`, and the
annotation-rewriting code. That stack is about 1,570 changed lines under `src/` and 4,770 in
total over `main`.

**Size target.** The goal is less framework than the stack, and close to neutral against
`main`. Measure with `jj diff --from main --stat -- src`. An estimate, not a measurement: `src/`
lands between 100 lines smaller and 250 lines larger than `main`. If a task pushes well past
that, stop and report before continuing.

## 5. Out of scope

- Typed declarations for function settings and triggers (`max_duration`, `topic`, `schedule`),
  and a typed `Topic[T]` for producers.
- Folder-wide function defaults in `boundary.py`.
- OpenAPI for status-preserving reformatters (§3.3 limits).
- A typed alternative to `Query`/`Header`/`Cookie` markers.

---

## Plan

Two tasks, each one reviewable change on top of `main`, in order. Each task updates its own
documentation; neither leaves docs describing removed API.

### Task 1: Outcomes and checked `Depends` (§1, §2)

- **Code.**
  - Raise the floor to 3.13: `requires-python`, ruff `target-version`, the CI matrix.
  - Add `Outcome`, `Failure`, `ErrorDetail` and `chano.status`.
  - Replace `HTTPException` everywhere in `src/`, and make `RequestValidationError` a failure.
  - Read responses from return annotations in OpenAPI; delete `@raises` and `declared_responses`.
  - Short-circuit on a `Response` returned by a dependency or inherited check.
  - Switch `Depends` to the default-value form.
- **Left alone until task 2.** The three segment files keep working. `handle_error` now receives
  failures as exceptions, and `dependencies.py` checks may return a `Response`.
- **Tests.**
  - Rewrite existing tests that use `HTTPException`, `@raises` or `Annotated[..., Depends]`.
  - Status, body, headers and cookies for returned and raised outcomes, including bodyless ones.
  - A dependency returning a failure, a redirect, and a value; nested; cached once.
  - OpenAPI: union branches, envelope subclass schema, dependency failures, same-status
    `anyOf`, docstring descriptions, `HTTPBearer`'s 401 with no decorator.
  - Queue retry on a returned and on a raised failure.
  - **Typing fixtures.** `tests/typing/ok.py` must pass `ty check`. `tests/typing/bad.py` holds
    the rejected mistakes listed above, one per marked line, and a test asserts `ty` reports
    exactly those lines.
- **Docs.**
  - `lat.md/`: `params.md`, `responses.md`, `routing.md` (error mapping), `build.md` (OpenAPI),
    `vercel-primitives.md` (queue retries), `overview.md`, and `tests.md` entries with matching
    `@lat:` references.
  - `site/content/`: guides and reference pages that show `HTTPException`, `@raises` or
    `Depends`, plus a new "Outcomes" guide.
  - `README.md`, `docs/guide.md`, `examples/`, and a superseded note on §6.7 of
    `docs/serverless-spec.md`.

### Task 2: `boundary.py` (§3)

- **Code.**
  - Add `Boundary` and its loader; compose guards, recover and wrap per §3.5.
  - Replace the three loaders in `routing.py`, their use in `queues.py` and `schedules.py`, and
    the trace roots in `build.py`.
  - Implement recover matching and its OpenAPI replacement.
- **Tests.**
  - Port the middleware, error-boundary and inherited-check tests to `boundary.py`.
  - Recover: match by class, match by content type, nearest first, list order, raise to pass
    outward, sync and async, returned failures reach it, wrapper errors skip it.
  - OpenAPI: a recover function replaces the 422 schema.
  - Queue and schedule inheritance without `wrap`.
  - Extend the typing fixtures with `Boundary(...)` shapes.
- **Docs.**
  - `lat.md/routing.md` (Middleware, Error Boundaries, Inherited Dependencies become one
    Boundary section), `params.md` (Inherited Checks), `vercel-primitives.md`, `build.md`,
    `tests.md`.
  - `site/content/`: application structure guide and routing reference.
  - `README.md`, `docs/guide.md`, `examples/`, and a superseded note on §7 of
    `docs/serverless-spec.md`.

### Done means, for each task

```sh
uv run pytest
uv run ty check
uv run ruff check .
uv run ruff format --check src tests examples site/build.py
lat check
jj diff --from main --stat -- src
```

All pass, the last one is within the size target, and a search of `src/`, `site/content/`,
`lat.md/`, `README.md`, `docs/guide.md` and `examples/` finds no remaining use of the API that
task removed.
