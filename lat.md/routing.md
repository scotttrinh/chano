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

[[src/chano/routing.py#Route#segment_files]] lists the existing named files across the segments, root to leaf (in the given name order within a segment); the build uses it for trace roots ([[build#Dependency Tree-Shaking and Size Reporting#Import Tracing, Closure, and Emit]]). [[src/chano/routing.py#Route#load_segments]] loads a named file from each segment directory that has one, root to leaf, via [[src/chano/routing.py#load_module]]. It reads only the route's own ancestors, never a sibling directory, and returns an empty list when no ancestor has the file. Both app factories use it to load `boundary.py` ([[routing#Boundary]]).

Routes are sorted by `(route.rank, route.url_path)` so static paths (e.g. `/items/new`) match before dynamic ones (`/items/{item_id}`), and catch-alls match last. When discovering routes, [[src/chano/routing.py#discover_routes]] also prepends `app_dir.parent` to `sys.path` so route modules can import sibling project packages.

## Handler Registration

Each `route.py` exports top-level functions named after standard HTTP methods.

[[src/chano/routing.py#_add_route]] loads the module via [[src/chano/routing.py#Route#load]] and registers any callable matching [[src/chano/routing.py#HTTP_METHODS]].

[[src/chano/routing.py#Route#load]] delegates to [[src/chano/routing.py#load_module]], which [[src/chano/routing.py#Route#load_segments]] also uses. It caches each module in `sys.modules` as `chano_routes._<hash>`, keyed only by 12 hex digits of the SHA-1 of the resolved file path. Same-named files in different projects never share a module, and one file reached from several routes or two `base_path` values reuses one, so a file runs at most once per process. If the import raises, the entry is removed before re-raising, so no partial module is cached.

- [[src/chano/routing.py#create_app]] registers all discovered routes, enables `/docs`, `/openapi.json`, and optional `public_dir` static serving, and wires local `queue.py` / `schedule.py` dispatch plus optional `[[tool.vercel.workflows]]` ASGI mounts without exposing them in OpenAPI ([[vercel-primitives]]).
- [[src/chano/routing.py#create_route_app]] registers only the single [[src/chano/routing.py#Route]] matching `function` and sets `docs=False`. Private queue and schedule functions use [[src/chano/app.py#EndpointASGIApp]] (or `vercel-queue` callback adapters) via [[src/chano/queues.py#create_queue_app]] and [[src/chano/schedules.py#create_schedule_app]].

Both take `base_path`; `None` reads it from `[tool.chano]` beside `app_dir`, and the value is passed to both [[src/chano/app.py#Chano]] and [[src/chano/routing.py#discover_routes]].

## ASGI Request Handling

[[src/chano/app.py#Chano#__call__]] dispatches HTTP requests through [[src/chano/app.py#Chano#_handle]] (after checking exact ASGI sub-apps registered via [[src/chano/app.py#Chano#mount_asgi]]), runs [[src/chano/app.py#Chano#on_startup]] / [[src/chano/app.py#Chano#on_shutdown]] hooks on ASGI lifespan events, and maps exceptions to HTTP responses.

Matching always uses the full public path from [[src/chano/requests.py#Request#path]] ([[config#Full Request Path]]).

- **Method matching & HEAD**: `HEAD` falls back to `GET` handlers if no explicit `HEAD` handler exists, and [[src/chano/app.py#Chano#__call__]] suppresses the response body for `HEAD` requests. It calls the response with `receive` and `send` ([[responses]]).
- **First match wins**: [[src/chano/app.py#Chano#_dispatch]] serves a request with the first route whose regex matches the path, as the platform sends it to that route's function. If that route has no endpoint for the method, the answer is [[src/chano/status.py#MethodNotAllowed]] (`405`) with that route's sorted `Allow` header: a literal route's 405 never falls through to docs, static files, or dynamic routes, and a dynamic route's never falls through to a later dynamic or catch-all route. A docs or `public/` file hit answers methods other than `GET`/`HEAD` with `405` and `Allow: GET, HEAD`.
- **Error mapping**: [[src/chano/app.py#error_response]] sends any [[src/chano/responses.py#Failure]] (including [[src/chano/status.py#NotFound]], [[src/chano/status.py#MethodNotAllowed]], and [[src/chano/exceptions.py#RequestValidationError]]) as-is; other exceptions log a traceback to `stderr` and return `500` plain text. Both returned and raised failures from handlers, dependencies, or guards go through the route's recover functions inside its wrapper chain, falling back to this mapping ([[routing#Boundary]]); [[src/chano/app.py#Chano#__call__]] maps the unmatched-path `404` and wrapper errors.
- **Match order**: [[src/chano/app.py#Chano#_handle]] mirrors Build Output priority: literal routes (no path parameters) match first, like static-path functions in the `filesystem` check; then [[src/chano/app.py#Chano#_dev_file]] serves docs and `public_dir` files; dynamic and catch-all routes match last, like the rewrites after the docs rewrite, so a catch-all never shadows `/docs`, `/openapi.json`, or a `public/` file.
- **Dev docs & static files**: [[src/chano/app.py#Chano#_dev_file]] follows the build's precedence and rejects non-`GET`/`HEAD` requests with `405` before generating OpenAPI or reading file bytes. If `docs=True`, the generated `{base_path}/openapi.json` and `{base_path}/docs.html` win over `public/` copies, as [[src/chano/build.py#_write_static]] writes them over `public/`. `{base_path}/docs` is the Swagger UI (loading the prefixed `openapi.json`) only when `public/docs` does not exist, because the `filesystem` check runs before the docs rewrite. [[src/chano/app.py#Chano#_static_file]] serves files from `public_dir`, guarding against path traversal with `Path.is_relative_to`. Under a prefix it only serves paths strictly under `{base_path}/`, so `/hello.txt` and `/apiary/hello.txt` get the JSON 404. Paths ending in `/`, symlink loops and NUL bytes are not files.

## Boundary

A `boundary.py` in any segment exports `boundary = Boundary(guards=..., recover=..., wrap=...)`, scoping guards, error recovery, and HTTP wrappers to every HTTP route (`route.py`) at or below its directory.

[[src/chano/routing.py#load_segment_boundaries]] loads [[src/chano/routing.py#BOUNDARY_FILE]] (`boundary.py`) via [[src/chano/routing.py#Route#load_segments]], root to leaf, requiring `boundary` to be a [[src/chano/app.py#Boundary]] instance or raising `TypeError` naming the file. `boundary.py` applies only to HTTP routes (`route.py`), never to `queue.py` or `schedule.py`. [[src/chano/routing.py#_add_route]] passes the tuple to [[src/chano/app.py#Chano#add_route]] for each method; two route files sharing a URL path with different boundary chains raise `ValueError`. Route groups count, so `app/(admin)/boundary.py` covers the group without changing URLs.

- **Execution order**: On a route match, [[src/chano/app.py#Chano#_dispatch]] composes `wrap` (root outermost, via [[src/chano/app.py#_wrap]]), then runs `guards` (root to leaf inside [[src/chano/params.py#Endpoint#call]], before handler arguments, sharing the per-request `Depends` cache), then handler dependencies and the handler. Any exception or returned `Failure` (plus the method-mismatch `405`) flows through `recover`, and the resulting `Response` flows back out through `wrap`.
- **Guards**: Each guard is a dependency whose return value is discarded unless it returns a `Response`, which short-circuits the request ([[params#Inherited Checks]]). Generator callables are rejected with `TypeError`.
- **Recover**: [[src/chano/app.py#_run_recover]] checks candidates from [[src/chano/app.py#_recover_candidates]] (nearest folder first, then list order) via [[src/chano/app.py#_matches_recover]]. Parameter 2 annotation `C` matches `isinstance(error, C)`; `C[B]` where `C` is an `Outcome` and `B` is a class also requires `isinstance(error.content, B)`. Sync and async recover functions are supported ([[src/chano/app.py#_call_sync_handler]] preserves active exceptions for bare `raise`). A returned `Response` is final; a raised exception (or non-`Response` `TypeError`) continues outward to remaining candidates, then falls back to [[src/chano/app.py#error_response]]. When both parameter 2 and return annotations are `Outcome` classes with fixed statuses, [[src/chano/openapi.py#_recover_rules]] replaces matching documented responses in OpenAPI.
- **Wrap**: Async wrappers (`async def wrap(request, call_next) -> Response`) run after a route matches (never for `404`, dev docs, or `public/` files). `call_next` always returns a `Response`. Returning without awaiting `call_next` short-circuits; exceptions raised by a wrapper skip `recover` and get [[src/chano/app.py#error_response]].

*Note on non-obvious behavior*: [[src/chano/app.py#Chano#__call__]] ignores non-`http`/non-`lifespan` ASGI scopes (e.g. `websocket`) by returning immediately.

## Schema Visibility

A route handler may be decorated with `@openapi(include_in_schema=False)` to keep that method out of `openapi.json` while still serving it.

[[src/chano/openapi.py#openapi]] attaches an [[src/chano/openapi.py#OpenAPIInfo]] record to the handler function (supporting `include_in_schema`, `summary`, `description`, `operation_id`, `tags`, `deprecated`, `response_description`, and `openapi_extra`), validating types eagerly and letting outer stacked decorators override inner ones. [[src/chano/app.py#Chano#add_route]] reads `include_in_schema` via [[src/chano/openapi.py#openapi_info]] (defaulting to `True`, with an explicit `include_in_schema` argument taking precedence) and records hidden `(path, method)` pairs in the `hidden_operations` set of [[src/chano/app.py#Chano]]. Dispatch ignores that set; only [[src/chano/openapi.py#openapi_schema]] reads it. Dev and the build's static `openapi.json` both come from [[src/chano/routing.py#create_app]], so they agree.

## Background Work

[[src/chano/app.py#after]] schedules sync or async work to run after the HTTP response is sent, backed by a per-request `ContextVar` in dev/tests or `vercel.functions.wait_until` in deployed functions.

- **Request scope**: [[src/chano/app.py#Chano#__call__]] installs a closable [[src/chano/app.py#_AfterState]] in [[src/chano/app.py#_after_state]] for the request and calls [[src/chano/app.py#_AfterState#close_and_take]] plus `reset` in `finally` after `await response(...)`, so nested calls (even in copied `wait_until` contexts) and detached tasks spawned during the request raise `RuntimeError`, and closed state drops callback references for GC. `fn` is positional-only (`after(fn, /, *args, **kwargs)`) so callbacks may accept a `fn` keyword argument; non-callables raise `TypeError`.
- **Execution and errors**: [[src/chano/app.py#_make_after_task]] binds arguments once and runs each callback in an isolated child task wrapped with `asyncio.shield` (awaiting async callables on the loop and sync callables via `asyncio.to_thread`, awaiting any returned awaitable). Exceptions and callback-raised `asyncio.CancelledError` are logged to `stderr` via `traceback.print_exception` without affecting the client or later tasks; outer request cancellation during handling skips queued local callbacks, and outer cancellation during a callback cancels the child and always propagates even if the callback swallows `CancelledError`.
- **Backends**: [[src/chano/app.py#Chano]] and [[src/chano/routing.py#create_route_app]] accept `after_backend` ([[src/chano/app.py#AfterBackend]]: `"local"` default, or `"wait_until"`), rejecting unknown values with `ValueError`; [[src/chano/routing.py#create_app]] always uses `"local"`. In `"local"` mode, queued tasks drain in `finally` even on disconnect or send errors (letting send errors propagate). In `"wait_until"` mode, [[src/chano/app.py#_AfterState#schedule]] lazily imports `vercel.functions` and passes each wrapped coroutine to `vercel.functions.wait_until`, so `vercel` is never imported in local dev or tests. [[src/chano/build.py#ENTRYPOINT_SOURCE]] opts deployed functions into `after_backend="wait_until"`.
