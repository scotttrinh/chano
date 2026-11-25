import dataclasses
import functools
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import msgspec
import pytest

from chano import (
    Body,
    Boundary,
    Chano,
    Depends,
    Header,
    create_app,
    create_route_app,
)
from chano import build as build_module
from chano.build import build, plan_build
from chano.status import (
    Conflict,
    Forbidden,
    Gone,
    NotFound,
    Unauthorized,
)
from tests.helpers import (
    FACTORIES,
    bearer,
    make_client,
    make_dist,
    write_project,
)

INHERITED_CHECKS = """\
import deplog
from chano import Depends, HTTPBearer
from chano.status import Forbidden, Unauthorized

bearer = HTTPBearer()


class InvalidToken(Unauthorized):
    \"\"\"Invalid token\"\"\"


class AdminsOnly(Forbidden):
    \"\"\"Admins only\"\"\"


def require_user(
    token: str = Depends(bearer), tenant: str = "main"
) -> str | InvalidToken:
    deplog.EVENTS.append("user")
    if token not in ("admin", "member"):
        return InvalidToken("Invalid token", headers={"www-authenticate": "Bearer"})
    return f"{token}@{tenant}"


def group_area() -> None:
    deplog.EVENTS.append("group")


def admin_area() -> None:
    deplog.EVENTS.append("admin-area")


def require_admin(
    user: str = Depends(require_user), tenant: str = "main"
) -> str | AdminsOnly:
    deplog.EVENTS.append("admin")
    if not user.startswith("admin@"):
        return AdminsOnly("Admins only")
    return user
"""

INHERITED_GROUP_ERROR = """\
from chano import ErrorDetail, Failure, JSONResponse


def handle_error(request, exc):
    if isinstance(exc, Failure):
        detail = (
            exc.content.detail
            if isinstance(exc.content, ErrorDetail)
            else exc.content
        )
        body = {"detail": detail, "via": "group"}
        return JSONResponse(body, exc.status_code, exc.headers)
    raise exc
"""


def inherited_project(root: Path) -> None:
    """A protected ``(group)`` whose ``admin`` subtree adds a second check, a
    handler that requests the inherited check, a newly added route that only
    inherits it, and a public sibling. ``deplog.EVENTS`` records each call."""
    write_project(
        root,
        {
            "pyproject.toml": '[project]\nname = "inherited"\nversion = "0"\n',
            "deplog.py": "EVENTS = []\n",
            "checks.py": INHERITED_CHECKS,
            "app/(group)/boundary.py": (
                "from chano import Boundary\n"
                "from checks import group_area, require_user\n\n"
                + INHERITED_GROUP_ERROR
                + "\nboundary = Boundary(\n"
                "    guards=[group_area, require_user],\n"
                "    recover=[handle_error],\n"
                ")\n"
            ),
            "app/(group)/admin/boundary.py": (
                "from chano import Boundary\n"
                "from checks import admin_area, require_admin\n\n"
                "boundary = Boundary(guards=(admin_area, require_admin))\n"
            ),
            "app/(group)/admin/route.py": (
                "import deplog\n"
                "from chano import Depends\n"
                "from checks import require_admin\n\n"
                "def GET(\n"
                "    admin: str = Depends(require_admin),\n"
                "    tenant: str = 'main',\n"
                "):\n"
                "    deplog.EVENTS.append('handler')\n"
                "    return {'admin': admin, 'tenant': tenant}\n"
            ),
            "app/(group)/admin/reports/route.py": (
                "import deplog\n\n"
                "def GET() -> list[str]:\n"
                "    deplog.EVENTS.append('reports')\n"
                "    return ['q1']\n"
            ),
            "app/public/route.py": (
                "import deplog\n\n"
                "def GET() -> str:\n"
                "    deplog.EVENTS.append('public')\n"
                "    return 'hi'\n"
            ),
        },
    )


