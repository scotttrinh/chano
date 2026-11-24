---
title: Vercel Functions model
description: Understand Chano's one-function-per-route architecture for Vercel Build Output API v3, and how to run the whole app as ASGI elsewhere.
lede: How Vercel Functions differ from a long-lived ASGI server, how Chano adapts familiar Python API patterns to isolated functions, and when to run as a single ASGI app.
section: guides
order: 2
nav_title: Vercel Functions model
---

## From a long-lived server to Vercel Functions

In a traditional Python web app, a single **long-lived server process** handles every endpoint:

1. You create one central application object.
2. You attach all routes, middleware, and startup hooks to that object.
3. You run `uvicorn main:app` on a machine or container that stays alive listening on a port.

Chano is built around **Vercel's Build Output API v3**. Instead of one always-on process handling every endpoint, `chano build` compiles each `route.py` into its own isolated **Vercel Function**.

### Five concepts that shape your code

- **One function per route**: When a request arrives for `GET /api/items/42`, Vercel invokes the function built specifically for `app/api/items/[item_id]/route.py`. Other routes live in separate functions with their own memory, timeouts, and scaling.
- **Cold starts vs. warm invocations**: If no instance of a function is running, Vercel boots a fresh Python runtime, imports that route's code, and calls the handler (a cold start). Subsequent requests arriving shortly after reuse that warm process, skipping startup overhead.
- **Bundle size drives cold-start speed**: Each cold start loads the function's code and vendored dependencies. Keeping unused libraries out of a route directly reduces bundle size and startup latency.
- **Ephemeral execution**: Once a response is sent, Vercel may freeze or tear down the function container. Unawaited background tasks or threads can be paused or killed unless registered with `vercel.functions.wait_until` (which Chano wraps via `after()`).
- **Static CDN assets**: Assets that do not change per request, including OpenAPI schemas and Swagger UI, are served directly from Vercel's CDN instead of waking up a Python function.

## How Chano maps familiar patterns to Vercel

Chano keeps familiar typed handler signatures, `Depends()`, automatic request validation, and OpenAPI docs while structuring your project around **one-function-per-route** builds.

| Concept | Long-lived ASGI app | Chano on Vercel |
| --- | --- | --- |
| App entry point | Central app object | No central app object; routes live in `app/**/route.py` |
| Route registration | Route decorators on app or router | `def GET(id: int):` in `app/items/[id]/route.py` |
| Validation and JSON | Pydantic | `msgspec` (with optional `pydantic.BaseModel` support) |
| Dependencies | `Depends(fn)` | `Depends(fn)` (per-request cached) + `lazy(factory)` |
| Router-level checks | Router dependency lists | `dependencies.py` in any route folder |
| Middleware | App-wide HTTP middleware | `middleware.py` in any route folder |
| Error handlers | App-wide exception handlers | `error.py` in any route folder |
| Startup and lifespan | Lifespan context manager | `lazy(factory)` (initializes on first use per warm instance) |
| Background tasks | In-process background tasks | `after(fn, *args, **kwargs)` (`wait_until` in production) |
| Queue workers | Separate worker service / broker | `app/**/queue.py` (`CONSUME`) compiled to private `_queues/` functions |
| Scheduled jobs | External cron runner | `app/**/schedule.py` (`RUN`) compiled to private `_schedules/` functions |
| Durable workflows | External workflow engine | `[[tool.vercel.workflows]]` compiled to `.well-known/workflow/v1/` functions |
| OpenAPI and `/docs` | Generated at runtime in Python | Pre-rendered at build time to static CDN files |
| Deployment unit | One container running `uvicorn` | One isolated Vercel Function per route, queue, schedule, or workflow |

## Why `msgspec` and a minimal ASGI core

Chano implements a lightweight ASGI core directly and uses [`msgspec`](https://jcristharif.com/msgspec/) for request decoding, parameter coercion, JSON serialization, and OpenAPI 3.1 schema generation.

`msgspec` is a compact C extension that decodes and validates JSON in a single pass. If your codebase already uses `pydantic.BaseModel`, Chano detects and supports it automatically when `pydantic` is imported, without requiring Pydantic as a dependency for projects that do not use it.

## Running as a single ASGI app

Although `chano build` targets Vercel's Build Output API v3, every Chano project is still a standard ASGI application underneath. If you want to deploy to another platform — a container, VM, or any Python ASGI host — call `create_app()` to mount all routes, `/docs`, `/openapi.json`, and `public/` into one ASGI app and serve it with Uvicorn, Hypercorn, or Granian. See [Running on other platforms (ASGI)](/guides/deploying-to-vercel#running-on-other-platforms-asgi).
