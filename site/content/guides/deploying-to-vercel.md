---
title: Deploying to Vercel
description: Build Vercel Build Output API v3 artifacts, tune per-route function settings, inspect tree-shaking, and deploy to production.
lede: Compile your Chano app into isolated Vercel Functions and static CDN assets, inspect bundle sizes, and deploy with the Vercel CLI.
section: guides
order: 4
nav_title: Deploying to Vercel
---

## Build for production

Run `chano build` to compile your project into Vercel's Build Output API v3 directory (`.vercel/output/`):

```sh
uv run chano build
```

What `chano build` writes:

- `.vercel/output/static/`: `public/` assets plus pre-rendered `openapi.json` and `docs.html`.
- `.vercel/output/functions/`: one `<route>.func/` per `route.py`, plus private `_queues/<name>.func/` for `queue.py`, `_schedules/<name>.func/` for `schedule.py`, and `.well-known/workflow/v1/{flow,step}.func/` for `[[tool.vercel.workflows]]`, each with `.vc-config.json` and tree-shaken `_vendor/` dependencies.
- `.vercel/output/config.json`: Vercel routing rules (`filesystem` check first, then `/docs` rewrite, then dynamic route regex rewrites), plus `schedules` when configured.

Deploy the prebuilt output directly with the [Vercel CLI](https://vercel.com/docs/cli):

```sh
vercel deploy --prebuilt
```

Because `chano build` already assembled `.vercel/output/`, Vercel uploads the static files and functions directly without a remote build step.

## Tune per-route function settings

Because every `route.py` becomes its own function, you can configure compute resources per endpoint using module-level variables:

```python
# app/api/reports/route.py
max_duration = 60  # seconds
memory = 2048      # MB
regions = ["iad1"] # pin near your database
```

You can also set:

- `include = ["some-pkg"]`: bundle an extra distribution if it is loaded dynamically and missed by static import tracing.

To exclude an individual method from `/openapi.json` and `/docs` while still serving requests, decorate the handler with `@openapi(include_in_schema=False)`.

## Dependency tree-shaking and `--explain`

During `chano build`, Chano performs static AST import tracing starting from each `route.py` and its ancestor `middleware.py`, `error.py`, and `dependencies.py` files. Only the third-party distributions a route actually imports (plus their transitive dependencies) are hard-linked into that function's `_vendor/` directory.

If only `app/api/reports/route.py` imports a heavy library, only that function pays the bundle-size cost. Lightweight endpoints like `/api/health` stay minimal.

Inspect why each distribution was included or excluded for a specific function:

```sh
uv run chano build --explain api/items/[item_id]
```

Chano also prints uncompressed bundle sizes for every built function, warns as a function approaches Vercel's bundle size limits, and runs an isolated import check when targeting the host platform (`--platform native`).

## Pairing with a frontend using `base_path`

To mount a Chano API alongside a Next.js or static frontend as Vercel Services, set `base_path` in your backend's `pyproject.toml`:

```toml
[tool.chano]
base_path = "/api"
```

With `base_path = "/api"`:

- `app/route.py` serves `/api`
- `app/items/[item_id]/route.py` serves `/api/items/{item_id}`
- `public/hello.txt` serves `/api/hello.txt`
- Docs serve at `/api/docs` and `/api/openapi.json`

Your `app/` directory stays clean without an extra `api/` wrapper folder, and `chano dev` mirrors the exact same prefixed URLs.

## Running on other platforms (ASGI)

`chano build` is specific to Vercel's Build Output API v3, but your application itself is a standard ASGI app. To deploy the entire project to another platform — such as a container, VM, or any Python ASGI host — create a small entrypoint with `create_app()`:

```python
# main.py
from chano import create_app

app = create_app("app", public_dir="public")
```

Then serve it with any ASGI server such as Uvicorn, Hypercorn, or Granian:

```sh
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

When running as a single ASGI app:

- All routes under `app/`, `/docs`, `/openapi.json`, and `public/` are served from the same process.
- `base_path` from `[tool.chano]` in `pyproject.toml` is applied automatically (or can be passed explicitly to `create_app()`).
- `after()` callbacks drain in the request's `finally` block instead of delegating to `vercel.functions.wait_until`.
