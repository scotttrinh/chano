import asyncio
import sys
import types
from typing import Any

import pytest

from chano import (
    Chano,
    create_route_app,
)
from chano import build as build_module
from chano.build import build, plan_build
from chano.routing import discover_routes
from tests.helpers import (
    make_client,
    make_dist,
    write_project,
)


# @lat: [[tests#Background work with after()#Runs after response body]]
def test_after_runs_sync_and_async_after_response_sent():
    import threading

    from chano import after

    events: list[tuple[object, ...]] = []
    loop_thread = threading.current_thread().ident
    nested_error: list[type[BaseException]] = []

    def sync_work(label: str, *, value: int, fn: str = "") -> None:
        in_worker = threading.current_thread().ident != loop_thread
        events.append(("sync", label, value, fn, in_worker))
        try:
            after(lambda: None)
        except RuntimeError as exc:
            nested_error.append(type(exc))

    async def async_work(label: str, *, value: int) -> None:
        on_loop = threading.current_thread().ident == loop_thread
        events.append(("async", label, value, on_loop))

    async def async_route():
        after(sync_work, "from-async", value=1, fn="kw-ok")
        after(async_work, "from-async", value=2)
        return {"ok": True}

    def sync_route():
        after(sync_work, "from-sync", value=3, fn="kw-sync")
        after(async_work, "from-sync", value=4)
        return {"ok": True}

    app = Chano()
    app.add_route("/async", async_route, methods=["GET"])
    app.add_route("/sync", sync_route, methods=["GET"])

    async def run_request(path: str) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                events.append(("response_body_sent", path))

        await app(scope, receive, send)

    asyncio.run(run_request("/async"))
    assert events == [
        ("response_body_sent", "/async"),
        ("sync", "from-async", 1, "kw-ok", True),
        ("async", "from-async", 2, True),
    ]
    assert nested_error == [RuntimeError]

    events.clear()
    nested_error.clear()
    asyncio.run(run_request("/sync"))
    assert events == [
        ("response_body_sent", "/sync"),
        ("sync", "from-sync", 3, "kw-sync", True),
        ("async", "from-sync", 4, True),
    ]
    assert nested_error == [RuntimeError]


# @lat: [[tests#Background work with after()#Logs exceptions safely]]
def test_after_logs_exceptions_without_affecting_response_or_other_tasks(capsys):
    from chano import after

    ran: list[str] = []

    def bad_sync() -> None:
        raise ValueError("sync boom")

    async def bad_async() -> None:
        raise KeyError("async boom")

    async def self_cancelled() -> None:
        raise asyncio.CancelledError("inner cancel")

    def good_task() -> None:
        ran.append("still-ran")

    async def route():
        after(bad_sync)
        after(bad_async)
        after(self_cancelled)
        after(good_task)
        return {"status": "ok"}

    app = Chano()
    app.add_route("/fail-after", route, methods=["GET"])
    client = make_client(app)

    response = client("GET", "/fail-after")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert ran == ["still-ran"]

    err = capsys.readouterr().err
    assert "ValueError: sync boom" in err
    assert "KeyError: 'async boom'" in err
    assert "CancelledError: inner cancel" in err


# @lat: [[tests#Background work with after()#Outside request raises RuntimeError]]
def test_after_outside_request_raises():
    from chano import after

    with pytest.raises(RuntimeError, match="request"):
        after(lambda: None)

    detached_errors: list[type[BaseException]] = []

    async def run_detached() -> None:
        release = asyncio.Event()
        detached: list[asyncio.Task[None]] = []

        async def later() -> None:
            await release.wait()
            try:
                after(lambda: None)
            except RuntimeError as exc:
                detached_errors.append(type(exc))

        async def route():
            detached.append(asyncio.create_task(later()))
            return {"ok": True}

        app = Chano()
        app.add_route("/detach", route, methods=["GET"])
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/detach",
            "query_string": b"",
            "headers": [],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        await app(scope, receive, send)
        release.set()
        await detached[0]

    asyncio.run(run_detached())
    assert detached_errors == [RuntimeError]


# @lat: [[tests#Background work with after()#Runs on disconnect and send error]]
def test_after_runs_on_disconnect_and_send_error():
    from chano import StreamingResponse, after

    ran: list[str] = []

    async def stream_route():
        after(ran.append, "after-disconnect")

        async def chunks():
            yield b"first"
            await asyncio.sleep(60)

        return StreamingResponse(chunks())

    async def broken_route():
        after(ran.append, "after-broken-pipe")
        return {"ok": True}

    app = Chano()
    app.add_route("/stream", stream_route, methods=["GET"])
    app.add_route("/broken", broken_route, methods=["GET"])

    async def run_disconnect() -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/stream",
            "query_string": b"",
            "headers": [],
        }
        first_chunk_sent = asyncio.Event()

        async def receive():
            await first_chunk_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                first_chunk_sent.set()

        await app(scope, receive, send)

    asyncio.run(run_disconnect())
    assert ran == ["after-disconnect"]

    ran.clear()

    async def run_broken_pipe() -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/broken",
            "query_string": b"",
            "headers": [],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                raise BrokenPipeError("client gone")

        await app(scope, receive, send)

    with pytest.raises(BrokenPipeError, match="client gone"):
        asyncio.run(run_broken_pipe())
    assert ran == ["after-broken-pipe"]


