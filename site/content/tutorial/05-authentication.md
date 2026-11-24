---
title: 5. Authentication
description: Add sign up, sign in, and per-user chat history with sqlstruct, HTTPBearer, Depends(), boundary.py, and after().
lede: Persist users and per-user chat history with sqlstruct, protect routes with HTTPBearer and boundary.py, and record turns with after().
section: tutorial
order: 5
nav_title: 5. Authentication
---

## 1. Define `sqlstruct` models and lazy engine

Create `backend/db.py`:

```python
import os

from sqlstruct import Field, SQLStruct, create_engine

from chano import lazy


class User(SQLStruct, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_hash: str
    token: str = Field(unique=True, index=True)


class Chat(SQLStruct, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    title: str


class ChatMessage(SQLStruct, table=True):
    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(foreign_key="chat.id", index=True)
    role: str
    content: str


@lazy
def get_engine():
    url = os.environ.get("DATABASE_URL", "sqlite:///assistant.db")
    engine = create_engine(url)
    SQLStruct.metadata.create_all(engine)
    return engine
```

No separate migration step is needed locally: `SQLStruct.metadata.create_all(engine)` runs on the first request that calls `get_engine()` and creates `assistant.db` with the `user`, `chat`, and `chatmessage` tables. For deployed serverless functions, set `DATABASE_URL` to a hosted database.

## 2. Add sign up and sign in routes

Create `backend/app/auth/signup/route.py`:

```python
import hashlib
import secrets

from db import User, get_engine
from msgspec import Struct
from sqlstruct import Session, col, select

from chano.status import Conflict


class AuthRequest(Struct):
    email: str
    password: str


class AuthResponse(Struct):
    token: str


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def POST(body: AuthRequest) -> AuthResponse | Conflict:
    with Session(get_engine()) as session:
        existing = session.exec(
            select(User).where(col(User.email) == body.email)
        ).first()
        if existing is not None:
            return Conflict("Email already registered")

        token = secrets.token_urlsafe(32)
        user = User(
            email=body.email,
            password_hash=_hash_password(body.password),
            token=token,
        )
        session.add(user)
        session.commit()
    return AuthResponse(token=token)
```

Create `backend/app/auth/signin/route.py`:

```python
import hashlib
import hmac

from db import User, get_engine
from msgspec import Struct
from sqlstruct import Session, col, select

from chano.status import Unauthorized


class AuthRequest(Struct):
    email: str
    password: str


class AuthResponse(Struct):
    token: str


def _verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split(":", 1)
    expected = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1
    )
    return hmac.compare_digest(expected.hex(), digest_hex)


def POST(body: AuthRequest) -> AuthResponse | Unauthorized:
    with Session(get_engine()) as session:
        user = session.exec(
            select(User).where(col(User.email) == body.email)
        ).first()
        if user is None or not _verify_password(body.password, user.password_hash):
            return Unauthorized("Invalid credentials")
        return AuthResponse(token=user.token)
```

## 3. Add `backend/auth.py` and a route-group `boundary.py`

Put the shared `current_user` dependency in `backend/auth.py`:

```python
from db import User, get_engine
from sqlstruct import Session, col, select

from chano import Depends, HTTPBearer
from chano.status import Unauthorized

bearer = HTTPBearer()


def current_user(token: str = Depends(bearer)) -> User | Unauthorized:
    with Session(get_engine()) as session:
        user = session.exec(
            select(User).where(col(User.token) == token)
        ).first()
        if user is None:
            return Unauthorized("Invalid token")
        return user
```

Create `backend/app/(authed)/boundary.py` and move `backend/app/chat` into `backend/app/(authed)/chat`:

```sh
mkdir -p backend/app/'(authed)'
mv backend/app/chat backend/app/'(authed)'/chat
```

```python
from auth import current_user

from chano import Boundary

boundary = Boundary(guards=[current_user])
```

Parenthesized folders like `(authed)` do not add a URL segment, so `backend/app/(authed)/chat/route.py` still serves `/api/chat`. Every HTTP route under `(authed)` runs `current_user` before the handler and shares the per-request `Depends(current_user)` cache.

## 4. Add per-user chat creation and history

Create `backend/app/(authed)/chats/route.py`:

