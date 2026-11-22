# Routing

Chano maps an `app/` directory tree to HTTP endpoints using Next.js App Router conventions and serves them via a lightweight ASGI router.

## File-System Discovery

Routes are discovered from `route.py` files under `app/` and sorted from most specific to least specific.

[[src/chano/routing.py#discover_routes]] walks `app_dir` for [[src/chano/routing.py#ROUTE_FILE]] (`route.py`) and builds [[src/chano/routing.py#Route]] records using [[src/chano/routing.py#_route]]:
- Static directory segments map literally and receive rank `0`.
- `[param]` segments map to `{param}` (`[^/]+`) and receive rank `1`.
- `[...param]` catch-all segments map to `{param:path}` (`(.+)`) and receive rank `2`.
- `(group)` parenthesized directories are stripped from both the URL path and the deployed function name for organization only.

With a normalized `base_path` ([[config#Base Path]]), `url_path`, `function` and `pattern` all carry the prefix: under `/api`, `app/route.py` is `/api` (function `api`) and `app/users/[id]/route.py` is `/api/users/{id}` (function `api/users/[id]`, pattern `^/api/users/([^/]+)$`). Only an unprefixed root route is named `index`.

Each [[src/chano/routing.py#Route]] also records `segments`: the directories from the resolved `app_dir` down to the route's own directory, inclusive and ordered root to leaf. Route groups and dynamic directories count, so `app/(admin)/users/route.py` has `app/`, `app/(admin)/`, `app/(admin)/users/`, and `app/route.py` has just `app/`.

[[src/chano/routing.py#Route#segment_files]] lists the existing named files across the segments, root to leaf (in the given name order within a segment); the build uses it for trace roots ([[build#Dependency Tree-Shaking and Size Reporting#Import Tracing, Closure, and Emit]]). [[src/chano/routing.py#Route#load_segments]] loads a named file from each segment directory that has one, root to leaf, via [[src/chano/routing.py#load_module]]. It reads only the route's own ancestors, never a sibling directory, and returns an empty list when no ancestor has the file. Both app factories use it to load `middleware.py` ([[routing#Middleware]]), `error.py` ([[routing#Error Boundaries]]) and `dependencies.py` ([[routing#Inherited Dependencies]]).

Routes are sorted by `(route.rank, route.url_path)` so static paths (e.g. `/items/new`) match before dynamic ones (`/items/{item_id}`), and catch-alls match last. When discovering routes, [[src/chano/routing.py#discover_routes]] also prepends `app_dir.parent` to `sys.path` so route modules can import sibling project packages.

## Handler Registration

Each `route.py` exports top-level functions named after standard HTTP methods.

[[src/chano/routing.py#_add_route]] loads the module via [[src/chano/routing.py#Route#load]] and registers any callable matching [[src/chano/routing.py#HTTP_METHODS]].

[[src/chano/routing.py#Route#load]] delegates to [[src/chano/routing.py#load_module]], which [[src/chano/routing.py#Route#load_segments]] also uses. It caches each module in `sys.modules` as `chano_routes._<hash>`, keyed only by 12 hex digits of the SHA-1 of the resolved file path. Same-named files in different projects never share a module, and one file reached from several routes or two `base_path` values reuses one, so a file runs at most once per process. If the import raises, the entry is removed before re-raising, so no partial module is cached.

- [[src/chano/routing.py#create_app]] registers all discovered routes and enables `/docs`, `/openapi.json`, and optional `public_dir` static serving.
- [[src/chano/routing.py#create_route_app]] registers only the single [[src/chano/routing.py#Route]] matching `function` and sets `docs=False`.

Both take `base_path`; `None` reads it from `[tool.chano]` beside `app_dir`, and the value is passed to both [[src/chano/app.py#Chano]] and [[src/chano/routing.py#discover_routes]].

## ASGI Request Handling

[[src/chano/app.py#Chano#__call__]] dispatches HTTP requests through [[src/chano/app.py#Chano#_handle]], handles lifespan events as no-ops, and maps exceptions to HTTP responses.

Matching always uses the full public path from [[src/chano/requests.py#Request#path]] ([[config#Full Request Path]]).

- **Method matching & HEAD**: `HEAD` falls back to `GET` handlers if no explicit `HEAD` handler exists, and [[src/chano/app.py#Chano#__call__]] suppresses the response body for `HEAD` requests. It calls the response with `receive` and `send` ([[responses]]).
- **First match wins**: [[src/chano/app.py#Chano#_dispatch]] serves a request with the first route whose regex matches the path, as the platform sends it to that route's function. If that route has no endpoint for the method, the answer is its [[src/chano/exceptions.py#HTTPException]] `405` with that route's sorted `Allow` header: a literal route's 405 never falls through to docs, static files, or dynamic routes, and a dynamic route's never falls through to a later dynamic or catch-all route. A docs or `public/` file hit answers methods other than `GET`/`HEAD` with `405` and `Allow: GET, HEAD`.
- **Error mapping**: [[src/chano/app.py#error_response]] renders [[src/chano/exceptions.py#HTTPException]] as JSON `{"detail": ...}` with its status/headers and [[src/chano/exceptions.py#RequestValidationError]] as `422`; other exceptions log a traceback to `stderr` and return `500` plain text. Endpoint errors go through the route's error boundary inside its middleware chain, falling back to this mapping ([[routing#Error Boundaries]]); [[src/chano/app.py#Chano#__call__]] maps the 404 and middleware errors.
- **Match order**: [[src/chano/app.py#Chano#_handle]] mirrors Build Output priority: literal routes (no path parameters) match first, like static-path functions in the `filesystem` check; then [[src/chano/app.py#Chano#_dev_file]] serves docs and `public_dir` files; dynamic and catch-all routes match last, like the rewrites after the docs rewrite, so a catch-all never shadows `/docs`, `/openapi.json`, or a `public/` file.
- **Dev docs & static files**: [[src/chano/app.py#Chano#_dev_file]] follows the build's precedence and rejects non-`GET`/`HEAD` requests with `405` before generating OpenAPI or reading file bytes. If `docs=True`, the generated `{base_path}/openapi.json` and `{base_path}/docs.html` win over `public/` copies, as [[src/chano/build.py#_write_static]] writes them over `public/`. `{base_path}/docs` is the Swagger UI (loading the prefixed `openapi.json`) only when `public/docs` does not exist, because the `filesystem` check runs before the docs rewrite. [[src/chano/app.py#Chano#_static_file]] serves files from `public_dir`, guarding against path traversal with `Path.is_relative_to`. Under a prefix it only serves paths strictly under `{base_path}/`, so `/hello.txt` and `/apiary/hello.txt` get the JSON 404. Paths ending in `/`, symlink loops and NUL bytes are not files.

## Middleware

A `middleware.py` in any segment exports `async def middleware(request, call_next) -> Response`, wrapping every route at or below its directory.

[[src/chano/routing.py#_middleware]] loads [[src/chano/routing.py#MIDDLEWARE_FILE]] from the route's segments via [[src/chano/routing.py#Route#load_segments]], root to leaf, before the route module. A file whose `middleware` is missing, not callable, or not an `async def` raises `TypeError` naming the file. [[src/chano/routing.py#_add_route]] passes the chain to [[src/chano/app.py#Chano#add_route]] for each method, so [[src/chano/routing.py#create_app]] and [[src/chano/routing.py#create_route_app]] build the same per-route chain. The chain is per URL path in the `middleware` dict of [[src/chano/app.py#Chano]]; two route files sharing a path with different chains raise `ValueError`, since the platform could deploy only one of them.

On a match, [[src/chano/app.py#Chano#_dispatch]] sets `path_params` and wraps the route's endpoint call in each middleware via [[src/chano/app.py#_wrap]], root outermost:

- **Short-circuiting**: a middleware may return a response without awaiting `call_next`; inner middleware and the handler never run.
- **Middleware sees a response**: the innermost call from [[src/chano/app.py#Chano#_endpoint_call]] passes the endpoint's `HTTPException`, `RequestValidationError`, other exceptions, and the method mismatch `405` through the route's error boundary ([[routing#Error Boundaries]]) before returning, so `call_next` always returns a `Response`.
- **Middleware errors**: an exception raised by middleware propagates through the outer middleware (which may catch it) to [[src/chano/app.py#Chano#__call__]], which applies the default mapping. A middleware returning something other than a `Response` raises `TypeError`, so it becomes a 500.
- **Where it runs**: only after a route's regex matches the path, including its 405. Unmatched paths (the JSON 404) and the dev docs and `public_dir` files from [[src/chano/app.py#Chano#_dev_file]] run no middleware.

## Error Boundaries

An `error.py` in any segment exports `handle_error(request, exc) -> Response`, sync or async, handling endpoint errors for every route at or below its directory.

[[src/chano/routing.py#_error_handlers]] loads [[src/chano/routing.py#ERROR_FILE]] from the route's segments via [[src/chano/routing.py#Route#load_segments]], root to leaf, after `middleware.py` and before the route module. A file whose `handle_error` is missing or not callable raises `TypeError` naming the file. [[src/chano/routing.py#_add_route]] passes the chain to [[src/chano/app.py#Chano#add_route]] for each method, so both factories build the same boundary. It is kept per URL path in the `error_handlers` dict of [[src/chano/app.py#Chano]]; two route files sharing a path with different chains raise `ValueError`.

When the endpoint call in [[src/chano/app.py#Chano#_endpoint_call]] raises (including the method mismatch `405` and request validation), [[src/chano/app.py#_error_boundary]] runs inside the innermost middleware:

- **Nearest wins**: the leaf-most `handle_error` gets the exception first. A returned `Response` becomes the response that every middleware sees from `call_next`.
- **Re-raising**: anything the handler raises, whether a bare `raise` or a new exception, goes to the next ancestor. Each ancestor runs inside the `except` of the handler below it, so a bare `raise` re-raises the latest exception. A result that is not a `Response` is raised as `TypeError` and treated the same way.
- **Default**: past the root `error.py`, or with none, [[src/chano/app.py#error_response]] maps the last exception.
- **Sync and async**: [[src/chano/app.py#_is_async]] treats an `async def` function or an instance with `async def __call__` as async and calls it on the event loop. Anything else runs in a worker thread via `asyncio.to_thread`, like sync route handlers. [[src/chano/app.py#_call_sync_handler]] makes the exception active in that thread, restoring its traceback, so a bare `raise` works there too. An awaitable result, such as a coroutine from a sync wrapper, is awaited on the loop while the exception is still active, before the `Response` check.
- **Where it runs**: only for endpoint errors after a route matches. Exceptions raised by middleware skip `error.py` and get the default mapping in [[src/chano/app.py#Chano#__call__]]; unmatched paths and dev files run no `error.py`.

*Note on non-obvious behavior*: [[src/chano/app.py#Chano#__call__]] ignores non-`http`/non-`lifespan` ASGI scopes (e.g. `websocket`) by returning immediately.

## Inherited Dependencies

A `dependencies.py` in any segment exports `dependencies`, a `list` or `tuple` of dependency callables that every method at or below its directory runs before its handler.

[[src/chano/routing.py#_dependencies]] loads [[src/chano/routing.py#DEPENDENCIES_FILE]] via [[src/chano/routing.py#Route#load_segments]] after `middleware.py` and `error.py`, returning one group per file, root to leaf, in declared order. A missing name, any other container (a string or a set), a non-callable item, including a `Depends(...)` marker, or a generator callable ([[src/chano/params.py#is_generator_callable]]) raises `TypeError` naming the file. [[src/chano/routing.py#_add_route]] passes them to [[src/chano/app.py#Chano#add_route]] for each method, which attaches them to that method's [[src/chano/params.py#Endpoint]], so both factories build the same checks ([[params#Inherited Checks]]). Route groups count, so `app/(admin)/dependencies.py` protects every route in the group without changing URLs.

The checks run inside [[src/chano/params.py#Endpoint#call]], within the error boundary and middleware, so a rejection passes through `error.py` and middleware like any handler error. A 404 or 405 selects no endpoint and runs no check.

## Schema Visibility

A route module may set `include_in_schema = False` to keep its operations out of `openapi.json` while still serving them.

[[src/chano/routing.py#_include_in_schema]] reads the module-level value, defaulting to `True` and raising `TypeError` for anything that is not a `bool`. [[src/chano/routing.py#_add_route]] passes it to [[src/chano/app.py#Chano#add_route]], which records hidden `(path, method)` pairs in the `hidden_operations` set of [[src/chano/app.py#Chano]]. Dispatch ignores that set; only [[src/chano/openapi.py#openapi_schema]] reads it. Dev and the build's static `openapi.json` both come from [[src/chano/routing.py#create_app]], so they agree.

## Background Work

[[src/chano/app.py#after]] schedules sync or async work to run after the HTTP response is sent, backed by a per-request `ContextVar` in dev/tests or `vercel.functions.wait_until` in deployed functions.

- **Request scope**: [[src/chano/app.py#Chano#__call__]] installs a closable [[src/chano/app.py#_AfterState]] in [[src/chano/app.py#_after_state]] for the request and calls [[src/chano/app.py#_AfterState#close_and_take]] plus `reset` in `finally` after `await response(...)`, so nested calls (even in copied `wait_until` contexts) and detached tasks spawned during the request raise `RuntimeError`, and closed state drops callback references for GC. `fn` is positional-only (`after(fn, /, *args, **kwargs)`) so callbacks may accept a `fn` keyword argument; non-callables raise `TypeError`.
- **Execution and errors**: [[src/chano/app.py#_make_after_task]] binds arguments once and runs each callback in an isolated child task wrapped with `asyncio.shield` (awaiting async callables on the loop and sync callables via `asyncio.to_thread`, awaiting any returned awaitable). Exceptions and callback-raised `asyncio.CancelledError` are logged to `stderr` via `traceback.print_exception` without affecting the client or later tasks; outer request cancellation during handling skips queued local callbacks, and outer cancellation during a callback cancels the child and always propagates even if the callback swallows `CancelledError`.
- **Backends**: [[src/chano/app.py#Chano]] and [[src/chano/routing.py#create_route_app]] accept `after_backend` ([[src/chano/app.py#AfterBackend]]: `"local"` default, or `"wait_until"`), rejecting unknown values with `ValueError`; [[src/chano/routing.py#create_app]] always uses `"local"`. In `"local"` mode, queued tasks drain in `finally` even on disconnect or send errors (letting send errors propagate). In `"wait_until"` mode, [[src/chano/app.py#_AfterState#schedule]] lazily imports `vercel.functions` and passes each wrapped coroutine to `vercel.functions.wait_until`, so `vercel` is never imported in local dev or tests. [[src/chano/build.py#ENTRYPOINT_SOURCE]] opts deployed functions into `after_backend="wait_until"`.