@pytest.fixture
def inherited_serve(tmp_path, isolated_imports):
    """``serve(factory, method, path, function, **kwargs)`` against
    ``inherited_project``, returning the response and the recorded events."""
    inherited_project(tmp_path)
    app_dir = tmp_path / "app"
    dev = create_app(app_dir)
    events: list[str] = sys.modules["deplog"].EVENTS

    def serve(factory, method, path, function, **kwargs):
        app = dev if factory == "dev" else create_route_app(app_dir, function)
        events.clear()
        response = make_client(app)(method, path, **kwargs)
        return response, list(events)

    return serve


# @lat: [[tests#Inherited typed dependencies#Run root to leaf before the handler]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_inherited_dependencies_run_root_to_leaf(inherited_serve, factory):
    response, events = inherited_serve(
        factory, "GET", "/admin?tenant=t", "admin", headers=bearer("admin")
    )
    # The handler requested require_admin itself and got the cached value;
    # require_admin's own Depends(require_user) reused the group's result.
    assert response.json() == {"admin": "admin@t", "tenant": "t"}
    # group_area and admin_area depend on nothing, so their places prove
    # the group's checks run before the admin directory's.
    assert events == ["group", "user", "admin-area", "admin", "handler"]

    # A newly added route inherits both checks by placement alone.
    response, events = inherited_serve(
        factory, "GET", "/admin/reports", "admin/reports", headers=bearer("admin")
    )
    assert response.json() == ["q1"]
    assert events == ["group", "user", "admin-area", "admin", "reports"]

    # The public sibling runs no check.
    response, events = inherited_serve(factory, "GET", "/public", "public")
    assert response.json() == "hi"
    assert events == ["public"]


