import asyncio
import gc
import inspect
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from chano import (
    create_app,
    lazy,
)
from chano import params as params_module
from tests.helpers import (
    make_client,
    write_project,
)


# @lat: [[tests#Lazy values#Sync lazy creates once]]
@pytest.mark.parametrize("value", [None, 0, "", False, [], object()])
def test_lazy_sync_creates_once(value):
    calls = []
    barrier = threading.Barrier(16)

    def factory():
        calls.append(1)
        time.sleep(0.01)  # widen the race window
        return value

    get = lazy(factory)

    def first_call():
        barrier.wait()
        return get()

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: first_call(), range(16)))
    assert all(result is value for result in results)
    assert get() is value
    assert len(calls) == 1


# @lat: [[tests#Lazy values#Async lazy creates once]]
@pytest.mark.parametrize("value", [None, 0, object()])
def test_lazy_async_creates_once(value):
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(0.01)
        return value

    get = lazy(factory)
    assert inspect.iscoroutinefunction(get)

    async def main():
        return await asyncio.gather(*(get() for _ in range(16)))

    assert all(result is value for result in asyncio.run(main()))
    # The value outlives the event loop that created it.
    assert asyncio.run(get()) is value
    assert asyncio.run(main()) == [value] * 16
    assert len(calls) == 1


# @lat: [[tests#Lazy values#Failed creation is not cached]]
def test_lazy_failure_is_retried():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("down")
        return "client"

    get = lazy(flaky)
    with pytest.raises(ConnectionError):
        get()
    assert get() == "client"
    assert get() == "client"
    assert len(attempts) == 2

    async_attempts = []

    async def async_flaky():
        async_attempts.append(1)
        await asyncio.sleep(0.01)
        if len(async_attempts) == 1:
            raise ConnectionError("down")
        return "pool"

    aget = lazy(async_flaky)

    async def main():
        return await asyncio.gather(*(aget() for _ in range(4)), return_exceptions=True)

    # Callers sharing the failed attempt all see its error...
    results = asyncio.run(main())
    assert all(isinstance(result, ConnectionError) for result in results)
    # ...and the next call starts a fresh one.
    assert asyncio.run(main()) == ["pool"] * 4
    assert len(async_attempts) == 2


# @lat: [[tests#Lazy values#Cancelled waiter keeps creation]]
def test_lazy_cancelled_waiter_does_not_cancel_creation():
    calls = []
    release = None

    async def factory():
        calls.append(1)
        assert release is not None
        await release.wait()
        return "pool"

    get = lazy(factory)

    async def main():
        nonlocal release
        release = asyncio.Event()
        first = asyncio.ensure_future(get())
        second = asyncio.ensure_future(get())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        return await second

    assert asyncio.run(main()) == "pool"
    assert asyncio.run(get()) == "pool"
    assert len(calls) == 1


# @lat: [[tests#Lazy values#Lazy as a dependency]]
def test_lazy_as_dependency(tmp_path, isolated_project_imports):
    project = tmp_path / "clients"
    write_project(
        project,
        {
            "app/route.py": (
                "from typing import Annotated\n"
                "from chano import Depends, lazy\n\n"
                "made = []\n\n"
                "def make_client(timeout: int = 5) -> dict:\n"
                "    made.append('sync')\n"
                "    return {'kind': 'sync', 'timeout': timeout}\n\n"
                "async def make_pool() -> dict:\n"
                "    made.append('async')\n"
                "    return {'kind': 'async'}\n\n"
                "get_client = lazy(make_client)\n"
                "get_pool = lazy(make_pool)\n\n"
                "def GET(client: dict = Depends(get_client),\n"
                "        pool: dict = Depends(get_pool)):\n"
                "    return {'client': client, 'pool': pool, 'made': made}\n\n"
                "async def POST(pool: dict = Depends(get_pool),\n"
                "               client: dict = Depends(get_client)):\n"
                "    return {'pool': pool, 'client': client, 'made': made}\n"
            ),
        },
    )
    app = create_app(project / "app")
    client = make_client(app)
    expected = {
        "client": {"kind": "sync", "timeout": 5},
        "pool": {"kind": "async"},
        "made": ["sync", "async"],
    }
    assert client("GET", "/?timeout=9").json() == expected
    assert client("POST", "/").json() == expected
    assert client("GET", "/").json() == expected
    # The factory's own parameters are not request inputs.
    operations = app.openapi()["paths"]["/"]
    assert all("parameters" not in op for op in operations.values())


