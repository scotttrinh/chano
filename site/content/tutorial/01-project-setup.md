---
title: 1. Project setup
description: Create the full-stack Next.js and Chano workspace with Vercel Services.
lede: Set up a uv workspace with a Next.js frontend and a Chano backend routed under /api.
section: tutorial
order: 1
nav_title: 1. Project setup
---

## 1. Create the workspace layout

```text
ai-assistant/
├── vercel.json
├── pyproject.toml
├── backend/
│   ├── pyproject.toml
│   └── app/
│       └── health/
│           └── route.py
└── frontend/
```

## 2. Configure `vercel.json`

```json
{
  "services": {
    "web": { "root": "frontend/", "framework": "nextjs" },
    "api": {
      "root": "backend/",
      "framework": "vite",
      "buildCommand": "uv run --frozen chano build",
      "devCommand": "uv run --frozen chano dev --port $PORT"
    }
  },
  "rewrites": [
    { "source": "/api/:path*", "destination": { "service": "api" } },
    { "source": "/(.*)", "destination": { "service": "web" } }
  ]
}
```

## 3. Configure `pyproject.toml`

Root `pyproject.toml`:

```toml
[tool.uv.workspace]
members = ["backend"]
```

`backend/pyproject.toml`:

```toml
[project]
name = "assistant-api"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "chano",
    "httpx",
    "sqlstruct",
    "vercel-blob",
    "vercel-queue",
    "vercel-sandbox",
    "vercel-workflow",
]

[dependency-groups]
dev = ["chano[dev]"]

[tool.chano]
base_path = "/api"
```

## 4. Create the frontend and health route

```sh
npx create-next-app@latest frontend --ts --app --eslint --no-src-dir --yes
```

Create `backend/app/health/route.py`:

```python
def GET():
    return {"status": "ok"}
```

## 5. Install the Vercel CLI and create a project

```sh
npm i -g vercel
vercel login
vercel link
vercel env pull
```

Follow the prompts in `vercel link` to create and link a new Vercel project. `vercel env pull` writes `VERCEL_OIDC_TOKEN` to `.env.local` for local OIDC authentication with AI Gateway and Vercel Sandbox.

## 6. Run locally

```sh
uv sync
vercel dev --local
```

Verify `http://localhost:3000/api/health` returns `{"status": "ok"}` and `http://localhost:3000/api/docs` renders the Swagger UI.