# @lat: [[tests#Inherited typed dependencies#Rejection stops the handler]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_inherited_dependencies_reject_before_handler(inherited_serve, factory):
    for path, function in (("/admin", "admin"), ("/admin/reports", "admin/reports")):
        response, events = inherited_serve(factory, "GET", path, function)
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated", "via": "group"}
        assert response.headers["www-authenticate"] == "Bearer"
        assert events == ["group"]

        response, events = inherited_serve(
            factory, "GET", path, function, headers=bearer("member")
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Admins only", "via": "group"}
        assert events == ["group", "user", "admin-area", "admin"]


# @lat: [[tests#Inherited typed dependencies#Check validation stops the handler]]
def test_inherited_dependency_validation_stops_handler():
    calls: list[str] = []

    def check(n: int) -> int:
        calls.append("check")
        return n

    def handler(m: int = 0) -> int:
        calls.append("handler")
        return m

    app = Chano(docs=False)
    app.add_route("/", handler, methods=["GET"], boundaries=[Boundary(guards=[check])])
    response = make_client(app)("GET", "/?n=x&m=1")
    assert response.status_code == 422
    assert [e["loc"] for e in response.json()["detail"]] == [["query", "n"]]
    assert calls == []
    assert make_client(app)("GET", "/?n=1&m=2").json() == 2
    assert calls == ["check", "handler"]


# @lat: [[tests#Inherited typed dependencies#No check without a selected handler]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_inherited_dependencies_skip_404_and_405(inherited_serve, factory):
    response, events = inherited_serve(
        factory, "DELETE", "/admin", "admin", headers=bearer("admin")
    )
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert events == []

    response, events = inherited_serve(
        factory, "GET", "/admin/nope", "admin", headers=bearer("admin")
    )
    assert response.status_code == 404
    assert events == []


# @lat: [[tests#Inherited typed dependencies#Documented once without running]]
def test_inherited_dependencies_schema(tmp_path, isolated_imports):
    inherited_project(tmp_path)
    schema = make_client(create_app(tmp_path / "app"))("GET", "/openapi.json").json()
    assert sys.modules["deplog"].EVENTS == []  # schema generation runs no check

    assert schema["components"]["securitySchemes"] == {
        "HTTPBearer": {"type": "http", "scheme": "bearer"}
    }
    for path in ("/admin", "/admin/reports"):
        operation = schema["paths"][path]["get"]
        assert operation["security"] == [{"HTTPBearer": []}]
        # tenant is read by both checks (and the /admin handler): one input,
        # and the bearer credential is no `authorization` header.
        assert [(p["name"], p["in"]) for p in operation["parameters"]] == [
            ("tenant", "query")
        ]
        assert {k: r["description"] for k, r in operation["responses"].items()} == {
            "200": "Successful Response",
            "401": "Invalid token",
            "403": "Admins only",
            "422": "Validation Error",
        }
    public = schema["paths"]["/public"]["get"]
    assert "security" not in public
    assert "parameters" not in public
    assert list(public["responses"]) == ["200"]

    static = {f.path: f.content for f in plan_build(tmp_path).static_files}
    built = json.loads(static["openapi.json"])
    assert (built["paths"], built["components"]) == (
        schema["paths"],
        schema["components"],
    )


# @lat: [[tests#Inherited typed dependencies#Declared response precedence]]
def test_inherited_dependency_response_precedence():
    class Root401(Unauthorized):
        """root"""

    class Root403(Forbidden):
        """root"""

    class Root404(NotFound):
        """root"""

    class Root409(Conflict):
        """root"""

    class Root410(Gone):
        """root"""

    class LeafFirst401(Unauthorized):
        """leaf first"""

    class LeafFirst403(Forbidden):
        """leaf first"""

    class LeafSecond403(Forbidden):
        """leaf second"""

    class LeafSecond409(Conflict):
        """leaf second"""

    class Direct404(NotFound):
        """direct"""

    class Direct410(Gone):
        """direct"""

    class Handler410(Gone):
        """handler"""

    def root() -> None | Root401 | Root403 | Root404 | Root409 | Root410:
        return None

    def leaf_first() -> None | LeafFirst401 | LeafFirst403:
        return None

    def leaf_second() -> None | LeafSecond403 | LeafSecond409:
        return None

    def direct() -> None | Direct404 | Direct410:
        return None

    def handler(value: None = Depends(direct)) -> None | Handler410:
        return value

    app = Chano()
    app.add_route(
        "/",
        handler,
        methods=["GET"],
        boundaries=[
            Boundary(guards=[root]),
            Boundary(guards=[leaf_first, leaf_second]),
        ],
    )
    responses = app.openapi()["paths"]["/"]["get"]["responses"]
    # Handler, then its direct dependencies, then checks nearest segment
    # first and in declared order within a segment.
    assert {code: r["description"] for code, r in responses.items()} == {
        "200": "Successful Response",
        "401": "leaf first",
        "403": "leaf first",
        "404": "direct",
        "409": "leaf second",
        "410": "handler",
    }


# @lat: [[tests#Inherited typed dependencies#Shared inputs are documented once]]
def test_shared_dependency_inputs_documented_once():
    def shared(q: int = 0) -> int:
        return q

    def first(
        value: int = Depends(shared), x_key: Annotated[str, Header()] = ""
    ) -> int:
        return value

    def second(
        value: int = Depends(shared), x_key: Annotated[str, Header()] = ""
    ) -> int:
        return value

    def handler(
        a: int = Depends(first),
        b: int = Depends(second),
        q: int = 0,
    ):
        return a + b + q

    app = Chano()
    app.add_route("/", handler, methods=["GET"], boundaries=[Boundary(guards=[shared])])
    parameters = app.openapi()["paths"]["/"]["get"]["parameters"]
    assert [(p["name"], p["in"]) for p in parameters] == [
        ("q", "query"),
        ("x-key", "header"),
    ]
    assert make_client(app)("GET", "/?q=2", headers={"x-key": "k"}).json() == 6


# @lat: [[tests#Inherited typed dependencies#Trace roots and bundling]]
def test_inherited_dependencies_trace_roots(
    tmp_path, isolated_imports, stub_build_install
):
    store_files: dict[str, str] = {}
    for dist in (
        make_dist("chano", "chano"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("check-pkg", "check_pkg"),
    ):
        store_files.update(dist)

    stub_build_install(store_files)

    project = tmp_path / "checks"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "checks"\nversion = "0"\n',
            "app/(group)/boundary.py": (
                "from typing import Annotated\n"
                "from chano import Boundary, Header\n"
                "from chano.status import Forbidden\n\n"
                "def check(x_key: Annotated[str, Header()]) -> str | Forbidden:\n"
                "    import check_pkg\n"
                "    return x_key\n\n"
                "boundary = Boundary(guards=[check])\n"
            ),
            "app/(group)/admin/route.py": "def GET():\n    return {}\n",
            "app/public/route.py": "def GET():\n    return {}\n",
        },
    )
    app = (project / "app").resolve()
    plan = plan_build(project, platform=None)
    by_name = {spec.name: spec for spec in plan.functions}
    assert by_name["admin"].trace_roots == (
        app / "(group)" / "admin" / "route.py",
        app / "(group)" / "boundary.py",
    )
    assert by_name["public"].trace_roots == (app / "public" / "route.py",)

    built = json.loads({f.path: f.content for f in plan.static_files}["openapi.json"])
    admin = built["paths"]["/admin"]["get"]
    assert [(p["name"], p["in"]) for p in admin["parameters"]] == [("x-key", "header")]
    assert "403" in admin["responses"]
    assert "parameters" not in built["paths"]["/public"]["get"]

    out = build(project, platform=None, output=tmp_path / "out")

    def bundled(function: str) -> set[str]:
        vendor = out / "functions" / f"{function}.func" / "_vendor"
        return set(build_module.index_store(vendor).distributions)

    assert bundled("admin") == {"chano", "vercel-runtime", "check-pkg"}
    assert bundled("public") == {"chano", "vercel-runtime"}


