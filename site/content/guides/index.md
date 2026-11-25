---
title: Guides
description: Practical walkthroughs for building and deploying Python APIs with Chano on Vercel.
lede: Step-by-step guides covering local setup, the Vercel Functions model, application structure, and production deployment.
section: guides
order: 0
nav_title: Overview
---

## Recommended reading order

If you are new to Chano, read these guides in order:

1. [Getting started](/guides/getting-started): Create a minimal project with `uv`, write your first `route.py`, and run `chano dev`.
2. [Vercel Functions model](/guides/vercel-functions): Understand how one-function-per-route changes cold starts, bundling, and background work compared to a long-lived ASGI server.
3. [Application structure](/guides/application-structure): Organize routes with route groups, folder-scoped `dependencies.py`, `middleware.py`, and `error.py`, `lazy()`, `after()`, and Vercel Queues, Schedules, and Workflows.
4. [Outcomes and typed errors](/guides/outcomes): Declare status codes, structured error payloads, envelopes, and checked `Depends` in return annotations.
5. [Deploying to Vercel](/guides/deploying-to-vercel): Compile Build Output API v3 artifacts with `chano build`, inspect tree-shaking with `--explain`, deploy with `vercel deploy --prebuilt`, or run as a standard ASGI app elsewhere.

Looking for exact signatures, flags, or configuration keys? See the [Reference](/reference/).
