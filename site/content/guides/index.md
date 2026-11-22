---
title: Guides
description: Practical walkthroughs for building and deploying serverless Python APIs with Chano on Vercel.
lede: Step-by-step guides covering local setup, the serverless mental model, application structure, and production deployment on Vercel.
section: guides
order: 0
nav_title: Overview
---

## Recommended reading order

If you are new to Chano or coming from FastAPI, read these guides in order:

1. [Getting started](/guides/getting-started): Create a minimal project with `uv`, write your first `route.py`, and run `chano dev`.
2. [Serverless mental model](/guides/serverless-mental-model): Understand how one-function-per-route changes cold starts, bundling, and background work compared to a long-lived FastAPI server.
3. [Application structure](/guides/application-structure): Organize routes with route groups, folder-scoped `dependencies.py`, `middleware.py`, and `error.py`, plus `lazy()` and `after()`.
4. [Deploying to Vercel](/guides/deploying-to-vercel): Compile Build Output API v3 artifacts with `chano build`, inspect tree-shaking with `--explain`, and deploy with `vercel deploy --prebuilt`.

Looking for exact signatures, flags, or configuration keys? See the [Reference](/reference/).