# @lat: [[tests#Background work with after()#Cancellation and callback GC]]
def test_after_request_cancellation_and_callback_gc():
    import gc
    import weakref

    from chano import after

    # 1. Request cancelled during handler/send: queued callback never starts.
    handler_callback_ran = False

    async def cancel_during_handler() -> None:
        nonlocal handler_callback_ran
        in_handler = asyncio.Event()

        def should_not_run() -> None:
            nonlocal handler_callback_ran
            handler_callback_ran = True

        async def route():
            after(should_not_run)
            in_handler.set()
            await asyncio.sleep(60)
            return {"ok": True}

        app = Chano()
        app.add_route("/slow", route, methods=["GET"])
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/slow",
            "query_string": b"",
            "headers": [],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        req_task = asyncio.create_task(app(scope, receive, send))
        await in_handler.wait()
        req_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await req_task
        assert req_task.cancelled()

    asyncio.run(cancel_during_handler())
    assert handler_callback_ran is False

    # 2. Request cancelled during a callback that swallows CancelledError:
    # subsequent callbacks do not run and outer request task stays cancelled.
    later_callback_ran = False
    swallowed_cancel = False

    async def cancel_during_callback() -> None:
        nonlocal later_callback_ran, swallowed_cancel
        in_callback = asyncio.Event()

        async def swallowing_callback() -> None:
            nonlocal swallowed_cancel
            in_callback.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                swallowed_cancel = True
                return  # intentionally swallow and return normally

        def later_callback() -> None:
            nonlocal later_callback_ran
            later_callback_ran = True

        async def route():
            after(swallowing_callback)
            after(later_callback)
            return {"ok": True}

        app = Chano()
        app.add_route("/cb", route, methods=["GET"])
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/cb",
            "query_string": b"",
            "headers": [],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        req_task = asyncio.create_task(app(scope, receive, send))
        await in_callback.wait()
        req_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await req_task
        assert req_task.cancelled()

    asyncio.run(cancel_during_callback())
    assert swallowed_cancel is True
    assert later_callback_ran is False

    # 3. Closed request state releases callbacks even while a detached task lives.
    class Payload:
        pass

    async def check_gc_with_detached_task() -> None:
        release = asyncio.Event()
        detached: list[asyncio.Task[None]] = []
        payload_ref: list[weakref.ref[Payload]] = []

        async def linger() -> None:
            await release.wait()

        def use_payload(_obj: Payload) -> None:
            return None

        async def route():
            obj = Payload()
            payload_ref.append(weakref.ref(obj))
            after(use_payload, obj)
            detached.append(asyncio.create_task(linger()))
            return {"ok": True}

        app = Chano()
        app.add_route("/gc", route, methods=["GET"])
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/gc",
            "query_string": b"",
            "headers": [],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        await app(scope, receive, send)
        gc.collect()
        assert not detached[0].done()
        assert payload_ref[0]() is None
        release.set()
        await detached[0]

    asyncio.run(check_gc_with_detached_task())