```python
from auth import current_user
from db import Chat, User, get_engine
from msgspec import Struct
from sqlstruct import Session, col, select

from chano import Depends


class CreateChatRequest(Struct):
    title: str = "New chat"


def GET(user: User = Depends(current_user)) -> list[Chat]:
    with Session(get_engine()) as session:
        return list(
            session.exec(
                select(Chat)
                .where(col(Chat.user_id) == user.id)
                .order_by(col(Chat.id))
            ).all()
        )


def POST(
    body: CreateChatRequest,
    user: User = Depends(current_user),
) -> Chat:
    assert user.id is not None
    with Session(get_engine()) as session:
        chat = Chat(user_id=user.id, title=body.title)
        session.add(chat)
        session.commit()
        session.refresh(chat)
        return chat
```

Create `backend/app/(authed)/chats/[chat_id]/route.py`:

```python
from auth import current_user
from db import Chat, ChatMessage, User, get_engine
from sqlstruct import Session, col, select

from chano import Depends
from chano.status import NotFound


def GET(
    chat_id: int,
    user: User = Depends(current_user),
) -> list[ChatMessage] | NotFound:
    with Session(get_engine()) as session:
        chat = session.exec(
            select(Chat)
            .where(col(Chat.id) == chat_id)
            .where(col(Chat.user_id) == user.id)
        ).first()
        if chat is None:
            return NotFound("Chat not found")

        return list(
            session.exec(
                select(ChatMessage)
                .where(col(ChatMessage.chat_id) == chat_id)
                .order_by(col(ChatMessage.id))
            ).all()
        )
```

## 5. Persist turns with `after()`

In `backend/app/(authed)/chat/route.py`, require `current_user`, verify `chat_id` belongs to the user, and record the message after returning:

```python
from auth import current_user
from db import Chat, ChatMessage, User, get_engine
from sqlstruct import Session, col, select

from chano import Depends, Request, SSEResponse, ServerSentEvent, after
from chano.status import InternalServerError, NotFound
```

Update `POST`:

```python
async def POST(
    request: Request,
    body: ChatRequest,
    chat_id: int,
    user: User = Depends(current_user),
) -> SSEResponse | NotFound | InternalServerError:
    with Session(get_engine()) as session:
        chat = session.exec(
            select(Chat)
            .where(col(Chat.id) == chat_id)
            .where(col(Chat.user_id) == user.id)
        ).first()
        if chat is None:
            return NotFound("Chat not found")

    # ... build payload and stream() ...

    def record_turn() -> None:
        if not body.messages:
            return
        with Session(get_engine()) as session:
            last = body.messages[-1]
            content = "".join(p.text for p in last.parts if p.type == "text")
            session.add(
                ChatMessage(chat_id=chat_id, role=last.role, content=content)
            )
            session.commit()

    after(record_turn)
    return SSEResponse(
        stream(),
        headers={"x-vercel-ai-ui-message-stream": "v1"},
    )
```

## 6. Wire auth and `chat_id` into `frontend/app/page.tsx`

Update `frontend/app/page.tsx` to sign up or sign in, create a chat via `POST /api/chats`, and pass `Authorization: Bearer <token>` and `chat_id` to `useChat`:

```tsx
"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useMemo, useState } from "react";

export default function Home() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState<string | null>(null);
  const [chatId, setChatId] = useState<number | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [input, setInput] = useState("");

  const transport = useMemo(
    () =>
      new DefaultChatTransport({
        api: chatId ? `/api/chat?chat_id=${chatId}` : "/api/chat",
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      }),
    [chatId, token],
  );

  const { messages, sendMessage, status } = useChat({ transport });

  async function authenticate(mode: "signup" | "signin") {
    setAuthError(null);
    const authRes = await fetch(`/api/auth/${mode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!authRes.ok) {
      setAuthError(await authRes.text());
      return;
    }
    const { token: nextToken } = await authRes.json();
    const chatRes = await fetch("/api/chats", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${nextToken}`,
      },
      body: JSON.stringify({ title: "New chat" }),
    });
    const chat = await chatRes.json();
    setToken(nextToken);
    setChatId(chat.id);
  }

  if (!token || !chatId) {
    return (
      <main>
        <input
          type="email"
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="button" onClick={() => authenticate("signup")}>
          Sign up
        </button>
        <button type="button" onClick={() => authenticate("signin")}>
          Sign in
        </button>
        {authError && <p>{authError}</p>}
      </main>
    );
  }

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
