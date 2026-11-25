---
lat:
  require-code-mention: true
---
# Tests

Test specifications for Chano, anchored to [[tests]].

## Routes and parameters

Verify basic routing, query/path parameter binding, `HEAD` handling, `405` `Allow` headers, and `404` responses.

## Validation errors

Verify `422` `RequestValidationError` responses and `loc` paths for invalid path types, missing body fields, `msgspec.Meta` constraints, malformed JSON, and missing required bodies.

## Body and dependencies

Verify JSON body decoding alongside `Depends` and `HTTPBearer` resolution, a missing credential's `401` with `WWW-Authenticate: Bearer`, and a wrong token's `Unauthorized` `401`.

## Dev docs and static files

Verify `public/` static file serving, raw-scope path traversal and trailing-slash rejection (`404`), `/docs` Swagger UI, and `/openapi.json` schema generation including summaries, parameters, `$ref` schemas, and `Meta` constraints.

It also checks the hello `PUT`'s bearer `security` and declared `401`, with no `authorization` header parameter and no `security` on the public `GET`.

## Project config

Verify [[src/chano/config.py#load_config]] and [[src/chano/config.py#normalize_base_path]] parsing, normalization, and validation errors.

### Defaults and keys

Verify default [[src/chano/config.py#ChanoConfig]] values, explicit `[tool.chano]` keys, NUL and `.func` rejection, and `.funcs` acceptance.

### Base path normalization

Verify [[src/chano/config.py#ChanoConfig]] normalizes empty, root, trailing-slash, nested, and punctuation-bearing `base_path` values.

### Invalid tool.chano table

Verify unknown keys, wrong types, control characters, `%`, quotes, `\`, `<>`, `$`, non-ASCII, and `.func` segments raise `ValueError`.

### Invalid tool table

Verify non-table `tool` and `tool.chano` values raise `ValueError` referencing `[tool.chano]`.

## Prefixed dev routing

Verify a `base_path = "/api/"` project served by `create_app` picks up the config: prefixed routes, static files, docs and `openapi.json` paths, and JSON 404s outside the prefix.

Uses `isolated_imports`. Also checks raw-scope traversal rejection (httpx would normalize `..`), that `/api/` misses the root route, and that an explicit `base_path=""` overrides the config.

## Root path reconstruction

Verify [[src/chano/requests.py#Request#path]] joins `root_path + path` with the prefix-root exception and no `startswith` check, and that routing matches the reconstructed path.

### Request path joining

Verify `Request.path` joins `root_path` and `path` across empty, prefixed, trailing-slash, `/apiary`, and missing `root_path` scopes.

### Routing with root_path

Verify `create_app` and `create_route_app` route requests using the reconstructed path and return JSON 404s for unstripped or double-prefixed misses.

## Dev routing priority

Verify a prefixed catch-all route does not shadow `/api/docs.html`, `/api/openapi.json` or `/api/hello.txt` in dev, while literal routes still win: a `POST`-only `app/docs/route.py` makes `GET /api/docs` a `405`.

Raw scopes check that trailing slashes, symlink loops, `..` and NUL bytes fall through to the catch-all instead of serving a file or returning `500`.

## Dev docs precedence

Verify dev follows the build's file precedence: `public/docs` beats the Swagger UI on `/api/docs`, while generated `docs.html` and `openapi.json` beat their `public/` copies.

Non-`GET`/`HEAD` requests to those files get `405` with `Allow: GET, HEAD` before generating the OpenAPI schema or reading static file bytes, instead of reaching a `POST` catch-all, and without `public/docs` the docs path serves Swagger.

## Route load cache

Verify [[src/chano/routing.py#Route#load]] returns one module for a file discovered under two prefixes, and that a failing import leaves `sys.modules` unchanged and raises again on retry.

## Route segments

Verify `Route.segments` lists the directories from `app/` to the route's own directory, root to leaf, for the root route, nested routes, `(group)` directories, and `[id]` and `[...path]` segments, with and without `base_path`.

## Segment module loading

Verify [[src/chano/_primitives.py#load_module]] runs a segment file once when two sibling routes load it, shares the cache with [[src/chano/routing.py#Route#load]], and leaves `sys.modules` clean after a failing import that raises again on retry.

## Ancestor segment loading

Verify [[src/chano/routing.py#Route#load_segments]] loads only the route's own ancestor files, root to leaf, skipping a sibling directory's file and missing files, and that routes sharing an ancestor (including through a route group) reuse its module.

## Middleware

Exercise `boundary.py` `wrap` chains ([[routing#Boundary]]); each request test runs against both [[src/chano/routing.py#create_app]] and [[src/chano/routing.py#create_route_app]] to prove parity.

### Runs outer to inner

Root, `(group)` and leaf wrappers enter outermost first, the handler runs, and they exit in reverse; a sibling outside the group runs only the root's wrapper.

### Short-circuits

A wrapper that returns a response without calling `call_next` skips inner wrappers and the handler; outer wrappers still see its response.

### Sees the error response

A handler's `Unauthorized` 401 (with headers), a query `RequestValidationError` 422, and an unhandled 500 reach every wrapper as a `Response` from `call_next`, which can still edit it.

### Middleware errors get the default mapping

`Failure`, `RequestValidationError` and other exceptions raised by a wrapper propagate past outer wrappers and become JSON 403 with headers, JSON 422, and plain-text 500.

### Skips unmatched paths but runs on 405

A 404 runs no wrapper. A literal route's 405 runs its chain; a path matching two dynamic routes is the first one's 405 through its chain, as the platform's first rewrite would send it, not the later catch-all.

### Skips dev files

In `create_app`, `public_dir` files, `openapi.json`, and a docs 405 are served without running any wrapper.

## Boundary

Exercise `boundary.py` loading, recover matching, conflict checks, and OpenAPI replacement ([[routing#Boundary]]).

### Invalid boundary.py

A `boundary.py` with no `boundary` or a non-`Boundary` value makes both factories raise `TypeError` naming the file.

### Non-response wrap result

A `wrap` function that returns something other than a `Response` becomes a plain-text 500 under both factories.

### Conflicting boundaries for one path

Two route files with the same URL path but different ancestor `boundary.py` chains make `create_app` raise `ValueError` instead of picking one chain.

### Awaitable recover handlers

A recover callable instance with `async def __call__` and a sync wrapper returning a coroutine both handle the error at the nearest boundary without unawaited-coroutine warnings, and a bare `raise` in the awaited body reaches the ancestor.

### Recover matching and order

Verify recover matches by exception class and `Failure[ContentType]`, runs nearest folder first then list order, passes raised exceptions outward, handles returned failures, and skips wrapper errors.

### OpenAPI recover replacement

Verify fixed-status `Outcome` recover functions replace matching documented `Failure` responses (such as `RequestValidationError` -> `ApiUnprocessable`), respect earlier catch-all precedence, and never rewrite non-`Failure` outcomes.

### Local schedule runner ignores boundary

Verify local schedule execution (`run_schedule`) ignores ancestor `boundary.py` (`guards`, `recover`, and `wrap`), returning a returned `Failure` directly and letting raised exceptions propagate.

### HTTP-only boundary scoping for queues and schedules

Verify an ancestor `boundary.py` applies `guards`, `recover`, and `wrap` to `route.py` while `queue.py` and `schedule.py` ignore it at runtime and exclude it from build `trace_roots` and vendored packages.

## Error boundaries

Exercise `boundary.py` `recover` chains ([[routing#Boundary]]) with sync root and leaf handlers and an async `(group)` handler; request tests run against both factories to prove parity.

### Nearest recover wins

The leaf-most recover function (sync for `/admin`, async for `/other`, root for `/plain`) handles a handler error, a 422 and a 405; wrappers see its response, and successful requests skip it.

### Re-raising passes to the next ancestor

A bare `raise` passes the same exception to the next ancestor, a new exception replaces it even through a later bare `raise` (sync or async), and past the root the default mapping renders the last exception.

### Unhandled errors get the default mapping

When every recover function re-raises the result is a plain-text 500 seen by wrappers; a project without `boundary.py` keeps the JSON 401 with headers, 422, and plain-text 500.

### Wrapper errors skip recover

Exceptions raised by wrappers get the default 500, 403 and 422 without running any recover function, and a 404 runs no segment file.

### Non-response result

A recover function returning `None` passes a `TypeError` to the next ancestor; if every handler does, the result is a plain-text 500.

## Route module isolation

Verify `app/route.py` in two projects loads two modules, and that `base_path = "/v1+"` keeps `app/route.py` and `app/!/route.py` apart: modules are keyed by file, not by the function path.

## Symlinked app directory

Verify a project whose `app` symlinks to `src/app` reads `base_path` from the root `pyproject.toml` in `create_app`, `create_route_app` and `plan_build`.

## Build plan

Verify [[src/chano/build.py#plan_build]] on a temporary project without writing output: paths, functions, entrypoints, `.vc-config.json` overrides, generated docs, and `config.json` routes.

The `isolated_imports` fixture restores `sys.path`, disables bytecode writes, and evicts non-installed modules added to `sys.modules` while keeping installed environment modules (stdlib, site-packages, built-in, and frozen).

## Build emit

Verify [[src/chano/build.py#emit_build]] (with vendoring stubbed) preserves `copytree` semantics for `public/` (symlinks, modes, late files/dirs) and clones route config safely.

Checks mutable `regions` isolation, mixed dict keys, and values whose `__deepcopy__` raises.

## Build output layout

Verify `.vercel/output` `config.json` rewrites, static docs/assets, per-route `.func` directories, `.vc-config.json` overrides, and `_vendor` contents (`chano`, `msgspec`, `vercel`, and no `uvicorn`).

## Built function invocation

Verify built `.func` directories execute standalone via `vc_handler` with `python -S` using only vendored dependencies, handle their own route, and return `404` for other routes and `/docs`.

## Prefixed build output

Verify a real build of a `base_path = "/api/"` project: prefixed `config.json` rewrites, `static/api/` files and docs, `functions/api.func`-style paths, and standalone invocation under the prefix.

Dependencies are vendored from `examples/hello`'s lock, since the temporary project has none. The module-scoped `prefixed_output` fixture restores `sys.path`, `sys.modules` and bytecode writes after building.

## Prefixed build without public

Verify a prefixed build with no `public/` writes only `static/api/` docs, and that a `public/` created while route modules import is still copied under `static/api/`.

## Custom build output directory

Verify [[src/chano/build.py#plan_build]], [[src/chano/build.py#build]], and [[src/chano/cli.py#main]] (`--output`) handle custom output paths and reject unsafe overlapping directories.

### Custom output directory

Verify builds to paths outside or inside `project_dir` do not create `.vercel/output` and never leak output or `.chano-*` staging directories into `.func` payloads.

### Rejects overlapping output directory

Verify [[src/chano/build.py#plan_build]] rejects output directories containing the project or overlapping `app/` or `public/` with `ValueError` before deleting files.

## Locked lockfile

Verify [[src/chano/build.py#build]] fails with `RuntimeError` when `uv.lock` is stale relative to `pyproject.toml` or missing, without modifying `uv.lock`.

## Deterministic build output

Verify repeated builds of the same project into separate directories produce byte-for-byte identical trees and that `config.json`, `openapi.json`, and `.vc-config.json` serialize with sorted keys.

## Workspace discovery

Verify [[src/chano/build.py#plan_build]] finds the nearest `[tool.uv.workspace]` with `uv.lock`, stops at standalone `uv.lock` boundaries, sets `package` from `[project].name` in workspaces (`None` outside), and rejects unnamed workspace projects.

Covers ancestors missing `uv.lock` or `[tool.uv.workspace]`, standalone projects or ancestors with their own `uv.lock` under a workspace root, nested workspaces where the nearest match wins, a non-virtual workspace root acting as a service, and `ValueError` rejection for unnamed workspace members or virtual roots.

## Workspace build

Verify [[src/chano/build.py#build]] on a uv workspace with `apps/api`, `packages/core`, and an unrelated `apps/worker`/`packages/unused` sibling resolves `workspace_root`, vendors `core` non-editably, omits `api` and `unused`, and serves `core` via `vc_handler`.

## Full-stack example build

Verify [[examples/fullstack]] declares the Vercel Services config and Python 3.13 pin, builds [[examples/fullstack/backend]] as a uv workspace member under `/api`, emits prefixed docs/static/functions without `uvicorn` in `_vendor/`, and invokes each built function standalone.

## Dev requires uvicorn

Verify [[src/chano/cli.py#main]] exits with a `SystemExit` message pointing to `chano[dev]` when `chano dev` cannot import `uvicorn`.

## Store index

Verify [[src/chano/_store.py#normalize_dist_name]] and [[src/chano/_store.py#index_store]] record PEP 503 names, in-store `RECORD` paths, top-level import names, `Requires-Dist` names, and byte sizes.

Covers a synthetic store fixture (including `top_level.txt`, namespace packages, quoted CSV paths, escaped/absolute `RECORD` entries, excluded `*.dist-info`/`*.data`/`__pycache__`/`bin/`, and missing stores) and the real `_vendor/` from `examples/hello`.

## Build size report and limits

Verify [[src/chano/_report.py#SIZE_WARN_BYTES]], [[src/chano/_report.py#SIZE_LIMIT_BYTES]], per-function size and distribution count reporting in [[src/chano/build.py#build]] and [[src/chano/cli.py#main]], warnings above the warn threshold, and `RuntimeError` failure above the limit.

Lowers thresholds via `monkeypatch` to confirm warning messages name the largest distributions in descending size order while letting the build succeed, and that exceeding the fail limit raises `RuntimeError` naming the oversized function and largest distributions.

## Per-function tree-shaking

Verify [[src/chano/build.py#build]] on a multi-package uv workspace bundles only each route's traced dependencies plus `chano`, `vercel-runtime`, and their closures, invokes every built function standalone via `vc_handler`, and honors `include` and `tree_shake = false`.

Covers recursive project helpers with relative imports and cycles, transitive `Requires-Dist`, lazy imports inside handlers, skipped `TYPE_CHECKING` blocks, `importlib.import_module` and `__import__` string literals, route and project `include`, hard-linking across functions, per-function reported distribution counts, and `tree_shake = false` restoring the full store.

## Import tracer and closure

Verify [[src/chano/_trace.py#_trace_function_chains]], [[src/chano/_trace.py#_resolve_closure_with_reasons]], [[src/chano/_trace.py#ALWAYS_INCLUDED]], and [[src/chano/build.py#_read_module_include]] handle tracer, closure, size-report, and validation edge cases against a synthetic store.

Covers `vercel` inclusion with its closure, cyclic `Requires-Dist`, namespace packages in the store and project helpers, package `__init__.py` tracing, `if TYPE_CHECKING:` `else` branches and `if not TYPE_CHECKING:`, aliased/relative `importlib.import_module`, unknown/stdlib ignores, per-function size warnings, and `ValueError` on invalid route `include`.

## Segment files as trace roots

Verify [[src/chano/build.py#plan_build]] adds each route's ancestor `boundary.py` files to its `trace_roots`, so their dependencies are bundled only under that subtree.

A dependency imported only by `app/admin/boundary.py` (directly or through a project helper) lands in every HTTP route function (`route.py`) under `app/admin/`, including route-group and dynamic descendants, but not in sibling routes, `queue.py`, or `schedule.py`; a sibling's own `boundary.py` never enters an admin function's roots. `--explain` names the segment file at the head of each reason chain.

## Build-time import check

Verify [[src/chano/build.py#build]] with `platform=None` fails with a `RuntimeError` naming the function, missing module, and `include` when a route imports a workspace package by computed name, and succeeds once added to route or project `include`.

Also verifies `PYTHONPATH` is ignored by `-I` and no `__pycache__` directories are written into built functions.

## Import check platform matching

Verify [[src/chano/_importcheck.py#_check_function_imports]] skips with a printed message (without invoking `uv python find`) when `plan.platform` does not match the host OS/architecture, and runs when it matches.

## Import check target interpreter

Verify [[src/chano/_importcheck.py#_check_function_imports]] executes the interpreter returned by `uv python find <python>` with `-I` instead of `sys.executable`, and skips with a printed message when `uv python find` finds none.

## Build explain

Verify `chano build --explain <function>` (via [[src/chano/cli.py#main]] and [[src/chano/build.py#build]]) prints reason chains for included distributions and lists excluded ones.

Covers direct and helper-file traced imports, multi-hop transitive `Requires-Dist` chains, always-included packages and their dependencies, route and project `include`, excluded distributions omitted from `_vendor/`, `.func` suffix normalization, `tree_shake = false`, and `ValueError` on unknown function names without modifying an existing output directory.

## Streaming responses

Verify [[src/chano/responses.py#StreamingResponse]] and [[src/chano/responses.py#SSEResponse]] streaming chunk order and `HEAD` handling.

### Streams chunks in order

Verify async-generator, sync-generator, and list chunks stream in order (`str` as UTF-8) with `more_body: True`, a final empty body, and no `content-length`.

### HEAD skips producer

Verify `HEAD` on `StreamingResponse` and `SSEResponse` sends `200` headers and an empty final body without stepping a generator or calling `__iter__`/`__aiter__`.

## Streaming disconnect

Verify an `http.disconnect` from `receive` during an endless stream cancels the producer and closes both async and sync generators (their `finally` runs), with chunks still sent in order and no final `more_body: False` message.

## SSE framing

Verify [[src/chano/responses.py#SSEResponse]] headers and framing for `ServerSentEvent` fields, multi-line data, and `str`, dict and `msgspec.Struct` events.

Also covers raw `None` as `data: null` versus data-less `ServerSentEvent`s, `: ping` lines while the source is idle, sync iterables, and `ValueError` for newlines in `event`/`id`.

## Streaming fast producer disconnect

Verify a disconnect that becomes ready after the first chunk stops a 10,000-chunk producer early even when neither the producer nor `send` suspends, and the generator is closed.

## Streaming receive errors

Verify an exception raised by `receive()` while streaming closes the producer and propagates out of the response instead of returning normally.

## Streaming cancellation during cleanup

Verify that cancelling the request while a disconnect is closing the producer waits for the producer's `finally` to finish, then raises `CancelledError` rather than `RuntimeError` from `aclose()`.

## SSE headers and ping validation

Verify [[src/chano/responses.py#SSEResponse]] header enforcement and `ping` validation.

### Enforces SSE headers

Verify `SSEResponse` always sends `content-type: text/event-stream` and `cache-control: no-cache` over caller headers and accepts `None` or positive `ping`.

### Rejects invalid ping

Verify `SSEResponse` rejects zero, negative, and non-finite `ping` values with `ValueError`.

## Request cookies

Verify [[src/chano/requests.py#Request#cookies]] for a missing or empty header, several cookies, stray whitespace and `;`, `=` in values, quoted and escaped values, name-less pairs, several `cookie` headers, and first-wins repeats.

## Cookie parameters

Verify `Cookie()` binding in handlers and dependencies with defaults, `alias`, `int` coercion, `422` `loc: ["cookie", name]` errors, the `chano` export, and OpenAPI `in: cookie` parameters.

## Set-cookie headers

Verify through the ASGI boundary that [[src/chano/responses.py#Response#set_cookie]] and `delete_cookie` send secure defaults and every attribute, each `set-cookie` as its own raw header tuple after the single-valued headers.

Also covers `ValueError` for a bad name or `samesite`, and `TypeError` for `delete_cookie` keywords beyond the spec's `path` and `domain`.

## Set-cookie on every response type

Verify `JSONResponse`, `StreamingResponse` and `SSEResponse` each send two separate `set-cookie` headers through the ASGI boundary.

## Set-cookie encoding

Verify `set_cookie` rejects non-Latin-1 values, non-ASCII `path`/`domain`/`expires`, and `;` or newlines in attributes with `ValueError`, and that an app doing so sends a complete `500` rather than failing while sending headers.

## Cookie round trip

Verify cookies set through an app, including empty values and ones with spaces, `=`, quotes, backslashes, `;`, `,` and Latin-1 text, come back intact through `Cookie()` and `request.cookies` when sent back.

## Platform metadata from Vercel headers

Verify `request.id`, `request.ip` and `request.geo` read `x-vercel-id`, `x-real-ip` and every `x-vercel-ip-*` field, percent-decoding `city` (including non-ASCII), and that `Geo` is exported from `chano` and `chano.requests`.

## Platform client IP sources

Verify [[src/chano/requests.py#Request#ip]] prefers `x-real-ip`, then the first trimmed `x-forwarded-for` entry over the ASGI client, then the client host, and is `None` when `client` is missing or empty.

## Platform metadata dev fallbacks

Verify that without Vercel headers `request.id` is a UUID4 hex string, stable on one request and distinct across requests, `ip` is the client host, and `geo` is an all-`None` `Geo`.

## Platform metadata through create_app

Verify a `route.py` handler taking `Request` sees Vercel header metadata through `create_app`, and without the headers gets distinct generated ids, the ASGI client host and an all-`None` geo.

## Platform client IP from repeated forwarded headers

Verify that with two raw `x-forwarded-for` header tuples [[src/chano/requests.py#Request#ip]] returns the first entry of the first line, while `request.headers` still keeps the last value.

## Platform request id under concurrent reads

Verify that threads released together by a barrier all read the same generated `request.id` from one `Request`, repeated over many requests to expose races.

## Hidden routes leave the schema

Verify `@openapi(include_in_schema=False)` hides individual methods or whole routes and exclusive schemas from dev and built `openapi.json`, while keeping sibling methods, shared schemas, and request serving.

## Schema with only hidden routes

Verify that when every route handler is hidden via `@openapi(include_in_schema=False)`, `openapi.json` has empty `paths` and no `components`, while the hidden route still answers requests.

## Invalid openapi metadata

Verify invalid argument types passed to [[src/chano/openapi.py#openapi]] (such as non-`bool` `include_in_schema`/`deprecated`, non-`str` text fields, string or non-`str` `tags`, or non-mapping/non-`str`-key `openapi_extra`) raise `TypeError` naming the field at decoration time.

## OpenAPI decorator metadata

Verify [[src/chano/openapi.py#openapi]] sets `summary`, `description`, `operation_id`, `tags`, `deprecated`, `response_description`, and deep-merged `openapi_extra`, supports stacking with outer winning, and ignores module-level `include_in_schema`.

## Explicit security and responses

Verify a protected operation references one `HTTPBearer` scheme in `security` with no credential header parameter, a public sibling has no `security`, and `Outcome` return annotations document `401`/`403` alongside `200`/`422`.

The same project checks that a dependency raising if called still documents its scheme and `HTTPBearer`'s `401`, that a hidden protected route leaks no scheme into `components.securitySchemes`, that the build's static `openapi.json` matches dev, and that the protected route enforces its token.

## HTTP bearer credentials

Verify `HTTPBearer` returns the stripped token for a case-insensitive `Bearer` credential and answers missing, empty, malformed or non-bearer credentials with `401` and `WWW-Authenticate: Bearer`.

## HTTP bearer scheme names

Verify `HTTPBearer` names outside the OpenAPI component-name pattern (empty, spaces, `/`, `:`, non-ASCII) raise `ValueError` at declaration, while valid names become `components.securitySchemes` keys.

## Declared response precedence

Verify through the generated schema that a handler's outcome docstring beats its dependencies, and a direct dependency beats a nested one in an earlier parameter.

## Inherited typed dependencies

Verify `boundary.py` guards on a project with a protected `(group)`, a nested `admin` guard, a handler requesting the inherited callable, a newly added route and a public sibling.

### Run root to leaf before the handler

Under both factories, independent checks prove the group's run before the admin directory's and the handler, once per request even though the admin check and handler `Depends` on them; the new route inherits both and the sibling runs none.

### Rejection stops the handler

Under both factories, a missing bearer token (`401`) or a non-admin (`403`) answers through the group's `boundary.py` recover function before the handler runs, keeping `WWW-Authenticate`.

### Check validation stops the handler

Verify a check whose own query input fails validation answers `422` naming only that input, and neither the check nor the handler runs.

### No check without a selected handler

Under both factories, a 405 on a protected route and a 404 under a protected directory run no inherited check.

### Documented once without running

Verify protected operations document one bearer `security` requirement, one shared `tenant` input, no `authorization` header and the checks' `401`/`403`, without calling a check.

The build's `openapi.json` matches dev, and the public sibling documents no security, parameters or error responses.

### Declared response precedence

Verify a repeated outcome status prefers the handler, then its direct dependency, then inherited checks nearest segment first and, within a segment, the first declared.

### Async callable checks

Verify an inherited check that is an instance with `async def __call__`, or a sync callable returning a coroutine, is awaited and its `403` stops the handler, and an async callable `Depends` yields its value.

### Unhashable callable checks

Verify a non-frozen dataclass instance works as an inherited check and as the handler's `Depends` under both factories: it runs once per request, rejects, and documents its header once.

### Shared input stays required

Verify an input optional in a check but required by the handler is documented as required, matching the `422` when it is omitted.

### Distinct equal callables

Verify two distinct inherited checks that compare equal (frozen dataclasses) both run, so the second one's `403` stops the handler.

### Bound method checks run once

Verify a bound method used as an inherited check and as the handler's `Depends` runs once per request, though each `obj.method` access makes a new object.

### Shared inputs keep every constraint

Verify a query parameter and a keyed body field read by a check and the handler with different constraints document the `allOf` of both, matching requests rejected by either.

Identical schemas stay a single schema, and an input is required if any consumer requires it.

### Generator checks are rejected

Verify sync and async generator functions, and instances whose `__call__` is one, fail `add_route` and both factories with a `TypeError`, instead of being called and skipped.

### Mixed list and scalar query inputs

Verify a query input read as `list[int]` by a check and as `int` by the handler makes schema generation raise `ValueError`, while the same list shape everywhere stays one input.

### Keyed body list and scalar consumers

Verify a keyed body field read as `Any` by a check and `list[int]` by the handler documents both schemas without raising, matching a list body accepted and a scalar rejected.

### Checks returning generators fail closed

Verify `functools.wraps` wrappers returning a generator or async generator, including one already cached by an earlier check's `Depends`, fail the request without running the handler.

### Header names are case-insensitive

Verify `X-Key` in a check and `x-key` in the handler document one header, required, with both schemas, matching requests rejected by either consumer.

### Body consumers across checks

Verify whole-body consumers and a multi-field check document the request body as `allOf` of their schemas, a body satisfying all succeeds and one missing a key fails, and a lone consumer keeps its plain schema.

### Shared inputs are documented once

Verify an input reached through several dependencies and an inherited check, or a dependency used twice, is documented as one parameter while requests still resolve it.

### Trace roots and bundling

Verify a group's `boundary.py` is a trace root of the functions beneath it, its package is bundled only there and not into the sibling, and the built schema documents the guard's header and `403`.

## Lazy values

Tests for [[src/chano/params.py#lazy]]: one creation per process for sync and async factories, retry after failure, and use as a dependency.

### Sync lazy creates once

Verify 16 threads released by a barrier get the same value from one `factory` call, including `None` and falsy values, and later calls reuse it.

### Async lazy creates once

Verify `asyncio.gather` callers share one creation, the callable is a coroutine function, and the value is reused from later event loops without calling `factory` again.

### Failed creation is not cached

Verify a sync `factory` that raises is retried on the next call, and that async callers sharing a failed attempt all get its error while the next call starts a fresh attempt.

### Cancelled waiter keeps creation

Verify cancelling one caller awaiting an async creation does not cancel it: another caller still gets the value and `factory` runs once.

### Lazy as a dependency

Verify sync and async `lazy` callables work with `Depends` (as a default and in `Annotated`) in sync and async handlers through `create_app`, creating once across requests and adding no OpenAPI parameters from the factory's own defaults.

### Lazy exports

Verify `lazy` is exported from `chano` and `chano.params`, and that a sync `lazy` callable has an empty signature and is not a coroutine function.

### Async lazy across event loops

Verify two threads each running `asyncio.run` share one in-flight async creation started on the other thread's loop, get the same value, and call `factory` once.

The second loop sets an event via `call_soon` just before awaiting, so the creation is only released once that caller is suspended on it.

### Async callable instance factory

Verify `Depends(lazy(Pool()))` where `Pool` has `async def __call__` awaits the factory through `create_app`, returning its value rather than a coroutine, created once across requests.

### Async factory raising synchronously

Verify a `markcoroutinefunction` factory that raises before returning an awaitable fails that call, leaves no unresolved attempt behind, and lets the next call create the value.

### Creation outlives its initiator

Verify that after the initiating caller is cancelled and dropped and `gc.collect()` runs, the creation task survives (its awaited future is only weakly referenced by the test), another caller gets the value, and asyncio logs no errors.

### Abandoned waiter after failure

Verify that when a cancelled caller's creation later fails, an active caller still gets the error and asyncio logs no unretrieved-exception error after `gc.collect()`.

## Lazy Pydantic model detection

Tests for [[src/chano/_pydantic.py#is_pydantic_model]] and [[src/chano/params.py#_is_model]] body-source inference without importing `pydantic`.

### Msgspec routes never import pydantic

Verify in a fresh subprocess with a rejecting `sys.meta_path` finder that serving a `msgspec.Struct` JSON route, an `SSEResponse` route, and `/openapi.json` never import `pydantic`.

### BaseModel parameter infers body source

Verify over HTTP that unmarked and `Annotated` `pydantic.BaseModel` parameters report missing-input `422` errors at `["body"]`, while explicit `Query()` overrides and plain classes report missing-input `422` errors at `["query", ...]`.

## Pydantic request validation

Tests for cached `pydantic.TypeAdapter` decoding, validation, and `422` error mapping across HTTP endpoints.

### Body binding for BaseModel shapes

Verify over HTTP that single `BaseModel`, `Annotated[BaseModel, ...]`, multiple embedded `BaseModel` fields, optional/defaulted `BaseModel` with empty bodies, and `list[BaseModel]` all validate and bind.

### Invalid payloads and malformed JSON

Verify over HTTP that invalid single, nested, `list[BaseModel]`, and strict embedded `BaseModel` payloads, empty required bodies, and malformed JSON return `422` with field-level `loc` paths and messages.

### Cached TypeAdapter and containers

Verify that `pydantic.TypeAdapter` is built once per Pydantic-backed field at route registration and reused across requests, and that `dict[str, BaseModel]` bodies and string-sourced `Query()` models validate with `strict=False`.

### Specialized TypeAliasType and strict JSON

Verify that a specialized `TypeAliasType` wrapping a `BaseModel` (`type Payload[T] = tuple[T, Item]`) caches a `TypeAdapter` and binds over HTTP, and that a strict `BaseModel` `date` field validates via `validate_json(raw)` from raw JSON bytes.

## Pydantic responses and OpenAPI

Tests for lazy `pydantic.BaseModel` JSON/SSE response serialization and OpenAPI 3.1 schema generation in dev and build outputs.

### JSON and SSE serialization

Verify over HTTP that returning a `BaseModel`, `list[BaseModel]`, `dict[str, BaseModel]`, a `Struct` holding a `BaseModel`, and `SSEResponse` yielding `BaseModel` serialize to JSON, while unsupported objects still raise `TypeError`.

### Dev and build OpenAPI schemas

Verify dev `/openapi.json` and the emitted `build()` `static/openapi.json` on disk match and emit `BaseModel` request/response schemas (including `Annotated`, `list`, and `| None`) with `$defs` hoisted into `components.schemas`.

### Component name collisions

Verify that same-named `msgspec.Struct` and `pydantic.BaseModel` models (including discriminated union members) keep distinct `components.schemas` entries with rewritten `$ref` and `discriminator.mapping` targets.

### Response serialization schema mode

Verify that `BaseModel`s with `@field_serializer`, `Field(alias=...)`, and `ConfigDict(serialize_by_alias=True)` emit validation request schemas and serialization response schemas matching the JSON returned over HTTP.

## Background work with after()

Tests for [[src/chano/app.py#after]], `after_backend` selection, and build wiring for `vercel.functions.wait_until`.

### Runs after response body

Verify over ASGI that sync and async tasks scheduled via [[src/chano/app.py#after]] (including a `fn` keyword argument) run after the final `http.response.body` (`more_body=False`) is sent, sync tasks run in a worker thread, and nested calls raise `RuntimeError`.

### Logs exceptions safely

Verify that exceptions and callback-raised `asyncio.CancelledError` in [[src/chano/app.py#after]] tasks are logged to `stderr`, never affect the client response, and do not stop subsequent scheduled tasks from running.

### Outside request raises RuntimeError

Verify that calling [[src/chano/app.py#after]] outside an active request or from a detached task after request completion raises `RuntimeError`.

### Runs on disconnect and send error

Verify that queued local [[src/chano/app.py#after]] tasks still run after a streaming `http.disconnect` and after a send-side `BrokenPipeError` while letting `BrokenPipeError` propagate.

### Cancellation and callback GC

Verify that outer request cancellation skips queued local callbacks, cancels a running callback without letting a swallowed `CancelledError` suppress cancellation, and releases callback payloads for GC while a detached task lives.

### wait_until backend and backend validation

Verify invalid `after_backend` values raise `ValueError`, `"local"` never imports `vercel`, and `"wait_until"` lazily passes wrapped awaitables to `vercel.functions.wait_until` while blocking nested `after()` even in copied registration contexts.

### Build entrypoint and vercel install

Verify [[src/chano/build.py#plan_build]] emits `after_backend="wait_until"` in `index.py`, [[src/chano/_vendor.py#_install_dependencies]] installs `vercel>=0.10.0`, and executing the emitted `index.py` schedules route `after()` work via `vercel.functions.wait_until`.

## Vercel Queues

Tests for `app/**/queue.py` discovery, validation, local/SDK delivery, tree-shaken builds, and `chano queue` CLI commands.

### Discovery and OpenAPI isolation

Verify [[src/chano/queues.py#discover_queues]] strips `(group)` segments, maps to `_queues/...`, and keeps queue consumers and dev triggers out of public routes, `openapi.json`, and `/docs`.

### Sync, async, validation, and Depends

Verify `CONSUME` supports sync/async handlers, `msgspec.Struct` body binding, `Depends()`, `after()`, and `RequestValidationError` `loc` reporting.

### Multi-consumer dev trigger and ASGI fallback

Verify multiple consumer groups on one topic dispatch via [[src/chano/app.py#Chano#dispatch_queue]] and `/_chano/queues/{topic}`, and [[src/chano/queues.py#_LocalQueueASGIApp]] rejects CloudEvent deliveries with `500` when `vercel-queue` is absent.

### Delivery lifecycle, ack, and retry

Verify [[src/chano/queues.py#create_queue_app]] registers with `vercel.queue`, acknowledges successful runs, retries on exceptions or `>= 400` responses, and supports both `create_queue_service_app()` and `accept_and_handle`.

### Real vercel-queue SDK registration and delivery

Verify end-to-end registration and delivery with the real `vercel-queue` SDK, including `Message[bytes]` metadata headers, sanitized consumer groups, and strong subscriber retention.

### Build requires vercel-queue in store

Verify [[src/chano/build.py#emit_build]] raises `RuntimeError` when a project defines `queue.py` consumers but `vercel-queue` is missing from the dependency store.

### Queue validation errors

Verify invalid `queue.py` files (dynamic segments, missing/non-callable/generator `CONSUME`, bad `topic`, `consumer`, `retry_after`, `initial_delay`, or `max_concurrency`) fail during planning.

### Duplicate topic and consumer rejected

Verify [[src/chano/queues.py#load_all_queues]] raises `ValueError` when two `queue.py` modules register the same topic and consumer group.

### Wildcard dispatch and consumer sanitization

Verify wildcard topic patterns (`prefix*` and `*`) and sanitized consumer names resolve deterministically in [[src/chano/queues.py#resolve_queues]], [[src/chano/app.py#Chano#dispatch_queue]], and `/_chano/queues/{topic}`.

### Overlapping wildcard topics rejected

Verify [[src/chano/queues.py#load_all_queues]] rejects overlapping wildcard/exact topics on the same consumer group while permitting distinct consumer groups on overlapping topics.

### Queue build output and tree-shaking

Verify [[src/chano/build.py#emit_build]] emits `_queues/*.func` with `queue/v2beta` triggers, `max_duration`/`memory`/`regions`/`include`, `vercel-queue`, and per-function tree-shaking isolated from HTTP routes.

### Queue CLI list and send

Verify `chano queue list` and `chano queue send` (including `--consumer` and stdin `-` payload) list and invoke local queue consumers.

### Consumer lookup strips whitespace

Verify [[src/chano/queues.py#resolve_queues]] strips whitespace from `consumer` before sanitizing so `" default "` matches `"default"` as in [[src/chano/queues.py#read_queue_trigger]].

## Vercel Schedules

Tests for `app/**/schedule.py` private jobs, cron/timezone/name validation, build output, and `chano schedule` CLI commands.

### Discovery and OpenAPI isolation

Verify [[src/chano/schedules.py#discover_schedules]] strips `(group)` segments, maps to `_schedules/...`, and keeps private schedules and dev triggers out of public routes and `openapi.json`.

### Sync, async, Depends, and ASGI app

Verify `RUN` supports sync/async handlers, `Depends()`, `after()`, and [[src/chano/schedules.py#create_schedule_app]] accepts `POST`/`GET` and returns `405` on other methods.

### Schedule build output and tree-shaking

Verify [[src/chano/build.py#emit_build]] emits `_schedules/*.func` with `schedule/v1beta` triggers, `config.json["schedules"]` (including optional `timezone` and custom `name`), no legacy `crons`, and isolated per-function tree-shaking.

### Schedule validation errors

Verify invalid `schedule.py` modules (dynamic segments, missing/generator `RUN`, malformed/out-of-bounds/DOM+DOW crons, unknown timezones, or invalid names) fail planning.

### Schedule name collision rejected

Verify [[src/chano/schedules.py#load_all_schedules]] raises `ValueError` when two `schedule.py` modules resolve to the same Vercel schedule name.

### Schedule handler KeyError returns 500

Verify an unhandled `KeyError` inside a `schedule.py` `RUN` handler runs the handler once and returns `500` rather than being misclassified as missing.

### Schedule dev trigger and CLI

Verify `POST /_chano/schedules/{target:path}` plus `chano schedule list` and `chano schedule run` inspect and execute `schedule.py` jobs by schedule name, logical path, or function path.

## Vercel Workflows

Tests for `[[tool.vercel.workflows]]` config parsing, optional SDK behavior, live subscription introspection, build output, and local dev mounting.

### Config parsing and optional dependency

Verify Chano's `pyproject.toml` does not require `vercel-workflow`, and [[src/chano/config.py#load_config]] parses `[[tool.vercel.workflows]]` into [[src/chano/config.py#WorkflowConfig]] entries.

### Config validation errors

Verify malformed `[tool.vercel]` / `[[tool.vercel.workflows]]` tables, invalid `module:object` entrypoints, and duplicate entrypoints raise `ValueError`.

### Build output and sequential replays

Verify [[src/chano/build.py#plan_build]] introspects live subscriptions to emit `.well-known/workflow/v1/flow.func` (and `step.func` only when step subscriptions exist), honoring `WORKFLOW_SEQUENTIAL_REPLAYS=1`.

### Build fails without SDK or subscriptions

Verify building `[[tool.vercel.workflows]]` fails with `RuntimeError` when `vercel-workflow`/`vercel-queue` is missing, `http_handler` is unavailable, or no subscriptions are registered.

### Local dev without SDK is graceful

Verify [[src/chano/routing.py#create_app]] succeeds without mounting `/.well-known/workflow/v1/flow` (404) when `vercel-workflow` is not installed, while deployed workflow factories raise `RuntimeError`.

### Partial SDK fallback to http_handler

Verify [[src/chano/workflows.py#create_workflow_app]] falls back to the registry's `http_handler` when importing `vercel_runtime.workers` raises `ModuleNotFoundError` for another optional Vercel SDK package (such as `vercel_workflow.dependencies`).

### Real vercel-workflow SDK end-to-end run and build

Verify end-to-end workflow execution and build output against the real `vercel-workflow` and `vercel-queue` SDKs, including namespaced topics and local `LocalWorld` runs.

### Ignores unrelated queue subscribers

Verify [[src/chano/workflows.py#introspect_workflow_triggers]] attributes only subscriptions owned by the configured `Workflows` registry and ignores unrelated `@vercel.queue.subscribe` handlers on step topics.

### Real SDK shape local dev and build

Verify multi-namespace `Workflows` registries mount `/.well-known/workflow/v1/flow` and `manifest.json` in `create_app`, route by `x-vqs-queue-name`, manage lifespan hooks, and emit valid build artifacts.

### Multi-namespace step topic routing

Verify [[src/chano/workflows.py#create_workflow_app]] routes step-prefixed `x-vqs-queue-name` headers to the matching namespace registry and raises `ValueError` on unexpected ASGI body messages.

### Namespace validation and prefix fallback

Verify [[src/chano/workflows.py#validate_workflow_namespace]], [[src/chano/workflows.py#get_workflow_topic_prefix]], and [[src/chano/workflows.py#get_step_topic_prefix]] reject invalid namespaces with `ValueError`.

### Topic prefix fallback and concurrency

Verify [[src/chano/workflows.py#get_workflow_topic_prefix]] falls back when `vercel.workflow` is absent and [[src/chano/workflows.py#workflow_flow_trigger]] emits `maxConcurrency`.

### Src trace roots and missing attribute

Verify [[src/chano/workflows.py#resolve_workflow_trace_roots]] finds package `__init__.py` under `src/` and [[src/chano/workflows.py#load_workflow_entrypoint]] raises `AttributeError` on missing attributes.

### Trigger introspection error paths

Verify [[src/chano/workflows.py#introspect_workflow_triggers]] reports missing `get_subscriptions`, missing `vercel-workflow`, propagated user `ModuleNotFoundError`, non-string namespaces, and unregistered subscriptions.

### ASGI adapter lifespan and chunking

Verify [[src/chano/workflows.py#create_workflow_app]] handles ASGI `lifespan`, ignores non-HTTP scopes, chunks request bodies in `aiter_bytes(chunk_size=...)`, encodes `str` bodies, and stops on `http.disconnect`.

### Dev mount edge cases

Verify [[src/chano/workflows.py#mount_workflows]] returns `False` for empty configs or non-serving registries, propagates user `ModuleNotFoundError`, and mounts flow even when `manifest_handler` raises.

## Combined Vercel primitives

Verify a single project combining HTTP `route.py`, `queue.py`, `schedule.py`, and `[[tool.vercel.workflows]]` builds cleanly with all functions, `config.json` sections, and isolated OpenAPI output.

## Outcomes

Tests for [[src/chano/responses.py#Outcome]], [[src/chano/responses.py#Failure]], [[src/chano/status.py]], OpenAPI return-annotation schemas, queue retries, and static typing fixtures.

### Returned and raised outcomes

Verify status, body, headers, cookies, bodyless framing, and payload wrapping for returned and raised outcomes.

#### Base types require status

Verify instantiating abstract [[src/chano/responses.py#Outcome]] or [[src/chano/responses.py#Failure]] directly raises `TypeError`.

#### Success outcomes

Verify `Created` and `Accepted` outcomes serialize JSON payloads, empty `Accepted()`, custom headers, and `Set-Cookie` headers.

#### Bodyless outcomes

Verify `103`, `204`, custom `204`, and `304` outcomes send empty bodies without `content-type` or `content-length`, while `OK(None)` sends an empty body with `content-length: 0` and no `content-type`.

#### Returned vs raised failures

Verify returning or raising `NotFound` produces identical `404` JSON, headers, and cookies.

#### Custom failure media type

Verify a `Failure` subclass with custom `media_type` emits `application/problem+json` and its struct payload.

#### Failure payload wrapping

Verify `Failure[str]` preserves raw strings while default/explicit/aliased `ErrorDetail` wraps strings, generic subclass `__init__` and nesting work, and `RequestValidationError` is a `422` `Failure`.

### Returned failures reach error boundaries

Verify a returned `Failure` passes through `boundary.py` `recover` identically to a raised `Failure`.

### OpenAPI from return annotations

Verify OpenAPI schema generation from handler, dependency, and guard return annotations.

#### Handler, deps, and guards

Verify `GET /items/{item_id}` combines `@openapi` description, guard `303`, nested `HTTPBearer` `401`, dependency `403`, merged `anyOf` `404`, and `422`.

#### Envelopes and media types

Verify generic `ApiCreated[Item]` resolves to `Document[Item]` at `201` and `ApiNotFound` emits `application/problem+json` at `404` without inventing `200`.

#### Bodyless, unions, bearer

Verify `204` omits `content`, plain unions form `anyOf` on `200` without `422`, and bare `HTTPBearer` documents `401` without `422`.

#### Aliases and generics

Verify `Annotated` outcomes/aliases, unhashable metadata, PEP 695 default and chained type parameters, multi-param subclasses, and generic mixins resolve cleanly.

#### Union meta and dep 200

Verify `Annotated` union `Meta(description=...)` is preserved on plain and struct unions and `@openapi(response_description=...)` overrides a dependency's `200`.

### Queue retry on returned and raised Failure

Verify a queue consumer returning or raising a `Failure` triggers a retry even when an ancestor `boundary.py` defines `guards`, `recover`, and `wrap`.

### Static typing fixtures

Verify `ty check` passes on [[tests/typing/ok.py]] and reports errors on the exact marked lines of [[tests/typing/bad.py]].

### Outcome type aliases

Verify routes returning PEP 695 `Outcome` aliases produce the expected HTTP response and OpenAPI schema, and [[src/chano/responses.py#resolve_outcome_content]] unwraps plain and generic aliases.

## List query parameters with unions and aliases

Verify [[src/chano/params.py#Field#is_list]] binds list-only unions and PEP 695 aliases as repeated query lists while keeping mixed scalar/list unions scalar.

Covers `list[str] | None`, `Optional[list[str]]`, PEP 695 aliases, and a mixed `str | list[str]` query parameter paired with a scalar `Depends` (both request handling and `/openapi.json`).

## Embedded body fields

Verify [[src/chano/params.py#Endpoint#_resolve]] behavior on routes with multiple embedded `Body()` fields for invalid and empty request bodies.

### Malformed or non-object JSON

Verify malformed JSON, JSON arrays, and JSON `null` report only the top-level `["body"]` validation error.

### Empty body reports missing fields

Verify an empty request body reports `Field required` for each missing embedded `Body()` field.

## Root-only build ignores preserve nested public and tests

Verify [[src/chano/build.py#_project_ignore]] ignores `public` and `tests` only at the project root while ignoring dotfiles, `__pycache__`, `*.pyc`, `node_modules`, and `uv.lock` at every depth.

Invokes built functions in a fresh interpreter after deleting the source tree to cover `app/public/` importing a nested `tests/` package alongside nested `node_modules`, a dangling symlink inside nested `subpkg/tests/fixtures/`, `public -> app/images`, and `public -> shared` with an imported `shared` package.

## Checked Depends

Tests for default-value [[src/chano/params.py#Depends]] short-circuiting and caching.

### Short-circuiting and caching

Verify a dependency or inherited check returning a `Failure` or `SeeOther` short-circuits later dependencies and the handler, while a shared dependency returning a value is cached once per request.

## CLI

Tests for [[src/chano/cli.py#main]] and [[src/chano/cli.py#dev_app]].

### Dev app factory

Verify [[src/chano/cli.py#dev_app]] loads routes from `app/` and static files from `public/` in the current working directory.

### Dev command delegates to uvicorn

Verify `chano dev` passes `chano.cli:dev_app`, host, port, reload directory, and reload includes to `uvicorn.run`.

### Dev propagates non-uvicorn import errors

Verify `chano dev` re-raises `ModuleNotFoundError` when importing `uvicorn` fails on a different missing dependency.

### Empty queue and schedule lists

Verify `chano queue list` and `chano schedule list` print their empty-state messages when no consumers or schedules exist.

### Queue and schedule result formatting

Verify `chano schedule run` prints nothing for `None` or empty `Response`, prints decoded text for non-empty `Response`, and falls back to `str()` for non-JSON objects.

### Invalid arguments exit with usage error

Verify missing or unknown subcommands exit with code `2` and print `usage: chano` to stderr.
