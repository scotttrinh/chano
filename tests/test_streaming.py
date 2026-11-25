import asyncio
import re
import time

import msgspec
import pytest

from chano import (
    ServerSentEvent,
    SSEResponse,
    StreamingResponse,
)
from tests.helpers import (
    body_messages,
    call_asgi,
    stream_app,
    stream_scope,
    wait_for_disconnect,
)


async def async_chunks():
    for chunk in (b"one ", "two ", b"three"):
        await asyncio.sleep(0)
        yield chunk


def sync_chunks():
    yield from (b"one ", "two ", b"three")


# @lat: [[tests#Streaming responses]]
@pytest.mark.parametrize(
    "make_content",
    [async_chunks, sync_chunks, lambda: [b"one ", "two ", b"three"]],
    ids=["async-generator", "sync-generator", "list"],
)
def test_streaming_response_streams_chunks_in_order(make_content):
    app = stream_app(
        lambda: StreamingResponse(
            make_content(),
            status_code=201,
            headers={"Content-Length": "99", "X-Trace": "t"},
            media_type="text/plain",
        )
    )
    sent = call_asgi(app, wait_for_disconnect)
    start = sent[0]
    assert start["status"] == 201
    headers = dict(start["headers"])
    assert b"content-length" not in headers
    assert headers[b"x-trace"] == b"t"
    assert headers[b"content-type"] == b"text/plain; charset=utf-8"
    bodies = body_messages(sent)
    assert [m["body"] for m in bodies] == [b"one ", b"two ", b"three", b""]
    assert [m["more_body"] for m in bodies] == [True, True, True, False]

    # HEAD sends headers and an empty final body without running the producer.
    consumed = []

    def tracked():
        consumed.append(True)
        yield b"never sent"

    head = call_asgi(
        stream_app(lambda: StreamingResponse(tracked())), wait_for_disconnect, "HEAD"
    )
    assert consumed == []
    assert body_messages(head) == [
        {"type": "http.response.body", "body": b"", "more_body": False}
    ]
    assert b"content-length" not in dict(head[0]["headers"])

    class NoIter:
        def __iter__(self):
            raise AssertionError("__iter__ called for HEAD")

    class NoAiter:
        def __aiter__(self):
            raise AssertionError("__aiter__ called for HEAD")

    for make in (
        lambda: StreamingResponse(NoIter()),
        lambda: StreamingResponse(NoAiter()),
        lambda: SSEResponse(NoIter()),
        lambda: SSEResponse(NoAiter()),
    ):
        head = call_asgi(stream_app(make), wait_for_disconnect, "HEAD")
        assert head[0]["type"] == "http.response.start"
        assert body_messages(head) == [
            {"type": "http.response.body", "body": b"", "more_body": False}
        ]


def endless_sync(closed: list):
    try:
        n = 0
        while True:
            time.sleep(0.001)
            yield f"{n},"
            n += 1
    finally:
        closed.append(True)


async def endless_async(closed: list):
    try:
        n = 0
        while True:
            await asyncio.sleep(0.001)
            yield f"{n},"
            n += 1
    finally:
        closed.append(True)


# @lat: [[tests#Streaming disconnect]]
@pytest.mark.parametrize(
    "producer", [endless_async, endless_sync], ids=["async", "sync"]
)
def test_streaming_response_disconnect_closes_generator(producer):
    closed: list = []
    disconnect: list[asyncio.Event] = []

    async def receive():
        # A request body message first, then wait until a few chunks went out.
        if not disconnect:
            disconnect.append(asyncio.Event())
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect[0].wait()
        return {"type": "http.disconnect"}

    def on_send(sent):
        if len(body_messages(sent)) == 3:
            disconnect[0].set()

    app = stream_app(lambda: StreamingResponse(producer(closed)))
    sent = call_asgi(app, receive, on_send=on_send)
    assert closed == [True]
    bodies = body_messages(sent)
    assert len(bodies) >= 3
    assert all(m["more_body"] for m in bodies)
    chunks = b"".join(m["body"] for m in bodies).decode().rstrip(",").split(",")
    assert chunks == [str(n) for n in range(len(chunks))]


class Point(msgspec.Struct):
    x: int
    y: int