# @lat: [[tests#Inherited typed dependencies#Async callable checks]]
def test_async_callable_checks_block_the_handler():
    calls: list[str] = []

    class Deny:
        async def __call__(self) -> None:
            calls.append("deny")
            raise Forbidden("denied")

    class Value:
        async def __call__(self) -> int:
            return 5

    async def deny_later() -> None:
        calls.append("later")
        raise Forbidden("later")

    def handler(value: int = Depends(Value())) -> int:
        calls.append("handler")
        return value

    for check in (Deny(), lambda: deny_later()):
        app = Chano(docs=False)
        app.add_route(
            "/", handler, methods=["GET"], boundaries=[Boundary(guards=[check])]
        )
        calls.clear()
        response = make_client(app)("GET", "/")
        assert response.status_code == 403
        assert "handler" not in calls

    app = Chano(docs=False)
    app.add_route("/", handler, methods=["GET"])
    assert make_client(app)("GET", "/").json() == 5


# @lat: [[tests#Inherited typed dependencies#Unhashable callable checks]]
def test_unhashable_callable_checks(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "roles.py": (
                "from dataclasses import dataclass\n"
                "from typing import Annotated\n"
                "from chano import Header\n"
                "from chano.status import Forbidden\n\n"
                "CALLS = []\n\n"
                "@dataclass\n"
                "class Role:\n"
                "    name: str\n\n"
                "    def __call__(\n"
                "        self, x_role: Annotated[str, Header()] = ''\n"
                "    ) -> str | Forbidden:\n"
                "        CALLS.append(self.name)\n"
                "        if x_role != self.name:\n"
                "            return Forbidden('wrong role')\n"
                "        return x_role\n\n"
                "admin = Role('admin')  # eq=True without frozen: unhashable\n"
            ),
            "app/boundary.py": (
                "from chano import Boundary\n"
                "from roles import admin\n\n"
                "boundary = Boundary(guards=[admin])\n"
            ),
            "app/route.py": (
                "from typing import Annotated\n"
                "from chano import Depends, Header\n"
                "from roles import admin\n\n"
                "def GET(\n"
                "    role: str = Depends(admin),\n"
                "    x_role: Annotated[str, Header()] = '',\n"
                "):\n"
                "    return {'role': role}\n"
            ),
        },
    )
    for app in (
        create_app(tmp_path / "app"),
        create_route_app(tmp_path / "app", "index"),
    ):
        client = make_client(app)
        calls = sys.modules["roles"].CALLS
        calls.clear()
        assert client("GET", "/", headers={"x-role": "admin"}).json() == {
            "role": "admin"
        }
        assert calls == ["admin"]
        assert client("GET", "/", headers={"x-role": "x"}).status_code == 403
    schema = create_app(tmp_path / "app").openapi()
    parameters = schema["paths"]["/"]["get"]["parameters"]
    assert [(p["name"], p["in"]) for p in parameters] == [("x-role", "header")]