# @lat: [[tests#Lazy values#Lazy exports]]
def test_lazy_exports():
    assert params_module.lazy is lazy
    get = lazy(lambda: 1)
    assert inspect.signature(get).parameters == {}
    assert not inspect.iscoroutinefunction(get)


# @lat: [[tests#Lazy values#Async lazy across event loops]]
def test_lazy_async_shared_across_event_loops():
    calls = []
    started = threading.Event()
    release = threading.Event()

    async def factory():
        calls.append(threading.get_ident())
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return object()

    get = lazy(factory)

    joined = threading.Event()

    async def join():
        # Runs once get() below is suspended on the in-flight creation.
        asyncio.get_running_loop().call_soon(joined.set)
        return await get()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(asyncio.run, get())
        assert started.wait(5)
        second = pool.submit(asyncio.run, join())
        assert joined.wait(5)
        release.set()
        assert first.result(5) is second.result(5)
    assert asyncio.run(get()) is first.result()
    assert len(calls) == 1


# @lat: [[tests#Lazy values#Async callable instance factory]]
def test_lazy_async_callable_instance(tmp_path, isolated_project_imports):
    project = tmp_path / "callable"
    write_project(
        project,
        {
            "app/route.py": (
                "from typing import Annotated\n"
                "from chano import Depends, lazy\n\n"
                "class Pool:\n"
                "    calls = 0\n\n"
                "    async def __call__(self) -> dict:\n"
                "        Pool.calls += 1\n"
                "        return {'pool': Pool.calls}\n\n"
                "get_pool = lazy(Pool())\n\n"
                "def GET(pool: dict = Depends(get_pool)):\n"
                "    return {'pool': pool, 'calls': Pool.calls}\n"
            ),
        },
    )
    client = make_client(create_app(project / "app"))
    expected = {"pool": {"pool": 1}, "calls": 1}
    assert client("GET", "/").json() == expected
    assert client("GET", "/").json() == expected


def asyncio_errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "asyncio"]


# @lat: [[tests#Lazy values#Async factory raising synchronously]]
def test_lazy_async_factory_raising_synchronously():
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("down")
        return asyncio.sleep(0, "pool")

    get = lazy(inspect.markcoroutinefunction(factory))

    async def main():
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(get(), 1)
        return await asyncio.wait_for(get(), 1)

    assert asyncio.run(main()) == "pool"
    assert len(attempts) == 2


# @lat: [[tests#Lazy values#Creation outlives its initiator]]
def test_lazy_creation_outlives_dropped_initiator(caplog):
    # Only the shared state can keep the creation task alive: its future is
    # weakly held here and referenced by nothing else.
    waiting = weakref.WeakSet()

    async def factory():
        future = asyncio.get_running_loop().create_future()
        waiting.add(future)
        return await future

    get = lazy(factory)

    async def main():
        first = asyncio.ensure_future(get())
        await asyncio.sleep(0)
        second = asyncio.ensure_future(get())
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert first.cancelled()
        del first
        gc.collect()
        assert len(waiting) == 1
        for future in waiting:
            future.set_result("pool")
        return await asyncio.wait_for(second, 1)

    assert asyncio.run(main()) == "pool"
    gc.collect()
    assert asyncio_errors(caplog) == []


# @lat: [[tests#Lazy values#Abandoned waiter after failure]]
def test_lazy_abandoned_waiter_after_failure(caplog):
    gate = threading.Event()

    async def factory():
        await asyncio.to_thread(gate.wait, 5)
        raise ConnectionError("late")

    get = lazy(factory)

    async def main():
        abandoned = asyncio.ensure_future(get())
        active = asyncio.ensure_future(get())
        await asyncio.sleep(0)
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
        gate.set()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(active, 5)
        del abandoned
        await asyncio.sleep(0)

    asyncio.run(main())
    gc.collect()
    assert asyncio_errors(caplog) == []