# @lat: [[tests#SSE framing]]
def test_sse_response_framing_and_pings():
    state: dict = {}

    async def events():
        yield ServerSentEvent(
            data="multi\nline\r\nevent", event="update", id="7", retry=1500
        )
        yield "plain"
        yield {"a": [1, "two"]}
        yield Point(x=1, y=2)
        yield ServerSentEvent(event="empty")
        state["resume"] = asyncio.Event()
        await state["resume"].wait()
        yield "done"

    def on_send(sent):
        if sent[-1].get("body") == b": ping\n\n" and "resume" in state:
            state["resume"].set()

    app = stream_app(lambda: SSEResponse(events(), ping=0.01))
    sent = call_asgi(app, wait_for_disconnect, on_send=on_send)
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"text/event-stream"
    assert headers[b"cache-control"] == b"no-cache"
    assert b"content-length" not in headers
    body = b"".join(m["body"] for m in body_messages(sent)).decode()
    expected_head = (
        "event: update\nid: 7\nretry: 1500\n"
        "data: multi\ndata: line\ndata: event\n\n"
        "data: plain\n\n"
        'data: {"a":[1,"two"]}\n\n'
        'data: {"x":1,"y":2}\n\n'
        "event: empty\n\n"
    )
    assert body.startswith(expected_head)
    rest = body[len(expected_head) :]
    assert re.fullmatch(r"(: ping\n\n)+data: done\n\n", rest)

    # Sync iterables work too, without pings.
    sync_sent = call_asgi(
        stream_app(lambda: SSEResponse(["a", 1])), wait_for_disconnect
    )
    sync_body = b"".join(m["body"] for m in body_messages(sync_sent))
    assert sync_body == b"data: a\n\ndata: 1\n\n"

    # A raw `None` is JSON `null`; only a `ServerSentEvent` without data omits it.
    nulls = [None, ServerSentEvent(data=None), ServerSentEvent(event="tick")]
    null_sent = call_asgi(stream_app(lambda: SSEResponse(nulls)), wait_for_disconnect)
    null_body = b"".join(m["body"] for m in body_messages(null_sent))
    assert null_body == b"data: null\n\ndata: null\n\nevent: tick\n\n"
    assert ServerSentEvent().encode() == b"\n\n"

    # Newlines in single-line fields would break framing.
    with pytest.raises(ValueError):
        ServerSentEvent(data="x", event="a\nb")
    with pytest.raises(ValueError):
        ServerSentEvent(data="x", id="1\r2")


# @lat: [[tests#Streaming fast producer disconnect]]
def test_streaming_response_disconnect_interrupts_fast_producer():
    """Neither the producer nor ``send`` suspends, yet a disconnect wins."""
    closed: list = []

    async def fast():
        try:
            for n in range(10_000):
                yield f"{n},"
        finally:
            closed.append(True)

    ready: list = []

    async def receive():
        while not ready:
            await asyncio.sleep(0)
        return {"type": "http.disconnect"}

    def on_send(sent):
        ready.append(True)

    sent = call_asgi(
        stream_app(lambda: StreamingResponse(fast())), receive, on_send=on_send
    )
    bodies = body_messages(sent)
    assert len(bodies) < 100
    assert all(m["more_body"] for m in bodies)
    assert closed == [True]


# @lat: [[tests#Streaming receive errors]]
def test_streaming_response_propagates_receive_errors():
    closed: list = []

    async def receive():
        raise RuntimeError("receive failed")

    app = stream_app(lambda: StreamingResponse(endless_async(closed)))
    with pytest.raises(RuntimeError, match="receive failed"):
        call_asgi(app, receive)
    assert closed == [True]


# @lat: [[tests#Streaming cancellation during cleanup]]
def test_streaming_response_cancel_during_disconnect_cleanup():
    async def run():
        disconnect = asyncio.Event()
        cleanup_started = asyncio.Event()
        release = asyncio.Event()
        cleaned: list = []

        async def producer():
            try:
                yield "first"
                await asyncio.Event().wait()  # suspended inside the generator
            finally:
                cleanup_started.set()
                await release.wait()
                cleaned.append(True)

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message.get("body") == b"first":
                disconnect.set()

        app = stream_app(lambda: StreamingResponse(producer()))
        request = asyncio.ensure_future(app(stream_scope(), receive, send))
        await cleanup_started.wait()
        request.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not request.done(), "returned before the producer finished cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert cleaned == [True]

    asyncio.run(asyncio.wait_for(run(), timeout=5))


# @lat: [[tests#SSE headers and ping validation]]
def test_sse_response_enforces_headers_and_validates_ping():
    response = SSEResponse(
        ["a"],
        headers={
            "Content-Type": "text/plain",
            "Cache-Control": "max-age=60",
            "X-Extra": "1",
        },
    )
    sent = call_asgi(stream_app(lambda: response), wait_for_disconnect)
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"text/event-stream"
    assert headers[b"cache-control"] == b"no-cache"
    assert headers[b"x-extra"] == b"1"

    for ping in (0, -1, 0.0, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="ping"):
            SSEResponse(["a"], ping=ping)
    SSEResponse(["a"], ping=None)
    SSEResponse(["a"], ping=0.5)
