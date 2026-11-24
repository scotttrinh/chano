# Parameters and Validation

Chano infers each handler parameter's source, coercion rules, and dependencies directly from its Python signature and type annotations.

## Source Inference

[[src/chano/params.py#_field]] classifies each parameter of a handler or dependency into one of seven sources (`path`, `query`, `header`, `cookie`, `body`, `request`, `depends`).

1. [[src/chano/params.py#Depends]] (inside `Annotated`) creates a recursive [[src/chano/params.py#Endpoint]] (`depends`).
2. A [[src/chano/requests.py#Request]] annotation receives the raw request (`request`).
3. Explicit [[src/chano/params.py#Query]], [[src/chano/params.py#Header]], [[src/chano/params.py#Cookie]], or [[src/chano/params.py#Body]] markers inside `Annotated` override inference; `Header()` converts parameter underscores to hyphens unless `alias` is set. Using any marker or `Depends()` as a default value raises `TypeError`.
4. A parameter name matching a route `{segment}` comes from `path`.
5. A model type ([[src/chano/params.py#_is_model]]: `msgspec.Struct`, `dataclass`, `attrs` class, or `pydantic.BaseModel` via [[src/chano/_pydantic.py#is_pydantic_model]]) comes from `body`.
6. Everything else defaults to `default_source` (`query` for HTTP routes and schedules, or `body` for queue consumers via [[src/chano/queues.py#load_queue]]).

[[src/chano/_pydantic.py#pydantic_module]] inspects `sys.modules.get("pydantic")` without importing `pydantic`, so `BaseModel` detection adds no import or runtime dependency when user code has not loaded `pydantic`.

## Body Decoding and Conversion

[[src/chano/params.py#Endpoint#_resolve]] applies distinct decoding strategies depending on parameter source and body field count.

Fields are planned per function: [[src/chano/params.py#Endpoint]] marks a function's body fields `embed` when it has more than one.

- **Single body field**: Decodes and validates raw request bytes directly into the field's annotation via [[src/chano/params.py#_decode_body]] in a single `msgspec.json.decode` pass, or via [[src/chano/_pydantic.py#validate_json]] when [[src/chano/params.py#Field]] holds a cached `pydantic.TypeAdapter`.
- **Multiple body fields**: Decodes the request body once as `dict[str, Any]` and validates each field by alias using `strict=True` in [[src/chano/params.py#_convert]] (`msgspec.convert` or [[src/chano/_pydantic.py#validate_python]]).
- **Path, query, header, and cookie**: Values arrive as strings, so [[src/chano/params.py#_convert]] runs lenient coercion (`strict=False`, e.g. `"7"` -> `int`) through `msgspec.convert` or [[src/chano/_pydantic.py#validate_python]]. Non-list query parameters take the last value when repeated. Cookie fields read [[src/chano/requests.py#Request#cookies]] by `alias` (the parameter name unless set), and OpenAPI documents them as `in: cookie`.

When a field's annotation is or wraps a `pydantic.BaseModel` (checked lazily via [[src/chano/_pydantic.py#contains_pydantic_model]]), [[src/chano/params.py#_field]] caches a `pydantic.TypeAdapter` on [[src/chano/params.py#Field]] via [[src/chano/_pydantic.py#pydantic_adapter]] at [[src/chano/params.py#Endpoint]] construction time rather than per request. Empty bodies still flow through [[src/chano/params.py#_or_default]] so required/default semantics match `msgspec` fields.

## Dependencies and Execution

[[src/chano/params.py#Endpoint#_call]] resolves dependencies recursively, caches results per request, and offloads sync functions to a worker thread.

Dependencies are cached in a per-request dictionary keyed by callable identity ([[src/chano/params.py#_key]]) through [[src/chano/params.py#_depend]], so a shared dependency runs at most once per request. Async handlers and dependencies are awaited directly; synchronous ones run via `asyncio.to_thread`.

## Inherited Checks

An [[src/chano/params.py#Endpoint]] built with `dependencies` (one group of inherited checks per segment, root to leaf, from [[routing#Inherited Dependencies]]) runs them before resolving any handler argument.

[[src/chano/params.py#Endpoint#call]] runs each check through [[src/chano/params.py#_depend]] with the same cache the handler's `Depends` uses, so a handler or nested dependency requesting a check's callable gets its cached result and the check runs once per request. A check that raises stops the request before the handler's arguments resolve; a check whose own inputs fail validation raises `RequestValidationError` right away, so no later check, handler dependency or handler runs. Check results are otherwise discarded. [[src/chano/params.py#Endpoint]] rejects a check for which [[src/chano/params.py#is_generator_callable]] holds (a sync or async generator function, or an instance whose `__call__` is one) with `TypeError`, since calling it would only create a generator. A wrapper (such as `functools.wraps`) can hide one, so [[src/chano/params.py#Endpoint#call]] also fails closed with `TypeError` when a check's result, fresh or from the shared cache, is a generator or async generator; the handler never runs. A `Depends` may still return a generator as an ordinary value.

[[src/chano/params.py#Endpoint#_call]] treats callables the same way wherever they appear: [[src/chano/params.py#_is_async]] sends coroutine functions and instances with `async def __call__` to the event loop, others to a worker thread, and any awaitable result is awaited, so an async check can never be dropped unawaited and let the handler run. [[src/chano/params.py#_key]] keys the cache and traversal by identity, never equality: a bound method by `id` of its instance and function (each `obj.method` access is a new object, yet runs once), anything else by `id`. Distinct but equal instances, such as two equal frozen dataclasses, each run, and unhashable callables work; the `Endpoint` tree keeps each one alive.

Schema traversal reads the same tree without calling it. [[src/chano/params.py#Endpoint#_walk]] visits inherited checks root to leaf, then the handler, each callable once and its `Depends` in place, so [[src/chano/params.py#Endpoint#request_inputs]] lists each path, query, header, cookie or embedded body input (`source`, `alias`, header names lowercased as dispatch reads them) once at its first use as a [[src/chano/params.py#RequestInput]] holding every consumer, plus each whole-body field on its own. An input is required if any consumer requires it, and [[src/chano/openapi.py#_input_schema]] documents the `allOf` of its consumers' distinct schemas (or the one schema), so every constraint the runtime enforces is published. A query input (only query strings switch between one and repeated values) that one consumer reads as a list and another as a single value raises `ValueError`, since no `allOf` of an array and a scalar accepts any request. [[src/chano/params.py#Endpoint#security_schemes]] includes checks' `HTTPBearer`s.

## Security and Declared Responses

Dependencies declare a credential scheme with [[src/chano/security.py#HTTPBearer]], and handlers or dependencies declare documented error responses with [[src/chano/representation.py#Status]] (`status_code >= 400`), [[src/chano/status.py]] aliases, or [[src/chano/representation.py#raises]].

- **Bearer credentials**: an `HTTPBearer` instance is an ordinary dependency (`token: Annotated[str, Depends(bearer)]`). Its `name` must match [[src/chano/security.py#_COMPONENT_NAME]], the OpenAPI component-name pattern, or the constructor raises `ValueError`. [[src/chano/security.py#HTTPBearer#__call__]] takes the raw [[src/chano/requests.py#Request]], so it adds no `authorization` header parameter. It splits `Authorization` on its first space, matches `Bearer` case-insensitively, strips the token, and raises `401` `Not authenticated` with `WWW-Authenticate: Bearer` when the scheme or token is missing.
- **Declared responses**: [[src/chano/representation.py#Status]] with `status_code >= 400` inside a return `Annotated[T, Status(code, description=...)]` (or [[src/chano/status.py]] aliases like `NotFound[Item]`, or `@raises(status, description)`, thin sugar attaching `Status(status, description=description)` via [[src/chano/representation.py#_wrap_return_annotation]]) adds error metadata without replacing the `< 400` branch status or changing runtime dispatch; code still raises [[src/chano/exceptions.py#HTTPException]]. `raises` raises `TypeError` for a non-`int` (or `bool`) status or non-`str` description and `ValueError` outside `400`–`599`. Outer/later declarations repeating a status win, while an explicit description fills in a descriptionless `Status(code)`.
- **Traversal**: [[src/chano/params.py#Endpoint]] parses each callable's return metadata once via [[src/chano/representation.py#parse_return]]. [[src/chano/params.py#Endpoint#security_schemes]] yields every `HTTPBearer` in the dependency tree and inherited checks, and [[src/chano/params.py#Endpoint#declared_responses]] merges `>= 400` `error_statuses` breadth-first (never propagating `< 400` statuses from `Depends` or `dependencies.py`), keeping the first explicit description per status and falling back to [[src/chano/representation.py#default_status_description]]: the handler; then its direct dependencies in parameter order, followed by its inherited checks nearest segment first and in declared order within a segment; then their dependencies. A handler beats its dependencies, and a direct dependency beats a nested one in an earlier parameter. Both read signatures without calling dependencies, so a dependency that raises still permits schema generation.

[[src/chano/openapi.py#_operation]] turns these into OpenAPI ([[build#Static Docs and Routing Config]]).

## Validation Errors

All conversion and decoding failures are collected into a list of `{"loc": [...], "msg": ...}` dictionaries and raised as [[src/chano/exceptions.py#RequestValidationError]] (`422`).

[[src/chano/params.py#_error]] parses `msgspec`'s `- at $.path` error suffix and appends object keys and array indices to `loc`, giving field-level error locations even for nested `Struct` constraints. For Pydantic-backed fields, [[src/chano/_pydantic.py#_format_errors]] maps each `pydantic.ValidationError.errors()` entry (including malformed JSON) into `{"loc": [*loc, *err["loc"]], "msg": err["msg"]}`.

## Request Cookies

[[src/chano/requests.py#Request#cookies]] parses every `cookie` request header into a `dict[str, str]`, which `Cookie()` parameters read from.

Each `;`-separated pair splits on its first `=`, so values may contain `=`; names and values are stripped, and pairs without `=` or a name are skipped. [[src/chano/requests.py#_unquote]] reverses `http.cookies` quoting (surrounding quotes, backslash and `\ooo` octal escapes), so values written by [[src/chano/responses.py#Response#set_cookie]] round-trip. When a name repeats the first value wins, since browsers send the most specific path first.

## Platform Metadata

[[src/chano/requests.py#Request]] exposes Vercel's per-request metadata as `id`, `ip` and `geo`, each with a local fallback so handlers run unchanged in dev and tests.

- [[src/chano/requests.py#Request#id]] is `x-vercel-id`, else a UUID4 hex string generated when the `Request` is constructed, so repeated and concurrent reads from several threads agree.
- [[src/chano/requests.py#Request#ip]] is `x-real-ip`, else the first trimmed `x-forwarded-for` entry, else the host of `scope["client"]`, or `None` without a client.

[[src/chano/requests.py#Request#_first_header]] reads `x-vercel-id`, `x-real-ip` and `x-forwarded-for` in `scope["headers"]` order. `Request.headers` keeps the last of repeated field lines, which would pick a proxy-appended `x-forwarded-for` line over the client's.
- [[src/chano/requests.py#Request#geo]] returns a frozen [[src/chano/requests.py#Geo]] from the `x-vercel-ip-*` headers; fields are `None` when their header is absent.

`Geo.region` reads `x-vercel-ip-country-region`, Vercel's ISO 3166-2 region code (`countryRegion` in `@vercel/functions`); there is no `x-vercel-ip-region` header, and the edge region lives in `x-vercel-id`. Vercel percent-encodes non-ASCII city names (RFC 3986), so `city` is decoded with `urllib.parse.unquote`.

Vercel sets these headers at the edge, but outside Vercel they come straight from the client, so they must not be trusted for authorization.

## Lazy Values

[[src/chano/params.py#lazy]] turns a zero-argument `factory` into a callable that creates a process-wide value (a client or pool) on first call, in place of startup hooks.

- **Sync factories**: creation runs under a `threading.Lock` with a double check against a private sentinel, so concurrent first calls run `factory` once and `None` or falsy values are cached too.
- **Async factories**: [[src/chano/params.py#_is_async]] treats coroutine functions and instances with `async def __call__` as async. The callable is then a coroutine function, so [[src/chano/params.py#Endpoint#_call]] awaits it.
- **Shared creation**: the first call starts a task on its own loop that calls and awaits `factory()`, and publishes the outcome to a `concurrent.futures.Future`. Every caller awaits that through [[src/chano/params.py#_await_shared]], which bridges the shared future onto the caller's own `asyncio.Future` via `loop.call_soon_threadsafe`, so callers on other threads' event loops share the creation and cancelling one caller leaves it running for the others. Once settled, calls return the stored value directly.
- **Task lifetime**: the shared state holds the creation task alongside its future until it settles, since event loops only keep weak references to tasks and the initiating caller may be cancelled and garbage-collected.
- **Abandoned waiters**: because each caller awaits its own `asyncio.Future` rather than `asyncio.shield(asyncio.wrap_future(...))`, a cancelled caller's waiter is already done when the shared future settles, so [[src/chano/params.py#_await_shared]] skips forwarding a later failure and asyncio never logs an unretrieved or shielded-future exception while active waiters still receive the error.
- **Failures**: if `factory` raises, even synchronously from a function marked with `inspect.markcoroutinefunction`, nothing is cached. Callers sharing that attempt get the error, and the next call tries again. If the creating task is cancelled (for example its `asyncio.run` loop ends first), waiters get `CancelledError` and the next call retries.
- **As a dependency**: [[src/chano/params.py#_named]] copies the factory's name and docstring but not `__wrapped__`, so `inspect.signature` reports no parameters and `Depends(lazy(factory))` adds no request inputs. Its per-request cache key is the `lazy` callable itself.
