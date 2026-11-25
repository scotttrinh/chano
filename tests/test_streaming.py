import asyncio
import json
import re
import time

import msgspec
import pytest

from chano import (
    Chano,
    ClientDisconnect,
    Request,
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


# @lat: [[tests#Streaming responses#Streams chunks in order]]
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


def _tracked_generator(called: list[str]):
    called.append("next")
    raise AssertionError("generator stepped for HEAD")
    yield b"never sent"


class _TrackedNoIter:
    def __init__(self, called: list[str]) -> None:
        self._called = called

    def __iter__(self):
        self._called.append("iter")
        raise AssertionError("__iter__ called for HEAD")


class _TrackedNoAiter:
    def __init__(self, called: list[str]) -> None:
        self._called = called

    def __aiter__(self):
        self._called.append("aiter")
        raise AssertionError("__aiter__ called for HEAD")


_SSE_DEFAULT_HEADERS = {
    b"content-type": b"text/event-stream",
    b"cache-control": b"no-cache",
}


# @lat: [[tests#Streaming responses#HEAD skips producer]]
@pytest.mark.parametrize(
    ("make_response", "expected_headers"),
    [
        pytest.param(
            lambda called: StreamingResponse(_tracked_generator(called)),
            {},
            id="streaming-generator",
        ),
        pytest.param(
            lambda called: StreamingResponse(_TrackedNoIter(called)),
            {},
            id="streaming-sync-no-iter",
        ),
        pytest.param(
            lambda called: StreamingResponse(_TrackedNoAiter(called)),
            {},
            id="streaming-async-no-aiter",
        ),
        pytest.param(
            lambda called: SSEResponse(_TrackedNoIter(called)),
            _SSE_DEFAULT_HEADERS,
            id="sse-sync-no-iter",
        ),
        pytest.param(
            lambda called: SSEResponse(_TrackedNoAiter(called)),
            _SSE_DEFAULT_HEADERS,
            id="sse-async-no-aiter",
        ),
    ],
)
def test_streaming_head_skips_producer(make_response, expected_headers):
    called: list[str] = []
    head = call_asgi(
        stream_app(lambda: make_response(called)), wait_for_disconnect, "HEAD"
    )
    assert called == []
    assert head[0]["type"] == "http.response.start"
    assert head[0]["status"] == 200
    headers = dict(head[0]["headers"])
    assert headers == expected_headers
    assert b"content-length" not in headers
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
    with pytest.raises(ValueError, match="SSE event must not contain a newline"):
        ServerSentEvent(data="x", event="a\nb")
    with pytest.raises(ValueError, match="SSE id must not contain a newline"):
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


# @lat: [[tests#SSE headers and ping validation#Enforces SSE headers]]
def test_sse_response_enforces_headers():
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
    SSEResponse(["a"], ping=None)
    SSEResponse(["a"], ping=0.5)


# @lat: [[tests#SSE headers and ping validation#Rejects invalid ping]]
@pytest.mark.parametrize(
    "ping",
    [0, -1, 0.0, float("nan"), float("inf"), float("-inf")],
    ids=["zero", "negative", "zero-float", "nan", "inf", "neg-inf"],
)
def test_sse_response_rejects_invalid_ping(ping):
    with pytest.raises(ValueError, match="ping"):
        SSEResponse(["a"], ping=ping)


def _post_app(handler) -> Chano:
    app = Chano(docs=False)
    app.add_route("/s", handler, ["POST"])
    return app


def _json_body(sent: list[dict]):
    raw = b"".join(m["body"] for m in body_messages(sent))
    return json.loads(raw)


def _messages_receive(messages: list[dict]):
    it = iter(messages)

    async def receive():
        return next(it)

    return receive


# @lat: [[tests#Request body streaming#Streams chunks incrementally]]
def test_request_stream_incremental(tmp_path):
    """Each chunk reaches the handler before the next chunk is sent."""
    target = tmp_path / "upload.bin"
    release_next: list[asyncio.Event] = []
    seen_order: list[str] = []

    async def handler(request: Request):
        chunks: list[str] = []
        with target.open("wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                seen_order.append(f"read:{chunk.decode()}")
                chunks.append(chunk.decode())
                release_next[-1].set()
        return {"chunks": chunks}

    incoming: list[tuple[bytes, bool]] = [
        (b"part1-", True),
        (b"", True),
        (b"part2-", True),
        (b"part3", False),
    ]
    index = 0

    async def receive():
        nonlocal index
        body, more_body = incoming[index]
        index += 1
        if body:
            if release_next:
                await release_next[-1].wait()
            release_next.append(asyncio.Event())
            seen_order.append(f"send:{body.decode()}")
        return {"type": "http.request", "body": body, "more_body": more_body}

    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert sent[0]["status"] == 200
    assert _json_body(sent) == {"chunks": ["part1-", "part2-", "part3"]}
    assert target.read_bytes() == b"part1-part2-part3"
    assert seen_order == [
        "send:part1-",
        "read:part1-",
        "send:part2-",
        "read:part2-",
        "send:part3",
        "read:part3",
    ]


# @lat: [[tests#Request body streaming#Empty body yields zero chunks]]
def test_request_stream_empty_body():
    async def handler(request: Request):
        chunks = [chunk.decode() async for chunk in request.stream()]
        return {"chunks": chunks}

    receive = _messages_receive(
        [{"type": "http.request", "body": b"", "more_body": False}]
    )
    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert sent[0]["status"] == 200
    assert _json_body(sent) == {"chunks": []}


# @lat: [[tests#Request body streaming#Stream after body yields cached bytes]]
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(b"hello world", ["hello world"], id="non-empty"),
        pytest.param(b"", [], id="empty"),
    ],
)
def test_request_stream_after_body_uses_cache(payload, expected):
    async def handler(request: Request):
        raw = await request.body()
        first = [chunk.decode() async for chunk in request.stream()]
        second = [chunk.decode() async for chunk in request.stream()]
        return {"body": raw.decode(), "first": first, "second": second}

    receive = _messages_receive(
        [{"type": "http.request", "body": payload, "more_body": False}]
    )
    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert sent[0]["status"] == 200
    assert _json_body(sent) == {
        "body": payload.decode(),
        "first": expected,
        "second": expected,
    }


