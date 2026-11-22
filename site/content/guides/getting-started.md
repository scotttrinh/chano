---
title: Getting started
description: Create a Chano project with uv, define your first route, and run the local development server.
lede: Set up a Python 3.12+ Chano project with uv, write typed HTTP handlers, and explore the generated Swagger UI locally.
section: guides
order: 1
nav_title: Getting started
---

## Prerequisites

Chano targets **Python 3.12+** and uses [`uv`](https://docs.astral.sh/uv/) for dependency resolution and vendoring during production builds.

## Project layout

A minimal Chano project has a `pyproject.toml`, an `app/` directory for routes, and an optional `public/` directory for static assets:

```text
my-app/
├── pyproject.toml
├── app/
│   ├── route.py
│   └── api/
│       └── items/
│           └── [item_id]/
│               └── route.py
└── public/
    └── hello.txt
```

## Configure `pyproject.toml`

Declare `chano` as a runtime dependency and `chano[dev]` in `[dependency-groups].dev`:

```toml
[project]
name = "my-app"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["chano"]

[dependency-groups]
dev = ["chano[dev]"]
```

Keeping `chano[dev]` in `[dependency-groups].dev` installs `uvicorn` for `chano dev` without vendoring it into deployed Vercel functions, which run on `vercel-runtime`.

Install dependencies and create a lockfile:

```sh
uv sync
```

## Write your first routes

Each `route.py` exports top-level functions named after HTTP methods (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `HEAD`). Handlers can be sync (`def`) or async (`async def`).

Start with a root endpoint in `app/route.py`:

```python
# app/route.py
def GET():
    return {"message": "Hello from Chano"}
```

Add a dynamic route with typed validation in `app/api/items/[item_id]/route.py`:

```python
# app/api/items/[item_id]/route.py
from typing import Annotated

from msgspec import Meta, Struct

from chano import Cookie, Header


class Item(Struct):
    name: str
    price: Annotated[float, Meta(gt=0)]
    tags: list[str] = []


def GET(item_id: int, verbose: bool = False):
    """Fetch an item."""
    return {"item_id": item_id, "verbose": verbose}


async def PUT(
    item_id: int,
    item: Item,
    x_trace_id: Annotated[str | None, Header()] = None,
    session: Annotated[str | None, Cookie()] = None,
) -> Item:
    """Update an item."""
    return item
```

Chano infers where each parameter comes from based on its name, type, and markers:

- `item_id` matches the `[item_id]` folder, so it comes from the URL path.
- `verbose` is a scalar not in the path, so it defaults to a query parameter.
- `item` is a `msgspec.Struct`, so it is decoded and validated from the JSON body.
- `x_trace_id` and `session` use explicit `Header()` and `Cookie()` markers.

Invalid inputs automatically return `422 Unprocessable Entity` with field-level error locations.

## Run locally

Start the development server with hot reload:

```sh
uv run chano dev
```

Your local server exposes:

- API routes at `http://127.0.0.1:8000`
- Interactive Swagger UI at `http://127.0.0.1:8000/docs`
- OpenAPI 3.1 schema at `http://127.0.0.1:8000/openapi.json`
- Files from `public/` at the root path (for example, `http://127.0.0.1:8000/hello.txt`)

## Next steps

- Read the [Serverless mental model](/guides/serverless-mental-model) to see how Chano maps FastAPI idioms to isolated functions.
- Learn how to organize shared auth, middleware, and error handling in [Application structure](/guides/application-structure).
