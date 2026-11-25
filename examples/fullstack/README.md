# fullstack

A Next.js frontend and a Chano backend deployed together as Vercel Services in
a uv workspace.

## Layout

- `vercel.json` — declares `web` (`frontend/`, Next.js) and `api` (`backend/`,
  Chano), routing `/api/:path*` to `api` and everything else to `web`.
- `pyproject.toml`, `uv.lock`, `.python-version` — uv workspace root pinned to
  Python 3.13 (matching the `python3.13` Vercel function runtime; `uv run` in a
  workspace reads `.python-version` here).
- `backend/` — Chano service with `base_path = "/api"`, `chano[dev]` in
  `[dependency-groups].dev` (so `chano dev` has `uvicorn` without vendoring
  it into built functions), a backup `backend/.python-version` (`3.13`) for
  standalone commands inside `backend/`, and routes under `app/` (`/api`,
  `/api/hello`, `/api/items/{item_id}`, `/api/docs`, `/api/openapi.json`,
  `/api/hello.txt`).
- `frontend/` — minimal Next.js App Router app serving `/`.

## Local development

Install frontend dependencies once, then run both services together with the
Vercel CLI:

```sh
npm --prefix frontend install
vercel dev --local
```

Visit `http://localhost:3000`:

- `/` is served by Next.js
- `/api`, `/api/hello?name=world`, `/api/items/42`, `/api/docs`,
  `/api/openapi.json`, and `/api/hello.txt` are served by Chano

You can also run or build the backend directly:

```sh
cd backend
uv run --frozen chano dev      # http://127.0.0.1:8000/api
uv run --frozen chano build    # writes backend/.vercel/output
```

## Build and deploy

Because `pyproject.toml` points `chano` at the repository root
(`chano = { path = "../..", editable = true }`), deploying from
`examples/fullstack/` alone will not upload `chano`. Deploy from the
repository root against a Vercel project whose Root Directory is set to
`examples/fullstack` with source files outside the root directory included (for
example, the `chano-spike` project in `scotttrinhs-projects`):

```sh
# From the repository root:
vercel project update chano-spike --scope scotttrinhs-projects \
  --root-directory examples/fullstack --source-files-outside-root-directory
vercel deploy -p chano-spike --scope scotttrinhs-projects
```

To verify a preview deployment with Deployment Protection enabled:

```sh
vercel curl /api --deployment <deployment-url> --scope scotttrinhs-projects
```

To deploy `examples/fullstack` as a standalone repository outside this repo,
either vendor Chano inside the project (for example
`chano = { path = "vendor/chano" }`) or remove `[tool.uv.sources]` once
`chano` is installed from PyPI, then regenerate `uv.lock` with `uv lock`.
