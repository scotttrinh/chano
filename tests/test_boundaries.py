import re
import sys

import httpx
import pytest

from chano import (
    create_app,
    create_route_app,
)
from tests.helpers import (
    FACTORIES,
    make_client,
    write_project,
)


def middleware_source(name: str, *, include_boundary: bool = True) -> str:
    """Source for a ``boundary.py`` wrapper that records ``name>`` on the way in
    and ``<name:status`` on the way out in ``mwlog.EVENTS``. ``x-stop: name``
    short-circuits it with a 418; ``x-raise: name`` makes it raise
    ``x-raise-kind`` (``http``, ``validation`` or anything else)."""
    tail = "\nboundary = Boundary(wrap=[middleware])\n" if include_boundary else ""
    return (
        "import mwlog\n"
        "from chano import Boundary, JSONResponse, RequestValidationError\n"
        "from chano.status import Forbidden\n\n"
        "async def middleware(request, call_next):\n"
        f"    mwlog.EVENTS.append({name + '>'!r})\n"
        f"    if request.headers.get('x-stop') == {name!r}:\n"
        f"        return JSONResponse({{'stopped': {name!r}}}, 418)\n"
        f"    if request.headers.get('x-raise') == {name!r}:\n"
        "        kind = request.headers.get('x-raise-kind')\n"
        "        if kind == 'http':\n"
        f"            raise Forbidden({name!r}, headers={{'x-why': 'mw'}})\n"
        "        if kind == 'validation':\n"
        f"            raise RequestValidationError([{{'msg': {name!r}}}])\n"
        f"        raise RuntimeError({name!r})\n"
        "    response = await call_next(request)\n"
        f"    mwlog.EVENTS.append(f'<{name}:{{response.status_code}}')\n"
        f"    response.headers['x-seen-' + {name!r}] = str(response.status_code)\n"
        "    return response\n"
        f"{tail}"
    )


MIDDLEWARE_ROUTE = (
    "import mwlog\n"
    "from chano.status import Unauthorized\n\n"
    "def GET(n: int = 0, fail: str = ''):\n"
    "    mwlog.EVENTS.append('handler')\n"
    "    if fail == '401':\n"
    "        raise Unauthorized('nope', headers={'www-authenticate': 'Bearer'})\n"
    "    if fail == 'boom':\n"
    "        raise RuntimeError('boom')\n"
    "    return {'n': n}\n"
)


@pytest.fixture
def middleware_project(tmp_path, isolated_project_imports):
    """A project with root, ``(group)`` and leaf middleware, a sibling with
    only the root's, and two dynamic routes that overlap on ``/items/x``.

    Yields ``(serve, events)``. ``serve(factory, method, path)`` sends one
    request to ``create_app`` (``factory="dev"``) or to the
    ``create_route_app`` for ``function`` (``factory="route"``)."""
    write_project(
        tmp_path,
        {
            "mwlog.py": "EVENTS = []\n",
            "app/boundary.py": middleware_source("root"),
            "app/(group)/boundary.py": middleware_source("group"),
            "app/(group)/admin/boundary.py": middleware_source("admin"),
            "app/(group)/admin/route.py": MIDDLEWARE_ROUTE,
            "app/public/route.py": MIDDLEWARE_ROUTE,
            "app/items/[id]/boundary.py": middleware_source("id"),
            "app/items/[id]/route.py": MIDDLEWARE_ROUTE,
            "app/items/[...rest]/boundary.py": middleware_source("rest"),
            "app/items/[...rest]/route.py": (
                "def POST(rest: str):\n    return {'rest': rest}\n"
            ),
            "public/hello.txt": "static\n",
        },
    )
    app_dir = tmp_path / "app"
    dev = create_app(app_dir, public_dir=tmp_path / "public")
    events: list[str] = sys.modules["mwlog"].EVENTS  # imported by boundary.py

    def serve(
        factory, method, path, function: str | None = None, **kwargs
    ) -> httpx.Response:
        if factory == "dev":
            app = dev
        else:
            assert function is not None
            app = create_route_app(app_dir, function)
        events.clear()
        return make_client(app)(method, path, **kwargs)

    return serve, events


