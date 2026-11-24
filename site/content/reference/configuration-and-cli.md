---
title: Configuration and CLI
description: Reference for [tool.chano] configuration in pyproject.toml, CLI commands, and programmatic ASGI factories.
lede: Project-level configuration keys, CLI commands and flags for local development and Vercel builds, and programmatic ASGI entrypoints.
section: reference
order: 4
nav_title: Configuration and CLI
---

## `[tool.chano]` in `pyproject.toml`

Project-level settings live under `[tool.chano]` in `pyproject.toml` and are parsed with `tomllib` without importing route modules:

```toml
[tool.chano]
base_path = "/api"
tree_shake = true
include = ["some-dynamic-pkg"]
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `base_path` | `str` | `""` | URL prefix for all routes, `/docs`, `/openapi.json`, and `public/` files |
| `tree_shake` | `bool` | `True` | Bundle only traced distributions per function (`False` bundles all dependencies) |
| `include` | `list[str]` | `[]` | Distribution names (and their closures) bundled into every function |

Unknown keys or wrong types (strict validation) raise `ValueError`.

### `base_path` rules

- Normalized to start with `/` and never end with `/` (`"api"` and `"/api/"` both become `"/api"`; `""` and `"/"` mean no prefix).
- Segments may contain ASCII letters, digits, and `-._~!+,;=@`. Empty segments, `.`, `..`, and segments ending in `.func` are rejected.

## `[[tool.vercel.workflows]]` in `pyproject.toml`

Optional Vercel Workflows registries are configured as an array of tables in `pyproject.toml`:

```toml
[[tool.vercel.workflows]]
entrypoint = "workflows.orders:wf"
```

| Key | Type | Required | Description |
| --- | --- | --- | --- |
| `entrypoint` | `str` | Yes | Dotted `module:object` path to a `vercel.workflow.Workflows` registry |

- `vercel-workflow` is strictly optional for Chano; projects that declare `[[tool.vercel.workflows]]` must install `vercel-workflow` (and `vercel-queue`) in the build environment.
- Duplicate or malformed entrypoints raise `ValueError`.
- `chano build` introspects each registry's registered `vercel.queue` subscriptions and emits `.well-known/workflow/v1/flow.func` (`maxDuration: 60`) plus `.well-known/workflow/v1/step.func` (`maxDuration: "max"`, only when step subscriptions exist). Set `WORKFLOW_SEQUENTIAL_REPLAYS=1` to emit `maxConcurrency: 1` on flow triggers.
- `chano dev` / `create_app()` mounts `/.well-known/workflow/v1/flow` and `/.well-known/workflow/v1/manifest.json` outside OpenAPI when `vercel-workflow` is installed, and skips mounting gracefully otherwise.

## CLI commands

### `chano dev`

Runs all routes, interactive Swagger UI, OpenAPI schema, and `public/` static files in a single local ASGI server with reload:

```sh
uv run chano dev [--host 127.0.0.1] [--port 8000]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Bind port |

Requires `uvicorn`, installed via the `chano[dev]` extra. When `watchfiles` is installed, `chano dev` reloads on both `.py` and `pyproject.toml` changes.

### `chano build`

Compiles the project into a deterministic Vercel Build Output API v3 directory:

```sh
uv run chano build [--python 3.12] [--platform x86_64-manylinux_2_28] [--output .vercel/output] [--explain FUNCTION]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--python` | `3.12` | Target Python version for vendored wheels and `.vc-config.json` |
| `--platform` | `x86_64-manylinux_2_28` | Target platform for `uv pip install`; pass `native` for the local host |
| `--output` | `.vercel/output` | Destination Build Output directory |
| `--explain` | `None` | Print why each distribution was included or excluded for one function |

`chano build` exports locked dependencies with `uv export --locked --no-dev` (supporting `uv` workspaces automatically), hard-links shared payloads and tree-shaken `_vendor/` trees into each `.func` directory, pre-renders static `openapi.json` and `docs.html`, and reports uncompressed function sizes against Vercel's limits.

### `chano queue`

Inspect or deliver messages to local `app/**/queue.py` consumers:

```sh
uv run chano queue list [--app app]
uv run chano queue send TOPIC [PAYLOAD] [--consumer GROUP] [--app app]
```

| Subcommand / Flag | Default | Description |
| --- | --- | --- |
| `list` | — | Print discovered topics, consumer groups, and `_queues/...` function paths |
| `send TOPIC [PAYLOAD]` | `"{}"` | Deliver a JSON payload string (or `-` to read stdin) to matching consumers |
| `--consumer` | `None` | Restrict `send` to one consumer group |
| `--app` | `"app"` | Path to the `app/` directory |

During `chano dev`, Chano also exposes a hidden `POST {base_path}/_chano/queues/{topic}` trigger (accepting optional `?consumer=`).

### `chano schedule`

Inspect or run local `app/**/schedule.py` jobs:

```sh
uv run chano schedule list [--app app]
uv run chano schedule run TARGET [--app app]
```

| Subcommand / Flag | Default | Description |
| --- | --- | --- |
| `list` | — | Print discovered schedule jobs with their cron expressions and optional timezones |
| `run TARGET` | — | Invoke a schedule by name, logical path, or function path |
| `--app` | `"app"` | Path to the `app/` directory |

During `chano dev`, Chano also exposes a hidden `POST {base_path}/_chano/schedules/{target}` trigger.

## Programmatic factories

For testing (for example with `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`) or custom ASGI hosting:

- `create_app(app_dir="app", *, public_dir=None, base_path=None) -> Chano`: discovers all routes, enables `/docs` and `/openapi.json`, serves optional `public_dir`, and wires local queue/schedule triggers (`app.dispatch_queue`, `app.run_schedule`) and optional workflow mounts.
- `create_route_app(app_dir, function, *, base_path=None, after_backend="local") -> Chano`: mounts only a single route function with docs disabled (used by built Vercel entrypoints with `after_backend="wait_until"`).
- `create_queue_app(app_dir, function, *, after_backend="local")` and `create_schedule_app(app_dir, function, *, after_backend="local")`: single-function ASGI apps for deployed queue consumers and private schedules.
- `create_workflow_flow_app(entrypoints)` and `create_workflow_step_app(entrypoints)`: ASGI apps for deployed workflow flow and step functions.
- `invoke_queue(app_dir, topic, payload, *, consumer=None, headers=None)` and `invoke_schedule(app_dir, target, *, headers=None)`: async helpers to invoke local queues and schedules in tests.
- `load_config(pyproject) -> ChanoConfig`: reads and validates `[tool.chano]` and `[[tool.vercel.workflows]]` from a `pyproject.toml` path.