# @lat: [[tests#Background work with after()#wait_until backend and backend validation]]
def test_after_wait_until_backend_and_validation(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    import contextvars
    from collections.abc import Awaitable

    bad_backend: Any = "unknown"
    with pytest.raises(ValueError, match="after_backend"):
        Chano(after_backend=bad_backend)

    project = tmp_path / "wait_until_app"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "wait-until-app"\nversion = "0"\n',
            "app/work/route.py": (
                "from chano import after\n\n"
                "LOG = []\n"
                "nested_errors = []\n\n"
                "def _sync(x):\n"
                "    LOG.append(('sync', x))\n\n"
                "async def _async(x):\n"
                "    LOG.append(('async', x))\n"
                "    try:\n"
                "        after(lambda: None)\n"
                "    except RuntimeError as exc:\n"
                "        nested_errors.append(type(exc))\n\n"
                "def _boom():\n"
                "    raise RuntimeError('wait_until boom')\n\n"
                "async def GET():\n"
                "    after(_sync, 1)\n"
                "    after(_async, 2)\n"
                "    after(_boom)\n"
                "    return {'scheduled': True}\n"
            ),
        },
    )

    bad_route_backend: Any = "nope"
    with pytest.raises(ValueError, match="after_backend"):
        create_route_app(project / "app", "work", after_backend=bad_route_backend)

    # Local mode never imports vercel.
    monkeypatch.delitem(sys.modules, "vercel", raising=False)
    monkeypatch.delitem(sys.modules, "vercel.functions", raising=False)
    local_app = create_route_app(project / "app", "work")
    local_resp = make_client(local_app)("GET", "/work")
    assert local_resp.status_code == 200
    assert "vercel" not in sys.modules
    assert "RuntimeError: wait_until boom" in capsys.readouterr().err

    # wait_until mode lazily imports vercel.functions, passes wrapped awaitables,
    # and blocks nested after() even when drained in a copied registration context.
    captured: list[tuple[Awaitable[object], contextvars.Context]] = []

    def fake_wait_until(awaitable: Awaitable[object]) -> None:
        captured.append((awaitable, contextvars.copy_context()))

    fake_vercel = types.ModuleType("vercel")
    fake_functions = types.ModuleType("vercel.functions")
    fake_functions.__dict__["wait_until"] = fake_wait_until
    fake_vercel.__dict__["functions"] = fake_functions
    monkeypatch.setitem(sys.modules, "vercel", fake_vercel)
    monkeypatch.setitem(sys.modules, "vercel.functions", fake_functions)

    route_mod = discover_routes(project / "app")[0].load()
    route_mod.LOG.clear()
    route_mod.nested_errors.clear()

    wu_app = create_route_app(project / "app", "work", after_backend="wait_until")
    resp = make_client(wu_app)("GET", "/work")
    assert resp.status_code == 200
    assert resp.json() == {"scheduled": True}
    assert len(captured) == 3
    assert route_mod.LOG == []  # wait_until owns execution, not Chano

    async def drain() -> None:
        async def run_one(awaitable: Awaitable[object]) -> None:
            await awaitable

        for awaitable, ctx in captured:
            task = asyncio.create_task(run_one(awaitable), context=ctx)
            await task

    asyncio.run(drain())
    assert route_mod.LOG == [("sync", 1), ("async", 2)]
    assert route_mod.nested_errors == [RuntimeError]
    assert "RuntimeError: wait_until boom" in capsys.readouterr().err


# @lat: [[tests#Background work with after()#Build entrypoint and vercel install]]
def test_build_after_entrypoint_and_vercel_install(
    tmp_path, isolated_imports, monkeypatch, stub_build_install
):
    import importlib.util
    from collections.abc import Awaitable

    project = tmp_path / "after_build_app"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "after-build-app"\nversion = "0"\n',
            "app/ping/route.py": (
                "from chano import after\n\n"
                "DONE = []\n\n"
                "async def _record(msg):\n"
                "    DONE.append(msg)\n\n"
                "async def GET():\n"
                "    after(_record, 'via-entrypoint')\n"
                "    return {'ok': True}\n"
            ),
        },
    )

    plan = plan_build(project, platform=None, output=tmp_path / "out")
    ping = next(spec for spec in plan.functions if spec.name == "ping")
    assert 'after_backend="wait_until"' in ping.entrypoint
    assert "vercel" in build_module.ALWAYS_INCLUDED

    commands: list[list[str]] = []

    def fake_run(cmd, *, cwd):
        commands.append(list(cmd))

    monkeypatch.setattr(build_module, "_run", fake_run)
    build_module._install_dependencies(project, tmp_path / "vendor", "3.12", None)
    pip_install = next(cmd for cmd in commands if cmd[:3] == ["uv", "pip", "install"])
    assert "vercel-runtime" in pip_install
    assert "vercel>=0.10.0" in pip_install

    # Emit the function and exercise its generated index.py entrypoint end-to-end.
    stub_build_install(
        {
            **make_dist("chano", "chano"),
            **make_dist("vercel-runtime", "vercel_runtime"),
            **make_dist("vercel", "vercel"),
        }
    )
    out = build(project, platform=None, output=tmp_path / "emitted")

    captured: list[Awaitable[object]] = []
    fake_vercel = types.ModuleType("vercel")
    fake_functions = types.ModuleType("vercel.functions")
    fake_functions.__dict__["wait_until"] = captured.append
    fake_vercel.__dict__["functions"] = fake_functions
    monkeypatch.setitem(sys.modules, "vercel", fake_vercel)
    monkeypatch.setitem(sys.modules, "vercel.functions", fake_functions)

    entrypoint_file = out / "functions" / "ping.func" / "index.py"
    spec = importlib.util.spec_from_file_location(
        "test_emitted_ping_index", entrypoint_file
    )
    assert spec and spec.loader
    entry_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry_mod)

    resp = make_client(entry_mod.app)("GET", "/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(captured) == 1

    emitted_route = discover_routes(entrypoint_file.parent / "app")[0].load()
    assert emitted_route.DONE == []

    async def drain_emitted() -> None:
        for awaitable in captured:
            await awaitable

    asyncio.run(drain_emitted())
    assert emitted_route.DONE == ["via-entrypoint"]