async def _consume_stream_twice(request: Request) -> None:
    _ = [chunk async for chunk in request.stream()]
    _ = [chunk async for chunk in request.stream()]


async def _body_after_stream(request: Request) -> None:
    _ = [chunk async for chunk in request.stream()]
    await request.body()


# @lat: [[tests#Request body streaming#Consumed stream raises RuntimeError]]
@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(_consume_stream_twice, id="stream-then-stream"),
        pytest.param(_body_after_stream, id="stream-then-body"),
    ],
)
def test_request_stream_consumed_raises(reader):
    async def handler(request: Request):
        try:
            await reader(request)
        except RuntimeError as exc:
            return {"error": str(exc)}
        return {"error": None}

    receive = _messages_receive(
        [{"type": "http.request", "body": b"abc", "more_body": False}]
    )
    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert sent[0]["status"] == 200
    assert _json_body(sent) == {"error": "Stream consumed"}


async def _read_via_stream(request: Request) -> None:
    async for _ in request.stream():
        pass


async def _read_via_body(request: Request) -> None:
    await request.body()


# @lat: [[tests#Request body streaming#Disconnect raises ClientDisconnect]]
@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(_read_via_stream, id="stream"),
        pytest.param(_read_via_body, id="body"),
    ],
)
def test_request_disconnect_raises_client_disconnect(reader):
    async def handler(request: Request):
        try:
            await reader(request)
        except ClientDisconnect:
            return {"disconnected": True}
        return {"disconnected": False}

    receive = _messages_receive(
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert sent[0]["status"] == 200
    assert _json_body(sent) == {"disconnected": True}


# @lat: [[tests#Request body streaming#Unexpected ASGI message raises RuntimeError]]
def test_request_stream_unexpected_message_raises():
    async def handler(request: Request):
        try:
            async for _ in request.stream():
                pass
        except RuntimeError as exc:
            return {"error": str(exc)}
        return {"error": None}

    receive = _messages_receive([{"type": "websocket.receive"}])
    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert sent[0]["status"] == 200
    assert _json_body(sent) == {"error": "Unexpected ASGI message: 'websocket.receive'"}


# @lat: [[tests#Request body streaming#Full-duplex echo with StreamingResponse]]
def test_streaming_response_from_request_stream_full_duplex():
    """StreamingResponse(request.stream()) echoes all chunks without stealing."""
    chunks = [b"alpha", b"beta", b"gamma"]
    sent_index = 0
    chunk_acked: list[asyncio.Event] = []

    def handler(request: Request):
        return StreamingResponse(request.stream())

    async def receive():
        nonlocal sent_index
        if sent_index < len(chunks):
            if chunk_acked:
                await chunk_acked[-1].wait()
            body = chunks[sent_index]
            sent_index += 1
            chunk_acked.append(asyncio.Event())
            return {
                "type": "http.request",
                "body": body,
                "more_body": sent_index < len(chunks),
            }
        await asyncio.Event().wait()

    def on_send(sent):
        if sent[-1]["type"] == "http.response.body" and sent[-1].get("body"):
            chunk_acked[-1].set()

    sent = call_asgi(_post_app(handler), receive, method="POST", on_send=on_send)
    assert sent[0]["status"] == 200
    bodies = body_messages(sent)
    assert [m["body"] for m in bodies] == [b"alpha", b"beta", b"gamma", b""]
    assert [m["more_body"] for m in bodies] == [True, True, True, False]


# @lat: [[tests#Request body streaming#Disconnect after stream cancels response]]
def test_streaming_response_observes_disconnect_after_request_stream_finishes():
    """After request.stream() finishes, http.disconnect still cancels the stream."""
    closed: list[bool] = []
    disconnect = asyncio.Event()

    def handler(request: Request):
        async def produce():
            try:
                async for chunk in request.stream():
                    yield chunk
                while True:
                    await asyncio.sleep(0.001)
                    yield b"tick"
            finally:
                closed.append(True)

        return StreamingResponse(produce())

    incoming = iter(
        [
            {"type": "http.request", "body": b"in-1", "more_body": True},
            {"type": "http.request", "body": b"in-2", "more_body": False},
        ]
    )

    async def receive():
        msg = next(incoming, None)
        if msg is not None:
            return msg
        await disconnect.wait()
        return {"type": "http.disconnect"}

    def on_send(sent):
        ticks = [m for m in body_messages(sent) if m["body"] == b"tick"]
        if len(ticks) == 2:
            disconnect.set()

    sent = call_asgi(_post_app(handler), receive, method="POST", on_send=on_send)
    bodies = [m["body"] for m in body_messages(sent)]
    assert bodies[:2] == [b"in-1", b"in-2"]
    assert bodies[2:].count(b"tick") >= 2
    assert closed == [True]


# @lat: [[tests#Request body streaming#Disconnect during stream stops response]]
def test_streaming_response_disconnect_during_request_stream():
    """A disconnect seen by request.stream() is replayed to the response listener."""
    saw_disconnect: list[bool] = []
    closed: list[bool] = []
    receive_calls = 0

    def handler(request: Request):
        async def produce():
            try:
                try:
                    async for chunk in request.stream():
                        yield chunk
                except ClientDisconnect:
                    saw_disconnect.append(True)
                    await asyncio.Event().wait()
            finally:
                closed.append(True)

        return StreamingResponse(produce())

    incoming = iter(
        [
            {"type": "http.request", "body": b"first", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        msg = next(incoming, None)
        if msg is None:
            raise AssertionError("receive called again after http.disconnect")
        return msg

    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert [m["body"] for m in body_messages(sent)] == [b"first"]
    assert saw_disconnect == [True]
    assert closed == [True]
    assert receive_calls == 2


# @lat: [[tests#Request body streaming#SSEResponse receives all request stream chunks]]
def test_sse_response_from_request_stream_no_stolen_chunks():
    """SSEResponse(request.stream()) delivers every chunk without listener theft."""
    incoming = iter(
        [
            {"type": "http.request", "body": b"one", "more_body": True},
            {"type": "http.request", "body": b"two", "more_body": True},
            {"type": "http.request", "body": b"three", "more_body": False},
        ]
    )

    def handler(request: Request):
        return SSEResponse(request.stream())

    async def receive():
        msg = next(incoming, None)
        if msg is not None:
            return msg
        await asyncio.Event().wait()

    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert sent[0]["status"] == 200
    bodies = [m["body"] for m in body_messages(sent)]
    expected = [
        ServerSentEvent(data=chunk).encode() for chunk in (b"one", b"two", b"three")
    ]
    assert bodies == [*expected, b""]


# @lat: [[tests#Request body streaming#Disconnect while paused on terminal chunk]]
def test_streaming_response_disconnect_while_paused_on_terminal_chunk():
    """A producer pausing inside the terminal-chunk iteration still sees disconnect."""
    closed: list[bool] = []
    disconnect = asyncio.Event()

    def handler(request: Request):
        async def produce():
            try:
                async for chunk in request.stream():
                    yield chunk
                    await asyncio.Event().wait()
            finally:
                closed.append(True)

        return StreamingResponse(produce())

    incoming = iter([{"type": "http.request", "body": b"only", "more_body": False}])

    async def receive():
        msg = next(incoming, None)
        if msg is not None:
            return msg
        await disconnect.wait()
        return {"type": "http.disconnect"}

    def on_send(sent):
        if any(m["body"] == b"only" for m in body_messages(sent)):
            disconnect.set()

    sent = call_asgi(_post_app(handler), receive, method="POST", on_send=on_send)
    assert sent[0]["status"] == 200
    assert [m["body"] for m in body_messages(sent)] == [b"only"]
    assert closed == [True]


# @lat: [[tests#Request body streaming#StreamingResponse ignores ClientDisconnect]]
def test_streaming_response_request_stream_disconnect_exits_cleanly():
    """Direct StreamingResponse(request.stream()) treats ClientDisconnect as normal."""
    first_sent = asyncio.Event()
    sent_first = False

    def handler(request: Request):
        return StreamingResponse(request.stream())

    async def receive():
        nonlocal sent_first
        if not sent_first:
            sent_first = True
            return {"type": "http.request", "body": b"first", "more_body": True}
        await first_sent.wait()
        return {"type": "http.disconnect"}

    def on_send(sent):
        if any(m["body"] == b"first" for m in body_messages(sent)):
            first_sent.set()

    sent = call_asgi(_post_app(handler), receive, method="POST", on_send=on_send)
    assert sent[0]["status"] == 200
    assert [m["body"] for m in body_messages(sent)] == [b"first"]


# @lat: [[tests#Request body streaming#Lazy stream after await loses no chunks]]
def test_streaming_response_lazy_request_stream_after_await():
    """Calling request.stream() after an await in a producer receives every chunk."""
    incoming = iter(
        [
            {"type": "http.request", "body": b"one", "more_body": True},
            {"type": "http.request", "body": b"two", "more_body": True},
            {"type": "http.request", "body": b"three", "more_body": False},
        ]
    )

    def handler(request: Request):
        async def produce():
            await asyncio.sleep(0)
            async for chunk in request.stream():
                yield chunk

        return StreamingResponse(produce())

    async def receive():
        msg = next(incoming, None)
        if msg is not None:
            return msg
        await asyncio.Event().wait()

    sent = call_asgi(_post_app(handler), receive, method="POST")
    assert sent[0]["status"] == 200
    bodies = body_messages(sent)
    assert [m["body"] for m in bodies] == [b"one", b"two", b"three", b""]
    assert [m["more_body"] for m in bodies] == [True, True, True, False]


# @lat: [[tests#Request body streaming#Backpressure prevents draining receive ahead]]
def test_streaming_response_request_stream_backpressure():
    """While send() is blocked on chunk 1, receive() is not drained ahead."""

    async def run():
        chunks = [f"chunk-{i}".encode() for i in range(10)]
        receive_calls = 0
        first_send_entered = asyncio.Event()
        release_send = asyncio.Event()
        sent: list[dict] = []

        def handler(request: Request):
            return StreamingResponse(request.stream())

        async def receive():
            nonlocal receive_calls
            if receive_calls < len(chunks):
                body = chunks[receive_calls]
                receive_calls += 1
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": receive_calls < len(chunks),
                }
            await asyncio.Event().wait()

        async def send(message):
            if (
                message["type"] == "http.response.body"
                and message["body"] == b"chunk-0"
            ):
                first_send_entered.set()
                await release_send.wait()
            sent.append(message)

        app = _post_app(handler)
        task = asyncio.ensure_future(app(stream_scope("POST"), receive, send))
        await first_send_entered.wait()
        for _ in range(5):
            await asyncio.sleep(0)
        assert receive_calls == 1, f"drained {receive_calls} chunks while send blocked"
        release_send.set()
        await asyncio.wait_for(task, timeout=5)
        bodies = body_messages(sent)
        assert [m["body"] for m in bodies] == [*chunks, b""]

    asyncio.run(asyncio.wait_for(run(), timeout=5))
