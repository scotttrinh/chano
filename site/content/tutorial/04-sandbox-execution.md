---
title: 4. Sandbox execution
description: Run assistant-generated Python in Vercel Sandbox using vercel-sandbox.
lede: Add an isolated /api/tools/run route with per-function memory and duration settings.
section: tutorial
order: 4
nav_title: 4. Sandbox execution
---

## 1. Create `backend/app/tools/run/route.py`

`vercel-sandbox` automatically authenticates using Vercel OIDC (`x-vercel-oidc-token` on deployments, `VERCEL_OIDC_TOKEN` from `.env.local` locally). Configure higher `max_duration` and `memory` on this route only:

```python
from msgspec import Struct
from vercel import sandbox

max_duration = 60
memory = 1024


class RunRequest(Struct):
    code: str
    file_urls: list[str] = []


class RunResponse(Struct):
    stdout: str
    stderr: str
    exit_code: int


async def POST(body: RunRequest) -> RunResponse:
    async with sandbox.create_sandbox() as box:
        result = await box.run_command(
            "python",
            ["-c", body.code],
            capture_output=True,
        )

    return RunResponse(
        stdout=(result.stdout or b"").decode(),
        stderr=(result.stderr or b"").decode(),
        exit_code=result.returncode,
    )
```
