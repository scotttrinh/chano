---
title: 6. Durable workflows
description: Trigger and poll long-running analysis reports with vercel-workflow.
lede: Offload multi-file report generation to a durable Vercel Workflow and expose status endpoints in Chano.
section: tutorial
order: 6
nav_title: 6. Durable workflows
---

## 1. Create `backend/workflows.py` and register it

```python
from vercel import workflow

wf = workflow.Workflows(
    sandbox_policy=workflow.SandboxPolicy(share_sandboxes=True),
)


@wf.step
async def analyze_files(prompt: str, file_urls: list[str]) -> str:
    # Run sandbox analysis across file_urls and return markdown summary
    return f"Report for {len(file_urls)} file(s): {prompt}"


@wf.workflow
async def generate_report(
    *,
    user_id: int,
    prompt: str,
    file_urls: list[str],
) -> dict[str, str]:
    summary = await analyze_files(prompt=prompt, file_urls=file_urls)
    return {"summary": summary}
```

Register the workflow entrypoint in `backend/pyproject.toml`:

```toml
[[tool.vercel.workflows]]
entrypoint = "workflows:wf"
```

## 2. Create `backend/app/(authed)/reports/route.py`

```python
from auth import current_user
from db import User
from msgspec import Struct
from vercel import workflow
from workflows import generate_report

from chano import Depends
from chano.status import Accepted


class ReportRequest(Struct):
    prompt: str
    file_urls: list[str]


class ReportCreated(Struct):
    job_id: str
    status: str


async def POST(
    body: ReportRequest,
    user: User = Depends(current_user),
) -> Accepted[ReportCreated]:
    assert user.id is not None
    run = await workflow.start(
        generate_report,
        user_id=user.id,
        prompt=body.prompt,
        file_urls=body.file_urls,
    )
    return Accepted(
        ReportCreated(job_id=run.run_id, status=str(await run.status()))
    )
```

## 3. Create `backend/app/(authed)/reports/[job_id]/route.py`

```python
from typing import Any

from auth import current_user
from db import User
from msgspec import Struct
from vercel import workflow

from chano import Depends


class ReportStatus(Struct):
    job_id: str
    status: str
    result: Any = None


async def GET(
    job_id: str,
    user: User = Depends(current_user),
) -> ReportStatus:
    run = workflow.Run(job_id)
    status = await run.status()
    result = await run.return_value() if status == "completed" else None
    return ReportStatus(job_id=job_id, status=str(status), result=result)
```

## 4. Run locally and build

`chano dev` automatically mounts `LocalWorld` (`/.well-known/workflow/v1/{flow,step}`) for any `[[tool.vercel.workflows]]` in `pyproject.toml`. `chano build` emits isolated `.well-known/workflow/v1/{flow,step}.func` functions with their `experimentalTriggers`:

```sh
uv run --frozen chano build
vercel deploy --prebuilt
```
