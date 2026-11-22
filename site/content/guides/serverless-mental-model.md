---
title: Serverless mental model
description: Understand one-function-per-route serverless architecture and how Chano bridges FastAPI ergonomics with Vercel Functions.
lede: How Function-as-a-Service differs from a long-lived ASGI server, and how Chano adapts familiar Python API idioms to isolated functions.
section: guides
order: 2
nav_title: Serverless mental model
---

## From a long-lived server to isolated functions

If you have built APIs with FastAPI, you are used to a **long-lived server process**:

1. You create a single `app = FastAPI()` instance.
2. You attach all routes, middleware, and startup hooks to that object.
3. You run `uvicorn main:app` on a machine or container that stays alive listening on a port.

**Serverless** (Function-as-a-Service) flips this model around. Instead of one always-on process handling every endpoint, your application is compiled into **isolated functions** that a cloud platform runs on demand.

### Five concepts that shape your code

- **One function per route**: When a request arrives for `GET /api/items/42`, the platform invokes the function built specifically for `app/api/items/[item_id]/route.py`. Other routes live in separate functions with their own memory, timeouts, and scaling.
- **Cold starts vs. warm invocations**: If no instance of a function is running, the platform boots a fresh Python runtime, imports that route's code, and calls the handler (a cold start). Subsequent requests arriving shortly after reuse that warm process, skipping startup overhead.
- **Bundle size drives cold-start speed**: Each cold start loads the function's code and dependencies. Keeping unused libraries out of a route directly reduces bundle size and startup latency.
- **Ephemeral execution**: Once a response is sent, the platform may freeze or tear down the function container. Unawaited background tasks or threads can be paused or killed unless registered with a platform-aware hook like `wait_until` (which Chano wraps via `after()`).
- **CDNs serve static files**: Assets that do not change per request, including OpenAPI schemas and Swagger UI, belong on the edge CDN instead of waking up a Python function.

## How Chano bridges FastAPI and serverless

Chano keeps typed handler signatures, `Depends()`, automatic request validation, and OpenAPI docs while redesigning application structure around **one-function-per-route** deployments.

| Concept | FastAPI | Chano |
| --- | --- | --- |
| App entry point | `app = FastAPI()` | No central app object; routes live in `app/**/route.py` |
| Route registration | `@app.get("/items/{id}")` | `def GET(id: int):` in `app/items/[id]/route.py` |
| Validation and JSON | Pydantic | `msgspec` (with optional `pydantic.BaseModel` support) |
| Dependencies | `Depends(fn)` | `Depends(fn)` (per-request cached) + `lazy(factory)` |
| Router-level checks | `APIRouter(dependencies=[...])` | `dependencies.py` in any route folder |
| Middleware | `@app.middleware("http")` | `middleware.py` in any route folder |
| Error handlers | `@app.exception_handler(Exc)` | `error.py` in any route folder |
| Startup and lifespan | `@asynccontextmanager lifespan` | `lazy(factory)` (initializes on first use per warm instance) |
| Background tasks | `BackgroundTasks` | `after(fn, *args, **kwargs)` (`wait_until` in production) |
| OpenAPI and `/docs` | Generated at runtime in Python | Pre-rendered at build time to static CDN files |
| Deployment unit | One container running `uvicorn` | One isolated Vercel function per `route.py` |

## Why `msgspec` and a minimal ASGI core

FastAPI builds on Starlette and Pydantic. Chano implements a lightweight ASGI core directly and uses [`msgspec`](https://jcristharif.com/msgspec/) for request decoding, parameter coercion, JSON serialization, and OpenAPI 3.1 schema generation.

`msgspec` is a compact C extension that decodes and validates JSON in a single pass. If your codebase already uses `pydantic.BaseModel`, Chano detects and supports it automatically when `pydantic` is imported, without requiring Pydantic as a dependency for projects that do not use it.