# @lat: [[tests#Inherited typed dependencies#Shared input stays required]]
def test_shared_input_required_by_any_consumer():
    def check(q: str = "a") -> str:
        return q

    def handler(q: str) -> str:
        return q

    app = Chano()
    app.add_route("/", handler, methods=["GET"], boundaries=[Boundary(guards=[check])])
    client = make_client(app)
    (parameter,) = client("GET", "/openapi.json").json()["paths"]["/"]["get"][
        "parameters"
    ]
    assert (parameter["name"], parameter["required"]) == ("q", True)
    assert client("GET", "/").status_code == 422
    assert client("GET", "/?q=b").json() == "b"


class WholeA(msgspec.Struct):
    x: int


class WholeB(msgspec.Struct):
    y: int


# @lat: [[tests#Inherited typed dependencies#Body consumers across checks]]
def test_body_consumers_across_checks():
    def whole_check(a: WholeA) -> int:
        return a.x

    def embedded_check(x: Annotated[int, Body()], y: Annotated[int, Body()]) -> int:
        return x + y

    def handler(b: WholeB, x: int = Depends(whole_check)) -> list[int]:
        return [x, b.y]

    app = Chano()
    app.add_route(
        "/", handler, methods=["POST"], boundaries=[Boundary(guards=[embedded_check])]
    )
    client = make_client(app)
    schema = client("GET", "/openapi.json").json()
    body = schema["paths"]["/"]["post"]["requestBody"]
    assert body["required"] is True
    # Each single-body consumer decodes the whole body; the multi-field one
    # reads keys of it. The body must satisfy all of them at once.
    assert body["content"]["application/json"]["schema"] == {
        "allOf": [
            {"$ref": "#/components/schemas/WholeB"},
            {"$ref": "#/components/schemas/WholeA"},
            {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
            },
        ]
    }
    assert client("POST", "/", json={"x": 1, "y": 2}).json() == [1, 2]
    assert client("POST", "/", json={"x": 1}).status_code == 422

    # A single consumer keeps its plain schema.
    def plain(b: WholeB) -> int:
        return b.y

    single = Chano()
    single.add_route("/", plain, methods=["POST"])
    body = single.openapi()["paths"]["/"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/WholeB"}


# @lat: [[tests#Inherited typed dependencies#Distinct equal callables]]
def test_distinct_equal_callables_each_run():
    calls: list[str] = []

    @dataclasses.dataclass(frozen=True)
    class Gate:
        name: str
        block: bool = dataclasses.field(default=False, compare=False)

        def __call__(self) -> None:
            calls.append(f"{self.name}:{self.block}")
            if self.block:
                raise Forbidden("blocked")

    first, second = Gate("gate"), Gate("gate", block=True)
    assert first == second and hash(first) == hash(second)

    def handler() -> str:
        calls.append("handler")
        return "ok"

    app = Chano(docs=False)
    app.add_route(
        "/",
        handler,
        methods=["GET"],
        boundaries=[Boundary(guards=[first]), Boundary(guards=[second])],
    )
    response = make_client(app)("GET", "/")
    assert response.status_code == 403
    assert calls == ["gate:False", "gate:True"]


# @lat: [[tests#Inherited typed dependencies#Bound method checks run once]]
def test_bound_method_check_runs_once():
    class Guard:
        def __init__(self) -> None:
            self.calls = 0

        def check(self) -> int:
            self.calls += 1
            return self.calls

    guard = Guard()

    # guard.check makes a new bound-method object on each access.
    def handler(value: int = Depends(guard.check)) -> int:
        return value

    app = Chano(docs=False)
    app.add_route(
        "/", handler, methods=["GET"], boundaries=[Boundary(guards=[guard.check])]
    )
    assert make_client(app)("GET", "/").json() == 1
    assert guard.calls == 1


# @lat: [[tests#Inherited typed dependencies#Shared inputs keep every constraint]]
def test_shared_inputs_keep_every_constraint():
    def check(
        n: Annotated[int, msgspec.Meta(ge=0)],
        x: Annotated[int, msgspec.Meta(ge=0), Body()],
        y: Annotated[int, Body()],
    ) -> None:
        return None

    def handler(
        n: Annotated[int, msgspec.Meta(le=10)],
        x: Annotated[int, msgspec.Meta(le=10), Body()],
        z: Annotated[int, Body()],
        same: int = 0,
    ) -> list[int]:
        return [n, x]

    def same_check(same: int = 0) -> None:
        return None

    app = Chano()
    app.add_route(
        "/",
        handler,
        methods=["POST"],
        boundaries=[Boundary(guards=[check]), Boundary(guards=[same_check])],
    )
    client = make_client(app)
    operation = client("GET", "/openapi.json").json()["paths"]["/"]["post"]
    both = {
        "allOf": [
            {"type": "integer", "minimum": 0},
            {"type": "integer", "maximum": 10},
        ]
    }
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert list(parameters) == ["n", "same"]
    assert parameters["n"]["schema"] == both
    assert parameters["n"]["required"] is True
    assert parameters["same"]["schema"] == {"type": "integer"}  # identical: one
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body == {
        "type": "object",
        "properties": {"x": both, "y": {"type": "integer"}, "z": {"type": "integer"}},
        "required": ["x", "y", "z"],
    }

    def post(n: int, x: int) -> int:
        return client("POST", f"/?n={n}", json={"x": x, "y": 0, "z": 0}).status_code

    assert post(5, 5) == 200
    assert post(-1, 5) == post(11, 5) == 422
    assert post(5, -1) == post(5, 11) == 422


def sync_gen_check():
    yield


async def async_gen_check():
    yield


class GeneratorCall:
    def __call__(self):
        yield


class AsyncGeneratorCall:
    async def __call__(self):
        yield


GENERATOR_CHECKS = [
    pytest.param(sync_gen_check, id="sync"),
    pytest.param(async_gen_check, id="async"),
    pytest.param(GeneratorCall(), id="sync-instance"),
    pytest.param(AsyncGeneratorCall(), id="async-instance"),
]


# @lat: [[tests#Inherited typed dependencies#Generator checks are rejected]]
@pytest.mark.parametrize("check", GENERATOR_CHECKS)
def test_generator_checks_are_rejected(tmp_path, isolated_imports, check):
    def handler() -> str:
        return "ok"

    with pytest.raises(TypeError, match="generator"):
        Chano().add_route(
            "/", handler, methods=["GET"], boundaries=[Boundary(guards=[check])]
        )

    sources = {
        "sync": "def check():\n    yield\n",
        "async": "async def check():\n    yield\n",
    }
    for kind, source in sources.items():
        project = tmp_path / kind
        write_project(
            project,
            {
                "app/boundary.py": (
                    "from chano import Boundary\n\n"
                    + source
                    + "\nboundary = Boundary(guards=[check])\n"
                ),
                "app/route.py": "def GET():\n    return {}\n",
            },
        )
        with pytest.raises(TypeError, match="generator"):
            create_app(project / "app")
        with pytest.raises(TypeError, match="generator"):
            create_route_app(project / "app", "index")


# @lat: [[tests#Inherited typed dependencies#Mixed list and scalar query inputs]]
def test_mixed_list_and_scalar_query_inputs_are_rejected():
    def check(q: list[int]) -> None:
        return None

    def handler(q: int = 0) -> int:
        return q

    app = Chano()
    app.add_route("/", handler, methods=["GET"], boundaries=[Boundary(guards=[check])])
    with pytest.raises(ValueError, match="'q'"):
        app.openapi()

    # The same shape everywhere is still one input.
    def lists(q: list[int]) -> list[int]:
        return q

    same = Chano()
    same.add_route("/", lists, methods=["GET"], boundaries=[Boundary(guards=[check])])
    (parameter,) = same.openapi()["paths"]["/"]["get"]["parameters"]
    assert parameter["schema"] == {"type": "array", "items": {"type": "integer"}}


# @lat: [[tests#Inherited typed dependencies#Header names are case-insensitive]]
def test_header_inputs_merge_case_insensitively():
    def check(key: Annotated[str, Header(alias="X-Key")]) -> str:
        return key

    def handler(
        key: Annotated[str, msgspec.Meta(min_length=2), Header(alias="x-key")] = "",
    ) -> str:
        return key

    app = Chano()
    app.add_route("/", handler, methods=["GET"], boundaries=[Boundary(guards=[check])])
    client = make_client(app)
    (parameter,) = client("GET", "/openapi.json").json()["paths"]["/"]["get"][
        "parameters"
    ]
    assert (parameter["name"], parameter["in"]) == ("X-Key", "header")
    assert parameter["required"] is True
    assert parameter["schema"] == {
        "allOf": [{"type": "string"}, {"type": "string", "minLength": 2}]
    }
    assert client("GET", "/").status_code == 422
    assert client("GET", "/", headers={"x-key": "a"}).status_code == 422
    assert client("GET", "/", headers={"X-KEY": "ab"}).json() == "ab"


# @lat: [[tests#Inherited typed dependencies#Keyed body list and scalar consumers]]
def test_keyed_body_list_and_any_consumers():
    def check(value: Annotated[Any, Body()], tag: Annotated[str, Body()] = "") -> None:
        return None

    def handler(
        value: Annotated[list[int], Body()], note: Annotated[str, Body()] = ""
    ) -> list[int]:
        return value

    app = Chano()
    app.add_route("/", handler, methods=["POST"], boundaries=[Boundary(guards=[check])])
    client = make_client(app)
    operation = client("GET", "/openapi.json").json()["paths"]["/"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body["properties"]["value"] == {
        "allOf": [{}, {"type": "array", "items": {"type": "integer"}}]
    }
    assert client("POST", "/", json={"value": [1, 2]}).json() == [1, 2]
    assert client("POST", "/", json={"value": 1}).status_code == 422


# @lat: [[tests#Inherited typed dependencies#Checks returning generators fail closed]]
def test_checks_returning_generators_fail_closed():
    calls: list[str] = []

    def gen():
        calls.append("gen")
        yield

    async def agen():
        calls.append("agen")
        yield

    @functools.wraps(gen)
    def wrapped():
        return gen()

    @functools.wraps(agen)
    def awrapped():
        return agen()

    def needs_wrapped(value: Any = Depends(wrapped)) -> None:
        return None  # a Depends may take a generator as an ordinary value

    def handler() -> str:
        calls.append("handler")
        return "ok"

    cases = [
        [[wrapped]],
        [[awrapped]],
        # The earlier check's Depends puts the generator in the shared cache.
        [[needs_wrapped], [wrapped]],
    ]
    for guards_groups in cases:
        app = Chano(docs=False)
        app.add_route(
            "/",
            handler,
            methods=["GET"],
            boundaries=[Boundary(guards=g) for g in guards_groups],
        )
        calls.clear()
        response = make_client(app)("GET", "/")
        assert response.status_code == 500
        assert "handler" not in calls