# @lat: [[tests#Middleware#Runs outer to inner]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_order(middleware_project, factory):
    serve, events = middleware_project
    response = serve(factory, "GET", "/admin?n=3", function="admin")
    assert response.json() == {"n": 3}
    assert events == [
        "root>",
        "group>",
        "admin>",
        "handler",
        "<admin:200",
        "<group:200",
        "<root:200",
    ]
    assert response.headers["x-seen-root"] == "200"

    # A sibling outside the group runs only the root's middleware.
    assert serve(factory, "GET", "/public", function="public").json() == {"n": 0}
    assert events == ["root>", "handler", "<root:200"]


# @lat: [[tests#Middleware#Short-circuits]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_short_circuit(middleware_project, factory):
    serve, events = middleware_project
    headers = {"x-stop": "group"}
    response = serve(factory, "GET", "/admin", function="admin", headers=headers)
    assert response.status_code == 418
    assert response.json() == {"stopped": "group"}
    assert events == ["root>", "group>", "<root:418"]  # no admin, no handler


# @lat: [[tests#Middleware#Sees the error response]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_sees_error_response(middleware_project, factory):
    serve, events = middleware_project
    chain = ["root>", "group>", "admin>"]

    response = serve(factory, "GET", "/admin?fail=401", function="admin")
    assert response.status_code == 401
    assert response.json() == {"detail": "nope"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["x-seen-admin"] == "401"
    assert events == [*chain, "handler", "<admin:401", "<group:401", "<root:401"]

    response = serve(factory, "GET", "/admin?n=bad", function="admin")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "n"]
    assert events == [*chain, "<admin:422", "<group:422", "<root:422"]

    response = serve(factory, "GET", "/admin?fail=boom", function="admin")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["x-seen-root"] == "500"
    assert events == [*chain, "handler", "<admin:500", "<group:500", "<root:500"]


# @lat: [[tests#Middleware#Middleware errors get the default mapping]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_exception(middleware_project, factory):
    serve, events = middleware_project

    def raising(kind: str) -> httpx.Response:
        headers = {"x-raise": "group", "x-raise-kind": kind}
        return serve(factory, "GET", "/admin", function="admin", headers=headers)

    response = raising("http")
    assert response.status_code == 403
    assert response.json() == {"detail": "group"}
    assert response.headers["x-why"] == "mw"
    assert events == ["root>", "group>"]  # propagates past the outer layers

    response = raising("validation")
    assert response.status_code == 422
    assert response.json() == {"detail": [{"msg": "group"}]}

    response = raising("other")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert "x-seen-root" not in response.headers


# @lat: [[tests#Middleware#Skips unmatched paths but runs on 405]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_unmatched_and_405(middleware_project, factory):
    serve, events = middleware_project

    # A literal route's 405 runs through its own chain.
    response = serve(factory, "POST", "/admin", function="admin")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.headers["x-seen-admin"] == "405"
    assert events == [
        "root>",
        "group>",
        "admin>",
        "<admin:405",
        "<group:405",
        "<root:405",
    ]

    # /items/x matches [id] first, as the platform's first rewrite would, so
    # POST is that function's 405 rather than the catch-all's handler.
    response = serve(factory, "POST", "/items/x", function="items/[id]")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert events == ["root>", "id>", "<id:405", "<root:405"]
    response = serve(factory, "POST", "/items/x/y", function="items/[...rest]")
    assert response.json() == {"rest": "x/y"}
    assert events == ["root>", "rest>", "<rest:200", "<root:200"]

    # No route, no segment files.
    response = serve(factory, "GET", "/nope", function="admin")
    assert response.status_code == 404
    assert events == []


# @lat: [[tests#Middleware#Skips dev files]]
def test_middleware_skips_dev_files(middleware_project):
    serve, events = middleware_project
    assert serve("dev", "GET", "/hello.txt").text == "static\n"
    assert serve("dev", "GET", "/openapi.json").json()["openapi"] == "3.1.0"
    assert serve("dev", "POST", "/docs").status_code == 405
    assert events == []


