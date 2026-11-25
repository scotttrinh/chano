"""Tests for native Vercel Queues, Schedules, and Workflows in Chano."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import importlib
import io
import json
import shutil
import subprocess
import sys
import tomllib
import types
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from chano import (
    Chano,
    ChanoConfig,
    RequestValidationError,
    WorkflowConfig,
    create_app,
    create_queue_app,
    create_schedule_app,
    create_workflow_app,
    invoke_queue,
    invoke_schedule,
    load_config,
)
from chano import build as build_module
from chano.build import build, emit_build, plan_build
from chano.cli import main as cli_main
from chano.queues import QueueNotFoundError, discover_queues
from chano.requests import Message
from chano.schedules import ScheduleNotFoundError, discover_schedules
from chano.workflows import (
    get_step_topic_prefix,
    get_workflow_topic_prefix,
    introspect_workflow_triggers,
    load_workflow_entrypoint,
    mount_workflows,
    resolve_workflow_trace_roots,
    validate_workflow_namespace,
    workflow_flow_trigger,
)
from tests.helpers import make_client, make_dist, write_project

# ---------------------------------------------------------------------------
# Vercel Queues
# ---------------------------------------------------------------------------


# @lat: [[tests#Vercel Queues#Discovery and OpenAPI isolation]]
def test_queue_discovery_and_openapi_isolation(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qdemo"\nversion = "0.1.0"\n',
            "app/orders/route.py": ('def GET():\n    return {"ok": True}\n'),
            "app/orders/queue.py": (
                'topic = "orders"\n'
                "def CONSUME(payload: dict):\n"
                '    return {"consumed": payload}\n'
            ),
            "app/(workers)/emails/queue.py": (
                'topic = "emails"\n'
                'consumer = "mailer"\n'
                "async def CONSUME(payload: dict):\n"
                '    return {"sent": payload}\n'
            ),
        },
    )

    discovered = discover_queues(project / "app")
    assert [q.function for q in discovered] == ["_queues/emails", "_queues/orders"]

    app = create_app(project / "app")
    client = make_client(app)
    assert client("GET", "/orders").json() == {"ok": True}

    schema = client("GET", "/openapi.json").json()
    assert list(schema["paths"]) == ["/orders"]
    assert "_queues" not in json.dumps(schema)
    assert "_chano" not in json.dumps(schema)

    docs_html = client("GET", "/docs").text
    assert "swagger" in docs_html.lower()
    assert "_queues" not in docs_html


# @lat: [[tests#Vercel Queues#Sync, async, validation, and Depends]]
def test_queue_consume_sync_async_validation_and_depends(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qval"\nversion = "0.1.0"\n',
            "app/boundary.py": (
                "from chano import Boundary\n"
                "seen = []\n"
                "def record_check():\n"
                '    seen.append("checked")\n'
                '    return "root-dep"\n'
                "async def never_wrap(request, call_next):\n"
                '    raise RuntimeError("wrap must not run on queues")\n'
                "boundary = Boundary(guards=[record_check], wrap=[never_wrap])\n"
            ),
            "app/orders/queue.py": (
                "from typing import Annotated\n"
                "import msgspec\n"
                "from chano import Depends, Request, after\n"
                "\n"
                'topic = "orders"\n'
                "after_events = []\n"
                "\n"
                "class OrderEvent(msgspec.Struct):\n"
                "    order_id: str\n"
                "    amount: Annotated[int, msgspec.Meta(gt=0)]\n"
                "\n"
                "def get_prefix() -> str:\n"
                '    return "ord"\n'
                "\n"
                "async def CONSUME(\n"
                "    event: OrderEvent,\n"
                "    request: Request,\n"
                "    prefix: str = Depends(get_prefix),\n"
                "):\n"
                "    after(lambda oid: after_events.append(oid), event.order_id)\n"
                "    return {\n"
                '        "key": f"{prefix}:{event.order_id}",\n'
                '        "amount": event.amount,\n'
                '        "topic_header": request.headers.get("ce-vqsqueuename"),\n'
                '        "consumer_header": request.headers.get(\n'
                '            "ce-vqsconsumergroup"\n'
                "        ),\n"
                "    }\n"
            ),
            "app/sync_job/queue.py": (
                'topic = "sync-events"\n'
                "def CONSUME(item: dict[str, int]):\n"
                '    return {"total": sum(item.values())}\n'
            ),
        },
    )

    app = create_app(project / "app")
    result = asyncio.run(
        app.dispatch_queue("orders", {"order_id": "123", "amount": 50})
    )
    assert result == {
        "key": "ord:123",
        "amount": 50,
        "topic_header": "orders",
        "consumer_header": "default",
    }
    orders_queue = next(q for q in app.queues if q.trigger.topic == "orders")
    assert orders_queue.module.after_events == ["123"]

    sync_result = asyncio.run(
        invoke_queue(project / "app", "sync-events", {"a": 2, "b": 3})
    )
    assert sync_result == {"total": 5}

    with pytest.raises(RequestValidationError) as exc_info:
        asyncio.run(app.dispatch_queue("orders", {"order_id": "123", "amount": -5}))
    assert exc_info.value.errors[0]["loc"] == ["body", "amount"]


# @lat: [[tests#Vercel Queues#Multi-consumer dev trigger and ASGI fallback]]
def test_queue_multi_consumer_dev_trigger_and_asgi_app(
    tmp_path: Path, monkeypatch: Any, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qmulti"\nversion = "0.1.0"\n',
            "app/boundary.py": (
                "from chano import Boundary, JSONResponse, Request\n"
                "def handle_runtime(request: Request, exc: RuntimeError):\n"
                '    return JSONResponse({"handled": str(exc)}, status_code=503)\n'
                "boundary = Boundary(recover=[handle_runtime])\n"
            ),
            "app/billing/queue.py": (
                'topic = "orders"\n'
                'consumer = "billing"\n'
                "def CONSUME(payload: dict):\n"
                '    if payload.get("boom"):\n'
                '        raise RuntimeError("retry me")\n'
                '    return {"billed": payload["id"]}\n'
            ),
            "app/analytics/queue.py": (
                'topic = "orders"\n'
                'consumer = "analytics"\n'
                "async def CONSUME(payload: dict):\n"
                '    return {"tracked": payload["id"]}\n'
            ),
        },
    )

    app = create_app(project / "app")
    all_res = asyncio.run(app.dispatch_queue("orders", {"id": "o1"}))
    assert all_res == {
        "analytics": {"tracked": "o1"},
        "billing": {"billed": "o1"},
    }
    single = asyncio.run(app.dispatch_queue("orders", {"id": "o2"}, consumer="billing"))
    assert single == {"billed": "o2"}

    # Local dev HTTP trigger
    dev_client = make_client(app)
    resp = dev_client(
        "POST", "/_chano/queues/orders?consumer=analytics", json={"id": "o3"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"tracked": "o3"}
    assert dev_client("POST", "/_chano/queues/unknown", json={}).status_code == 404
    with pytest.raises(QueueNotFoundError):
        asyncio.run(app.dispatch_queue("unknown", {}))

    # KeyError inside CONSUME must NOT become 404!
    assert (
        dev_client(
            "POST", "/_chano/queues/orders?consumer=billing", json={}
        ).status_code
        == 500
    )
    with pytest.raises(KeyError, match="'id'"):
        asyncio.run(app.dispatch_queue("orders", {}, consumer="billing"))

    # Deployed single-consumer ASGI app (simulating vercel-queue absent):
    # plain local POST works, but platform CloudEvent delivery refuses to fake 200.
    monkeypatch.setitem(sys.modules, "vercel.queue", None)
    q_app = create_queue_app(project / "app", "_queues/billing")
    q_client = make_client(q_app)
    assert q_client("POST", "/_queues/billing", json={"id": "o4"}).json() == {
        "billed": "o4"
    }
    assert q_client("GET", "/_queues/billing").status_code == 405
    assert q_client("POST", "/_queues/billing", content=b"{bad").status_code == 422
    boom = q_client("POST", "/_queues/billing", json={"boom": True})
    assert boom.status_code == 500
    ce_without_sdk = q_client(
        "POST",
        "/_queues/billing",
        json={"id": "o5"},
        headers={
            "ce-type": "com.vercel.queue.v2beta",
            "ce-vqsmessageid": "msg_1",
            "ce-vqsreceipthandle": "rcpt_1",
        },
    )
    assert ce_without_sdk.status_code == 500
    assert "vercel-queue" in ce_without_sdk.text


# @lat: [[tests#Vercel Queues#Delivery lifecycle, ack, and retry]]
def test_queue_delivery_lifecycle_accept_renew_ack_and_retry(
    tmp_path: Path, monkeypatch: Any, isolated_project_imports: Any
):
    """Platform CloudEvent push delivery runs through vercel.queue +
    vercel_runtime.workers.create_queue_service_app: subscribe, accept, lease
    renewal, ack on success (204), no-ack/retry on failure (500), 400 on
    ProtocolError, 405 on GET."""
    events: list[tuple[str, Any]] = []
    subscribers: dict[tuple[str, str], Any] = {}

    class FakeProtocolError(Exception):
        pass

    @dataclasses.dataclass
    class FakeByteBufferTransport:
        pass

    @dataclasses.dataclass
    class FakeTopic:
        name: str
        transport: Any = None

    @dataclasses.dataclass(frozen=True)
    class FakeMetadata:
        message_id: str
        delivery_count: int
        created_at: datetime
        topic: str
        consumer_group: str
        receipt_handle: str
        content_type: str = "application/json"
        region: str = "iad1"

    @dataclasses.dataclass(frozen=True)
    class FakeMessage:
        payload: bytes
        metadata: FakeMetadata

    def fake_subscribe(**kwargs: Any):
        def decorator(fn: Any) -> Any:
            topic_obj = kwargs["topic"]
            topic_name = (
                topic_obj.name if isinstance(topic_obj, FakeTopic) else topic_obj
            )
            events.append(("subscribe", (topic_name, kwargs)))
            subscribers[(topic_name, kwargs["consumer_group"])] = fn
            return fn

        return decorator

    async def fake_accept_and_handle(
        raw_body: AsyncIterator[bytes], headers: Mapping[str, str]
    ) -> None:
        ce_type = headers.get("ce-type")
        if ce_type != "com.vercel.queue.v2beta":
            raise FakeProtocolError(f"unexpected ce-type: {ce_type!r}")
        body = b"".join([chunk async for chunk in raw_body])
        topic = headers["ce-vqsqueuename"]
        consumer = headers["ce-vqsconsumergroup"]
        msg_id = headers["ce-vqsmessageid"]
        receipt = headers["ce-vqsreceipthandle"]
        events.append(("accept", (topic, consumer, msg_id)))
        events.append(("lease_renew_start", receipt))
        sub = subscribers[(topic, consumer)]
        msg = FakeMessage(
            payload=body,
            metadata=FakeMetadata(
                message_id=msg_id,
                delivery_count=int(headers.get("ce-vqsdeliverycount", "1")),
                created_at=datetime(2026, 11, 24, tzinfo=UTC),
                topic=topic,
                consumer_group=consumer,
                receipt_handle=receipt,
            ),
        )
        try:
            await sub(msg)
        except Exception as exc:
            events.append(("retry_no_ack", (receipt, type(exc).__name__)))
            raise
        else:
            events.append(("ack", receipt))

    fake_vercel: Any = types.ModuleType("vercel")
    fake_queue: Any = types.ModuleType("vercel.queue")
    fake_queue.Topic = FakeTopic
    fake_queue.ByteBufferTransport = FakeByteBufferTransport
    fake_queue.ProtocolError = FakeProtocolError
    fake_queue.subscribe = fake_subscribe
    fake_queue.accept_and_handle = fake_accept_and_handle
    fake_vercel.queue = fake_queue

    fake_vr: Any = types.ModuleType("vercel_runtime")
    fake_workers: Any = types.ModuleType("vercel_runtime.workers")

    def fake_create_queue_service_app() -> Any:
        events.append(("create_queue_service_app", True))

        async def asgi(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                return
            if scope["method"] != "POST":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 405,
                        "headers": [(b"allow", b"POST")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            hdrs = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", ())
            }

            async def body_iter() -> AsyncIterator[bytes]:
                while True:
                    m = await receive()
                    if m.get("body"):
                        yield m["body"]
                    if not m.get("more_body", False):
                        return

            try:
                await fake_accept_and_handle(body_iter(), hdrs)
            except FakeProtocolError:
                status = 400
            except Exception:
                status = 500
            else:
                status = 204
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        return asgi

    fake_workers.create_queue_service_app = fake_create_queue_service_app
    fake_vr.workers = fake_workers

    monkeypatch.setitem(sys.modules, "vercel", fake_vercel)
    monkeypatch.setitem(sys.modules, "vercel.queue", fake_queue)
    monkeypatch.setitem(sys.modules, "vercel_runtime", fake_vr)
    monkeypatch.setitem(sys.modules, "vercel_runtime.workers", fake_workers)

    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qlife"\nversion = "0.1.0"\n',
            "app/orders/queue.py": (
                "import msgspec\n"
                "from chano import Request\n"
                'topic = "orders"\n'
                'consumer = "fulfillment"\n'
                "retry_after = 15\n"
                "initial_delay = 2\n"
                "max_concurrency = 3\n"
                "processed = []\n"
                "class Order(msgspec.Struct):\n"
                "    id: str\n"
                "    fail: bool = False\n"
                "async def CONSUME(order: Order, request: Request):\n"
                "    if order.fail:\n"
                '        raise RuntimeError("transient outage")\n'
                '    processed.append((order.id, request.headers["ce-vqsmessageid"]))\n'
            ),
        },
    )

    q_app = create_queue_app(project / "app", "_queues/orders")
    client = make_client(q_app)

    sub_name, sub_kwargs = next(val for kind, val in events if kind == "subscribe")
    assert sub_name == "orders"
    assert isinstance(sub_kwargs["topic"], FakeTopic)
    assert isinstance(sub_kwargs["topic"].transport, FakeByteBufferTransport)
    assert sub_kwargs["consumer_group"] == "fulfillment"
    assert sub_kwargs["retry_after"] == 15
    assert sub_kwargs["initial_delay"] == 2
    assert sub_kwargs["max_concurrency"] == 3
    assert ("create_queue_service_app", True) in events

    ce_headers = {
        "ce-type": "com.vercel.queue.v2beta",
        "ce-vqsqueuename": "orders",
        "ce-vqsconsumergroup": "fulfillment",
        "ce-vqsmessageid": "msg_001",
        "ce-vqsreceipthandle": "rcpt_001",
        "ce-vqsdeliverycount": "1",
    }

    assert (
        client(
            "POST", "/_queues/orders", json={"id": "ord-1"}, headers=ce_headers
        ).status_code
        == 204
    )
    assert ("accept", ("orders", "fulfillment", "msg_001")) in events
    assert ("lease_renew_start", "rcpt_001") in events
    assert ("ack", "rcpt_001") in events

    fail_headers = {
        **ce_headers,
        "ce-vqsmessageid": "msg_002",
        "ce-vqsreceipthandle": "rcpt_002",
    }
    assert (
        client(
            "POST",
            "/_queues/orders",
            json={"id": "ord-2", "fail": True},
            headers=fail_headers,
        ).status_code
        == 500
    )
    assert ("retry_no_ack", ("rcpt_002", "RuntimeError")) in events
    assert ("ack", "rcpt_002") not in events

    bad_headers = {
        **ce_headers,
        "ce-vqsmessageid": "msg_003",
        "ce-vqsreceipthandle": "rcpt_003",
    }
    assert (
        client(
            "POST", "/_queues/orders", json={"wrong": 1}, headers=bad_headers
        ).status_code
        == 500
    )
    assert ("retry_no_ack", ("rcpt_003", "RequestValidationError")) in events

    assert client("POST", "/_queues/orders", json={"id": "x"}).status_code == 400
    assert client("GET", "/_queues/orders").status_code == 405


# @lat: [[tests#Vercel Queues#Real vercel-queue SDK registration and delivery]]
def test_queue_real_sdk_registration_sanitization_and_delivery(
    tmp_path: Path, isolated_project_imports: Any
):
    """End-to-end test with the real vercel-queue SDK: Message[bytes] metadata,
    SanitizedName consumer normalization matching .vc-config.json, and real
    embedded queue delivery."""
    import vercel.queue as vq
    from vercel.queue._internal.embedded import embedded_queue_service

    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "realq"\nversion = "0.1.0"\n',
            "app/orders/queue.py": (
                "import msgspec\n"
                "from chano import Request\n"
                'topic = "real-sdk-orders"\n'
                'consumer = "order_worker.v1"\n'
                "retry_after = 20\n"
                "initial_delay = 0\n"
                "max_concurrency = 4\n"
                "seen = []\n"
                "class Order(msgspec.Struct):\n"
                "    id: str\n"
                "async def CONSUME(order: Order, request: Request):\n"
                "    seen.append(\n"
                "        (\n"
                "            order.id,\n"
                '            request.headers.get("ce-vqsmessageid"),\n'
                '            request.headers.get("ce-vqsconsumergroup"),\n'
                '            request.headers.get("ce-vqsdeliverycount"),\n'
                "        )\n"
                "    )\n"
            ),
        },
    )

    plan = plan_build(project, python="3.12", platform=None)
    q_spec = next(f for f in plan.functions if f.name == "_queues/orders")
    triggers = q_spec.vc_config["experimentalTriggers"]
    assert isinstance(triggers, list)
    emitted_trigger = triggers[0]
    assert emitted_trigger == {
        "type": "queue/v2beta",
        "topic": "real-sdk-orders",
        "consumer": "order__worker_Dv1",
        "retryAfterSeconds": 20,
        "initialDelaySeconds": 0,
        "maxConcurrency": 4,
    }

    q_app = create_queue_app(project / "app", "_queues/orders")
    subs = [s for s in vq.get_subscriptions() if s.topic == "real-sdk-orders"]
    assert len(subs) == 1
    assert subs[0].consumer_group == emitted_trigger["consumer"]
    assert subs[0].retry_after_seconds == 20
    assert subs[0].initial_delay_seconds == 0
    assert subs[0].max_concurrency == 4

    async def deliver() -> tuple[str | None, list[Any]]:
        async with embedded_queue_service() as svc:
            client = svc.get_async_client()
            msg_id = await client.send("real-sdk-orders", {"id": "ord-99"})
            mod = discover_queues(project / "app")[0].load()
            for _ in range(100):
                if mod.seen:
                    break
                await asyncio.sleep(0.02)
            return msg_id, list(mod.seen)

    msg_id, seen = asyncio.run(deliver())
    assert seen == [("ord-99", msg_id, "order__worker_Dv1", "1")]
    # Keep q_app alive through delivery so vercel.queue's weakref stays active.
    assert q_app is not None


# @lat: [[tests#Vercel Queues#Build requires vercel-queue in store]]
def test_queue_build_fails_when_vercel_queue_missing_from_store(
    tmp_path: Path, isolated_project_imports: Any, stub_build_install: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qmissing"\nversion = "0.1.0"\n',
            "app/orders/queue.py": (
                'topic = "orders"\n'
                'def CONSUME(payload: dict):\n    return {"ok": payload}\n'
            ),
        },
    )

    stub_build_install(
        {
            **make_dist("chano", "chano"),
            **make_dist("vercel-runtime", "vercel_runtime"),
        }
    )

    plan = plan_build(project, python="3.12", platform=None)
    with pytest.raises(RuntimeError, match="requires 'vercel-queue'"):
        emit_build(plan)


# @lat: [[tests#Vercel Queues#Queue validation errors]]
@pytest.mark.parametrize(
    ("rel_path", "source", "exc_type", "match"),
    [
        (
            "app/q/queue.py",
            'topic = "t"\n',
            TypeError,
            "invalid `CONSUME`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\ndef CONSUME():\n    yield 1\n',
            TypeError,
            "generator functions",
        ),
        (
            "app/q/queue.py",
            "def CONSUME():\n    return 1\n",
            ValueError,
            "invalid `topic`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\nconsumer = ""\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `consumer`",
        ),
        (
            "app/q/queue.py",
            'topic = "bad topic!"\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `topic`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\nretry_after = 0\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `retry_after`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\nretry_after = 86401\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `retry_after`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\nretry_after = -1\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `retry_after`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\nretry_after = True\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `retry_after`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\ninitial_delay = -5\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `initial_delay`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\ninitial_delay = 86401\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `initial_delay`",
        ),
        (
            "app/q/queue.py",
            'topic = "t"\nmax_concurrency = 0\ndef CONSUME():\n    return 1\n',
            ValueError,
            "invalid `max_concurrency`",
        ),
        (
            "app/[id]/queue.py",
            'topic = "t"\ndef CONSUME():\n    return 1\n',
            ValueError,
            "dynamic segment",
        ),
    ],
)
def test_queue_validation_errors(
    tmp_path: Path,
    isolated_project_imports: Any,
    rel_path: str,
    source: str,
    exc_type: type[Exception],
    match: str,
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qerr"\nversion = "0.1.0"\n',
            rel_path: source,
        },
    )
    with pytest.raises(exc_type, match=match):
        plan_build(project)


# @lat: [[tests#Vercel Queues#Duplicate topic and consumer rejected]]
def test_queue_duplicate_topic_consumer_rejected(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qdup"\nversion = "0.1.0"\n',
            "app/a/queue.py": 'topic = "orders"\ndef CONSUME():\n    return 1\n',
            "app/b/queue.py": 'topic = "orders"\ndef CONSUME():\n    return 2\n',
        },
    )
    with pytest.raises(ValueError, match="duplicate queue consumer"):
        plan_build(project)


# @lat: [[tests#Vercel Queues#Wildcard dispatch and consumer sanitization]]
def test_queue_wildcard_dispatch_and_deterministic_consumer_matching(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qwild"\nversion = "0.1.0"\n',
            "app/wild/queue.py": (
                "from chano import Request\n"
                'topic = "orders-*"\n'
                'consumer = "order.worker"\n'
                "def CONSUME(payload: dict, request: Request):\n"
                "    return {\n"
                '        "which": "dot",\n'
                '        "topic": request.headers["ce-vqsqueuename"],\n'
                '        "id": payload["id"],\n'
                "    }\n"
            ),
            "app/escaped/queue.py": (
                "from chano import Request\n"
                'topic = "orders-*"\n'
                'consumer = "order_Dworker"\n'
                "def CONSUME(payload: dict, request: Request):\n"
                "    return {\n"
                '        "which": "literal_underscore_D",\n'
                '        "topic": request.headers["ce-vqsqueuename"],\n'
                '        "id": payload["id"],\n'
                "    }\n"
            ),
        },
    )

    app = create_app(project / "app")
    # Wildcard pattern `orders-*` matches concrete topic `orders-placed` locally
    # and delivers concrete `ce-vqsqueuename: orders-placed`.
    all_res = asyncio.run(app.dispatch_queue("orders-placed", {"id": "o1"}))
    assert all_res == {
        "order_Dworker": {"which": "dot", "topic": "orders-placed", "id": "o1"},
        "order__Dworker": {
            "which": "literal_underscore_D",
            "topic": "orders-placed",
            "id": "o1",
        },
    }

    # Deterministic consumer matching: `order_Dworker` matches ONLY the literal
    # `order_Dworker` consumer (normalized `order__Dworker`), NEVER `order.worker`!
    only_dot = asyncio.run(
        app.dispatch_queue("orders-placed", {"id": "o2"}, consumer="order.worker")
    )
    assert only_dot == {"which": "dot", "topic": "orders-placed", "id": "o2"}

    only_literal = asyncio.run(
        app.dispatch_queue("orders-placed", {"id": "o3"}, consumer="order_Dworker")
    )
    assert only_literal == {
        "which": "literal_underscore_D",
        "topic": "orders-placed",
        "id": "o3",
    }

    client = make_client(app)
    resp = client(
        "POST",
        "/_chano/queues/orders-shipped?consumer=order.worker",
        json={"id": "o4"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"which": "dot", "topic": "orders-shipped", "id": "o4"}
    assert client("POST", "/_chano/queues/emails-sent", json={}).status_code == 404


# @lat: [[tests#Vercel Queues#Overlapping wildcard topics rejected]]
def test_queue_overlapping_wildcard_topics_rejected(
    tmp_path: Path, isolated_project_imports: Any
):
    # Wildcard + exact topic on same consumer group -> rejected.
    exact_overlap = write_project(
        tmp_path / "exact",
        {
            "pyproject.toml": '[project]\nname = "qov1"\nversion = "0.1.0"\n',
            "app/a/queue.py": 'topic = "orders-*"\ndef CONSUME():\n    return 1\n',
            "app/b/queue.py": 'topic = "orders-placed"\ndef CONSUME():\n    return 2\n',
        },
    )
    with pytest.raises(ValueError, match="overlapping queue topics"):
        plan_build(exact_overlap)

    # Overlapping wildcard prefixes on same consumer group -> rejected.
    wild_overlap = write_project(
        tmp_path / "wild",
        {
            "pyproject.toml": '[project]\nname = "qov2"\nversion = "0.1.0"\n',
            "app/a/queue.py": 'topic = "orders-*"\ndef CONSUME():\n    return 1\n',
            "app/b/queue.py": 'topic = "orders-v1-*"\ndef CONSUME():\n    return 2\n',
        },
    )
    with pytest.raises(ValueError, match="overlapping queue topics"):
        plan_build(wild_overlap)

    # Same overlapping topics on DIFFERENT consumer groups -> allowed.
    distinct_groups = write_project(
        tmp_path / "distinct",
        {
            "pyproject.toml": '[project]\nname = "qov3"\nversion = "0.1.0"\n',
            "app/a/queue.py": (
                'topic = "orders-*"\nconsumer = "audit"\ndef CONSUME():\n    return 1\n'
            ),
            "app/b/queue.py": (
                'topic = "orders-placed"\n'
                'consumer = "billing"\n'
                "def CONSUME():\n"
                "    return 2\n"
            ),
        },
    )
    plan = plan_build(distinct_groups)
    assert {f.name for f in plan.functions} == {"_queues/a", "_queues/b"}


# @lat: [[tests#Vercel Queues#Queue build output and tree-shaking]]
def test_queue_build_output_and_tree_shaking(
    tmp_path: Path,
    monkeypatch: Any,
    isolated_project_imports: Any,
    stub_build_install: Any,
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qbuild"\nversion = "0.1.0"\n',
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
            "app/orders/queue.py": (
                "import queue_only_pkg\n"
                'topic = "orders"\n'
                'consumer = "fulfillment"\n'
                "retry_after = 30\n"
                "initial_delay = 5\n"
                "max_concurrency = 4\n"
                "max_duration = 120\n"
                "memory = 1024\n"
                'regions = ["iad1"]\n'
                'include = ["extra-queue-pkg"]\n'
                "def CONSUME(payload: dict):\n"
                "    return queue_only_pkg.VALUE\n"
            ),
        },
    )

    monkeypatch.setitem(
        sys.modules, "queue_only_pkg", types.ModuleType("queue_only_pkg")
    )
    stub_build_install(
        {
            **make_dist("chano", "chano"),
            **make_dist("vercel-runtime", "vercel_runtime"),
            **make_dist("vercel-queue", "vercel/queue"),
            **make_dist(
                "queue-only-pkg",
                "queue_only_pkg",
                extra_files={"queue_only_pkg/__init__.py": "VALUE = 1\n"},
            ),
            **make_dist("extra-queue-pkg", "extra_queue_pkg"),
        }
    )

    plan = plan_build(project, python="3.12", platform=None)
    by_name = {f.name: f for f in plan.functions}
    assert set(by_name) == {"index", "_queues/orders"}

    q_spec = by_name["_queues/orders"]
    assert q_spec.route is None
    assert "create_queue_app(" in q_spec.entrypoint
    assert q_spec.vc_config == {
        "runtime": "python3.12",
        "handler": "vc__handler__python.vc_handler",
        "supportsResponseStreaming": True,
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "maxDuration": 120,
        "memory": 1024,
        "regions": ["iad1"],
        "experimentalTriggers": [
            {
                "type": "queue/v2beta",
                "topic": "orders",
                "consumer": "fulfillment",
                "retryAfterSeconds": 30,
                "initialDelaySeconds": 5,
                "maxConcurrency": 4,
            }
        ],
    }

    out = emit_build(plan)
    config_json = json.loads((out / "config.json").read_text())
    assert config_json == {
        "version": 3,
        "routes": [
            {"handle": "filesystem"},
            {"src": "^/docs$", "dest": "/docs.html"},
        ],
    }
    vc_config = json.loads(
        (out / "functions" / "_queues" / "orders.func" / ".vc-config.json").read_text()
    )
    assert vc_config == q_spec.vc_config

    # Tree-shaking: queue-only deps bundled in queue function, excluded from route.
    q_dists = set(
        build_module.index_store(
            out / "functions" / "_queues" / "orders.func" / "_vendor"
        ).distributions
    )
    r_dists = set(
        build_module.index_store(
            out / "functions" / "index.func" / "_vendor"
        ).distributions
    )
    assert {"queue-only-pkg", "extra-queue-pkg"} <= q_dists
    assert "queue-only-pkg" not in r_dists
    assert "extra-queue-pkg" not in r_dists


# @lat: [[tests#Vercel Queues#Queue CLI list and send]]
def test_queue_cli_list_and_send(
    tmp_path: Path, monkeypatch: Any, capsys: Any, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "qcli"\nversion = "0.1.0"\n',
            "app/jobs/queue.py": (
                "import msgspec\n"
                'topic = "jobs"\n'
                'consumer = "worker"\n'
                "class Job(msgspec.Struct):\n"
                "    name: str\n"
                "def CONSUME(job: Job):\n"
                '    return {"ran": job.name}\n'
            ),
        },
    )
    app_arg = str(project / "app")
    cli_main(["queue", "list", "--app", app_arg])
    listed = capsys.readouterr().out
    assert "jobs" in listed
    assert "consumer=worker" in listed
    assert "function=_queues/jobs" in listed

    cli_main(["queue", "send", "jobs", '{"name": "alpha"}', "--app", app_arg])
    assert json.loads(capsys.readouterr().out) == {"ran": "alpha"}

    monkeypatch.setattr(sys, "stdin", io.StringIO('{"name": "beta"}'))
    cli_main(["queue", "send", "jobs", "-", "--consumer", "worker", "--app", app_arg])
    assert json.loads(capsys.readouterr().out) == {"ran": "beta"}


# ---------------------------------------------------------------------------
# Vercel Schedules
# ---------------------------------------------------------------------------


# @lat: [[tests#Vercel Schedules#Discovery and OpenAPI isolation]]
def test_schedule_discovery_and_openapi_isolation(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "sdemo"\nversion = "0.1.0"\n',
            "app/cleanup/route.py": 'def GET():\n    return {"public": True}\n',
            "app/cleanup/schedule.py": (
                'schedule = "0 * * * *"\n'
                'timezone = "America/New_York"\n'
                "def RUN():\n"
                '    return {"cleaned": 10}\n'
            ),
            "app/(jobs)/nightly/schedule.py": (
                'schedule = "0 2 * * *"\n'
                "async def RUN():\n"
                '    return {"nightly": True}\n'
            ),
        },
    )

    jobs = discover_schedules(project / "app")
    assert [j.name for j in jobs] == ["cleanup", "nightly"]
    assert [j.function for j in jobs] == ["_schedules/cleanup", "_schedules/nightly"]

    app = create_app(project / "app")
    client = make_client(app)
    schema = client("GET", "/openapi.json").json()
    assert list(schema["paths"]) == ["/cleanup"]
    assert "_schedules" not in json.dumps(schema)
    assert "_chano" not in json.dumps(schema)


# @lat: [[tests#Vercel Schedules#Sync, async, Depends, and ASGI app]]
def test_schedule_run_sync_async_depends_and_asgi(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "srun"\nversion = "0.1.0"\n',
            "app/boundary.py": (
                "from chano import Boundary\n"
                "log = []\n"
                "def check():\n"
                '    log.append("checked")\n'
                "async def never_wrap(request, call_next):\n"
                '    raise RuntimeError("wrap must not run on schedules")\n'
                "boundary = Boundary(guards=[check], wrap=[never_wrap])\n"
            ),
            "app/cleanup/schedule.py": (
                "from typing import Annotated\n"
                "from chano import Depends, Request, after\n"
                'schedule = "*/15 * * * *"\n'
                "after_log = []\n"
                "def get_limit() -> int:\n"
                "    return 25\n"
                "async def RUN(\n"
                "    request: Request,\n"
                "    limit: int = Depends(get_limit),\n"
                "):\n"
                "    after(lambda n: after_log.append(n), limit)\n"
                '    return {"deleted": limit, "method": request.method}\n'
            ),
            "app/sync_job/schedule.py": (
                'schedule = "0 0 * * *"\ndef RUN():\n    return {"sync": True}\n'
            ),
        },
    )

    app = create_app(project / "app")
    res = asyncio.run(app.run_schedule("cleanup"))
    assert res == {"deleted": 25, "method": "POST"}
    cleanup_job = next(s for s in app.schedules if s.spec.name == "cleanup")
    assert cleanup_job.module.after_log == [25]

    sync_res = asyncio.run(invoke_schedule(project / "app", "_schedules/sync_job"))
    assert sync_res == {"sync": True}

    # Deployed single-schedule ASGI app accepts POST and GET, rejects DELETE.
    s_app = create_schedule_app(project / "app", "_schedules/cleanup")
    s_client = make_client(s_app)
    assert s_client("POST", "/_schedules/cleanup").json() == {
        "deleted": 25,
        "method": "POST",
    }
    assert s_client("GET", "/_schedules/cleanup").json() == {
        "deleted": 25,
        "method": "GET",
    }
    assert s_client("DELETE", "/_schedules/cleanup").status_code == 405


# @lat: [[tests#Vercel Schedules#Schedule build output and tree-shaking]]
def test_schedule_build_output_and_tree_shaking(
    tmp_path: Path,
    monkeypatch: Any,
    isolated_project_imports: Any,
    stub_build_install: Any,
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "sbuild"\nversion = "0.1.0"\n'
                '[tool.chano]\nbase_path = "/v1"\n'
            ),
            "app/health/route.py": ('def GET():\n    return {"ok": True}\n'),
            "app/cleanup/schedule.py": (
                "import sched_only_pkg\n"
                'schedule = "*/30 * * * *"\n'
                'timezone = "America/New_York"\n'
                "max_duration = 60\n"
                "memory = 512\n"
                'regions = ["sfo1"]\n'
                'include = ["extra-sched-pkg"]\n'
                "def RUN():\n"
                "    return sched_only_pkg.OK\n"
            ),
            "app/reports/nightly/schedule.py": (
                'schedule = "0 0 1,15 * *"\ndef RUN():\n    return None\n'
            ),
            "app/custom/schedule.py": (
                'name = "custom.job_1"\n'
                'schedule = "0 0 * * 0,7"\n'
                "def RUN():\n"
                "    return None\n"
            ),
        },
    )

    monkeypatch.setitem(
        sys.modules, "sched_only_pkg", types.ModuleType("sched_only_pkg")
    )
    stub_build_install(
        {
            **make_dist("chano", "chano"),
            **make_dist("vercel-runtime", "vercel_runtime"),
            **make_dist(
                "sched-only-pkg",
                "sched_only_pkg",
                extra_files={"sched_only_pkg/__init__.py": "OK = True\n"},
            ),
            **make_dist("extra-sched-pkg", "extra_sched_pkg"),
        }
    )

    plan = plan_build(project, python="3.12", platform=None)
    by_name = {f.name: f for f in plan.functions}
    assert set(by_name) == {
        "v1/health",
        "_schedules/cleanup",
        "_schedules/reports/nightly",
        "_schedules/custom",
    }

    cleanup_spec = by_name["_schedules/cleanup"]
    assert cleanup_spec.vc_config == {
        "runtime": "python3.12",
        "handler": "vc__handler__python.vc_handler",
        "supportsResponseStreaming": True,
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "maxDuration": 60,
        "memory": 512,
        "regions": ["sfo1"],
        "experimentalTriggers": [{"type": "schedule/v1beta"}],
    }
    assert by_name["_schedules/reports/nightly"].vc_config["experimentalTriggers"] == [
        {"type": "schedule/v1beta"}
    ]

    out = emit_build(plan)
    config_json = json.loads((out / "config.json").read_text())
    assert config_json == {
        "version": 3,
        "routes": [
            {"handle": "filesystem"},
            {"src": "^/v1/docs$", "dest": "/v1/docs.html"},
        ],
        "schedules": {
            "cleanup": {
                "function": "_schedules/cleanup",
                "schedule": "*/30 * * * *",
                "timezone": "America/New_York",
            },
            "custom.job_1": {
                "function": "_schedules/custom",
                "schedule": "0 0 * * 0,7",
            },
            "reports-nightly": {
                "function": "_schedules/reports/nightly",
                "schedule": "0 0 1,15 * *",
            },
        },
    }
    assert "crons" not in config_json

    # Tree-shaking: schedule-only deps bundled only in cleanup schedule function.
    cleanup_dists = set(
        build_module.index_store(
            out / "functions" / "_schedules" / "cleanup.func" / "_vendor"
        ).distributions
    )
    route_dists = set(
        build_module.index_store(
            out / "functions" / "v1" / "health.func" / "_vendor"
        ).distributions
    )
    assert {"sched-only-pkg", "extra-sched-pkg"} <= cleanup_dists
    assert "sched-only-pkg" not in route_dists
    assert "extra-sched-pkg" not in route_dists


# @lat: [[tests#Vercel Schedules#Schedule validation errors]]
@pytest.mark.parametrize(
    ("rel_path", "source", "exc_type", "match"),
    [
        (
            "app/job/schedule.py",
            'schedule = "0 * * * *"\n',
            TypeError,
            "invalid `RUN`",
        ),
        (
            "app/job/schedule.py",
            'schedule = "0 * * * *"\ndef RUN():\n    yield 1\n',
            TypeError,
            "generator functions",
        ),
        (
            "app/job/schedule.py",
            "def RUN():\n    return 1\n",
            ValueError,
            "invalid `schedule`",
        ),
        (
            "app/job/schedule.py",
            'schedule = "* * *"\ndef RUN():\n    return 1\n',
            ValueError,
            "5-field cron",
        ),
        (
            "app/job/schedule.py",
            'schedule = "99 25 32 13 9"\ndef RUN():\n    return 1\n',
            ValueError,
            "out of bounds",
        ),
        (
            "app/job/schedule.py",
            'schedule = "alpha beta gamma delta omega"\ndef RUN():\n    return 1\n',
            ValueError,
            "invalid minute field",
        ),
        (
            "app/job/schedule.py",
            'schedule = "*/0 * * * *"\ndef RUN():\n    return 1\n',
            ValueError,
            "step must be >= 1",
        ),
        (
            "app/job/schedule.py",
            'schedule = "5-2 * * * *"\ndef RUN():\n    return 1\n',
            ValueError,
            "out of bounds",
        ),
        (
            "app/job/schedule.py",
            'schedule = "0 9 * * MON"\ndef RUN():\n    return 1\n',
            ValueError,
            "invalid day-of-week field",
        ),
        (
            "app/job/schedule.py",
            'schedule = "0 0 1 * 1"\ndef RUN():\n    return 1\n',
            ValueError,
            "cannot constrain both day-of-month",
        ),
        (
            "app/job/schedule.py",
            'schedule = "1,,2 * * * *"\ndef RUN():\n    return 1\n',
            ValueError,
            "empty item",
        ),
        (
            "app/job/schedule.py",
            'schedule = "0 * * * *"\n'
            'timezone = "Mars/Olympus"\n'
            "def RUN():\n"
            "    return 1\n",
            ValueError,
            "unknown IANA timezone",
        ),
        (
            "app/job/schedule.py",
            'name = "-bad"\nschedule = "0 * * * *"\ndef RUN():\n    return 1\n',
            ValueError,
            "invalid schedule name",
        ),
        (
            "app/items/[id]/schedule.py",
            'schedule = "0 * * * *"\ndef RUN():\n    return 1\n',
            ValueError,
            "dynamic segment",
        ),
    ],
)
def test_schedule_validation_errors(
    tmp_path: Path,
    isolated_project_imports: Any,
    rel_path: str,
    source: str,
    exc_type: type[Exception],
    match: str,
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "serr"\nversion = "0.1.0"\n',
            rel_path: source,
        },
    )
    with pytest.raises(exc_type, match=match):
        plan_build(project)


# @lat: [[tests#Vercel Schedules#Schedule name collision rejected]]
def test_schedule_name_collision_rejected(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "scollide"\nversion = "0.1.0"\n',
            "app/a/b/schedule.py": 'schedule = "0 * * * *"\ndef RUN():\n    return 1\n',
            "app/a-b/schedule.py": 'schedule = "0 * * * *"\ndef RUN():\n    return 2\n',
        },
    )
    with pytest.raises(ValueError, match="duplicate schedule name 'a-b'"):
        plan_build(project)


# @lat: [[tests#Vercel Schedules#Schedule handler KeyError returns 500]]
def test_schedule_handler_keyerror_returns_500_and_runs_once(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "skeyerr"\nversion = "0.1.0"\n',
            "app/buggy/schedule.py": (
                'schedule = "0 * * * *"\n'
                "calls = 0\n"
                "def RUN():\n"
                "    global calls\n"
                "    calls += 1\n"
                '    return {}["missing"]\n'
            ),
        },
    )

    app = create_app(project / "app")
    job = app.schedules[0]
    client = make_client(app)

    assert client("POST", "/_chano/schedules/nope").status_code == 404
    with pytest.raises(ScheduleNotFoundError):
        asyncio.run(app.run_schedule("nope"))

    resp = client("POST", "/_chano/schedules/buggy")
    assert resp.status_code == 500
    assert job.module.calls == 1

    with pytest.raises(KeyError, match="'missing'"):
        asyncio.run(app.run_schedule("buggy"))
    assert job.module.calls == 2


# @lat: [[tests#Vercel Schedules#Schedule dev trigger and CLI]]
def test_schedule_dev_trigger_and_cli(
    tmp_path: Path, capsys: Any, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "scli"\nversion = "0.1.0"\n',
            "app/cleanup/schedule.py": (
                'schedule = "0 * * * *"\n'
                'timezone = "UTC"\n'
                "def RUN():\n"
                '    return {"job": "cleanup"}\n'
            ),
            "app/reports/daily/schedule.py": (
                'schedule = "0 8 * * *"\n'
                "def RUN():\n"
                '    return {"job": "daily-report"}\n'
            ),
        },
    )
    app = create_app(project / "app")
    client = make_client(app)
    assert client("POST", "/_chano/schedules/cleanup").json() == {"job": "cleanup"}
    assert client("POST", "/_chano/schedules/reports-daily").json() == {
        "job": "daily-report"
    }
    assert client("POST", "/_chano/schedules/reports/daily").json() == {
        "job": "daily-report"
    }
    assert client("POST", "/_chano/schedules/_schedules/reports/daily").json() == {
        "job": "daily-report"
    }
    assert client("POST", "/_chano/schedules/nope").status_code == 404

    app_arg = str(project / "app")
    cli_main(["schedule", "list", "--app", app_arg])
    listed = capsys.readouterr().out
    assert "cleanup\t0 * * * *\ttimezone=UTC" in listed
    assert "reports-daily\t0 8 * * *" in listed

    cli_main(["schedule", "run", "cleanup", "--app", app_arg])
    assert json.loads(capsys.readouterr().out) == {"job": "cleanup"}

    cli_main(["schedule", "run", "reports/daily", "--app", app_arg])
    assert json.loads(capsys.readouterr().out) == {"job": "daily-report"}


# ---------------------------------------------------------------------------
# Vercel Workflows
# ---------------------------------------------------------------------------


# @lat: [[tests#Vercel Workflows#Config parsing and optional dependency]]
def test_workflow_config_parsing_and_optional_dependency(tmp_path: Path):
    # Verify Chano's own pyproject.toml does not require vercel-workflow.
    repo_pyproject = Path(__file__).parent.parent / "pyproject.toml"
    meta = tomllib.loads(repo_pyproject.read_text())
    deps = meta["project"]["dependencies"]
    assert not any("vercel-workflow" in dep for dep in deps)

    # Parse [[tool.vercel.workflows]] alongside [tool.chano].
    pyproject = (
        write_project(
            tmp_path,
            {
                "pyproject.toml": (
                    '[project]\nname = "wfdemo"\nversion = "0.1.0"\n'
                    '[tool.chano]\nbase_path = "/api"\n'
                    "[[tool.vercel.workflows]]\n"
                    'entrypoint = "app.workflows:wf"\n'
                    "[[tool.vercel.workflows]]\n"
                    'entrypoint = "app.billing.flows:registry"\n'
                )
            },
        )
        / "pyproject.toml"
    )

    cfg = load_config(pyproject)
    assert cfg == ChanoConfig(
        base_path="/api",
        workflows=(
            WorkflowConfig(entrypoint="app.workflows:wf"),
            WorkflowConfig(entrypoint="app.billing.flows:registry"),
        ),
    )
    assert cfg.workflows[0].module_name == "app.workflows"
    assert cfg.workflows[0].object_name == "wf"


# @lat: [[tests#Vercel Workflows#Config validation errors]]
@pytest.mark.parametrize(
    ("toml_text", "match"),
    [
        (
            '[[tool.vercel.workflows]]\nentrypoint = "no_colon"\n',
            "invalid workflow entrypoint",
        ),
        (
            '[[tool.vercel.workflows]]\nentrypoint = "app.wf:a:b"\n',
            "invalid workflow entrypoint",
        ),
        (
            '[[tool.vercel.workflows]]\nentrypoint = "1bad:wf"\n',
            "invalid workflow entrypoint",
        ),
        (
            "[[tool.vercel.workflows]]\n"
            'entrypoint = "app.wf:wf"\n'
            "[[tool.vercel.workflows]]\n"
            'entrypoint = "app.wf:wf"\n',
            "duplicate",
        ),
        (
            '[tool.vercel]\nworkflows = "not-a-list"\n',
            "expected an array of tables",
        ),
        (
            "[tool.chano]\nworkflows = []\n",
            "invalid \\[tool.chano\\]",
        ),
    ],
)
def test_workflow_config_validation_errors(tmp_path: Path, toml_text: str, match: str):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(toml_text)
    with pytest.raises(ValueError, match=match):
        load_config(pyproject)


# @lat: [[tests#Vercel Workflows#Build output and sequential replays]]
def test_workflow_build_output_and_sequential_replays(
    tmp_path: Path,
    monkeypatch: Any,
    isolated_project_imports: Any,
    stub_build_install: Any,
):
    """Build introspects real vercel-workflow subscriptions, emits flow.func
    matching actual SDK subscriptions (no unbacked step.func), traces step
    imports, and reflects WORKFLOW_SEQUENTIAL_REPLAYS=1."""
    monkeypatch.setenv("WORKFLOW_LOCAL_DATA_DIR", str(tmp_path / ".wf-data"))
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "wfbuild"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_pkg.workflows:wf"\n'
            ),
            "app/route.py": 'def GET():\n    return {"hello": "world"}\n',
            "wf_test_pkg/__init__.py": "",
            "wf_test_pkg/workflows.py": (
                "from vercel import workflow\n"
                "from wf_test_pkg import steps\n"
                'wf = workflow.Workflows(namespace="wfbuild")\n'
            ),
            "wf_test_pkg/steps.py": "import wf_step_dep\n",
        },
    )

    monkeypatch.setitem(sys.modules, "wf_step_dep", types.ModuleType("wf_step_dep"))
    stub_build_install(
        {
            **make_dist("chano", "chano"),
            **make_dist("vercel-runtime", "vercel_runtime"),
            **make_dist("vercel-workflow", "vercel/workflow"),
            **make_dist("vercel-queue", "vercel/queue"),
            **make_dist(
                "wf-step-dep",
                "wf_step_dep",
                extra_files={"wf_step_dep/__init__.py": "X = 1\n"},
            ),
        }
    )

    monkeypatch.delenv("WORKFLOW_SEQUENTIAL_REPLAYS", raising=False)
    plan = plan_build(project, python="3.12", platform=None)
    by_name = {f.name: f for f in plan.functions}
    # Python vercel-workflow registers only the workflow topic (no phantom step.func).
    assert set(by_name) == {"index", ".well-known/workflow/v1/flow"}

    flow_spec = by_name[".well-known/workflow/v1/flow"]
    assert flow_spec.vc_config == {
        "runtime": "python3.12",
        "handler": "vc__handler__python.vc_handler",
        "supportsResponseStreaming": True,
        "maxDuration": 60,
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "experimentalTriggers": [
            {
                "type": "queue/v2beta",
                "topic": "__wfbuild_wkf_workflow_*",
                "consumer": "default",
                "retryAfterSeconds": 5,
                "initialDelaySeconds": 0,
            }
        ],
    }
    out = emit_build(plan)
    config_json = json.loads((out / "config.json").read_text())
    assert config_json == {
        "version": 3,
        "routes": [
            {"handle": "filesystem"},
            {"src": "^/docs$", "dest": "/docs.html"},
        ],
    }
    openapi_doc = json.loads((out / "static" / "openapi.json").read_text())
    assert list(openapi_doc["paths"]) == ["/"]

    flow_dists = set(
        build_module.index_store(
            out
            / "functions"
            / ".well-known"
            / "workflow"
            / "v1"
            / "flow.func"
            / "_vendor"
        ).distributions
    )
    route_dists = set(
        build_module.index_store(
            out / "functions" / "index.func" / "_vendor"
        ).distributions
    )
    assert {"wf-step-dep", "vercel-workflow", "vercel-queue"} <= flow_dists
    assert "wf-step-dep" not in route_dists
    assert "vercel-workflow" not in route_dists

    # WORKFLOW_SEQUENTIAL_REPLAYS=1 sets maxConcurrency: 1 on flow trigger.
    monkeypatch.setenv("WORKFLOW_SEQUENTIAL_REPLAYS", "1")
    seq_plan = plan_build(project, python="3.12", platform=None)
    seq_by_name = {f.name: f for f in seq_plan.functions}
    assert seq_by_name[".well-known/workflow/v1/flow"].vc_config[
        "experimentalTriggers"
    ] == [
        {
            "type": "queue/v2beta",
            "topic": "__wfbuild_wkf_workflow_*",
            "consumer": "default",
            "retryAfterSeconds": 5,
            "initialDelaySeconds": 0,
            "maxConcurrency": 1,
        }
    ]


# @lat: [[tests#Vercel Workflows#Build fails without SDK or subscriptions]]
def test_workflow_build_fails_without_sdk_or_subscriptions(
    tmp_path: Path, monkeypatch: Any, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "wfnosdkbuild"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_nosdk_b:wf"\n'
            ),
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
            "wf_test_nosdk_b.py": (
                "from vercel import workflow\n"
                'wf = workflow.Workflows(namespace="computed")\n'
            ),
        },
    )

    # Without vercel-workflow / vercel-queue in build env, fail loudly (no fallback).
    monkeypatch.setitem(sys.modules, "vercel.queue", None)
    monkeypatch.setitem(sys.modules, "vercel.workflow", None)
    with pytest.raises(
        RuntimeError, match="must be installed in the build environment"
    ):
        plan_build(project, python="3.12", platform=None)

    # Restore SDK; non-serving registry (as_vercel_job=False) fails even when
    # another serving registry exists in the same module with the same namespace.
    monkeypatch.undo()
    monkeypatch.setenv("WORKFLOW_LOCAL_DATA_DIR", str(tmp_path / ".wf-own"))
    shared_mod = (
        "from vercel import workflow\n"
        'serving = workflow.Workflows(namespace="shared")\n'
        'non_serving = workflow.Workflows(namespace="shared", as_vercel_job=False)\n'
        'unconfigured = workflow.Workflows(namespace="other")\n'
    )
    unbacked = write_project(
        tmp_path / "unbacked",
        {
            "pyproject.toml": (
                '[project]\nname = "wfunbacked"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_ownership:non_serving"\n'
            ),
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
            "wf_test_ownership.py": shared_mod,
        },
    )
    with pytest.raises(RuntimeError, match="does not serve queue deliveries"):
        plan_build(unbacked, python="3.12", platform=None)

    # Configuring only `serving` emits ONLY `__shared_wkf_workflow_*` and does
    # NOT emit `unconfigured`'s `__other_wkf_workflow_*` subscription.
    configured = write_project(
        tmp_path / "configured",
        {
            "pyproject.toml": (
                '[project]\nname = "wfconfigured"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_ownership:serving"\n'
            ),
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
            "wf_test_ownership.py": shared_mod,
        },
    )
    monkeypatch.delenv("WORKFLOW_SEQUENTIAL_REPLAYS", raising=False)
    plan = plan_build(configured, python="3.12", platform=None)
    by_name = {f.name: f for f in plan.functions}
    assert by_name[".well-known/workflow/v1/flow"].vc_config[
        "experimentalTriggers"
    ] == [
        {
            "type": "queue/v2beta",
            "topic": "__shared_wkf_workflow_*",
            "consumer": "default",
            "retryAfterSeconds": 5,
            "initialDelaySeconds": 0,
        }
    ]


# @lat: [[tests#Vercel Workflows#Local dev without SDK is graceful]]
def test_workflow_local_dev_without_sdk_is_graceful(
    tmp_path: Path, monkeypatch: Any, isolated_project_imports: Any
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "wfnosdk"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_nosdk:wf"\n'
            ),
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
            "wf_test_nosdk.py": (
                "from vercel import workflow\nwf = workflow.Workflows()\n"
            ),
        },
    )

    monkeypatch.setitem(sys.modules, "vercel", None)
    monkeypatch.setitem(sys.modules, "vercel.workflow", None)
    monkeypatch.setitem(sys.modules, "vercel.queue", None)

    app = create_app(project / "app")
    assert app.workflows == (WorkflowConfig(entrypoint="wf_test_nosdk:wf"),)
    client = make_client(app)
    assert client("GET", "/").json() == {"ok": True}
    assert client("POST", "/.well-known/workflow/v1/flow").status_code == 404

    write_project(project, {"wf_test_plain.py": "wf = object()\n"})
    sys.path.insert(0, str(project))
    with pytest.raises(RuntimeError, match="vercel-workflow is required"):
        create_workflow_app(["wf_test_plain:wf"])


# @lat: [[tests#Vercel Workflows#Partial SDK fallback to http_handler]]
def test_workflow_app_falls_back_on_partial_vercel_sdk(
    tmp_path: Path, monkeypatch: Any, isolated_project_imports: Any
):
    project = tmp_path / "wf-partial-sdk"
    write_project(
        project,
        {
            "wf_test_partial.py": (
                "from types import SimpleNamespace\n\n"
                "class _Registry:\n"
                "    namespace = None\n\n"
                "    async def http_handler(self, req):\n"
                "        return SimpleNamespace(\n"
                "            status=200,\n"
                '            headers={"content-type": "application/json"},\n'
                "            body=b'{\"fallback\": true}',\n"
                "        )\n\n"
                "wf = _Registry()\n"
            ),
            "vercel_runtime/__init__.py": "",
            "vercel_runtime/workers.py": "import vercel_workflow.dependencies\n",
            "vercel_workflow/__init__.py": "",
        },
    )
    sys.path.insert(0, str(project))

    fake_vr = types.ModuleType("vercel_runtime")
    fake_vr.__path__ = [str(project / "vercel_runtime")]
    monkeypatch.setitem(sys.modules, "vercel_runtime", fake_vr)
    monkeypatch.delitem(sys.modules, "vercel_runtime.workers", raising=False)
    monkeypatch.delitem(sys.modules, "vercel_workflow", raising=False)
    monkeypatch.delitem(sys.modules, "vercel_workflow.dependencies", raising=False)

    app = create_workflow_app(["wf_test_partial:wf"])
    client = make_client(app)
    resp = client("POST", "/.well-known/workflow/v1/flow", json={"ok": True})
    assert resp.status_code == 200
    assert resp.json() == {"fallback": True}


# @lat: [[tests#Vercel Workflows#Real vercel-workflow SDK end-to-end run and build]]
@pytest.mark.slow
def test_workflow_real_sdk_end_to_end_run_and_build(
    tmp_path: Path, monkeypatch: Any, isolated_project_imports: Any
):
    """End-to-end test using the REAL vercel-workflow + vercel-queue SDKs:
    runs a real workflow with a @wf.step to completion under create_app(),
    verifies mounted flow/manifest endpoints, and verifies emitted
    .vc-config.json triggers match vercel.queue.get_subscriptions()."""
    import vercel.queue as vq
    from vercel import workflow

    monkeypatch.setenv("WORKFLOW_LOCAL_DATA_DIR", str(tmp_path / ".workflow-data"))
    monkeypatch.setenv("WORKFLOW_PUBLIC_MANIFEST", "1")
    monkeypatch.delenv("WORKFLOW_SEQUENTIAL_REPLAYS", raising=False)

    project = write_project(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "wfe2e"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_e2e:wf"\n'
            ),
            "app/route.py": 'def GET():\n    return {"app": "ok"}\n',
            "wf_test_e2e.py": (
                "from vercel import workflow\n"
                'wf = workflow.Workflows(namespace="e2e")\n'
                "@wf.step\n"
                "async def double(n: int) -> int:\n"
                "    return n * 2\n"
                "@wf.workflow\n"
                "async def pipeline(n: int) -> int:\n"
                "    return (await double(n)) + 1\n"
            ),
        },
    )

    app = create_app(project / "app")

    async def run_end_to_end() -> tuple[Any, Any, Any, Any]:
        in_q: asyncio.Queue[Message] = asyncio.Queue()
        out_q: asyncio.Queue[Message] = asyncio.Queue()
        lifespan_task = asyncio.create_task(
            app({"type": "lifespan"}, in_q.get, out_q.put)
        )
        await in_q.put({"type": "lifespan.startup"})
        assert (await out_q.get())["type"] == "lifespan.startup.complete"
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                health = await http.post("/.well-known/workflow/v1/flow?__health")
                manifest = await http.get("/.well-known/workflow/v1/manifest.json")
                schema = await http.get("/openapi.json")

            wf_mod = importlib.import_module("wf_test_e2e")
            run = await workflow.start(wf_mod.pipeline, 20)
            result = await run.return_value()
            status = await run.status()
            return health, manifest, schema, (result, status)
        finally:
            await in_q.put({"type": "lifespan.shutdown"})
            assert (await out_q.get())["type"] == "lifespan.shutdown.complete"
            await lifespan_task

    health, manifest, schema, (result, status) = asyncio.run(run_end_to_end())
    assert health.status_code == 200
    assert health.json()["healthy"] is True
    assert manifest.status_code == 200
    assert "wf_test_e2e.py" in manifest.json()["workflows"]
    assert list(schema.json()["paths"]) == ["/"]
    assert result == 41
    assert status == "completed"

    # Emitted .vc-config.json triggers match the SDK's actual subscriptions.
    plan = plan_build(project, python="3.12", platform=None)
    by_name = {f.name: f for f in plan.functions}
    assert set(by_name) == {"index", ".well-known/workflow/v1/flow"}
    emitted = by_name[".well-known/workflow/v1/flow"].vc_config["experimentalTriggers"]
    sdk_topics = {s.topic for s in vq.get_subscriptions()}
    assert emitted == [
        {
            "type": "queue/v2beta",
            "topic": "__e2e_wkf_workflow_*",
            "consumer": "default",
            "retryAfterSeconds": 5,
            "initialDelaySeconds": 0,
        }
    ]
    assert "__e2e_wkf_workflow_*" in sdk_topics


# @lat: [[tests#Vercel Workflows#Ignores unrelated queue subscribers]]
def test_workflow_ignores_unrelated_queue_subscriber_on_step_topic(
    tmp_path: Path, monkeypatch: Any, isolated_project_imports: Any
):
    """Real-SDK regression test: an unrelated @vercel.queue.subscribe on
    `__review_wkf_step_*` must NOT be attributed to Workflows(namespace="review")."""
    import vercel.queue as vq

    monkeypatch.setenv("WORKFLOW_LOCAL_DATA_DIR", str(tmp_path / ".wf-review"))
    monkeypatch.delenv("WORKFLOW_SEQUENTIAL_REPLAYS", raising=False)

    project = write_project(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "wfreview"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_review:wf"\n'
            ),
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
            "wf_test_review.py": (
                "from vercel import workflow\n"
                "from vercel.queue import subscribe\n"
                'wf = workflow.Workflows(namespace="review")\n'
                '@subscribe(topic="__review_wkf_step_*", consumer_group="unrelated")\n'
                "async def unrelated_subscriber(payload: bytes) -> None:\n"
                "    return None\n"
            ),
        },
    )

    plan = plan_build(project, python="3.12", platform=None)
    by_name = {f.name: f for f in plan.functions}
    assert set(by_name) == {"index", ".well-known/workflow/v1/flow"}
    assert by_name[".well-known/workflow/v1/flow"].vc_config[
        "experimentalTriggers"
    ] == [
        {
            "type": "queue/v2beta",
            "topic": "__review_wkf_workflow_*",
            "consumer": "default",
            "retryAfterSeconds": 5,
            "initialDelaySeconds": 0,
        }
    ]
    # Confirm the unrelated subscriber really was registered in vercel.queue
    # yet ignored by workflow trigger introspection.
    assert any(
        s.topic == "__review_wkf_step_*" and s.consumer_group == "unrelated"
        for s in vq.get_subscriptions()
    )


# @lat: [[tests#Vercel Workflows#Real SDK shape local dev and build]]
def test_workflow_real_sdk_shape_local_dev_and_build(
    tmp_path: Path,
    monkeypatch: Any,
    isolated_project_imports: Any,
    stub_build_install: Any,
):
    """Tests the real vercel-workflow SDK API shape: Workflows(namespace=...),
    http_handler(HTTPRequest) -> HTTPResponse, manifest_handler, ENDPOINT_PATH,
    MANIFEST_PATH, get_queue_topic_prefix, vercel.queue.get_subscriptions, and
    vercel_runtime.workers.create_queue_service_app."""
    registered_subs: list[Any] = []

    @dataclasses.dataclass(frozen=True)
    class FakeSubscription:
        topic: str
        func: Any = None
        consumer_group: str = "default"
        retry_after_seconds: int | None = None
        initial_delay_seconds: int | None = None
        max_concurrency: int | None = None

    def _make_owned_callback(registry: Any) -> Any:
        bound = functools.partial(lambda **_: None, registry=registry)

        async def async_handler(_msg: Any) -> None:
            return bound()

        return async_handler

    @dataclasses.dataclass
    class FakeHTTPResponse:
        status: int
        body: bytes
        headers: dict[str, str]

        @classmethod
        def json(cls, data: Any, *, status: int = 200) -> FakeHTTPResponse:
            return cls(
                status,
                json.dumps(data).encode("utf-8"),
                {"content-type": "application/json"},
            )

    def fake_get_queue_topic_prefix(namespace: str | None = None) -> str:
        if namespace is not None:
            return f"__{namespace}_wkf_workflow_"
        return "__wkf_workflow_"

    class FakeWorkflows:
        def __init__(self, *, namespace: str | None = None) -> None:
            self._namespace = namespace
            prefix = fake_get_queue_topic_prefix(namespace)
            registered_subs.append(
                FakeSubscription(
                    topic=f"{prefix}*",
                    func=_make_owned_callback(self),
                    consumer_group="default",
                )
            )

        @property
        def namespace(self) -> str | None:
            return self._namespace

        @property
        def http_handler(self) -> Any:
            ns = self._namespace

            async def handle(request: Any) -> FakeHTTPResponse:
                body = b"".join([chunk async for chunk in request.aiter_bytes()])
                return FakeHTTPResponse.json(
                    {
                        "namespace": ns,
                        "method": request.method,
                        "url": request.url,
                        "queue": request.headers.get("x-vqs-queue-name"),
                        "body": json.loads(body) if body else None,
                    }
                )

            return handle

        @property
        def manifest_handler(self) -> Any:
            ns = self._namespace

            async def handle(request: Any) -> FakeHTTPResponse:
                return FakeHTTPResponse.json(
                    {"version": "1", "namespace": ns, "url": request.url}
                )

            return handle

    fake_vercel: Any = types.ModuleType("vercel")
    fake_workflow: Any = types.ModuleType("vercel.workflow")
    fake_wf_internal: Any = types.ModuleType("vercel.workflow._internal")
    fake_wf_world: Any = types.ModuleType("vercel.workflow._internal.world")
    fake_queue: Any = types.ModuleType("vercel.queue")

    fake_workflow.ENDPOINT_PATH = "/.well-known/workflow/v1/flow"
    fake_workflow.MANIFEST_PATH = "/.well-known/workflow/v1/manifest.json"
    fake_workflow.Workflows = FakeWorkflows
    fake_workflow.HTTPResponse = FakeHTTPResponse
    fake_wf_world.get_queue_topic_prefix = fake_get_queue_topic_prefix
    fake_queue.get_subscriptions = lambda: tuple(registered_subs)
    fake_vercel.workflow = fake_workflow
    fake_vercel.queue = fake_queue

    monkeypatch.setitem(sys.modules, "vercel", fake_vercel)
    monkeypatch.setitem(sys.modules, "vercel.workflow", fake_workflow)
    monkeypatch.setitem(sys.modules, "vercel.workflow._internal", fake_wf_internal)
    monkeypatch.setitem(sys.modules, "vercel.workflow._internal.world", fake_wf_world)
    monkeypatch.setitem(sys.modules, "vercel.queue", fake_queue)

    project = write_project(
        tmp_path / "proj",
        {
            "pyproject.toml": (
                '[project]\nname = "wfsdk"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_real:default_wf"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_real:payments_wf"\n'
            ),
            "app/route.py": 'def GET():\n    return {"app": "ok"}\n',
            "wf_test_real.py": (
                "from vercel.workflow import Workflows\n"
                "default_wf = Workflows()\n"
                'payments_wf = Workflows(namespace="payments")\n'
            ),
        },
    )

    app = create_app(project / "app")
    client = make_client(app)
    assert client("GET", "/").json() == {"app": "ok"}

    resp_default = client(
        "POST",
        "/.well-known/workflow/v1/flow?x=1",
        json={"run": "r1"},
        headers={"x-vqs-queue-name": "__wkf_workflow_abc"},
    )
    assert resp_default.status_code == 200
    assert resp_default.json() == {
        "namespace": None,
        "method": "POST",
        "url": "/.well-known/workflow/v1/flow?x=1",
        "queue": "__wkf_workflow_abc",
        "body": {"run": "r1"},
    }

    resp_pay = client(
        "POST",
        "/.well-known/workflow/v1/flow",
        json={"run": "r2"},
        headers={"x-vqs-queue-name": "__payments_wkf_workflow_xyz"},
    )
    assert resp_pay.status_code == 200
    assert resp_pay.json()["namespace"] == "payments"
    assert resp_pay.json()["body"] == {"run": "r2"}

    manifest_resp = client("GET", "/.well-known/workflow/v1/manifest.json")
    assert manifest_resp.status_code == 200
    assert manifest_resp.json()["version"] == "1"

    assert list(client("GET", "/openapi.json").json()["paths"]) == ["/"]

    # Build planning introspects namespaces from live SDK registries & subscriptions.
    monkeypatch.delenv("WORKFLOW_SEQUENTIAL_REPLAYS", raising=False)
    plan = plan_build(project, python="3.12", platform=None)
    by_name = {f.name: f for f in plan.functions}
    assert set(by_name) == {"index", ".well-known/workflow/v1/flow"}
    assert by_name[".well-known/workflow/v1/flow"].vc_config[
        "experimentalTriggers"
    ] == [
        {
            "type": "queue/v2beta",
            "topic": "__wkf_workflow_*",
            "consumer": "default",
            "retryAfterSeconds": 5,
            "initialDelaySeconds": 0,
        },
        {
            "type": "queue/v2beta",
            "topic": "__payments_wkf_workflow_*",
            "consumer": "default",
            "retryAfterSeconds": 5,
            "initialDelaySeconds": 0,
        },
    ]

    # Unowned step subscription is NOT attributed; only a registry-owned one is.
    registered_subs.append(
        FakeSubscription(topic="__wkf_step_*", consumer_group="unrelated")
    )
    plan_unrelated = plan_build(project, python="3.12", platform=None)
    assert ".well-known/workflow/v1/step" not in {
        f.name for f in plan_unrelated.functions
    }

    default_reg = importlib.import_module("wf_test_real").default_wf
    registered_subs.append(
        FakeSubscription(
            topic="__wkf_step_*",
            func=_make_owned_callback(default_reg),
            consumer_group="default",
        )
    )
    plan_with_step = plan_build(
        project, python="3.12", platform=None, output=tmp_path / "emitted_out"
    )
    by_name_step = {f.name: f for f in plan_with_step.functions}
    assert by_name_step[".well-known/workflow/v1/step"].vc_config[
        "experimentalTriggers"
    ] == [
        {
            "type": "queue/v2beta",
            "topic": "__wkf_step_*",
            "consumer": "default",
            "retryAfterSeconds": 5,
            "initialDelaySeconds": 0,
        }
    ]

    # Deployed factory uses vercel_runtime.workers.create_queue_service_app.
    async def queue_service_asgi(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        body = json.dumps({"served_by": "create_queue_service_app"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    fake_vr: Any = types.ModuleType("vercel_runtime")
    fake_workers: Any = types.ModuleType("vercel_runtime.workers")
    fake_workers.create_queue_service_app = lambda: queue_service_asgi
    fake_vr.workers = fake_workers
    monkeypatch.setitem(sys.modules, "vercel_runtime", fake_vr)
    monkeypatch.setitem(sys.modules, "vercel_runtime.workers", fake_workers)

    wf_app = create_workflow_app(["wf_test_real:default_wf"])
    assert make_client(wf_app)("POST", "/.well-known/workflow/v1/flow").json() == {
        "served_by": "create_queue_service_app"
    }
    assert make_client(wf_app)("POST", "/.well-known/workflow/v1/step").json() == {
        "served_by": "create_queue_service_app"
    }

    stub_build_install(
        {
            **make_dist("chano", "chano"),
            **make_dist("vercel-runtime", "vercel_runtime"),
            **make_dist("vercel-workflow", "vercel/workflow"),
            **make_dist("vercel-queue", "vercel/queue"),
        }
    )
    emitted_out = emit_build(plan_with_step)
    shutil.rmtree(project)

    runner = (
        "import asyncio, json, sys, types, httpx\n"
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class FakeHTTPResponse:\n"
        "    status: int\n"
        "    body: bytes\n"
        "    headers: dict[str, str]\n"
        "class FakeWorkflows:\n"
        "    def __init__(self, *, namespace=None):\n"
        "        self.namespace = namespace\n"
        "    @property\n"
        "    def http_handler(self):\n"
        "        ns = self.namespace\n"
        "        async def handle(request):\n"
        '            body = b"".join([c async for c in request.aiter_bytes()])\n'
        "            return FakeHTTPResponse(\n"
        "                200,\n"
        "                json.dumps({\n"
        '                    "namespace": ns,\n'
        '                    "method": request.method,\n'
        '                    "url": request.url,\n'
        '                    "queue": request.headers.get("x-vqs-queue-name"),\n'
        '                    "body": json.loads(body) if body else None,\n'
        "                }).encode(),\n"
        '                {"content-type": "application/json"},\n'
        "            )\n"
        "        return handle\n"
        'fake_vercel = types.ModuleType("vercel")\n'
        'fake_wf = types.ModuleType("vercel.workflow")\n'
        "fake_wf.Workflows = FakeWorkflows\n"
        "fake_vercel.workflow = fake_wf\n"
        'sys.modules["vercel"] = fake_vercel\n'
        'sys.modules["vercel.workflow"] = fake_wf\n'
        'sys.modules["vercel_runtime"] = None\n'
        'sys.modules["vercel_runtime.workers"] = None\n'
        "import index\n"
        "func_kind, queue_name = sys.argv[1], sys.argv[2]\n"
        "async def run():\n"
        "    async with httpx.AsyncClient(\n"
        "        transport=httpx.ASGITransport(app=index.app),\n"
        '        base_url="http://test",\n'
        "    ) as client:\n"
        "        r = await client.post(\n"
        '            f"/.well-known/workflow/v1/{func_kind}",\n'
        '            json={"run": func_kind},\n'
        '            headers={"x-vqs-queue-name": queue_name},\n'
        "        )\n"
        '        print(json.dumps({"status": r.status_code, "body": r.json()}))\n'
        "asyncio.run(run())\n"
    )
    for func_kind, queue_name in (
        ("flow", "__payments_wkf_workflow_emitted"),
        ("step", "__payments_wkf_step_emitted"),
    ):
        func_dir = (
            emitted_out
            / "functions"
            / ".well-known"
            / "workflow"
            / "v1"
            / f"{func_kind}.func"
        )
        proc = subprocess.run(
            [sys.executable, "-c", runner, func_kind, queue_name],
            cwd=func_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload == {
            "status": 200,
            "body": {
                "namespace": "payments",
                "method": "POST",
                "url": f"/.well-known/workflow/v1/{func_kind}",
                "queue": queue_name,
                "body": {"run": func_kind},
            },
        }


# @lat: [[tests#Combined Vercel primitives]]
def test_combined_build_with_all_primitives(
    tmp_path: Path,
    monkeypatch: Any,
    isolated_project_imports: Any,
    stub_build_install: Any,
):
    monkeypatch.setenv("WORKFLOW_LOCAL_DATA_DIR", str(tmp_path / ".wf-data"))
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "combined"\nversion = "0.1.0"\n'
                "[[tool.vercel.workflows]]\n"
                'entrypoint = "wf_test_combined:wf"\n'
            ),
            "app/route.py": ('def GET():\n    return {"ok": True}\n'),
            "app/orders/queue.py": (
                'topic = "combined-orders"\n'
                'def CONSUME(payload: dict):\n    return {"ok": payload}\n'
            ),
            "app/cleanup/schedule.py": (
                'schedule = "0 * * * *"\ndef RUN():\n    return {"cleaned": True}\n'
            ),
            "wf_test_combined.py": (
                "from vercel import workflow\n"
                'wf = workflow.Workflows(namespace="billing")\n'
            ),
        },
    )

    monkeypatch.delenv("WORKFLOW_SEQUENTIAL_REPLAYS", raising=False)
    stub_build_install(
        {
            **make_dist("chano", "chano"),
            **make_dist("vercel-runtime", "vercel_runtime"),
            **make_dist("vercel-queue", "vercel/queue"),
            **make_dist("vercel-workflow", "vercel/workflow"),
        }
    )

    out = build(project, platform=None, output=tmp_path / "out")
    flow_cfg = json.loads(
        (
            out
            / "functions"
            / ".well-known"
            / "workflow"
            / "v1"
            / "flow.func"
            / ".vc-config.json"
        ).read_text()
    )
    assert flow_cfg["experimentalTriggers"] == [
        {
            "type": "queue/v2beta",
            "topic": "__billing_wkf_workflow_*",
            "consumer": "default",
            "retryAfterSeconds": 5,
            "initialDelaySeconds": 0,
        }
    ]
    assert (out / "functions" / "index.func" / ".vc-config.json").is_file()
    assert (out / "functions" / "_queues" / "orders.func" / ".vc-config.json").is_file()
    assert (
        out / "functions" / "_schedules" / "cleanup.func" / ".vc-config.json"
    ).is_file()
    config_json = json.loads((out / "config.json").read_text())
    assert config_json == {
        "version": 3,
        "routes": [
            {"handle": "filesystem"},
            {"src": "^/docs$", "dest": "/docs.html"},
        ],
        "schedules": {
            "cleanup": {
                "function": "_schedules/cleanup",
                "schedule": "0 * * * *",
            }
        },
    }


# @lat: [[tests#Boundary#HTTP-only boundary scoping for queues and schedules]]
def test_boundary_is_http_only_for_routes_queues_and_schedules(
    tmp_path: Path,
    monkeypatch: Any,
    isolated_project_imports: Any,
    stub_build_install: Any,
):
    project = write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "httpbound"\nversion = "0.1.0"\n',
            "app/boundary.py": (
                "import boundary_only_pkg\n"
                "from chano import (\n"
                "    Boundary, JSONResponse, RedirectResponse, Request\n"
                ")\n\n"
                "events = []\n\n"
                "def require_auth(request: Request):\n"
                '    events.append(("guard", request.path))\n'
                '    if request.headers.get("authorization") != "Bearer secret":\n'
                '        return RedirectResponse("/login")\n\n'
                "def recover_value(request: Request, exc: ValueError):\n"
                '    events.append(("recover", str(exc)))\n'
                '    return JSONResponse({"recovered": str(exc)}, status_code=200)\n\n'
                "async def add_header(request: Request, call_next):\n"
                '    events.append(("wrap", request.path))\n'
                "    resp = await call_next(request)\n"
                '    resp.headers["x-boundary"] = boundary_only_pkg.TAG\n'
                "    return resp\n\n"
                "boundary = Boundary(\n"
                "    guards=[require_auth],\n"
                "    recover=[recover_value],\n"
                "    wrap=[add_header],\n"
                ")\n"
            ),
            "app/orders/route.py": (
                "def GET(fail: bool = False):\n"
                "    if fail:\n"
                '        raise ValueError("route-boom")\n'
                '    return {"ok": True}\n'
            ),
            "app/orders/queue.py": (
                "from chano import Depends, after\n"
                'topic = "orders"\n'
                "after_log = []\n"
                "def get_prefix() -> str:\n"
                '    return "q"\n'
                "async def CONSUME(\n"
                "    payload: dict,\n"
                "    prefix: str = Depends(get_prefix),\n"
                "):\n"
                '    if payload.get("fail"):\n'
                '        raise ValueError("queue-boom")\n'
                "    tag = f\"{prefix}:{payload['id']}\"\n"
                "    after(lambda v: after_log.append(v), tag)\n"
                '    return {"handled": tag}\n'
            ),
            "app/orders/schedule.py": (
                "from chano import Depends, after\n"
                'schedule = "0 * * * *"\n'
                "after_log = []\n"
                "def get_label() -> str:\n"
                '    return "nightly"\n'
                "async def RUN(label: str = Depends(get_label)):\n"
                "    after(lambda v: after_log.append(v), label)\n"
                '    return {"ran": label}\n'
            ),
            "app/orders/failing/schedule.py": (
                'schedule = "0 * * * *"\n'
                "def RUN():\n"
                '    raise ValueError("schedule-boom")\n'
            ),
        },
    )

    fake_pkg: Any = types.ModuleType("boundary_only_pkg")
    fake_pkg.TAG = "yes"
    monkeypatch.setitem(sys.modules, "boundary_only_pkg", fake_pkg)

    # 1. HTTP route enforces guards, recover, and wrap.
    app = create_app(project / "app")
    client = make_client(app)

    unauth = client("GET", "/orders")
    assert unauth.status_code == 307
    assert unauth.headers["location"] == "/login"
    assert unauth.headers["x-boundary"] == "yes"

    ok = client("GET", "/orders", headers={"authorization": "Bearer secret"})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
    assert ok.headers["x-boundary"] == "yes"

    recovered = client(
        "GET", "/orders?fail=true", headers={"authorization": "Bearer secret"}
    )
    assert recovered.status_code == 200
    assert recovered.json() == {"recovered": "route-boom"}
    assert recovered.headers["x-boundary"] == "yes"

    from chano.routing import load_module

    boundary_mod = load_module((project / "app" / "boundary.py").resolve())
    boundary_mod.events.clear()

    # 2. Queue ignores ancestor boundary.py (local & deployed), keeps Depends/after.
    q_res = asyncio.run(app.dispatch_queue("orders", {"id": "1"}))
    assert q_res == {"handled": "q:1"}
    q_invoked = asyncio.run(invoke_queue(project / "app", "orders", {"id": "2"}))
    assert q_invoked == {"handled": "q:2"}
    orders_queue = next(q for q in app.queues if q.trigger.topic == "orders")
    assert orders_queue.module.after_log == ["q:1", "q:2"]

    with pytest.raises(ValueError, match="queue-boom"):
        asyncio.run(app.dispatch_queue("orders", {"id": "3", "fail": True}))

    from chano.queues import _make_queue_subscriber

    subscriber = _make_queue_subscriber(orders_queue, "local")

    async def deliver(payload: dict[str, Any]) -> None:
        await subscriber(payload)

    asyncio.run(deliver({"id": "4"}))
    assert orders_queue.module.after_log == ["q:1", "q:2", "q:4"]
    with pytest.raises(ValueError, match="queue-boom"):
        asyncio.run(deliver({"id": "5", "fail": True}))

    monkeypatch.setitem(sys.modules, "vercel.queue", None)
    q_app = create_queue_app(project / "app", "_queues/orders")
    q_client = make_client(q_app)
    assert q_client("POST", "/_queues/orders", json={"id": "6"}).json() == {
        "handled": "q:6"
    }
    assert (
        q_client("POST", "/_queues/orders", json={"id": "7", "fail": True}).status_code
        == 500
    )

    # 3. Schedule ignores ancestor boundary.py (local & deployed), keeps Depends/after.
    s_res = asyncio.run(app.run_schedule("orders"))
    assert s_res == {"ran": "nightly"}
    s_invoked = asyncio.run(invoke_schedule(project / "app", "orders"))
    assert s_invoked == {"ran": "nightly"}
    orders_schedule = next(s for s in app.schedules if s.spec.name == "orders")
    assert orders_schedule.module.after_log == ["nightly", "nightly"]

    with pytest.raises(ValueError, match="schedule-boom"):
        asyncio.run(app.run_schedule("orders/failing"))

    s_app = create_schedule_app(project / "app", "_schedules/orders")
    s_client = make_client(s_app)
    assert s_client("POST", "/_schedules/orders").json() == {"ran": "nightly"}
    s_fail_app = create_schedule_app(project / "app", "_schedules/orders/failing")
    s_fail_client = make_client(s_fail_app)
    assert s_fail_client("POST", "/_schedules/orders/failing").status_code == 500

    # No boundary hook ever fired for any queue or schedule call.
    assert boundary_mod.events == []

    # 4. Build tracing: route traces boundary.py and bundles boundary-only-pkg;
    # queue and schedule do not trace boundary.py or bundle boundary-only-pkg.
    stub_build_install(
        {
            **make_dist("chano", "chano", extra_files={"chano/__init__.py": "X = 1\n"}),
            **make_dist("vercel-runtime", "vercel_runtime"),
            **make_dist("vercel", "vercel"),
            **make_dist("vercel-queue", "vercel_queue"),
            **make_dist(
                "boundary-only-pkg",
                "boundary_only_pkg",
                extra_files={"boundary_only_pkg/__init__.py": 'TAG = "yes"\n'},
            ),
        }
    )

    plan = plan_build(project, python="3.12", platform=None)
    by_name = {f.name: f for f in plan.functions}
    app_dir = (project / "app").resolve()
    boundary_file = app_dir / "boundary.py"

    assert boundary_file in by_name["orders"].trace_roots
    assert by_name["_queues/orders"].trace_roots == (app_dir / "orders" / "queue.py",)
    assert by_name["_schedules/orders"].trace_roots == (
        app_dir / "orders" / "schedule.py",
    )

    out = emit_build(plan)
    route_dists = set(
        build_module.index_store(
            out / "functions" / "orders.func" / "_vendor"
        ).distributions
    )
    queue_dists = set(
        build_module.index_store(
            out / "functions" / "_queues" / "orders.func" / "_vendor"
        ).distributions
    )
    schedule_dists = set(
        build_module.index_store(
            out / "functions" / "_schedules" / "orders.func" / "_vendor"
        ).distributions
    )
    assert "boundary-only-pkg" in route_dists
    assert "boundary-only-pkg" not in queue_dists
    assert "boundary-only-pkg" not in schedule_dists

    # 5. Private entrypoints never even import ancestor boundary.py (create_app is
    # never called on this project, and app/boundary.py raises at import time).
    private_project = write_project(
        tmp_path / "private_only",
        {
            "pyproject.toml": '[project]\nname = "privonly"\nversion = "0.1.0"\n',
            "app/boundary.py": (
                'raise RuntimeError("boundary.py must not be imported by private '
                'functions")\n'
            ),
            "app/jobs/queue.py": (
                'topic = "jobs"\n'
                "def CONSUME(payload: dict):\n"
                '    if payload.get("fail"):\n'
                '        raise RuntimeError("job-boom")\n'
                '    return {"job": payload["id"]}\n'
            ),
            "app/nightly/schedule.py": (
                'schedule = "0 0 * * *"\ndef RUN():\n    return {"nightly": True}\n'
            ),
        },
    )
    private_app_dir = private_project / "app"

    assert asyncio.run(invoke_queue(private_app_dir, "jobs", {"id": "j1"})) == {
        "job": "j1"
    }
    assert asyncio.run(invoke_schedule(private_app_dir, "nightly")) == {"nightly": True}

    # Deployed queue app via public create_queue_app with fake vercel.queue SDK.
    registered: list[Any] = []
    fake_vq: Any = types.ModuleType("vercel.queue")

    def fake_subscribe(**_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            registered.append(fn)
            return fn

        return decorator

    async def fake_accept_and_handle(
        body_iter: AsyncIterator[bytes], _headers: Mapping[str, str]
    ) -> None:
        chunks = [chunk async for chunk in body_iter]
        await registered[-1](b"".join(chunks))

    fake_vq.subscribe = fake_subscribe
    fake_vq.accept_and_handle = fake_accept_and_handle
    monkeypatch.setitem(sys.modules, "vercel.queue", fake_vq)
    monkeypatch.setitem(sys.modules, "vercel_runtime.workers", None)

    deployed_q_app = create_queue_app(private_app_dir, "_queues/jobs")
    deployed_q_client = make_client(deployed_q_app)
    assert (
        deployed_q_client("POST", "/_queues/jobs", json={"id": "j2"}).status_code == 204
    )
    assert (
        deployed_q_client(
            "POST", "/_queues/jobs", json={"id": "j3", "fail": True}
        ).status_code
        == 500
    )

    # Local fallback queue app and deployed schedule app also never import boundary.py.
    monkeypatch.setitem(sys.modules, "vercel.queue", None)
    fallback_q_client = make_client(create_queue_app(private_app_dir, "_queues/jobs"))
    assert fallback_q_client("POST", "/_queues/jobs", json={"id": "j4"}).json() == {
        "job": "j4"
    }

    deployed_s_client = make_client(
        create_schedule_app(private_app_dir, "_schedules/nightly")
    )
    assert deployed_s_client("POST", "/_schedules/nightly").json() == {"nightly": True}


# @lat: [[tests#Vercel Workflows#Multi-namespace step topic routing]]
def test_workflow_multi_namespace_step_prefix_routing(
    tmp_path: Path, monkeypatch: Any, isolated_project_imports: Any
):
    write_project(
        tmp_path,
        {
            "wf_test_step_routing.py": (
                "import json\n"
                "from types import SimpleNamespace\n"
                "\n"
                "class FakeRegistry:\n"
                "    def __init__(self, namespace=None):\n"
                "        self.namespace = namespace\n"
                "\n"
                "    async def http_handler(self, req):\n"
                "        chunks = [c async for c in req.aiter_bytes()]\n"
                "        return SimpleNamespace(\n"
                "            status=200,\n"
                '            headers={"content-type": "application/json"},\n'
                "            body=json.dumps({\n"
                '                "namespace": self.namespace,\n'
                '                "queue": req.headers.get("x-vqs-queue-name"),\n'
                '                "body": json.loads(b"".join(chunks) or b"{}"),\n'
                "            }).encode(),\n"
                "        )\n"
                "\n"
                "default_wf = FakeRegistry()\n"
                'payments_wf = FakeRegistry(namespace="payments")\n'
            ),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(sys.modules, "vercel_runtime", None)
    monkeypatch.setitem(sys.modules, "vercel_runtime.workers", None)

    app = create_workflow_app(
        ["wf_test_step_routing:default_wf", "wf_test_step_routing:payments_wf"]
    )
    client = make_client(app)

    flow_resp = client(
        "POST",
        "/.well-known/workflow/v1/flow",
        json={"kind": "flow"},
        headers={"x-vqs-queue-name": "__payments_wkf_workflow_1"},
    )
    assert flow_resp.status_code == 200
    assert flow_resp.json()["namespace"] == "payments"

    step_resp = client(
        "POST",
        "/.well-known/workflow/v1/step",
        json={"kind": "step"},
        headers={"x-vqs-queue-name": "__payments_wkf_step_1"},
    )
    assert step_resp.status_code == 200
    assert step_resp.json()["namespace"] == "payments"

    messages = iter([{"type": "websocket.receive"}, {"type": "http.disconnect"}])

    async def receive() -> Message:
        return next(messages)

    async def send(_message: Message) -> None:
        pass

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/.well-known/workflow/v1/flow",
        "query_string": b"",
        "headers": [],
    }

    async def invoke_raw() -> None:
        await app(scope, receive, send)

    with pytest.raises(ValueError, match="unexpected ASGI message"):
        asyncio.run(invoke_raw())


# @lat: [[tests#Vercel Queues#Consumer lookup strips whitespace]]
def test_queue_consumer_lookup_strips_whitespace(tmp_path: Path):
    project = write_project(
        tmp_path,
        {
            "app/orders/queue.py": (
                'topic = "orders"\n'
                'consumer = " default "\n'
                'def CONSUME(item_id: int):\n    return {"item_id": item_id}\n'
            ),
        },
    )
    result = asyncio.run(
        invoke_queue(project / "app", "orders", 7, consumer=" default ")
    )
    assert result == {"item_id": 7}
    assert asyncio.run(
        invoke_queue(project / "app", "orders", 8, consumer="default")
    ) == {"item_id": 8}


# @lat: [[tests#Vercel Workflows#Namespace validation and prefix fallback]]
@pytest.mark.parametrize(
    "bad_ns",
    ["", "1abc", "Upper", "a-b", "a_b"],
    ids=["empty", "leading-digit", "uppercase", "hyphen", "underscore"],
)
def test_workflow_rejects_invalid_namespace(bad_ns: str):
    with pytest.raises(ValueError, match="Invalid queue namespace"):
        validate_workflow_namespace(bad_ns)
    with pytest.raises(ValueError, match="Invalid queue namespace"):
        get_workflow_topic_prefix(bad_ns)
    with pytest.raises(ValueError, match="Invalid queue namespace"):
        get_step_topic_prefix(bad_ns)


# @lat: [[tests#Vercel Workflows#Topic prefix fallback and concurrency]]
def test_workflow_prefix_fallback_and_flow_trigger_concurrency(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(sys.modules, "vercel.workflow._internal.world", None)
    assert get_workflow_topic_prefix() == "__wkf_workflow_"
    assert get_workflow_topic_prefix("billing") == "__billing_wkf_workflow_"

    trigger = workflow_flow_trigger(max_concurrency=4, sequential_replays=False)
    assert trigger["maxConcurrency"] == 4


# @lat: [[tests#Vercel Workflows#Src trace roots and missing attribute]]
def test_workflow_trace_roots_src_layout_and_missing_attr(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path / "src_wf",
        {
            "src/my_wf/__init__.py": "wf = object()\n",
        },
    )
    roots = resolve_workflow_trace_roots([WorkflowConfig("my_wf:wf")], project)
    assert roots == ((project / "src" / "my_wf" / "__init__.py").resolve(),)

    with pytest.raises(AttributeError, match="attribute 'does_not_exist' not found"):
        load_workflow_entrypoint("chano.workflows:does_not_exist")


# @lat: [[tests#Vercel Workflows#Trigger introspection error paths]]
def test_workflow_introspection_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_project_imports: Any
):
    project = write_project(
        tmp_path / "wf_errs",
        {
            "wf_missing_sdk.py": "import vercel.workflow\nwf = object()\n",
            "wf_missing_user_dep.py": "import no_such_user_dep_xyz\nwf = object()\n",
            "wf_bad_ns.py": (
                "class _Reg:\n"
                "    namespace = 123\n"
                "    async def http_handler(self, req):\n"
                "        return None\n"
                "wf = _Reg()\n"
            ),
            "wf_no_subs.py": (
                "class _Reg:\n"
                '    namespace = "orphan"\n'
                "    async def http_handler(self, req):\n"
                "        return None\n"
                "wf = _Reg()\n"
            ),
        },
    )

    monkeypatch.setitem(sys.modules, "vercel.queue", types.ModuleType("vercel.queue"))
    with pytest.raises(RuntimeError, match="does not expose get_subscriptions"):
        introspect_workflow_triggers([WorkflowConfig("wf_no_subs:wf")], project)

    fake_queue: Any = types.ModuleType("vercel.queue")
    fake_queue.get_subscriptions = lambda: []
    monkeypatch.setitem(sys.modules, "vercel.queue", fake_queue)
    monkeypatch.setitem(sys.modules, "vercel.workflow", None)
    with pytest.raises(RuntimeError, match="'vercel-workflow' is not installed"):
        introspect_workflow_triggers([WorkflowConfig("wf_missing_sdk:wf")], project)

    with pytest.raises(ModuleNotFoundError, match="no_such_user_dep_xyz"):
        introspect_workflow_triggers(
            [WorkflowConfig("wf_missing_user_dep:wf")], project
        )

    with pytest.raises(ValueError, match="invalid namespace"):
        introspect_workflow_triggers([WorkflowConfig("wf_bad_ns:wf")], project)

    with pytest.raises(
        RuntimeError, match=r"did not register any vercel\.queue subscription"
    ):
        introspect_workflow_triggers([WorkflowConfig("wf_no_subs:wf")], project)


# @lat: [[tests#Vercel Workflows#ASGI adapter lifespan and chunking]]
def test_workflow_asgi_adapter_lifespan_chunking_disconnect_and_str_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_project_imports: Any
):
    project = write_project(
        tmp_path / "wf_asgi",
        {
            "wf_asgi_mod.py": (
                "from dataclasses import dataclass, field\n"
                "@dataclass\n"
                "class _Resp:\n"
                "    status: int\n"
                "    body: str\n"
                "    headers: dict[str, str] = field(default_factory=dict)\n"
                "class _Reg:\n"
                "    namespace = None\n"
                "    async def http_handler(self, req):\n"
                "        chunks = [c async for c in req.aiter_bytes(chunk_size=3)]\n"
                '        joined = "café:" + "|".join(c.decode() for c in chunks)\n'
                '        return _Resp(200, joined, {"x-ok": "1"})\n'
                "wf = _Reg()\n"
            ),
        },
    )
    sys.path.insert(0, str(project))
    monkeypatch.setitem(sys.modules, "vercel_runtime.workers", None)
    app = create_workflow_app(["wf_asgi_mod:wf"])

    async def run_asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await app(scope, receive, send)

    lifespan_msgs = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    lifespan_sent: list[Message] = []

    async def lifespan_receive() -> Message:
        return next(lifespan_msgs)

    async def lifespan_send(msg: Message) -> None:
        lifespan_sent.append(msg)

    asyncio.run(run_asgi({"type": "lifespan"}, lifespan_receive, lifespan_send))
    assert lifespan_sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]

    ws_sent: list[Message] = []

    async def ws_send(msg: Message) -> None:
        ws_sent.append(msg)

    asyncio.run(run_asgi({"type": "websocket"}, lifespan_receive, ws_send))
    assert ws_sent == []

    client = make_client(app)
    resp = client("POST", "/.well-known/workflow/v1/flow", content=b"abcdefg")
    assert resp.status_code == 200
    assert resp.headers["x-ok"] == "1"
    assert resp.content == "café:abc|def|g".encode()

    disc_msgs = iter(
        [
            {"type": "http.request", "body": b"hi", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    disc_sent: list[Message] = []

    async def disc_receive() -> Message:
        return next(disc_msgs)

    async def disc_send(msg: Message) -> None:
        disc_sent.append(msg)

    asyncio.run(
        run_asgi(
            {
                "type": "http",
                "method": "POST",
                "path": "/.well-known/workflow/v1/flow",
                "query_string": b"",
                "headers": [],
            },
            disc_receive,
            disc_send,
        )
    )
    assert disc_sent[0]["status"] == 200
    assert disc_sent[1]["body"] == "café:hi".encode()


# @lat: [[tests#Vercel Workflows#Dev mount edge cases]]
def test_workflow_mount_workflows_edge_cases(
    tmp_path: Path, isolated_project_imports: Any
):
    project = write_project(
        tmp_path / "wf_mount",
        {
            "wf_non_serving.py": (
                "class _Reg:\n"
                "    @property\n"
                "    def http_handler(self):\n"
                '        raise RuntimeError("not serving")\n'
                "wf = _Reg()\n"
            ),
            "wf_bad_dep.py": "import no_such_mount_dep_xyz\nwf = object()\n",
            "wf_no_manifest.py": (
                "from dataclasses import dataclass, field\n"
                "@dataclass\n"
                "class _Resp:\n"
                "    status: int\n"
                "    body: bytes\n"
                "    headers: dict[str, str] = field(default_factory=dict)\n"
                "class _Reg:\n"
                "    namespace = None\n"
                "    async def http_handler(self, req):\n"
                '        return _Resp(200, b"flow-ok")\n'
                "    @property\n"
                "    def manifest_handler(self):\n"
                '        raise RuntimeError("no manifest")\n'
                "wf = _Reg()\n"
            ),
        },
    )

    assert mount_workflows(Chano(docs=False), []) is False

    app_ns = Chano(docs=False)
    assert (
        mount_workflows(
            app_ns, [WorkflowConfig("wf_non_serving:wf")], project_root=project
        )
        is False
    )
    client_ns = make_client(app_ns)
    assert client_ns("POST", "/.well-known/workflow/v1/flow").status_code == 404
    assert client_ns("GET", "/.well-known/workflow/v1/manifest.json").status_code == 404

    with pytest.raises(ModuleNotFoundError, match="no_such_mount_dep_xyz"):
        mount_workflows(
            Chano(docs=False),
            [WorkflowConfig("wf_bad_dep:wf")],
            project_root=project,
        )

    app_nm = Chano(docs=False)
    assert (
        mount_workflows(
            app_nm, [WorkflowConfig("wf_no_manifest:wf")], project_root=project
        )
        is True
    )
    client = make_client(app_nm)
    assert client("POST", "/.well-known/workflow/v1/flow").text == "flow-ok"
    assert client("GET", "/.well-known/workflow/v1/manifest.json").status_code == 404
