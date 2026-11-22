---
title: Reference
description: Complete technical reference for Chano routing, parameters, responses, configuration, and CLI commands.
lede: Concise API and convention reference for Chano routes, validation, request/response primitives, pyproject.toml configuration, and CLI commands.
section: reference
order: 0
nav_title: Overview
---

## Reference sections

- [Routing and special files](/reference/routing): File-system route conventions, HTTP methods, match precedence, `middleware.py`, `error.py`, `dependencies.py`, and route-level options.
- [Parameters and validation](/reference/parameters-and-validation): Parameter source inference, `Query`, `Header`, `Cookie`, `Body`, `Depends`, `lazy`, `HTTPBearer`, `@raises`, and error types.
- [Requests, responses, and background work](/reference/requests-and-responses): `Request` and `Geo`, buffered and streaming responses, cookies, Server-Sent Events, and `after()`.
- [Configuration and CLI](/reference/configuration-and-cli): `[tool.chano]` settings, `chano dev`, `chano build`, Build Output API v3 layout, and programmatic app factories.

New to Chano? Start with the [Getting started guide](/guides/getting-started).