# @lat: [[tests#Boundary#Invalid boundary.py]]
@pytest.mark.parametrize(
    "source",
    [
        "x = 1\n",
        "boundary = 1\n",
        "boundary = {}\n",
    ],
)
def test_invalid_boundary_file(tmp_path, isolated_project_imports, source):
    write_project(
        tmp_path,
        {
            "app/(group)/boundary.py": source,
            "app/(group)/a/route.py": "def GET():\n    return {}\n",
        },
    )
    file = (tmp_path / "app" / "(group)" / "boundary.py").resolve()
    for make_app in [
        lambda: create_app(tmp_path / "app"),
        lambda: create_route_app(tmp_path / "app", "a"),
    ]:
        with pytest.raises(TypeError, match=re.escape(str(file))):
            make_app()


# @lat: [[tests#Boundary#Non-response wrap result]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_non_response(tmp_path, isolated_project_imports, factory):
    write_project(
        tmp_path,
        {
            "app/boundary.py": (
                "from chano import Boundary\n\n"
                "async def middleware(request, call_next):\n"
                "    await call_next(request)\n"
                "    return {'not': 'a response'}\n\n"
                "boundary = Boundary(wrap=[middleware])\n"
            ),
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    app_dir = tmp_path / "app"
    app = (
        create_app(app_dir) if factory == "dev" else create_route_app(app_dir, "index")
    )
    response = make_client(app)("GET", "/")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


# @lat: [[tests#Boundary#Conflicting boundaries for one path]]
def test_boundary_conflicting_chains(tmp_path, isolated_project_imports):
    boundary_src = (
        "from chano import Boundary\n"
        "async def w(request, call_next):\n    return await call_next(request)\n"
        "boundary = Boundary(wrap=[w])\n"
    )
    write_project(
        tmp_path,
        {
            "app/(a)/boundary.py": boundary_src,
            "app/(a)/x/route.py": "def GET():\n    return {}\n",
            "app/(b)/x/route.py": "def POST():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="conflicting boundaries for route '/x'"):
        create_app(tmp_path / "app")


def error_source(name: str, *, sync: bool, with_wrap: bool = False) -> str:
    """Source for a ``boundary.py`` recover function that logs ``name!ExcType``
    in ``mwlog.EVENTS`` and, by default, returns a JSON 503 naming itself and
    the exception. The ``x-<name>`` header switches it to a bare ``raise``
    (``reraise``), a new ``Conflict(name)`` (``raise``), or returning ``None``."""
    wrap_code = middleware_source(name, include_boundary=False) if with_wrap else ""
    wrap_arg = ", wrap=[middleware]" if with_wrap else ""
    return (
        wrap_code + "import mwlog\n"
        "from chano import Boundary, JSONResponse\n"
        "from chano.status import Conflict\n\n"
        f"{'' if sync else 'async '}def handle_error(request, exc):\n"
        f"    mwlog.EVENTS.append(f'{name}!{{type(exc).__name__}}')\n"
        f"    action = request.headers.get('x-{name}')\n"
        "    if action == 'reraise':\n"
        "        raise\n"
        "    if action == 'raise':\n"
        f"        raise Conflict({name!r})\n"
        "    if action == 'none':\n"
        "        return None\n"
        f"    return JSONResponse({{'by': {name!r}, 'error': str(exc)}}, 503)\n\n"
        f"boundary = Boundary(recover=[handle_error]{wrap_arg})\n"
    )


@pytest.fixture
def error_project(tmp_path, isolated_project_imports):
    """Sync recover at the root and in ``admin``, async in ``(group)``,
    plus root and ``(group)`` wrap. ``/admin`` has all three boundary
    files, ``/other`` the group's and root's, ``/plain`` only the root's.

    Yields ``serve`` like ``middleware_project`` and the shared event log."""
    write_project(
        tmp_path,
        {
            "mwlog.py": "EVENTS = []\n",
            "app/boundary.py": error_source("root", sync=True, with_wrap=True),
            "app/(group)/boundary.py": error_source(
                "group", sync=False, with_wrap=True
            ),
            "app/(group)/admin/boundary.py": error_source("admin", sync=True),
            "app/(group)/admin/route.py": MIDDLEWARE_ROUTE,
            "app/(group)/other/route.py": MIDDLEWARE_ROUTE,
            "app/plain/route.py": MIDDLEWARE_ROUTE,
        },
    )
    app_dir = tmp_path / "app"
    dev = create_app(app_dir)
    events: list[str] = sys.modules["mwlog"].EVENTS

    def serve(factory, method, path, function: str, **kwargs) -> httpx.Response:
        app = dev if factory == "dev" else create_route_app(app_dir, function)
        events.clear()
        return make_client(app)(method, path, **kwargs)

    return serve, events


# @lat: [[tests#Error boundaries#Nearest recover wins]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_nearest_wins(error_project, factory):
    serve, events = error_project

    # Sync leaf handler; middleware sees its response from call_next.
    response = serve(factory, "GET", "/admin?fail=boom", "admin")
    assert response.status_code == 503
    assert response.json() == {"by": "admin", "error": "boom"}
    assert response.headers["x-seen-root"] == "503"
    assert events == [
        "root>",
        "group>",
        "handler",
        "admin!RuntimeError",
        "<group:503",
        "<root:503",
    ]

    # Validation errors and a 405 reach the boundary too.
    response = serve(factory, "GET", "/admin?n=bad", "admin")
    assert response.json()["by"] == "admin"
    assert events[2] == "admin!RequestValidationError"
    response = serve(factory, "POST", "/admin", "admin")
    assert response.json() == {"by": "admin", "error": "405: Method Not Allowed"}

    # Async nearest handler.
    response = serve(factory, "GET", "/other?fail=401", "other")
    assert response.json() == {"by": "group", "error": "401: nope"}
    assert events == [
        "root>",
        "group>",
        "handler",
        "group!Unauthorized",
        "<group:503",
        "<root:503",
    ]

    response = serve(factory, "GET", "/plain?fail=boom", "plain")
    assert response.json() == {"by": "root", "error": "boom"}
    assert events == ["root>", "handler", "root!RuntimeError", "<root:503"]

    # Successful responses skip the boundary.
    assert serve(factory, "GET", "/admin?n=2", "admin").json() == {"n": 2}
    assert not [event for event in events if "!" in event]


# @lat: [[tests#Error boundaries#Re-raising passes to the next ancestor]]
@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize(
    ("actions", "ran", "expected"),
    [
        # A bare raise passes the same exception on.
        (
            {"admin": "reraise"},
            ["admin!RuntimeError", "group!RuntimeError"],
            {"by": "group", "error": "boom"},
        ),
        # A new exception replaces it, even through a later bare raise
        # in the async group or the sync root.
        (
            {"admin": "raise"},
            ["admin!RuntimeError", "group!Conflict"],
            {"by": "group", "error": "409: admin"},
        ),
        (
            {"admin": "raise", "group": "reraise"},
            ["admin!RuntimeError", "group!Conflict", "root!Conflict"],
            {"by": "root", "error": "409: admin"},
        ),
        # Past the root the default mapping applies to the last exception.
        (
            {"admin": "reraise", "group": "raise", "root": "reraise"},
            ["admin!RuntimeError", "group!RuntimeError", "root!Conflict"],
            {"detail": "group"},
        ),
    ],
)
def test_error_reraise(error_project, factory, actions, ran, expected):
    serve, events = error_project
    headers = {f"x-{name}": action for name, action in actions.items()}
    response = serve(factory, "GET", "/admin?fail=boom", "admin", headers=headers)
    assert [event for event in events if "!" in event] == ran
    assert response.json() == expected
    assert response.headers["x-seen-root"] == str(response.status_code)


# @lat: [[tests#Error boundaries#Non-response result]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_non_response(error_project, factory):
    serve, events = error_project
    headers = {"x-admin": "none"}
    response = serve(factory, "GET", "/admin?fail=boom", "admin", headers=headers)
    assert [event for event in events if "!" in event] == [
        "admin!RuntimeError",
        "group!TypeError",
    ]
    body = response.json()
    assert body["by"] == "group"
    assert body["error"].endswith("returned None, not a Response")

    headers = {"x-admin": "none", "x-group": "none", "x-root": "none"}
    response = serve(factory, "GET", "/admin?fail=boom", "admin", headers=headers)
    assert (response.status_code, response.text) == (500, "Internal Server Error")


# @lat: [[tests#Error boundaries#Unhandled errors get the default mapping]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_unhandled_default(error_project, tmp_path, factory):
    serve, events = error_project
    headers = {"x-admin": "reraise", "x-group": "reraise", "x-root": "reraise"}
    response = serve(factory, "GET", "/admin?fail=boom", "admin", headers=headers)
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["x-seen-root"] == "500"
    assert [event for event in events if "!" in event] == [
        "admin!RuntimeError",
        "group!RuntimeError",
        "root!RuntimeError",
    ]

    # A project without any error.py keeps today's mapping.
    project = tmp_path / "bare"
    write_project(project, {"app/x/route.py": MIDDLEWARE_ROUTE})
    app = (
        create_app(project / "app")
        if factory == "dev"
        else create_route_app(project / "app", "x")
    )
    client = make_client(app)
    response = client("GET", "/x?fail=401")
    assert (response.status_code, response.json()) == (401, {"detail": "nope"})
    assert response.headers["www-authenticate"] == "Bearer"
    assert client("GET", "/x?n=bad").status_code == 422
    response = client("GET", "/x?fail=boom")
    assert (response.status_code, response.text) == (500, "Internal Server Error")


# @lat: [[tests#Error boundaries#Wrapper errors skip recover]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_skips_middleware_errors(error_project, factory):
    serve, events = error_project
    for kind, status in [("other", 500), ("http", 403), ("validation", 422)]:
        headers = {"x-raise": "group", "x-raise-kind": kind}
        response = serve(factory, "GET", "/admin", "admin", headers=headers)
        assert response.status_code == status
        assert events == ["root>", "group>"]

    # No route, no segment files.
    response = serve(factory, "GET", "/nope", "admin")
    assert response.status_code == 404
    assert events == []


ASYNC_RESULT = (
    "from chano import JSONResponse\n\n"
    "async def respond(request, exc, by):\n"
    "    if request.headers.get('x-reraise'):\n"
    "        raise\n"
    "    return JSONResponse({'by': by, 'error': str(exc)}, 503)\n\n"
)


# @lat: [[tests#Boundary#Awaitable recover handlers]]
@pytest.mark.filterwarnings("error::RuntimeWarning")
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_awaitable_handlers(tmp_path, isolated_project_imports, factory):
    write_project(
        tmp_path,
        {
            "app/boundary.py": (
                "from chano import Boundary, JSONResponse\n\n"
                "def handle_error(request, exc):\n"
                "    return JSONResponse({'by': 'root', 'error': str(exc)}, 500)\n\n"
                "boundary = Boundary(recover=[handle_error])\n"
            ),
            "app/obj/boundary.py": ASYNC_RESULT
            + (
                "from chano import Boundary\n\n"
                "class Handler:\n"
                "    async def __call__(self, request, exc):\n"
                "        return await respond(request, exc, 'obj')\n\n"
                "boundary = Boundary(recover=[Handler()])\n"
            ),
            "app/obj/route.py": MIDDLEWARE_ROUTE,
            "app/wrapper/boundary.py": ASYNC_RESULT
            + (
                "from chano import Boundary\n\n"
                "def handle_error(request, exc):\n"
                "    return respond(request, exc, 'wrapper')\n\n"
                "boundary = Boundary(recover=[handle_error])\n"
            ),
            "app/wrapper/route.py": MIDDLEWARE_ROUTE,
            "mwlog.py": "EVENTS = []\n",
        },
    )
    app_dir = tmp_path / "app"
    for name in ["obj", "wrapper"]:
        app = (
            create_app(app_dir) if factory == "dev" else create_route_app(app_dir, name)
        )
        client = make_client(app)
        response = client("GET", f"/{name}?fail=boom")
        assert response.json() == {"by": name, "error": "boom"}

        # A bare raise in the awaited body re-raises the original exception.
        response = client("GET", f"/{name}?fail=boom", headers={"x-reraise": "1"})
        assert response.json() == {"by": "root", "error": "boom"}
