---
title: Reference
description: Complete technical reference for Chano routing, parameters, responses, configuration, and CLI commands.
lede: Concise API and convention reference for Chano routes, validation, request/response primitives, pyproject.toml configuration, and CLI commands.
section: reference
order: 0
nav_title: Overview
---

## Reference sections

- [Routing and special files](/reference/routing): File-system route conventions, HTTP methods, match precedence, `middleware.py`, `error.py`, `dependencies.py`, `queue.py`, `schedule.py`, route-level options, and `@openapi`.
- [Parameters and validation](/reference/parameters-and-validation): Parameter source inference, `Query`, `Header`, `Cookie`, `Body`, `Depends`, `lazy`, `HTTPBearer`, and `RequestValidationError`.
- [Requests, responses, and background work](/reference/requests-and-responses): `Request` and `Geo`, `Outcome`, `Failure`, `chano.status`, buffered and streaming responses, cookies, Server-Sent Events, and `after()`.
- [Configuration and CLI](/reference/configuration-and-cli): `[tool.chano]` and `[[tool.vercel.workflows]]` settings, `chano dev`, `chano build`, `chano queue`, `chano schedule`, and programmatic app factories.

New to Chano? Start with the [Getting started guide](/guides/getting-started).
