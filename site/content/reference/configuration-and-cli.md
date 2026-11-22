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

## Programmatic factories

For testing (for example with `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`) or custom ASGI hosting:

- `create_app(app_dir="app", *, public_dir=None, base_path=None) -> Chano`: discovers all routes, enables `/docs` and `/openapi.json`, and serves optional `public_dir`.
- `create_route_app(app_dir, function, *, base_path=None, after_backend="local") -> Chano`: mounts only a single route function with docs disabled (used by built Vercel entrypoints with `after_backend="wait_until"`).
- `load_config(project_dir=".") -> ChanoConfig`: reads and validates `[tool.chano]` from `pyproject.toml`.
