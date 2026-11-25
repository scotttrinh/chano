---
title: 3. File uploads
description: Upload files to Vercel Blob with vercel-blob and pass attachment URLs to /api/chat.
lede: Add an /api/uploads endpoint backed by vercel-blob and reference uploaded files in chat prompts.
section: tutorial
order: 3
nav_title: 3. File uploads
---

## 1. Configure your Blob store

Create a Vercel Blob store for your project, then pull environment variables (`vercel env pull`) or add `BLOB_STORE_ID` to `.env.local`:

```sh
BLOB_STORE_ID=store_...
```

`vercel-blob` authenticates automatically using `BLOB_STORE_ID` and Vercel OIDC (`x-vercel-oidc-token` on deployments, `VERCEL_OIDC_TOKEN` from `.env.local` locally).

## 2. Create `backend/app/uploads/route.py`

Stream the incoming request body directly to Vercel Blob with `request.stream()` and `put_blob()` without buffering the file in memory:

```python
from typing import Annotated

from msgspec import Meta, Struct
from vercel.blob import put_blob

from chano import Header, Request


class UploadResponse(Struct):
    url: str
    pathname: str


async def POST(
    request: Request,
    content_length: Annotated[int, Meta(gt=0), Header()],
    filename: str = "upload.bin",
) -> UploadResponse:
    result = await put_blob(
        filename,
        request.stream(),
        access="private",
        content_length=content_length,
        content_type=request.headers.get(
            "content-type", "application/octet-stream"
        ),
        add_random_suffix=True,
    )
    return UploadResponse(url=result.url, pathname=result.pathname)
```

## 3. Accept `file_urls` in `backend/app/chat/route.py`

Update `ChatRequest` to include uploaded Blob URLs:

```python
class ChatRequest(Struct):
    messages: list[UIMessage]
    file_urls: list[str] = []
    model: str = "deepseek/deepseek-v4.1-flash"
```

Inject attachment URLs into the system message before calling AI Gateway:

```python
    messages = [
        {
            "role": m.role,
            "content": "".join(p.text for p in m.parts if p.type == "text"),
        }
        for m in body.messages
    ]
    if body.file_urls:
        messages.insert(
            0,
            {
                "role": "system",
                "content": "Attached file URLs:\n" + "\n".join(body.file_urls),
            },
        )
```
