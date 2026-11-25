---
title: 2. Streaming chat
description: Stream AI Gateway completions from Chano using the AI SDK UI Message Stream protocol.
lede: Add a typed /api/chat endpoint that emits AI SDK SSE events for useChat in Next.js.
section: tutorial
order: 2
nav_title: 2. Streaming chat
---

## 1. Create `backend/app/chat/route.py`

Emit the AI SDK UI Message Stream (`x-vercel-ai-ui-message-stream: v1`) so `@ai-sdk/react` can parse messages natively. On Vercel deployments, the OIDC token arrives in the `x-vercel-oidc-token` request header; locally it comes from `VERCEL_OIDC_TOKEN` in `.env.local`:

```python
import os
import uuid
from collections.abc import AsyncIterator
from typing import Literal

import httpx
import msgspec
from msgspec import Struct

from chano import Request, SSEResponse, ServerSentEvent
from chano.status import InternalServerError

max_duration = 60

GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"


class UIPart(Struct):
    type: str
    text: str = ""


class UIMessage(Struct):
    role: Literal["system", "user", "assistant"]
    parts: list[UIPart]


class ChatRequest(Struct):
    messages: list[UIMessage]
    model: str = "deepseek/deepseek-v4.1-flash"


async def POST(
    request: Request, body: ChatRequest
) -> SSEResponse | InternalServerError:
    oidc_token = request.headers.get("x-vercel-oidc-token") or os.environ.get(
        "VERCEL_OIDC_TOKEN"
    )
    if not oidc_token:
        return InternalServerError("Missing Vercel OIDC token")

    payload = {
        "model": body.model,
        "stream": True,
        "messages": [
            {
                "role": m.role,
                "content": "".join(p.text for p in m.parts if p.type == "text"),
            }
            for m in body.messages
        ],
    }

    async def stream() -> AsyncIterator[dict | ServerSentEvent]:
        msg_id = f"msg_{uuid.uuid4().hex}"
        text_id = f"txt_{uuid.uuid4().hex}"
        yield {"type": "start", "messageId": msg_id}
        yield {"type": "text-start", "id": text_id}

        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST",
                GATEWAY_URL,
                headers={"Authorization": f"Bearer {oidc_token}"},
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line.removeprefix("data: ")
                    if raw == "[DONE]":
                        break
                    chunk = msgspec.json.decode(raw)
                    delta = (
                        chunk.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content")
                    )
                    if delta:
                        yield {"type": "text-delta", "id": text_id, "delta": delta}

        yield {"type": "text-end", "id": text_id}
        yield {"type": "finish"}
        yield ServerSentEvent(data="[DONE]")

    return SSEResponse(
        stream(),
        headers={"x-vercel-ai-ui-message-stream": "v1"},
    )
```

## 2. Install AI SDK packages in `frontend/`

```sh
npm --prefix frontend install ai @ai-sdk/react
```

## 3. Stream with `useChat` in `frontend/app/page.tsx`

```tsx
"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useState } from "react";

export default function Home() {
  const { messages, sendMessage, status } = useChat({
    transport: new DefaultChatTransport({ api: "/api/chat" }),
  });
  const [input, setInput] = useState("");

  return (
    <main>
      {messages.map((message) => (
        <div key={message.id}>
          <strong>{message.role}: </strong>
          {message.parts.map((part, i) =>
            part.type === "text" ? <span key={i}>{part.text}</span> : null,
          )}
        </div>
      ))}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!input.trim()) return;
          sendMessage({ text: input });
          setInput("");
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={status !== "ready"}
        />
        <button type="submit" disabled={status !== "ready"}>
          Send
        </button>
      </form>
    </main>
  );
}
```
