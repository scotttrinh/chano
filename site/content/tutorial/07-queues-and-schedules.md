---
title: 7. Queues & schedules
description: Process background events with queue.py and run periodic maintenance with schedule.py.
lede: Add a native Vercel Queue consumer and a scheduled cleanup job using Chano's queue.py and schedule.py conventions.
section: tutorial
order: 7
nav_title: 7. Queues & schedules
---

## 1. Add a usage queue consumer

Create `backend/app/queues/usage/queue.py`:

```python
from db import get_engine
from msgspec import Struct
from sqlstruct import Session

topic = "usage-events"
consumer = "usage-worker"
retry_after = 5
max_concurrency = 1


class UsageEvent(Struct):
    user_id: int
    chat_id: int
    model: str


async def CONSUME(event: UsageEvent) -> None:
    with Session(get_engine()) as session:
        # Persist or aggregate usage metrics for event.user_id
        _ = session
```

Test the consumer locally:

```sh
uv run chano queue send usage-events '{"user_id": 1, "chat_id": 1, "model": "deepseek/deepseek-v4.1-flash"}'
```

## 2. Add a nightly cleanup schedule

Create `backend/app/schedules/cleanup/schedule.py`:

```python
from db import Chat, get_engine
from sqlstruct import Session, select

schedule = "0 0 * * *"
timezone = "UTC"


def RUN() -> dict[str, int]:
    with Session(get_engine()) as session:
        chats = session.exec(select(Chat)).all()
        return {"checked": len(chats)}
```

Run the schedule locally:

```sh
uv run chano schedule run schedules/cleanup
```

## 3. Build and deploy

`chano build` emits `_queues/queues/usage.func` (`queue/v2beta` trigger), `_schedules/schedules/cleanup.func` (`schedule/v1beta` trigger), and the `schedules` map in `.vercel/output/config.json`:

```sh
uv run --frozen chano build
vercel deploy --prebuilt
```
