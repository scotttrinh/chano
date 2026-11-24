import asyncio
import dataclasses
import functools
import gc
import inspect
import json
import re
import subprocess
import sys
import threading
import time
import types
import uuid
import warnings
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import msgspec
import pytest

from chano import (
    Body,
    Chano,
    ChanoConfig,
    Cookie,
    Depends,
    Geo,
    Header,
    HTMLResponse,
    HTTPBearer,
    HTTPException,
    JSONRepresentation,
    JSONResponse,
    PlainTextResponse,
    Query,
    Raw,
    RedirectResponse,
    Representation,
    Request,
    Response,
    ServerSentEvent,
    SSEResponse,
    Status,
    StatusRepresentation,
    StreamingResponse,
    create_app,
    create_route_app,
    lazy,
    load_config,
    openapi,
    raises,
    representation,
    status_code,
)
from chano import build as build_module
from chano import params as params_module
from chano import requests as requests_module
from chano.build import build, emit_build, plan_build
from chano.cli import main as cli_main
from chano.config import normalize_base_path
from chano.openapi import swagger_html
from chano.routing import discover_routes, load_module

EXAMPLE = Path(__file__).parent.parent / "examples" / "hello"
FULLSTACK = Path(__file__).parent.parent / "examples" / "fullstack"
AUTH = {"Authorization": "Bearer secret"}
HOST_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"


def make_client(app, root_path: str = ""):
    def request(method: str, url: str, **kwargs) -> httpx.Response:
        async def send():
            transport = httpx.ASGITransport(app=app, root_path=root_path)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                return await http.request(method, url, **kwargs)

        return asyncio.run(send())

    return request


def raw_get(app, path: str, root_path: str = "") -> tuple[int, bytes]:
    """Call ``app`` with an unnormalized ``path``, which httpx would clean up."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": root_path,
        "headers": [],
        "query_string": b"",
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return sent[0]["status"], body


@pytest.fixture(scope="module")
def example_app():
    return create_app(EXAMPLE / "app", public_dir=EXAMPLE / "public")


@pytest.fixture(scope="module")
def client(example_app):
    return make_client(example_app)


# @lat: [[tests#Routes and parameters]]
def test_routes_and_parameters(client):
    assert client("GET", "/").json() == {"message": "Hello from Chano"}
    assert client("GET", "/api/hello?name=jj").json() == {"hello": "jj"}
    assert client("GET", "/api/items/7?verbose=true").json() == {
        "item_id": 7,
        "verbose": True,
    }
    assert client("HEAD", "/api/hello").content == b""

    response = client("POST", "/api/hello")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert client("GET", "/nope").json() == {"detail": "Not Found"}


# @lat: [[tests#Validation errors]]
def test_validation_errors(client):
    response = client("GET", "/api/items/nope")
    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"loc": ["path", "item_id"], "msg": "Expected `int`, got `str`"}]
    }

    response = client("PUT", "/api/items/7", json={"name": "x"}, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"loc": ["body"], "msg": "Object missing required field `price`"}
    ]

    # Constraints declared with msgspec.Meta are enforced, with a path.
    item = {"name": "x", "price": -1}
    response = client("PUT", "/api/items/7", json=item, headers=AUTH)
    assert response.json()["detail"] == [
        {"loc": ["body", "price"], "msg": "Expected `float` > 0.0"}
    ]

    response = client("PUT", "/api/items/7", content=b"{nope", headers=AUTH)
    assert response.status_code == 422
    assert "malformed" in response.json()["detail"][0]["msg"]

    response = client("PUT", "/api/items/7", headers=AUTH)
    assert response.json()["detail"] == [{"loc": ["body"], "msg": "Field required"}]


# @lat: [[tests#Body and dependencies]]
def test_body_and_dependencies(client):
    item = {"name": "x", "price": 1.5}
    assert client("PUT", "/api/items/7", json=item, headers=AUTH).json() == item

    response = client("PUT", "/api/items/7", json=item)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"

    headers = {"Authorization": "Bearer wrong"}
    response = client("PUT", "/api/items/7", json=item, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


# @lat: [[tests#Dev docs and static files]]
def test_dev_docs_and_static_files(client, example_app):
    assert "static file" in client("GET", "/hello.txt").text
    assert raw_get(example_app, "/../pyproject.toml")[0] == 404
    assert raw_get(example_app, "/hello.txt/")[0] == 404
    assert "swagger" in client("GET", "/docs").text.lower()

    schema = client("GET", "/openapi.json").json()
    put = schema["paths"]["/api/items/{item_id}"]["put"]
    assert put["summary"] == "Replace an item. Requires `Authorization: Bearer secret`."
    # The bearer credential is a security scheme, not a header parameter.
    assert [(p["name"], p["in"]) for p in put["parameters"]] == [("item_id", "path")]
    assert put["security"] == [{"HTTPBearer": []}]
    assert schema["components"]["securitySchemes"] == {
        "HTTPBearer": {"type": "http", "scheme": "bearer"}
    }
    assert list(put["responses"]) == ["200", "401", "422"]
    assert put["responses"]["401"] == {"description": "Not authenticated"}
    assert "security" not in schema["paths"]["/api/items/{item_id}"]["get"]
    ref = {"$ref": "#/components/schemas/Item"}
    assert put["requestBody"]["content"]["application/json"]["schema"] == ref
    assert put["responses"]["200"]["content"]["application/json"]["schema"] == ref
    properties = schema["components"]["schemas"]["Item"]["properties"]
    assert properties["price"] == {"type": "number", "exclusiveMinimum": 0}
    verbose = schema["paths"]["/api/items/{item_id}"]["get"]["parameters"][1]
    assert verbose == {
        "name": "verbose",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    }


@pytest.fixture
def isolated_imports(monkeypatch):
    """Undo the sys.path and sys.modules changes made by loading route modules."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    modules = set(sys.modules)
    yield
    for name in set(sys.modules) - modules:
        del sys.modules[name]


def write_project(project: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(content)


# @lat: [[tests#Build plan]]
def test_build_plan(tmp_path, isolated_imports):
    project = tmp_path / "shop"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "shop"\nversion = "0"\n',
            "app/(admin)/orders/route.py": "def GET():\n    return []\n",
            "app/orders/[order_id]/route.py": (
                "max_duration = 30\nmemory = 1024\nregions = ['iad1']\n\n"
                "def GET(order_id: int):\n    return {'id': order_id}\n"
            ),
            "public/css/site.css": "body {}\n",
        },
    )

    plan = plan_build(project, python="3.13")

    assert plan.project_dir == plan.workspace_root == project
    assert plan.package is None
    assert plan.app_dir == project / "app"
    assert plan.public_dir == project / "public"
    assert plan.output_dir == project / ".vercel" / "output"
    assert not plan.output_dir.exists()

    assert [f.name for f in plan.functions] == ["orders", "orders/[order_id]"]
    assert [f.route.url_path for f in plan.functions if f.route is not None] == [
        "/orders",
        "/orders/{order_id}",
    ]
    orders, order = plan.functions
    assert orders.route is not None
    assert order.route is not None
    assert plan.config == ChanoConfig()
    assert "create_route_app(" in order.entrypoint
    assert (
        "'orders/[order_id]',\n    base_path='',\n    after_backend=\"wait_until\",\n)"
        in order.entrypoint
    )
    assert order.vc_config == {
        "runtime": "python3.13",
        "handler": "vc__handler__python.vc_handler",
        "supportsResponseStreaming": True,
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "maxDuration": 30,
        "memory": 1024,
        "regions": ["iad1"],
    }
    assert set(orders.vc_config) == {
        "runtime",
        "handler",
        "supportsResponseStreaming",
        "environment",
    }
    assert orders.trace_roots == (orders.route.file,)
    assert orders.include == ()
    assert order.trace_roots == (order.route.file,)
    assert order.include == ()

    static = {f.path: f.content for f in plan.static_files}
    assert set(static) == {"openapi.json", "docs.html"}
    assert set(json.loads(static["openapi.json"])["paths"]) == {
        "/orders",
        "/orders/{order_id}",
    }
    assert "swagger" in static["docs.html"].lower()

    assert plan.routes == (
        {"handle": "filesystem"},
        {"src": "^/docs$", "dest": "/docs.html"},
        {"src": "^/orders/([^/]+)$", "dest": "/orders/[order_id]"},
    )


# @lat: [[tests#Build emit]]
def test_emit_build_preserves_public_and_config_isolation(
    tmp_path, isolated_imports, monkeypatch
):
    project = tmp_path / "snap"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "snap"\nversion = "0"\n',
            "snap_config.py": (
                "REGIONS = ['iad1']\n\n"
                "class Regions(list):\n"
                "    def __deepcopy__(self, memo):\n"
                "        raise TypeError('no deepcopy')\n"
            ),
            "app/a/route.py": "from snap_config import REGIONS as regions\n",
            "app/b/route.py": (
                "from pathlib import Path\nfrom snap_config import REGIONS\n"
                "REGIONS.append('sfo1')\n"
                "(Path(__file__).parents[2] / 'public' / 'late.txt').write_text('x')\n"
            ),
            "app/c/route.py": (
                "from snap_config import Regions\nregions = Regions(['cdg1'])\n"
            ),
            "app/d/route.py": "regions = {1: 'first', '1': 'second'}\n",
            "assets/logo.svg": "<svg/>",
        },
    )
    (project / "public" / "empty").mkdir(parents=True)
    (project / "public" / "empty").chmod(0o750)
    (project / "public" / "linked").symlink_to(project / "assets")

    plan = plan_build(project)
    assert plan.functions[0].vc_config["regions"] == ["iad1"]
    assert plan.functions[2].vc_config["regions"] == ["cdg1"]

    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    output = emit_build(plan)

    static = output / "static"
    assert (static / "linked" / "logo.svg").read_text() == "<svg/>"
    assert (static / "empty").stat().st_mode & 0o777 == 0o750
    # Like the original builder, public/ is copied after route imports.
    assert (static / "late.txt").read_text() == "x"
    functions = output / "functions"
    assert (functions / "b.func" / "app" / "b" / "route.py").is_file()
    a_config = json.loads((functions / "a.func" / ".vc-config.json").read_text())
    assert a_config["regions"] == ["iad1"]
    c_config = json.loads((functions / "c.func" / ".vc-config.json").read_text())
    assert c_config["regions"] == ["cdg1"]
    d_raw = (functions / "d.func" / ".vc-config.json").read_text()
    assert d_raw.count('"1":') == 2

    # public/ created during route imports is also discovered at emit time.
    late_public = tmp_path / "late_public"
    write_project(
        late_public,
        {
            "pyproject.toml": '[project]\nname = "late"\nversion = "0"\n',
            "app/late/route.py": (
                "from pathlib import Path\n"
                "pub = Path(__file__).parents[2] / 'public'\n"
                "pub.mkdir(exist_ok=True)\n"
                "(pub / 'late.txt').write_text('created')\n"
            ),
        },
    )
    late_output = emit_build(plan_build(late_public))
    assert (late_output / "static" / "late.txt").read_text() == "created"


@pytest.fixture(scope="module")
def output():
    # Vendor for this machine so the function can be invoked locally.
    return build(EXAMPLE, python=HOST_PYTHON, platform=None)


# @lat: [[tests#Build output layout]]
def test_build_output_layout(output):
    config = json.loads((output / "config.json").read_text())
    assert config["version"] == 3
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/docs$", "dest": "/docs.html"},
        {"src": "^/api/items/([^/]+)$", "dest": "/api/items/[item_id]"},
    ]

    static = output / "static"
    assert (static / "hello.txt").is_file()
    assert "swagger" in (static / "docs.html").read_text().lower()
    schema = json.loads((static / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/", "/api/hello", "/api/items/{item_id}"}

    functions = output / "functions"
    assert {p.relative_to(functions).as_posix() for p in functions.rglob("*.func")} == {
        "index.func",
        "api/hello.func",
        "api/items/[item_id].func",
    }
    func = functions / "api" / "items" / "[item_id].func"
    vc_config = json.loads((func / ".vc-config.json").read_text())
    assert vc_config["runtime"] == f"python{HOST_PYTHON}"
    assert plan_build(EXAMPLE).python == "3.12"
    assert vc_config["handler"] == "vc__handler__python.vc_handler"
    assert vc_config["maxDuration"] == 10
    assert (func / "_vendor" / "chano").is_dir()
    assert (func / "_vendor" / "msgspec").is_dir()
    assert (func / "_vendor" / "vercel").is_dir()
    assert not any(p.name.startswith("uvicorn") for p in (func / "_vendor").iterdir())
    assert not (func / "public").exists()

    hello_config = (functions / "api" / "hello.func" / ".vc-config.json").read_text()
    assert "maxDuration" not in json.loads(hello_config)


INVOKE = """
import json, sys
from vc__handler__python import vc_handler
event = {"body": json.dumps({"method": "GET", "path": sys.argv[1], "headers": {}})}
print(json.dumps(vc_handler(event, None)))
"""


def invoke(output, function, path):
    """Invoke a built function the way Vercel does, using only vendored deps."""
    result = subprocess.run(
        [sys.executable, "-S", "-c", INVOKE, path],
        cwd=output / "functions" / f"{function}.func",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# @lat: [[tests#Built function invocation]]
def test_built_functions_handle_their_own_route(output):
    response = invoke(output, "api/items/[item_id]", "/api/items/42?q=1")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"item_id": 42, "verbose": False}

    response = invoke(output, "index", "/")
    assert json.loads(response["body"]) == {"message": "Hello from Chano"}

    # Each function only knows its own route.
    assert invoke(output, "api/hello", "/api/items/42")["statusCode"] == 404
    assert invoke(output, "api/hello", "/docs")["statusCode"] == 404


# @lat: [[tests#Project config]]
def test_project_config(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n')
    assert load_config(pyproject) == ChanoConfig(
        base_path="", tree_shake=True, include=()
    )

    pyproject.write_text(
        '[tool.chano]\nbase_path = "api/"\ntree_shake = false\ninclude = ["anyio"]\n'
    )
    assert load_config(pyproject) == ChanoConfig(
        base_path="/api", tree_shake=False, include=("anyio",)
    )

    for raw, normalized in [
        ("", ""),
        ("/", ""),
        ("/api/", "/api"),
        ("/api", "/api"),
        ("/v1/api", "/v1/api"),
        ("/v1+/a-b_c.d~e!,;=@", "/v1+/a-b_c.d~e!,;=@"),
    ]:
        assert ChanoConfig(base_path=raw).base_path == normalized

    for table, error in [
        ("base_pth = '/api'", "unknown field `base_pth`"),
        ("base_path = 1", "Expected `str`, got `int`"),
        ("base_path = '/a//b'", "invalid base_path"),
        ("base_path = '/../up'", "invalid base_path"),
        ("base_path = '/{x}'", "invalid base_path"),
        ("base_path = '/a/./b'", "invalid base_path"),
        ("base_path = '/a\\u0000b'", "invalid base_path"),
        ("base_path = '/a\\nb'", "invalid base_path"),
        ("base_path = '/a\\u007fb'", "invalid base_path"),
        ("base_path = '/a b'", "invalid base_path"),
        ("base_path = '/a%2fb'", "invalid base_path"),
        ("base_path = '/a\"b'", "invalid base_path"),
        ('base_path = "/a\'b"', "invalid base_path"),
        ("base_path = '/a\\\\b'", "invalid base_path"),
        ("base_path = '/<script>'", "invalid base_path"),
        ("base_path = '/a?b'", "invalid base_path"),
        ("base_path = '/a#b'", "invalid base_path"),
        ("base_path = '/$1'", "invalid base_path"),
        ("base_path = '/caf\u00e9'", "invalid base_path"),
        ("base_path = '/svc.func'", "invalid base_path"),
        ("base_path = '/v1/svc.func/'", "invalid base_path"),
        ("tree_shake = 1", "Expected `bool`, got `int`"),
        ("include = 'anyio'", "Expected `array`, got `str`"),
        ("include = [1]", "Expected `str`, got `int`"),
    ]:
        pyproject.write_text(f"[tool.chano]\n{table}\n")
        with pytest.raises(ValueError, match=error):
            load_config(pyproject)

    for toml, error in [
        ("tool = true", "`tool` must be a table"),
        ("[tool]\nchano = true", "`tool.chano` must be a table"),
    ]:
        pyproject.write_text(f"{toml}\n")
        with pytest.raises(ValueError, match=f"invalid \\[tool.chano\\] .*{error}"):
            load_config(pyproject)

    with pytest.raises(ValueError, match="invalid base_path"):
        ChanoConfig(base_path="/a\x00b")
    # functions/svc.func would be read as a function, not a directory.
    with pytest.raises(ValueError, match="invalid base_path"):
        normalize_base_path("/svc.func")
    assert normalize_base_path("/svc.funcs/x.func.d") == "/svc.funcs/x.func.d"


def test_swagger_html_escapes_title_and_url():
    page = swagger_html("<b>&", '/x"y</script><!--')
    assert "<title>&lt;b&gt;&amp;</title>" in page
    script = page.split("<script>\n", 1)[1].split("\n</script>", 1)[0]
    assert "<" not in script
    assert 'url: "/x\\"y\\u003c/script>\\u003c!--"' in script
    url = script.split("url: ", 1)[1].split(", dom_id", 1)[0]
    assert json.loads(url) == '/x"y</script><!--'


PREFIXED_FILES = {
    "pyproject.toml": (
        '[project]\nname = "prefixed"\nversion = "0"\n\n'
        '[tool.chano]\nbase_path = "/api/"\n'
    ),
    "app/route.py": "def GET():\n    return {'route': 'root'}\n",
    "app/users/route.py": "def GET():\n    return {'route': 'users'}\n",
    "app/users/[id]/route.py": (
        "from chano import Request\n\n"
        "def GET(id: int, request: Request):\n"
        "    return {'id': id, 'path': request.path}\n"
    ),
    "public/hello.txt": "prefixed static\n",
}


@pytest.fixture(scope="module")
def prefixed_project(tmp_path_factory):
    project = tmp_path_factory.mktemp("prefixed")
    write_project(project, PREFIXED_FILES)
    return project


# @lat: [[tests#Prefixed dev routing]]
def test_prefixed_dev_routing(prefixed_project, isolated_imports):
    # base_path comes from [tool.chano] beside app/, as in `chano dev`.
    app = create_app(prefixed_project / "app", public_dir=prefixed_project / "public")
    assert app.base_path == "/api"
    client = make_client(app)

    assert client("GET", "/api").json() == {"route": "root"}
    assert client("GET", "/api/users").json() == {"route": "users"}
    assert client("GET", "/api/users/7").json() == {"id": 7, "path": "/api/users/7"}
    assert client("GET", "/api/hello.txt").text == "prefixed static\n"
    assert raw_get(app, "/api/../pyproject.toml")[0] == 404
    assert raw_get(app, "/api/../../pyproject.toml")[0] == 404

    docs = client("GET", "/api/docs").text
    assert docs == swagger_html("Chano", "/api/openapi.json")
    schema = client("GET", "/api/openapi.json").json()
    assert set(schema["paths"]) == {"/api", "/api/users", "/api/users/{id}"}

    # Outside the prefix (or on its trailing-slash root): Chano's JSON 404.
    for path in ["/", "/api/", "/users", "/hello.txt", "/apiary/hello.txt", "/docs"]:
        response = client("GET", path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found"}

    # An explicit base_path overrides the config.
    plain = make_client(create_app(prefixed_project / "app", base_path=""))
    assert plain("GET", "/users").json() == {"route": "users"}


# @lat: [[tests#Root path reconstruction]]
def test_root_path_reconstruction(prefixed_project, isolated_imports):
    async def receive():
        return {"type": "http.request"}

    for root_path, path, full in [
        ("", "/", "/"),
        ("", "/api/users", "/api/users"),
        ("/api", "/", "/api"),
        ("/api/", "/", "/api"),
        ("/api", "/users", "/api/users"),
        ("/api", "/apiary", "/api/apiary"),
        ("/api", "/api/users", "/api/api/users"),
    ]:
        scope = {"path": path, "root_path": root_path}
        assert Request(scope, receive).path == full
    assert Request({"path": "/x"}, receive).path == "/x"

    app = create_app(prefixed_project / "app")

    # What vercel-runtime's prefix strip produces for both /api and /api/.
    assert make_client(app, "/api")("GET", "/").json() == {"route": "root"}
    # Without a strip, /api/ is a literal miss.
    assert make_client(app, "")("GET", "/api/").status_code == 404
    assert make_client(app, "/api")("GET", "/users").json() == {"route": "users"}
    assert make_client(app, "/api/")("GET", "/users/3").json() == {
        "id": 3,
        "path": "/api/users/3",
    }
    # No startswith check: a stripped path may itself begin with the prefix.
    assert make_client(app, "/api")("GET", "/apiary").status_code == 404
    response = make_client(app, "/api")("GET", "/api/users")
    assert response.status_code == 404

    route_app = create_route_app(prefixed_project / "app", "api/users/[id]")
    route_client = make_client(route_app, "/api")
    assert route_client("GET", "/users/3").json() == {"id": 3, "path": "/api/users/3"}
    assert route_client("GET", "/").status_code == 404
    response = route_client("GET", "/api/users/3")
    assert response.status_code == 404
    response = route_client("GET", "/apiary/x")
    assert response.status_code == 404


@pytest.fixture(scope="module")
def prefixed_output(prefixed_project):
    install = build_module._install_dependencies

    def install_like_example(
        workspace_root, vendor_dir, python, platform, package=None
    ):
        # Same dependencies as examples/hello, which has a lock to export.
        install(EXAMPLE, vendor_dir, python, platform)

    path, modules = list(sys.path), set(sys.modules)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(build_module, "_install_dependencies", install_like_example)
            patch.setattr(sys, "dont_write_bytecode", True)
            return build(prefixed_project, python=HOST_PYTHON, platform=None)
    finally:
        sys.path[:] = path
        for name in set(sys.modules) - modules:
            del sys.modules[name]


# @lat: [[tests#Prefixed build output]]
def test_prefixed_build_output(prefixed_project, prefixed_output):
    output = prefixed_output
    assert output == prefixed_project / ".vercel" / "output"

    config = json.loads((output / "config.json").read_text())
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/api/docs$", "dest": "/api/docs.html"},
        {"src": "^/api/users/([^/]+)$", "dest": "/api/users/[id]"},
    ]

    static = output / "static"
    assert {p.relative_to(static).as_posix() for p in static.rglob("*")} == {
        "api",
        "api/hello.txt",
        "api/docs.html",
        "api/openapi.json",
    }
    docs = (static / "api" / "docs.html").read_text()
    assert docs == swagger_html(prefixed_project.name, "/api/openapi.json")
    schema = json.loads((static / "api" / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/api", "/api/users", "/api/users/{id}"}

    functions = output / "functions"
    assert {p.relative_to(functions).as_posix() for p in functions.rglob("*.func")} == {
        "api.func",
        "api/users.func",
        "api/users/[id].func",
    }

    response = invoke(output, "api", "/api")
    assert json.loads(response["body"]) == {"route": "root"}
    response = invoke(output, "api/users/[id]", "/api/users/5")
    assert json.loads(response["body"]) == {"id": 5, "path": "/api/users/5"}
    assert invoke(output, "api/users", "/users")["statusCode"] == 404


# @lat: [[tests#Dev routing priority]]
def test_dev_routing_priority(tmp_path, isolated_imports):
    project = tmp_path / "catchall"
    write_project(
        project,
        {
            "pyproject.toml": '[tool.chano]\nbase_path = "/api"\n',
            "app/[...item]/route.py": (
                "def GET(item: str):\n    return {'item': item}\n"
            ),
            "app/docs/route.py": "def POST():\n    return {'route': 'docs'}\n",
            "app/users/route.py": "def GET():\n    return {'route': 'users'}\n",
            "public/hello.txt": "static\n",
            "public/users": "shadowed\n",
        },
    )
    (project / "public" / "loop").symlink_to(project / "public" / "loop")
    app = create_app(project / "app", public_dir=project / "public")
    client = make_client(app)

    # The filesystem check and docs rewrite run before dynamic routes.
    assert client("GET", "/api/hello.txt").text == "static\n"
    assert client("HEAD", "/api/hello.txt").content == b""
    assert client("GET", "/api/openapi.json").json()["openapi"] == "3.1.0"
    assert "swagger" in client("GET", "/api/docs.html").text.lower()
    # Literal routes still run, ahead of files and docs, like a static-path
    # function in the filesystem check: GET /api/docs is that function's 405.
    assert client("GET", "/api/users").json() == {"route": "users"}
    assert client("POST", "/api/docs").json() == {"route": "docs"}
    response = client("GET", "/api/docs")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    # Anything else falls through to the catch-all.
    assert client("GET", "/api/a/b").json() == {"item": "a/b"}
    assert client("POST", "/api/a").headers["allow"] == "GET"

    # Directory-style, looping and traversing paths are not files.
    for path in ["/api/hello.txt/", "/api/loop", "/api/../pyproject.toml"]:
        status, body = raw_get(app, path)
        assert status == 200, path
        assert json.loads(body) == {"item": path.removeprefix("/api/")}

    plain = create_app(project / "app", public_dir=project / "public", base_path="")
    for path in ["/hello.txt/", "/loop", "/../pyproject.toml", "/a\x00b"]:
        assert raw_get(plain, path)[0] == 200, path
    assert raw_get(plain, "/hello.txt")[1] == b"static\n"


# @lat: [[tests#Dev docs precedence]]
def test_dev_docs_precedence(tmp_path, isolated_imports):
    project = tmp_path / "docsfiles"
    write_project(
        project,
        {
            "pyproject.toml": '[tool.chano]\nbase_path = "/api"\n',
            "app/[...item]/route.py": (
                "def GET(item: str):\n    return {'get': item}\n\n"
                "def POST(item: str):\n    return {'post': item}\n"
            ),
            "public/docs": "static docs\n",
            "public/docs.html": "stale docs\n",
            "public/openapi.json": "{}\n",
        },
    )
    app = create_app(project / "app", public_dir=project / "public")
    client = make_client(app)
    swagger = swagger_html("Chano", "/api/openapi.json")

    # public/docs is found by the filesystem check, before the docs rewrite;
    # the generated docs.html and openapi.json overwrite their public/ copies.
    assert client("GET", "/api/docs").text == "static docs\n"
    assert client("GET", "/api/docs.html").text == swagger
    assert client("GET", "/api/openapi.json").json()["openapi"] == "3.1.0"
    assert client("HEAD", "/api/docs.html").content == b""

    # Method checks run before generating the schema or reading file bytes.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, "openapi", lambda: pytest.fail("openapi() called on POST"))
        patch.setattr(
            Path, "read_bytes", lambda self: pytest.fail("read_bytes() called on POST")
        )
        # Files and docs answer other methods with 405, not the catch-all.
        for path in ["/api/docs", "/api/docs.html", "/api/openapi.json"]:
            response = client("POST", path)
            assert response.status_code == 405, path
            assert response.headers["allow"] == "GET, HEAD"
    assert client("POST", "/api/other").json() == {"post": "other"}

    # Without public/docs, /api/docs is the generated Swagger UI.
    bare = make_client(create_app(project / "app"))
    assert bare("GET", "/api/docs").text == swagger
    assert bare("POST", "/api/docs").headers["allow"] == "GET, HEAD"
    assert bare("POST", "/api/a/b").json() == {"post": "a/b"}


# @lat: [[tests#Route load cache]]
def test_route_load_cache(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/route.py": "def GET():\n    return {}\n",
            "app/bad/route.py": "raise RuntimeError('boom')\n",
        },
    )
    plain, prefixed = (
        {r.url_path: r for r in discover_routes(tmp_path / "app", base)}
        for base in ["", "/v1"]
    )
    # One file under two prefixes is one module.
    assert plain["/"].function != prefixed["/v1"].function
    assert plain["/"].load() is prefixed["/v1"].load()

    before = set(sys.modules)
    for _ in range(2):  # retried, not served from a partial cache entry
        with pytest.raises(RuntimeError, match="boom"):
            prefixed["/v1/bad"].load()
        assert set(sys.modules) == before


# @lat: [[tests#Route segments]]
def test_route_segments(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            name: "def GET():\n    return {}\n"
            for name in [
                "app/route.py",
                "app/a/b/route.py",
                "app/(admin)/users/route.py",
                "app/items/[id]/route.py",
                "app/files/[...path]/route.py",
            ]
        },
    )
    app = (tmp_path / "app").resolve()
    for base in ["", "/v1"]:
        segments = {
            r.file.relative_to(app).as_posix(): r.segments
            for r in discover_routes(tmp_path / "app", base)
        }
        assert segments == {
            "route.py": (app,),
            "a/b/route.py": (app, app / "a", app / "a" / "b"),
            "(admin)/users/route.py": (app, app / "(admin)", app / "(admin)" / "users"),
            "items/[id]/route.py": (app, app / "items", app / "items" / "[id]"),
            "files/[...path]/route.py": (
                app,
                app / "files",
                app / "files" / "[...path]",
            ),
        }


def logging_segment(log: Path, name: str) -> str:
    """Source for a segment file that appends ``name`` to ``log`` when run."""
    return (
        f"NAME = {name!r}\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(NAME + '\\n')\n"
    )


# @lat: [[tests#Ancestor segment loading]]
def test_route_load_segments(tmp_path, isolated_imports):
    log = tmp_path / "runs.log"
    write_project(
        tmp_path,
        {
            "app/segment.py": logging_segment(log, "root"),
            "app/admin/segment.py": logging_segment(log, "admin"),
            "app/admin/users/route.py": "def GET():\n    return {}\n",
            "app/admin/(group)/settings/route.py": "def GET():\n    return {}\n",
            "app/public/segment.py": logging_segment(log, "public"),
            "app/public/route.py": "def GET():\n    return {}\n",
        },
    )
    routes = {r.url_path: r for r in discover_routes(tmp_path / "app")}

    users = routes["/admin/users"].load_segments("segment.py")
    assert [m.NAME for m in users] == ["root", "admin"]  # root to leaf
    assert log.read_text().split() == ["root", "admin"]  # sibling not loaded

    # A route sharing those ancestors reuses their modules.
    settings = routes["/admin/settings"].load_segments("segment.py")
    assert all(a is b for a, b in zip(settings, users, strict=True))
    assert log.read_text().split() == ["root", "admin"]

    public = routes["/public"].load_segments("segment.py")
    assert [m.NAME for m in public] == ["root", "public"]
    assert public[0] is users[0]
    assert log.read_text().split() == ["root", "admin", "public"]

    assert routes["/admin/users"].load_segments("middleware.py") == []


# @lat: [[tests#Segment module loading]]
def test_segment_module_loading(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/segment.py": logging_segment(tmp_path / "runs.log", "root"),
            "app/bad.py": "raise RuntimeError('boom')\n",
            "app/one/route.py": "def GET():\n    return {}\n",
            "app/two/route.py": "def GET():\n    return {}\n",
        },
    )
    routes = discover_routes(tmp_path / "app")
    assert len(routes) == 2
    # Sibling routes share their root segment, so its file loads once.
    modules = {load_module(r.segments[0] / "segment.py") for r in routes}
    assert len(modules) == 1
    assert load_module(tmp_path / "app" / "segment.py") in modules
    assert (tmp_path / "runs.log").read_text().split() == ["root"]

    # A route file and its module share the cache.
    assert load_module(routes[0].file) is routes[0].load()

    before = set(sys.modules)
    for _ in range(2):  # retried, not served from a partial cache entry
        with pytest.raises(RuntimeError, match="boom"):
            load_module(routes[0].segments[0] / "bad.py")
        assert set(sys.modules) == before


def middleware_source(name: str) -> str:
    """Source for a ``middleware.py`` that records ``name>`` on the way in and
    ``<name:status`` on the way out in ``mwlog.EVENTS``. ``x-stop: name``
    short-circuits it with a 418; ``x-raise: name`` makes it raise
    ``x-raise-kind`` (``http``, ``validation`` or anything else)."""
    return (
        "import mwlog\n"
        "from chano import HTTPException, JSONResponse, RequestValidationError\n\n"
        "async def middleware(request, call_next):\n"
        f"    mwlog.EVENTS.append({name + '>'!r})\n"
        f"    if request.headers.get('x-stop') == {name!r}:\n"
        f"        return JSONResponse({{'stopped': {name!r}}}, 418)\n"
        f"    if request.headers.get('x-raise') == {name!r}:\n"
        "        kind = request.headers.get('x-raise-kind')\n"
        "        if kind == 'http':\n"
        f"            raise HTTPException(403, {name!r}, {{'x-why': 'mw'}})\n"
        "        if kind == 'validation':\n"
        f"            raise RequestValidationError([{{'msg': {name!r}}}])\n"
        f"        raise RuntimeError({name!r})\n"
        "    response = await call_next(request)\n"
        f"    mwlog.EVENTS.append(f'<{name}:{{response.status_code}}')\n"
        f"    response.headers['x-seen-' + {name!r}] = str(response.status_code)\n"
        "    return response\n"
    )


MIDDLEWARE_ROUTE = (
    "import mwlog\n"
    "from chano import HTTPException\n\n"
    "def GET(n: int = 0, fail: str = ''):\n"
    "    mwlog.EVENTS.append('handler')\n"
    "    if fail == '401':\n"
    "        raise HTTPException(401, 'nope', {'www-authenticate': 'Bearer'})\n"
    "    if fail == 'boom':\n"
    "        raise RuntimeError('boom')\n"
    "    return {'n': n}\n"
)


@pytest.fixture
def middleware_project(tmp_path, isolated_imports):
    """A project with root, ``(group)`` and leaf middleware, a sibling with
    only the root's, and two dynamic routes that overlap on ``/items/x``.

    Yields ``(serve, events)``. ``serve(factory, method, path)`` sends one
    request to ``create_app`` (``factory="dev"``) or to the
    ``create_route_app`` for ``function`` (``factory="route"``)."""
    write_project(
        tmp_path,
        {
            "mwlog.py": "EVENTS = []\n",
            "app/middleware.py": middleware_source("root"),
            "app/(group)/middleware.py": middleware_source("group"),
            "app/(group)/admin/middleware.py": middleware_source("admin"),
            "app/(group)/admin/route.py": MIDDLEWARE_ROUTE,
            "app/public/route.py": MIDDLEWARE_ROUTE,
            "app/items/[id]/middleware.py": middleware_source("id"),
            "app/items/[id]/route.py": MIDDLEWARE_ROUTE,
            "app/items/[...rest]/middleware.py": middleware_source("rest"),
            "app/items/[...rest]/route.py": (
                "def POST(rest: str):\n    return {'rest': rest}\n"
            ),
            "public/hello.txt": "static\n",
        },
    )
    app_dir = tmp_path / "app"
    dev = create_app(app_dir, public_dir=tmp_path / "public")
    events: list[str] = sys.modules["mwlog"].EVENTS  # imported by middleware.py

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


FACTORIES = [
    pytest.param("dev", id="create_app"),
    pytest.param("route", id="create_route_app"),
]


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


# @lat: [[tests#Middleware#Invalid middleware]]
@pytest.mark.parametrize(
    "source",
    [
        "x = 1\n",
        "middleware = 1\n",
        "def middleware(request, call_next):\n    return call_next(request)\n",
    ],
)
def test_invalid_middleware(tmp_path, isolated_imports, source):
    write_project(
        tmp_path,
        {
            "app/(group)/middleware.py": source,
            "app/(group)/a/route.py": "def GET():\n    return {}\n",
        },
    )
    file = (tmp_path / "app" / "(group)" / "middleware.py").resolve()
    for make_app in [
        lambda: create_app(tmp_path / "app"),
        lambda: create_route_app(tmp_path / "app", "a"),
    ]:
        with pytest.raises(TypeError, match=re.escape(str(file))):
            make_app()


# @lat: [[tests#Middleware#Non-response result]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_non_response(tmp_path, isolated_imports, factory):
    write_project(
        tmp_path,
        {
            "app/middleware.py": (
                "async def middleware(request, call_next):\n"
                "    await call_next(request)\n"
                "    return {'not': 'a response'}\n"
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


# @lat: [[tests#Middleware#Conflicting chains for one path]]
def test_middleware_conflicting_chains(tmp_path, isolated_imports):
    middleware = "async def middleware(request, call_next):\n    return None\n"
    write_project(
        tmp_path,
        {
            "app/(a)/middleware.py": middleware,
            "app/(a)/x/route.py": "def GET():\n    return {}\n",
            "app/(b)/x/route.py": "def POST():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="conflicting middleware for route '/x'"):
        create_app(tmp_path / "app")


def error_source(name: str, *, sync: bool) -> str:
    """Source for an ``error.py`` that logs ``name!ExcType`` in ``mwlog.EVENTS``
    and, by default, returns a JSON 503 naming itself and the exception. The
    ``x-<name>`` header switches it to a bare ``raise`` (``reraise``), a new
    ``HTTPException(409, name)`` (``raise``), or returning ``None``."""
    return (
        "import mwlog\n"
        "from chano import HTTPException, JSONResponse\n\n"
        f"{'' if sync else 'async '}def handle_error(request, exc):\n"
        f"    mwlog.EVENTS.append(f'{name}!{{type(exc).__name__}}')\n"
        f"    action = request.headers.get('x-{name}')\n"
        "    if action == 'reraise':\n"
        "        raise\n"
        "    if action == 'raise':\n"
        f"        raise HTTPException(409, {name!r})\n"
        "    if action == 'none':\n"
        "        return None\n"
        f"    return JSONResponse({{'by': {name!r}, 'error': str(exc)}}, 503)\n"
    )


@pytest.fixture
def error_project(tmp_path, isolated_imports):
    """Sync ``error.py`` at the root and in ``admin``, async in ``(group)``,
    plus root and ``(group)`` middleware. ``/admin`` has all three error
    files, ``/other`` the group's and root's, ``/plain`` only the root's.

    Yields ``serve`` like ``middleware_project`` and the shared event log."""
    write_project(
        tmp_path,
        {
            "mwlog.py": "EVENTS = []\n",
            "app/middleware.py": middleware_source("root"),
            "app/error.py": error_source("root", sync=True),
            "app/(group)/middleware.py": middleware_source("group"),
            "app/(group)/error.py": error_source("group", sync=False),
            "app/(group)/admin/error.py": error_source("admin", sync=True),
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


# @lat: [[tests#Error boundaries#Nearest error.py wins]]
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
        "group!HTTPException",
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
            ["admin!RuntimeError", "group!HTTPException"],
            {"by": "group", "error": "409: admin"},
        ),
        (
            {"admin": "raise", "group": "reraise"},
            ["admin!RuntimeError", "group!HTTPException", "root!HTTPException"],
            {"by": "root", "error": "409: admin"},
        ),
        # Past the root the default mapping applies to the last exception.
        (
            {"admin": "reraise", "group": "raise", "root": "reraise"},
            ["admin!RuntimeError", "group!RuntimeError", "root!HTTPException"],
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


# @lat: [[tests#Error boundaries#Middleware errors skip error.py]]
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


# @lat: [[tests#Error boundaries#Awaitable handlers]]
@pytest.mark.filterwarnings("error::RuntimeWarning")
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_awaitable_handlers(tmp_path, isolated_imports, factory):
    write_project(
        tmp_path,
        {
            "app/error.py": (
                "from chano import JSONResponse\n\n"
                "def handle_error(request, exc):\n"
                "    return JSONResponse({'by': 'root', 'error': str(exc)}, 500)\n"
            ),
            "app/obj/error.py": ASYNC_RESULT
            + (
                "class Handler:\n"
                "    async def __call__(self, request, exc):\n"
                "        return await respond(request, exc, 'obj')\n\n"
                "handle_error = Handler()\n"
            ),
            "app/obj/route.py": MIDDLEWARE_ROUTE,
            "app/wrapper/error.py": ASYNC_RESULT
            + (
                "def handle_error(request, exc):\n"
                "    return respond(request, exc, 'wrapper')\n"
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


# @lat: [[tests#Error boundaries#Invalid error.py]]
@pytest.mark.parametrize("source", ["x = 1\n", "handle_error = 1\n"])
def test_invalid_error_handler(tmp_path, isolated_imports, source):
    write_project(
        tmp_path,
        {
            "app/(group)/error.py": source,
            "app/(group)/a/route.py": "def GET():\n    return {}\n",
        },
    )
    file = (tmp_path / "app" / "(group)" / "error.py").resolve()
    for make_app in [
        lambda: create_app(tmp_path / "app"),
        lambda: create_route_app(tmp_path / "app", "a"),
    ]:
        with pytest.raises(TypeError, match=re.escape(str(file))):
            make_app()


# @lat: [[tests#Error boundaries#Conflicting error.py for one path]]
def test_error_conflicting_handlers(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/(a)/error.py": "def handle_error(request, exc):\n    raise\n",
            "app/(a)/x/route.py": "def GET():\n    return {}\n",
            "app/(b)/x/route.py": "def POST():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="conflicting error handlers for route '/x'"):
        create_app(tmp_path / "app")


# @lat: [[tests#Route module isolation]]
def test_route_module_isolation(tmp_path, isolated_imports):
    for name in ["one", "two"]:
        write_project(
            tmp_path / name,
            {"app/route.py": f"def GET():\n    return {{'project': {name!r}}}\n"},
        )
    for name in ["one", "two"]:
        response = make_client(create_app(tmp_path / name / "app"))("GET", "/")
        assert response.json() == {"project": name}

    # "v1+" and "v1+/!" functions: distinct files, distinct modules.
    project = tmp_path / "punct"
    write_project(
        project,
        {
            "pyproject.toml": '[tool.chano]\nbase_path = "/v1+"\n',
            "app/route.py": "def GET():\n    return {'route': 'root'}\n",
            "app/!/route.py": "def GET():\n    return {'route': 'bang'}\n",
        },
    )
    client = make_client(create_app(project / "app"))
    assert client("GET", "/v1+").json() == {"route": "root"}
    assert client("GET", "/v1+/!").json() == {"route": "bang"}


# @lat: [[tests#Symlinked app directory]]
def test_symlinked_app_directory(tmp_path, isolated_imports):
    project = tmp_path / "linked"
    write_project(
        project,
        {
            "pyproject.toml": (
                '[project]\nname = "linked"\nversion = "0"\n\n'
                '[tool.chano]\nbase_path = "/api"\n'
            ),
            "src/app/users/route.py": "def GET():\n    return {'route': 'users'}\n",
        },
    )
    (project / "app").symlink_to(project / "src" / "app")

    app = create_app(project / "app")
    assert app.base_path == "/api"
    assert make_client(app)("GET", "/api/users").json() == {"route": "users"}
    route_app = create_route_app(project / "app", "api/users")
    assert route_app.base_path == "/api"

    plan = plan_build(project)
    assert plan.config.base_path == "/api"
    assert [f.name for f in plan.functions] == ["api/users"]


# @lat: [[tests#Prefixed build without public]]
def test_prefixed_build_without_public(tmp_path, isolated_imports, monkeypatch):
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    files = {
        "pyproject.toml": (
            '[project]\nname = "nopub"\nversion = "0"\n\n'
            '[tool.chano]\nbase_path = "/api"\n'
        ),
        "app/route.py": "def GET():\n    return {}\n",
    }
    project = tmp_path / "nopub"
    write_project(project, files)
    static = emit_build(plan_build(project)) / "static"
    assert {p.relative_to(static).as_posix() for p in static.rglob("*")} == {
        "api",
        "api/docs.html",
        "api/openapi.json",
    }

    late = tmp_path / "latepub"
    write_project(
        late,
        files
        | {
            "app/route.py": (
                "from pathlib import Path\n"
                "pub = Path(__file__).parents[1] / 'public'\n"
                "pub.mkdir(exist_ok=True)\n"
                "(pub / 'late.txt').write_text('created')\n"
            )
        },
    )
    plan = plan_build(late)
    assert plan.public_dir is None
    static = emit_build(plan) / "static"
    assert (static / "api" / "late.txt").read_text() == "created"
    assert (static / "api" / "docs.html").is_file()


# @lat: [[tests#Custom build output directory]]
def test_custom_build_output_directory(tmp_path, isolated_imports, monkeypatch, capsys):
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    project = tmp_path / "custom"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "custom"\nversion = "0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
            "public/hello.txt": "hi\n",
        },
    )

    # plan_build accepts a custom output path without creating it.
    external = tmp_path / "artifacts" / "out"
    plan = plan_build(project, output=external)
    assert plan.output_dir == external.resolve()
    assert not external.exists()

    # build() writes to the custom directory and leaves .vercel/output untouched.
    result = build(project, output=external)
    assert result == external.resolve()
    assert (external / "config.json").is_file()
    assert (external / "static" / "hello.txt").read_text() == "hi\n"
    assert (external / "functions" / "index.func" / "index.py").is_file()
    assert not (project / ".vercel").exists()

    # Output inside project_dir does not copy itself or temporary directories
    # into function payloads, even across repeated builds.
    nested = project / "dist" / "vercel-output"
    for _ in range(2):
        built = build(project, output=nested)
        assert built == nested.resolve()
        func = nested / "functions" / "index.func"
        assert (func / "app" / "route.py").is_file()
        assert not (func / "dist" / "vercel-output").exists()
        assert not list(func.rglob(".chano-*"))

    direct = project / "out"
    built = build(project, output=direct)
    assert built == direct.resolve()
    assert not (direct / "functions" / "index.func" / "out").exists()

    # CLI `--output` builds into the requested directory (relative to cwd).
    monkeypatch.chdir(project)
    cli_main(["build", "--output", "cli-out", "--platform", "native"])
    cli_out = project / "cli-out"
    assert (cli_out / "config.json").is_file()
    assert (cli_out / "functions" / "index.func" / "index.py").is_file()
    assert not (cli_out / "functions" / "index.func" / "cli-out").exists()
    assert "cli-out" in capsys.readouterr().out
    assert not (project / ".vercel").exists()

    # Output directories overlapping project source trees are rejected before deletion.
    for bad in [
        project,
        project.parent,
        project / "app",
        project / "app" / "out",
        project / "public",
        project / "public" / "out",
    ]:
        with pytest.raises(ValueError, match="output directory"):
            plan_build(project, output=bad)
        assert (project / "app" / "route.py").is_file()
        assert (project / "public" / "hello.txt").is_file()


# @lat: [[tests#Locked lockfile]]
def test_stale_lockfile_fails_build(tmp_path, isolated_imports):
    project = tmp_path / "stale"
    pyproject = (
        '[project]\nname = "stale"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = []\n'
    )
    write_project(
        project,
        {
            "pyproject.toml": pyproject,
            "app/route.py": "def GET():\n    return {'ok': True}\n",
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=project, check=True)
    lock_before = (project / "uv.lock").read_text()

    # Drift pyproject.toml from uv.lock without re-locking.
    (project / "pyproject.toml").write_text(
        pyproject.replace("dependencies = []", 'dependencies = ["msgspec"]')
    )
    with pytest.raises(RuntimeError, match="uv.lock"):
        build(project, platform=None, output=tmp_path / "out")
    assert (project / "uv.lock").read_text() == lock_before

    # Missing uv.lock also fails instead of resolving on the fly.
    (project / "uv.lock").unlink()
    with pytest.raises(RuntimeError, match="uv.lock"):
        build(project, platform=None, output=tmp_path / "out2")


# @lat: [[tests#Deterministic build output]]
def test_deterministic_build_output(tmp_path, isolated_imports, monkeypatch):
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    project = tmp_path / "det"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "det"\nversion = "0"\n',
            "app/z/route.py": "def GET():\n    return {'z': 1}\n",
            "app/a/route.py": "def GET():\n    return {'a': 1}\n",
            "app/items/[id]/route.py": (
                "import msgspec\n\n"
                "max_duration = 10\n"
                "memory = 512\n"
                "regions = ['iad1']\n\n"
                "class Item(msgspec.Struct):\n"
                "    name: str\n\n"
                "def PUT(id: int, item: Item) -> Item:\n"
                "    return item\n"
            ),
            "public/hello.txt": "hello\n",
        },
    )

    out1 = build(project, output=tmp_path / "out1")
    out2 = build(project, output=tmp_path / "out2")

    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert snapshot(out1) == snapshot(out2)

    config_text = (out1 / "config.json").read_text()
    assert config_text == json.dumps(json.loads(config_text), indent=2, sort_keys=True)

    openapi_text = (out1 / "static" / "openapi.json").read_text()
    assert openapi_text == json.dumps(json.loads(openapi_text), sort_keys=True)

    for vc_config in sorted((out1 / "functions").rglob(".vc-config.json")):
        text = vc_config.read_text()
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True)


# @lat: [[tests#Workspace discovery]]
def test_workspace_root_discovery(tmp_path, isolated_imports):
    # Non-workspace project falls back to project_dir, even under an ancestor
    # with pyproject.toml or [tool.uv.workspace] lacking uv.lock.
    outer = tmp_path / "outer"
    write_project(
        outer,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["apps/*"]\n',
            "apps/solo/pyproject.toml": '[project]\nname = "solo"\nversion = "0"\n',
            "apps/solo/app/route.py": "def GET():\n    return {}\n",
        },
    )
    solo = (outer / "apps" / "solo").resolve()
    solo_plan = plan_build(solo)
    assert solo_plan.workspace_root == solo
    assert solo_plan.package is None

    # Adding uv.lock at the ancestor makes it the workspace root.
    (outer / "uv.lock").write_text("version = 1\n")
    solo_ws_plan = plan_build(solo)
    assert solo_ws_plan.workspace_root == outer.resolve()
    assert solo_ws_plan.package == "solo"

    # Nearest ancestor with both [tool.uv.workspace] and uv.lock wins.
    inner = outer / "sub"
    write_project(
        inner,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["svc", "unnamed"]\n',
            "svc/pyproject.toml": '[project]\nname = "svc"\nversion = "0"\n',
            "svc/app/route.py": "def GET():\n    return {}\n",
        },
    )
    svc = (inner / "svc").resolve()
    # inner lacks uv.lock, so outer wins.
    svc_outer_plan = plan_build(svc)
    assert svc_outer_plan.workspace_root == outer.resolve()
    assert svc_outer_plan.package == "svc"
    # Once inner also has uv.lock, inner (nearest) wins.
    (inner / "uv.lock").write_text("version = 1\n")
    svc_inner_plan = plan_build(svc)
    assert svc_inner_plan.workspace_root == inner.resolve()
    assert svc_inner_plan.package == "svc"

    # A workspace member without [project].name cannot be exported by package name.
    unnamed = inner / "unnamed"
    write_project(
        unnamed,
        {
            "pyproject.toml": '[tool.chano]\nbase_path = "/api"\n',
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="name"):
        plan_build(unnamed)

    # A standalone project with its own uv.lock under a workspace root stops
    # walking up and resolves to itself.
    standalone = outer / "standalone"
    write_project(
        standalone,
        {
            "pyproject.toml": '[project]\nname = "standalone"\nversion = "0"\n',
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    standalone_plan = plan_build(standalone)
    assert standalone_plan.workspace_root == standalone.resolve()
    assert standalone_plan.package is None

    # An ancestor with uv.lock but without [tool.uv.workspace] is not a
    # workspace root and stops walking up before reaching an outer workspace.
    plain_parent = outer / "plain_parent"
    write_project(
        plain_parent,
        {
            "pyproject.toml": '[project]\nname = "parent"\nversion = "0"\n',
            "uv.lock": "version = 1\n",
            "child/pyproject.toml": '[project]\nname = "child"\nversion = "0"\n',
            "child/app/route.py": "def GET():\n    return {}\n",
        },
    )
    child = (plain_parent / "child").resolve()
    child_plan = plan_build(child)
    assert child_plan.workspace_root == child
    assert child_plan.package is None

    # A workspace root that is itself a Chano project resolves to itself,
    # while a virtual workspace root without [project].name is rejected.
    root_svc = tmp_path / "root_svc"
    write_project(
        root_svc,
        {
            "pyproject.toml": (
                '[project]\nname = "root-svc"\nversion = "0"\n\n'
                "[tool.uv.workspace]\nmembers = []\n"
            ),
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    root_svc_plan = plan_build(root_svc)
    assert root_svc_plan.workspace_root == root_svc.resolve()
    assert root_svc_plan.package == "root-svc"

    virtual_root = tmp_path / "virtual_root"
    write_project(
        virtual_root,
        {
            "pyproject.toml": "[tool.uv.workspace]\nmembers = []\n",
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="name"):
        plan_build(virtual_root)


# @lat: [[tests#Workspace build]]
def test_workspace_build(tmp_path, isolated_imports, monkeypatch):
    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "workspace"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
            "packages/core/pyproject.toml": (
                '[project]\nname = "core"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\ndependencies = []\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
            ),
            "packages/core/src/core/__init__.py": (
                'def greet(who: str) -> str:\n    return f"hello {who} from core"\n'
            ),
            "packages/unused/pyproject.toml": (
                '[project]\nname = "unused"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\ndependencies = []\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
            ),
            "packages/unused/src/unused/__init__.py": "VALUE = 1\n",
            "apps/worker/pyproject.toml": (
                '[project]\nname = "worker"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\ndependencies = ["unused"]\n\n'
                "[tool.uv.sources]\nunused = { workspace = true }\n"
            ),
            "apps/api/pyproject.toml": (
                '[project]\nname = "api"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\n'
                'dependencies = ["chano", "core"]\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n\n'
                "[tool.hatch.build.targets.wheel]\n"
                'packages = ["src/api_pkg"]\n\n'
                "[tool.uv.sources]\n"
                f"chano = {{ path = {json.dumps(repo_root.as_posix())},"
                " editable = true }\n"
                "core = { workspace = true }\n"
            ),
            "apps/api/src/api_pkg/__init__.py": 'MARKER = "api"\n',
            "apps/api/app/route.py": (
                "from core import greet\n\n"
                'def GET(name: str = "world"):\n'
                '    return {"greeting": greet(name)}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    # Mirror the workspace environment during planning so route.py can import core;
    # invoke() runs in a fresh `python -S` subprocess with only _vendor/ on sys.path.
    monkeypatch.syspath_prepend(str(workspace / "packages" / "core" / "src"))

    api = workspace / "apps" / "api"
    plan = plan_build(api, platform=None)
    assert plan.project_dir == api.resolve()
    assert plan.workspace_root == workspace.resolve()
    assert plan.package == "api"

    output = build(api, python=HOST_PYTHON, platform=None)
    assert output == api.resolve() / ".vercel" / "output"

    vendor = output / "functions" / "index.func" / "_vendor"
    assert (vendor / "core" / "__init__.py").is_file()
    assert not (vendor / "unused").exists()
    assert not (vendor / "api_pkg").exists()

    response = invoke(output, "index", "/?name=chano")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"greeting": "hello chano from core"}


# @lat: [[tests#Full-stack example build]]
def test_fullstack_example_build(tmp_path, isolated_imports):
    backend = FULLSTACK / "backend"
    vercel = json.loads((FULLSTACK / "vercel.json").read_text())
    assert vercel["services"]["web"] == {"root": "frontend/", "framework": "nextjs"}
    assert vercel["services"]["api"] == {
        "root": "backend/",
        "framework": "vite",
        "buildCommand": "uv run --frozen chano build",
        "devCommand": "uv run --frozen chano dev --port $PORT",
    }
    assert vercel["rewrites"] == [
        {"source": "/api/:path*", "destination": {"service": "api"}},
        {"source": "/(.*)", "destination": {"service": "web"}},
    ]
    assert (FULLSTACK / ".python-version").read_text().strip() == "3.12"
    assert (backend / ".python-version").read_text().strip() == "3.12"

    out = tmp_path / "output"
    plan = plan_build(backend, platform=None, output=out)
    assert plan.project_dir == backend.resolve()
    assert plan.workspace_root == FULLSTACK.resolve()
    assert plan.package == "fullstack-api"
    assert plan.config.base_path == "/api"

    output = build(backend, python=HOST_PYTHON, platform=None, output=out)
    assert output == out.resolve()

    config = json.loads((output / "config.json").read_text())
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/api/docs$", "dest": "/api/docs.html"},
        {"src": "^/api/items/([^/]+)$", "dest": "/api/items/[item_id]"},
    ]

    docs_html = output / "static" / "api" / "docs.html"
    assert docs_html.is_file()
    assert '"/api/openapi.json"' in docs_html.read_text()
    schema = json.loads((output / "static" / "api" / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/api", "/api/hello", "/api/items/{item_id}"}
    assert (output / "static" / "api" / "hello.txt").read_text().strip() == (
        "A static file served from /api/hello.txt"
    )

    for func in ("api", "api/hello", "api/items/[item_id]"):
        func_dir = output / "functions" / f"{func}.func"
        assert (func_dir / ".vc-config.json").is_file()
        assert not any(
            p.name.startswith("uvicorn") for p in (func_dir / "_vendor").iterdir()
        )

    root_res = invoke(output, "api", "/api")
    assert root_res["statusCode"] == 200
    assert json.loads(root_res["body"]) == {
        "service": "chano",
        "message": "Hello from Chano",
    }

    hello_res = invoke(output, "api/hello", "/api/hello?name=vercel")
    assert hello_res["statusCode"] == 200
    assert json.loads(hello_res["body"]) == {"hello": "vercel"}

    item_res = invoke(output, "api/items/[item_id]", "/api/items/42")
    assert item_res["statusCode"] == 200
    assert json.loads(item_res["body"]) == {"item_id": 42}


# @lat: [[tests#Dev requires uvicorn]]
def test_dev_requires_uvicorn(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["dev"])
    message = str(exc_info.value)
    assert "uvicorn" in message
    assert "chano[dev]" in message


# @lat: [[tests#Store index]]
def test_store_index(tmp_path, output):
    from chano.build import index_store, normalize_dist_name

    assert normalize_dist_name("My_Cool.Pkg--Name") == "my-cool-pkg-name"
    assert normalize_dist_name("msgspec") == "msgspec"

    # Missing store directory returns an empty index.
    empty = index_store(tmp_path / "missing")
    assert empty.distributions == {}
    assert empty.by_import == {}

    # Synthetic store covering RECORD, top_level.txt, Requires-Dist, namespace
    # packages, single-file modules, excluded directories, and escaped paths.
    store = tmp_path / "_vendor"
    write_project(
        store,
        {
            "Alpha_Pkg-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\n"
                "Name: Alpha_Pkg\n"
                "Version: 1.0\n"
                "Requires-Dist: Beta.Lib[extra1, extra2]>=2.0,<3; "
                "python_version >= '3.12'\n"
                "Requires-Dist: Gamma_Tool (==1.5)\n"
                "Requires-Dist: delta-dep @ https://example.com/delta.whl ; "
                "extra == 'dev'\n"
                "Requires-Dist: z\n"
            ),
            "Alpha_Pkg-1.0.dist-info/RECORD": (
                "alpha/__init__.py,sha256=abc,10\n"
                "alpha/sub.py,sha256=def,20\n"
                "alpha_helper.py,sha256=ghi,15\n"
                "ns_shared/alpha_part.py,sha256=jkl,5\n"
                "bin/alpha-cli,sha256=mno,30\n"
                "Alpha_Pkg-1.0.dist-info/METADATA,sha256=pqr,100\n"
                "Alpha_Pkg-1.0.dist-info/RECORD,,\n"
                "Alpha_Pkg-1.0.data/purelib/ignored.py,sha256=stu,10\n"
                "__pycache__/alpha_helper.cpython-312.pyc,sha256=vwx,10\n"
                "../../outside.py,sha256=yz,10\n"
                "/abs/outside.py,sha256=ab,10\n"
                '"alpha/data,file.txt",sha256=cd,4\n'
            ),
            "Alpha_Pkg-1.0.dist-info/top_level.txt": "\nalpha_extra\nns_top/sub\n",
            "alpha/__init__.py": "x = 1\n",
            "alpha/sub.py": "y = 2\n",
            "alpha/data,file.txt": "1234",
            "alpha_helper.py": "z = 3\n",
            "ns_shared/alpha_part.py": "a = 1\n",
            "bin/alpha-cli": "#!/usr/bin/env python\n",
            "Beta.Lib-2.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: Beta.Lib\nVersion: 2.0\n"
            ),
            "Beta.Lib-2.0.dist-info/RECORD": (
                "ns_shared/beta_part.py,sha256=aaa,8\n"
                "beta_ext.cpython-312-linux.so,sha256=bbb,12\n"
                "Beta.Lib-2.0.dist-info/METADATA,sha256=ccc,50\n"
                "Beta.Lib-2.0.dist-info/RECORD,,\n"
            ),
            "ns_shared/beta_part.py": "b = 2\n",
            "beta_ext.cpython-312-linux.so": "0123456789ab",
        },
    )

    index = index_store(store)
    assert set(index.distributions) == {"alpha-pkg", "beta-lib"}

    alpha = index.distributions["alpha-pkg"]
    assert alpha.name == "alpha-pkg"
    assert alpha.dist_info == "Alpha_Pkg-1.0.dist-info"
    assert alpha.requires == frozenset({"beta-lib", "gamma-tool", "delta-dep", "z"})
    assert alpha.top_level == frozenset(
        {"alpha", "alpha_helper", "ns_shared", "alpha_extra", "ns_top"}
    )
    assert "alpha/__init__.py" in alpha.files
    assert "alpha/data,file.txt" in alpha.files
    assert "bin/alpha-cli" in alpha.files
    assert "../../outside.py" not in alpha.files
    assert "/abs/outside.py" not in alpha.files
    expected_alpha_size = sum(
        (store / p).lstat().st_size
        for p in (
            "alpha/__init__.py",
            "alpha/sub.py",
            "alpha/data,file.txt",
            "alpha_helper.py",
            "ns_shared/alpha_part.py",
            "bin/alpha-cli",
            "Alpha_Pkg-1.0.dist-info/METADATA",
            "Alpha_Pkg-1.0.dist-info/RECORD",
            "Alpha_Pkg-1.0.dist-info/top_level.txt",
        )
    )
    assert alpha.size == expected_alpha_size

    beta = index.distributions["beta-lib"]
    assert beta.top_level == frozenset({"ns_shared", "beta_ext"})
    assert beta.requires == frozenset()

    assert index.by_import["alpha"] == ("alpha-pkg",)
    assert index.by_import["alpha_helper"] == ("alpha-pkg",)
    assert index.by_import["alpha_extra"] == ("alpha-pkg",)
    assert index.by_import["ns_shared"] == ("alpha-pkg", "beta-lib")
    assert index.by_import["beta_ext"] == ("beta-lib",)
    assert "bin" not in index.by_import

    # Real vendored store from examples/hello.
    real_index = index_store(output / "functions" / "index.func" / "_vendor")
    assert {"chano", "msgspec", "vercel-runtime"} <= set(real_index.distributions)
    chano_dist = real_index.distributions["chano"]
    assert chano_dist.top_level == frozenset({"chano"})
    assert "msgspec" in chano_dist.requires
    assert "chano/__init__.py" in chano_dist.files
    assert chano_dist.size > 0
    assert real_index.by_import["msgspec"] == ("msgspec",)
    assert real_index.by_import["vercel_runtime"] == ("vercel-runtime",)


# @lat: [[tests#Build size report and limits]]
def test_build_size_report_and_limits(tmp_path, isolated_imports, monkeypatch, capsys):
    assert build_module.SIZE_WARN_BYTES == 250 * 1024 * 1024
    assert build_module.SIZE_LIMIT_BYTES == 500 * 1024 * 1024

    project = tmp_path / "sized"
    write_project(
        project,
        {
            "pyproject.toml": (
                '[project]\nname = "sized"\nversion = "0"\n\n'
                '[tool.chano]\ninclude = ["big-pkg", "med-pkg", "small-pkg"]\n'
            ),
            "app/route.py": "def GET():\n    return {'ok': True}\n",
            "app/ping/route.py": "def GET():\n    return {'ping': True}\n",
        },
    )

    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(
            vendor_dir,
            {
                "big_pkg-1.0.dist-info/METADATA": (
                    "Metadata-Version: 2.1\nName: Big_Pkg\nVersion: 1.0\n"
                ),
                "big_pkg-1.0.dist-info/RECORD": (
                    "big_pkg/__init__.py,,\n"
                    "big_pkg/blob.bin,,\n"
                    "big_pkg-1.0.dist-info/METADATA,,\n"
                    "big_pkg-1.0.dist-info/RECORD,,\n"
                ),
                "big_pkg/__init__.py": "",
                "big_pkg/blob.bin": "x" * 8_192,
                "med_pkg-1.0.dist-info/METADATA": (
                    "Metadata-Version: 2.1\nName: med-pkg\nVersion: 1.0\n"
                ),
                "med_pkg-1.0.dist-info/RECORD": (
                    "med_pkg/__init__.py,,\n"
                    "med_pkg/blob.bin,,\n"
                    "med_pkg-1.0.dist-info/METADATA,,\n"
                    "med_pkg-1.0.dist-info/RECORD,,\n"
                ),
                "med_pkg/__init__.py": "",
                "med_pkg/blob.bin": "y" * 2_048,
                "small_pkg-1.0.dist-info/METADATA": (
                    "Metadata-Version: 2.1\nName: small-pkg\nVersion: 1.0\n"
                ),
                "small_pkg-1.0.dist-info/RECORD": (
                    "small_pkg/__init__.py,,\n"
                    "small_pkg-1.0.dist-info/METADATA,,\n"
                    "small_pkg-1.0.dist-info/RECORD,,\n"
                ),
                "small_pkg/__init__.py": "z = 1\n",
            },
        )

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    # Under thresholds: prints each function's size and distribution count, no warnings.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = build(project, platform=None, output=tmp_path / "out-ok")
    assert out.is_dir()
    assert caught == []
    report_out = capsys.readouterr().out
    assert "index" in report_out
    assert "ping" in report_out
    assert "KB" in report_out
    assert "3 distributions" in report_out

    # Above warn threshold (and below fail limit): emits warning naming largest
    # distributions in descending size order, and build still succeeds.
    monkeypatch.setattr(build_module, "SIZE_WARN_BYTES", 4_096)
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 1024 * 1024)
    with pytest.warns(UserWarning) as record:
        out_warn = build(project, platform=None, output=tmp_path / "out-warn")
    assert out_warn.is_dir()
    warn_messages = [str(item.message) for item in record]
    assert len(warn_messages) == 2
    for func_name in ("index", "ping"):
        msg = next(m for m in warn_messages if f"'{func_name}'" in m)
        assert "big-pkg" in msg
        assert "med-pkg" in msg
        assert msg.index("big-pkg") < msg.index("med-pkg")
    capsys.readouterr()

    # Above fail limit: build fails with RuntimeError naming function and largest dists.
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 4_096)
    with pytest.raises(RuntimeError) as exc_info:
        build(project, platform=None, output=tmp_path / "out-fail")
    error_message = str(exc_info.value)
    assert "index" in error_message
    assert "big-pkg" in error_message
    assert "med-pkg" in error_message
    capsys.readouterr()

    # CLI build prints the per-function size report alongside the build summary.
    monkeypatch.setattr(build_module, "SIZE_WARN_BYTES", 1024 * 1024)
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 2 * 1024 * 1024)
    monkeypatch.chdir(project)
    cli_main(["build", "--platform", "native", "--output", str(tmp_path / "cli-out")])
    cli_out = capsys.readouterr().out
    assert "index" in cli_out
    assert "ping" in cli_out
    assert "3 distributions" in cli_out
    assert "Build output written to" in cli_out


def _write_workspace_pkg(
    workspace: Path,
    dist_name: str,
    module_name: str,
    source: str,
    *,
    deps: list[str] | None = None,
) -> None:
    dep_list = deps or []
    sources_block = ""
    if dep_list:
        entries = "\n".join(f"{dep} = {{ workspace = true }}" for dep in dep_list)
        sources_block = f"\n[tool.uv.sources]\n{entries}\n"
    write_project(
        workspace / "packages" / dist_name,
        {
            "pyproject.toml": (
                f'[project]\nname = "{dist_name}"\nversion = "0.1.0"\n'
                f'requires-python = ">=3.12"\n'
                f"dependencies = {json.dumps(dep_list)}\n\n"
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
                f"{sources_block}"
            ),
            f"src/{module_name}/__init__.py": source,
        },
    )


# @lat: [[tests#Per-function tree-shaking]]
def test_tree_shaking_workspace_build(tmp_path, isolated_imports, monkeypatch, capsys):
    from chano.build import index_store

    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "shaken-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(workspace, "pkg-trans", "pkg_trans", 'VALUE = "from-trans"\n')
    _write_workspace_pkg(
        workspace,
        "pkg-a",
        "pkg_a",
        (
            "import pkg_trans\n\n"
            'def describe() -> str:\n    return f"pkg-a+{pkg_trans.VALUE}"\n'
        ),
        deps=["pkg-trans"],
    )
    _write_workspace_pkg(
        workspace,
        "pkg-b",
        "pkg_b",
        'def describe() -> str:\n    return "pkg-b"\n',
    )
    _write_workspace_pkg(
        workspace,
        "pkg-c",
        "pkg_c",
        'def describe() -> str:\n    return "pkg-c"\n',
    )
    _write_workspace_pkg(
        workspace,
        "pkg-d",
        "pkg_d",
        'def describe() -> str:\n    return "pkg-d"\n',
    )

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["chano", "pkg-a", "pkg-b", "pkg-c", "pkg-d"]\n\n'
        "[tool.uv.sources]\n"
        f"chano = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
        "pkg-b = { workspace = true }\n"
        "pkg-c = { workspace = true }\n"
        "pkg-d = { workspace = true }\n"
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": base_pyproject,
            "helpers/__init__.py": "",
            "helpers/chain.py": (
                "from . import leaf\n"
                "from .cycle import TAG\n\n"
                "def run() -> str:\n"
                '    return f"{leaf.run()}:{TAG}"\n'
            ),
            "helpers/cycle.py": (
                'TAG = "ok"\n\n'
                "def touch():\n"
                "    from . import chain\n"
                "    return chain\n"
            ),
            "helpers/leaf.py": (
                "import pkg_a\n\ndef run() -> str:\n    return pkg_a.describe()\n"
            ),
            "app/alpha/route.py": (
                "import typing\n"
                "from typing import TYPE_CHECKING\n"
                "from helpers.chain import run\n\n"
                "if TYPE_CHECKING:\n"
                "    import pkg_d\n"
                "if typing.TYPE_CHECKING:\n"
                "    import pkg_b\n\n"
                "def GET():\n"
                '    return {"result": run()}\n'
            ),
            "app/beta/route.py": (
                "def GET():\n"
                "    import pkg_b\n"
                '    return {"result": pkg_b.describe()}\n'
            ),
            "app/dynamic/route.py": (
                "import importlib\n"
                "from importlib import import_module\n\n"
                "def GET():\n"
                '    b = importlib.import_module("pkg_b")\n'
                '    c = import_module("pkg_c")\n'
                '    return {"b": b.describe(), "c": c.describe()}\n'
            ),
            "app/dunder/route.py": (
                "def GET():\n"
                '    c = __import__("pkg_c")\n'
                '    return {"c": c.describe()}\n'
            ),
            "app/included/route.py": (
                "import importlib\n\n"
                'include = ["Pkg_D"]\n\n'
                "def GET():\n"
                '    mod = importlib.import_module("".join(["pkg", "_d"]))\n'
                '    return {"d": mod.describe()}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    for dist_name in ("pkg-trans", "pkg-a", "pkg-b", "pkg-c", "pkg-d"):
        monkeypatch.syspath_prepend(str(workspace / "packages" / dist_name / "src"))

    api = workspace / "apps" / "api"
    plan = plan_build(api, platform=None, output=tmp_path / "out-shaken")
    by_name = {spec.name: spec for spec in plan.functions}
    assert by_name["alpha"].trace_roots == (
        (api / "app" / "alpha" / "route.py").resolve(),
    )
    assert by_name["alpha"].include == ()
    assert by_name["included"].include == ("pkg-d",)

    shaken = build(
        api, python=HOST_PYTHON, platform=None, output=tmp_path / "out-shaken"
    )
    report_out = capsys.readouterr().out

    def dists_for(out_dir: Path, func_name: str) -> set[str]:
        return set(
            index_store(
                out_dir / "functions" / f"{func_name}.func" / "_vendor"
            ).distributions
        )

    always = {"chano", "msgspec", "vercel-runtime"}

    alpha_dists = dists_for(shaken, "alpha")
    beta_dists = dists_for(shaken, "beta")
    dynamic_dists = dists_for(shaken, "dynamic")
    dunder_dists = dists_for(shaken, "dunder")
    included_dists = dists_for(shaken, "included")

    assert always | {"pkg-a", "pkg-trans"} <= alpha_dists
    assert not ({"pkg-b", "pkg-c", "pkg-d"} & alpha_dists)

    assert always | {"pkg-b"} <= beta_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-c", "pkg-d"} & beta_dists)

    assert always | {"pkg-b", "pkg-c"} <= dynamic_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-d"} & dynamic_dists)

    assert always | {"pkg-c"} <= dunder_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-b", "pkg-d"} & dunder_dists)

    assert always | {"pkg-d"} <= included_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-b", "pkg-c"} & included_dists)

    # Size report reflects each function's actual bundled distribution count.
    for func_name, expected_dists in (
        ("alpha", alpha_dists),
        ("beta", beta_dists),
        ("dynamic", dynamic_dists),
        ("dunder", dunder_dists),
        ("included", included_dists),
    ):
        line = next(
            row for row in report_out.splitlines() if row.strip().startswith(func_name)
        )
        assert f"({len(expected_dists)} distributions)" in line

    # Shared files across functions are still hard-linked.
    alpha_chano = (
        shaken / "functions" / "alpha.func" / "_vendor" / "chano" / "__init__.py"
    )
    beta_chano = (
        shaken / "functions" / "beta.func" / "_vendor" / "chano" / "__init__.py"
    )
    assert alpha_chano.stat().st_ino == beta_chano.stat().st_ino

    # Every tree-shaken function executes standalone via vc_handler (python -S).
    assert json.loads(invoke(shaken, "alpha", "/alpha")["body"]) == {
        "result": "pkg-a+from-trans:ok"
    }
    assert json.loads(invoke(shaken, "beta", "/beta")["body"]) == {"result": "pkg-b"}
    assert json.loads(invoke(shaken, "dynamic", "/dynamic")["body"]) == {
        "b": "pkg-b",
        "c": "pkg-c",
    }
    assert json.loads(invoke(shaken, "dunder", "/dunder")["body"]) == {"c": "pkg-c"}
    assert json.loads(invoke(shaken, "included", "/included")["body"]) == {"d": "pkg-d"}

    # Project-level `include` adds its normalized distribution to every function.
    (api / "pyproject.toml").write_text(
        base_pyproject + '\n[tool.chano]\ninclude = ["Pkg_D"]\n'
    )
    with_proj_inc = build(
        api, python=HOST_PYTHON, platform=None, output=tmp_path / "out-proj-inc"
    )
    capsys.readouterr()
    beta_with_inc = dists_for(with_proj_inc, "beta")
    assert always | {"pkg-b", "pkg-d"} <= beta_with_inc
    assert not ({"pkg-a", "pkg-trans", "pkg-c"} & beta_with_inc)
    assert json.loads(invoke(with_proj_inc, "beta", "/beta")["body"]) == {
        "result": "pkg-b"
    }

    # `tree_shake = false` restores the full store into every function.
    (api / "pyproject.toml").write_text(
        base_pyproject + "\n[tool.chano]\ntree_shake = false\n"
    )
    full = build(api, python=HOST_PYTHON, platform=None, output=tmp_path / "out-full")
    full_report = capsys.readouterr().out
    all_expected = always | {"pkg-a", "pkg-trans", "pkg-b", "pkg-c", "pkg-d"}
    for func_name in ("alpha", "beta", "dynamic", "dunder", "included"):
        func_dists = dists_for(full, func_name)
        assert all_expected <= func_dists
        line = next(
            row for row in full_report.splitlines() if row.strip().startswith(func_name)
        )
        assert f"({len(func_dists)} distributions)" in line
    assert json.loads(invoke(full, "alpha", "/alpha")["body"]) == {
        "result": "pkg-a+from-trans:ok"
    }
    assert json.loads(invoke(full, "beta", "/beta")["body"]) == {"result": "pkg-b"}


def make_dist(
    name: str,
    module: str,
    *,
    requires: list[str] | None = None,
    extra_files: dict[str, str] | None = None,
    top_level: str | None = None,
) -> dict[str, str]:
    """Files for a fake installed distribution: a package, `METADATA` and
    `RECORD`, ready for `write_project` into a store."""
    dist_info = f"{name.replace('-', '_')}-1.0.dist-info"
    meta_lines = ["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0"]
    for req in requires or ():
        meta_lines.append(f"Requires-Dist: {req}")
    files: dict[str, str] = {
        f"{module}/__init__.py": f"NAME = {name!r}\n",
        **(extra_files or {}),
    }
    record_lines = [f"{path},," for path in sorted(files)]
    record_lines.append(f"{dist_info}/METADATA,,")
    record_lines.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/METADATA"] = "\n".join(meta_lines) + "\n"
    if top_level is not None:
        files[f"{dist_info}/top_level.txt"] = top_level
        record_lines.append(f"{dist_info}/top_level.txt,,")
    files[f"{dist_info}/RECORD"] = "\n".join(record_lines) + "\n"
    return files


# @lat: [[tests#Import tracer and closure]]
def test_trace_and_closure_edge_cases(tmp_path, isolated_imports, monkeypatch, capsys):
    from chano.build import index_store

    store_files: dict[str, str] = {}
    for dist in (
        make_dist("chano", "chano", requires=["msgspec>=0.20"]),
        make_dist("msgspec", "msgspec"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("vercel", "vercel", requires=["vf-dep"]),
        make_dist("vf-dep", "vf_dep"),
        make_dist("cyc-a", "cyc_a", requires=["cyc-b"]),
        make_dist("cyc-b", "cyc_b", requires=["cyc-a"]),
        make_dist("ns-one", "ns_one", top_level="ns_shared\n"),
        make_dist("ns-two", "ns_two", top_level="ns_shared\n"),
        make_dist("else-pkg", "else_pkg"),
        make_dist("not-tc-pkg", "not_tc_pkg"),
        make_dist("tc-only", "tc_only"),
        make_dist("rel-pkg", "rel_pkg"),
        make_dist("init-pkg", "init_pkg"),
        make_dist("alias-pkg", "alias_pkg"),
        make_dist(
            "huge-pkg",
            "huge_pkg",
            extra_files={"huge_pkg/weights.bin": "x" * 16_384},
        ),
    ):
        store_files.update(dist)

    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(vendor_dir, store_files)

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    project = tmp_path / "edge"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "edge"\nversion = "0"\n',
            # Namespace-package project helper directory (no __init__.py).
            "ns_helpers/sub.py": (
                "def use():\n    import rel_pkg\n    return rel_pkg\n"
            ),
            # Package whose __init__.py itself imports init_pkg.
            "pkg_helpers/__init__.py": (
                "def init_use():\n    import init_pkg\n    return init_pkg\n"
            ),
            "pkg_helpers/mod.py": (
                "def use():\n"
                "    import importlib as il\n"
                "    from importlib import import_module as im\n"
                '    il.import_module(name="alias_pkg")\n'
                '    im(".leaf", __package__)\n'
            ),
            "pkg_helpers/leaf.py": ("def use():\n    import cyc_a\n    return cyc_a\n"),
            "pkg_helpers/shared.py": (
                "def use():\n    import ns_shared\n    return ns_shared\n"
            ),
            "pkg_helpers/sub/__init__.py": "",
            "pkg_helpers/sub/deep.py": (
                "from ..shared import use as shared_use\n\n"
                "def run():\n    return shared_use()\n"
            ),
            "app/traced/route.py": (
                "import os\n"
                "from typing import TYPE_CHECKING\n"
                "from ns_helpers import sub\n"
                "from pkg_helpers.mod import use\n"
                "from pkg_helpers.sub.deep import run\n\n"
                "if TYPE_CHECKING:\n"
                "    import tc_only\n"
                "else:\n"
                "    def runtime_else():\n"
                "        import else_pkg\n"
                "        return else_pkg\n\n"
                "if not TYPE_CHECKING:\n"
                "    def runtime_not_tc():\n"
                "        import not_tc_pkg\n"
                "        return not_tc_pkg\n"
                "else:\n"
                "    import tc_only\n\n"
                "if TYPE_CHECKING and True:\n"
                "    import tc_only\n\n"
                "def GET():\n"
                "    if False:\n"
                "        import unknown_not_in_store\n"
                '    return {"ok": True}\n'
            ),
            "app/heavy/route.py": (
                'def GET():\n    import huge_pkg\n    return {"ok": True}\n'
            ),
            "app/tiny/route.py": ("def GET():\n    return {'ok': True}\n"),
        },
    )

    monkeypatch.setattr(build_module, "SIZE_WARN_BYTES", 8_192)
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 1024 * 1024)
    with pytest.warns(UserWarning) as record:
        out = build(project, platform=None, output=tmp_path / "out")
    capsys.readouterr()

    # Only `heavy` exceeds SIZE_WARN_BYTES; `tiny` and `traced` stay below it.
    warn_messages = [str(item.message) for item in record]
    assert len(warn_messages) == 1
    assert "'heavy'" in warn_messages[0]
    assert "huge-pkg" in warn_messages[0]

    always_with_vf = {
        "chano",
        "msgspec",
        "vercel-runtime",
        "vercel",
        "vf-dep",
    }

    traced_dists = set(
        index_store(out / "functions" / "traced.func" / "_vendor").distributions
    )
    assert traced_dists == always_with_vf | {
        "rel-pkg",
        "init-pkg",
        "alias-pkg",
        "cyc-a",
        "cyc-b",
        "ns-one",
        "ns-two",
        "else-pkg",
        "not-tc-pkg",
    }
    assert "tc-only" not in traced_dists
    assert "huge-pkg" not in traced_dists

    tiny_dists = set(
        index_store(out / "functions" / "tiny.func" / "_vendor").distributions
    )
    assert tiny_dists == always_with_vf

    heavy_dists = set(
        index_store(out / "functions" / "heavy.func" / "_vendor").distributions
    )
    assert heavy_dists == always_with_vf | {"huge-pkg"}

    # Invalid route-level `include` raises ValueError during plan_build.
    for i, bad_include in enumerate(
        ('include = "pkg-a"\n', "include = [123]\n", 'include = [""]\n')
    ):
        bad_proj = tmp_path / f"bad-{i}"
        write_project(
            bad_proj,
            {
                "pyproject.toml": '[project]\nname = "bad"\nversion = "0"\n',
                "app/route.py": bad_include + "def GET():\n    return {}\n",
            },
        )
        with pytest.raises(ValueError, match="include"):
            plan_build(bad_proj)


def lazy_middleware(*modules: str) -> str:
    """Source for a pass-through ``middleware.py`` importing ``modules`` lazily,
    so planning never needs them installed."""
    imports = "".join(f"    import {module}\n" for module in modules)
    return (
        "async def middleware(request, call_next):\n"
        f"{imports}"
        "    return await call_next(request)\n"
    )


# @lat: [[tests#Segment files as trace roots]]
def test_segment_files_are_trace_roots(tmp_path, isolated_imports, monkeypatch, capsys):
    store_files: dict[str, str] = {}
    for dist in (
        make_dist("chano", "chano"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("root-mw-pkg", "root_mw_pkg"),
        make_dist("admin-mw-pkg", "admin_mw_pkg"),
        make_dist("admin-err-pkg", "admin_err_pkg"),
        make_dist("audit-pkg", "audit_pkg"),
        make_dist("public-mw-pkg", "public_mw_pkg"),
    ):
        store_files.update(dist)

    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(vendor_dir, store_files)

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    route = "def GET():\n    return {}\n"
    project = tmp_path / "segments"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "segments"\nversion = "0"\n',
            "helpers/__init__.py": "",
            "helpers/audit.py": "import audit_pkg\n",
            "app/middleware.py": lazy_middleware("root_mw_pkg"),
            "app/route.py": route,
            "app/admin/middleware.py": lazy_middleware("admin_mw_pkg", "helpers.audit"),
            "app/admin/error.py": (
                "def handle_error(request, exc):\n"
                "    import admin_err_pkg\n"
                "    raise exc\n"
            ),
            "app/admin/route.py": route,
            "app/admin/(team)/members/route.py": route,
            "app/admin/settings/[id]/route.py": route,
            "app/public/middleware.py": lazy_middleware("public_mw_pkg"),
            "app/public/route.py": route,
            "app/(site)/about/route.py": route,
        },
    )
    app = (project / "app").resolve()

    plan = plan_build(project, platform=None)
    by_name = {spec.name: spec for spec in plan.functions}
    admin_segments = (
        app / "middleware.py",
        app / "admin" / "middleware.py",
        app / "admin" / "error.py",
    )
    for name in ("admin", "admin/members", "admin/settings/[id]"):
        spec = by_name[name]
        assert spec.route is not None
        assert spec.trace_roots == (spec.route.file, *admin_segments)
    assert by_name["public"].trace_roots == (
        app / "public" / "route.py",
        app / "middleware.py",
        app / "public" / "middleware.py",
    )
    for name in ("index", "about"):
        spec = by_name[name]
        assert spec.route is not None
        assert spec.trace_roots == (spec.route.file, app / "middleware.py")

    out = build(
        project, platform=None, output=tmp_path / "out", explain="admin/members"
    )
    explain_out = capsys.readouterr().out

    def bundled(function: str) -> set[str]:
        vendor = out / "functions" / f"{function}.func" / "_vendor"
        return set(build_module.index_store(vendor).distributions)

    base = {"chano", "vercel-runtime", "root-mw-pkg"}
    admin_only = {"admin-mw-pkg", "admin-err-pkg", "audit-pkg"}
    for name in ("admin", "admin/members", "admin/settings/[id]"):
        assert bundled(name) == base | admin_only
    assert bundled("public") == base | {"public-mw-pkg"}
    for name in ("index", "about"):
        assert bundled(name) == base

    reasons = {
        line.split()[0]: line for line in explain_out.splitlines() if "traced" in line
    }
    assert reasons.keys() == {"root-mw-pkg"} | admin_only
    assert reasons["root-mw-pkg"].endswith("traced from app/middleware.py")
    assert reasons["admin-mw-pkg"].endswith("traced from app/admin/middleware.py")
    assert reasons["admin-err-pkg"].endswith("traced from app/admin/error.py")
    assert reasons["audit-pkg"].endswith(
        "traced from app/admin/middleware.py -> helpers/audit.py"
    )
    excluded = explain_out.split("Excluded (", 1)[1].split()
    assert "public-mw-pkg" in excluded


# @lat: [[tests#Build-time import check]]
def test_build_import_check_catches_computed_import(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    repo_root = Path(__file__).parent.parent.resolve()
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    workspace = tmp_path / "check-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(
        workspace, "pkg-a", "pkg_a", 'def value() -> str:\n    return "from-pkg-a"\n'
    )

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["chano", "pkg-a"]\n\n'
        "[tool.uv.sources]\n"
        f"chano = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
    )
    computed_route = (
        "import importlib\n\n"
        'MOD = importlib.import_module("".join(["pkg", "_a"]))\n\n'
        "def GET():\n"
        '    return {"value": MOD.value()}\n'
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": base_pyproject,
            "app/ok/route.py": 'def GET():\n    return {"ok": True}\n',
            "app/computed/route.py": computed_route,
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)
    monkeypatch.syspath_prepend(str(workspace / "packages" / "pkg-a" / "src"))
    monkeypatch.setenv("PYTHONPATH", str(workspace / "packages" / "pkg-a" / "src"))

    api = workspace / "apps" / "api"

    # Without `include`, the computed import is missed by static tracing and
    # caught by the build-time import check.
    with pytest.raises(RuntimeError) as exc_info:
        build(api, python=host_python, platform=None, output=tmp_path / "out-fail")
    message = str(exc_info.value)
    assert "computed" in message
    assert "pkg_a" in message
    assert "include" in message
    capsys.readouterr()

    # Adding route-level `include` bundles pkg-a and lets the check pass.
    (api / "app" / "computed" / "route.py").write_text(
        'include = ["pkg-a"]\n' + computed_route
    )
    for mod_name in [k for k in sys.modules if k.startswith("chano_routes.")]:
        sys.modules.pop(mod_name, None)
    out_route = build(
        api, python=host_python, platform=None, output=tmp_path / "out-route-inc"
    )
    capsys.readouterr()
    assert not list((out_route / "functions").rglob("__pycache__"))
    assert json.loads(invoke(out_route, "computed", "/computed")["body"]) == {
        "value": "from-pkg-a"
    }

    # Project-level `[tool.chano] include` also satisfies the check.
    (api / "app" / "computed" / "route.py").write_text(computed_route)
    for mod_name in [k for k in sys.modules if k.startswith("chano_routes.")]:
        sys.modules.pop(mod_name, None)
    (api / "pyproject.toml").write_text(
        base_pyproject + '\n[tool.chano]\ninclude = ["pkg-a"]\n'
    )
    out_proj = build(
        api, python=host_python, platform=None, output=tmp_path / "out-proj-inc"
    )
    capsys.readouterr()
    assert json.loads(invoke(out_proj, "computed", "/computed")["body"]) == {
        "value": "from-pkg-a"
    }


# @lat: [[tests#Import check platform matching]]
def test_build_import_check_skips_non_matching_platform(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    project = tmp_path / "plat"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "plat"\nversion = "0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
        },
    )
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)

    uv_calls: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["uv", "python", "find"]:
            uv_calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(build_module.subprocess, "run", spy_run)

    # On macOS arm64, DEFAULT_PLATFORM (x86_64-manylinux_2_28) skips the check.
    monkeypatch.setattr(build_module, "_host_os_arch", lambda: ("darwin", "aarch64"))
    out = build(project, output=tmp_path / "out-darwin")
    assert out.is_dir()
    skipped_out = capsys.readouterr().out
    assert "skip" in skipped_out.lower()
    assert build_module.DEFAULT_PLATFORM in skipped_out
    assert uv_calls == []

    # On Linux x86_64, a non-matching platform (e.g. aarch64-apple-darwin) also skips.
    monkeypatch.setattr(build_module, "_host_os_arch", lambda: ("linux", "x86_64"))
    out_linux_skip = build(
        project, platform="aarch64-apple-darwin", output=tmp_path / "out-linux-skip"
    )
    assert out_linux_skip.is_dir()
    linux_skip_out = capsys.readouterr().out
    assert "skip" in linux_skip_out.lower()
    assert "aarch64-apple-darwin" in linux_skip_out
    assert uv_calls == []

    # On Linux x86_64, DEFAULT_PLATFORM matches the host so the check runs
    # (and fails here because `_install_dependencies` was stubbed without chano).
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    with pytest.raises(RuntimeError) as exc_info:
        build(
            project,
            python=host_python,
            platform=build_module.DEFAULT_PLATFORM,
            output=tmp_path / "out-linux-match",
        )
    assert "index" in str(exc_info.value)
    assert "chano" in str(exc_info.value)
    assert uv_calls == [["uv", "python", "find", host_python]]


# @lat: [[tests#Import check target interpreter]]
def test_build_import_check_uses_uv_python_find(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    repo_root = Path(__file__).parent.parent.resolve()
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    workspace = tmp_path / "interp-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["apps/*"]\n',
        },
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": (
                '[project]\nname = "api"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\n'
                'dependencies = ["chano"]\n\n'
                "[tool.uv.sources]\n"
                f"chano = {{ path = {json.dumps(repo_root.as_posix())}, "
                "editable = true }\n"
            ),
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)
    api = workspace / "apps" / "api"

    # Build a wrapper script at a distinct path so we can prove the import check
    # invokes the interpreter returned by `uv python find`, not `sys.executable`.
    marker = tmp_path / "invocations.log"
    wrapper = tmp_path / "resolved-python"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s %s\\n" "$1" "$2" "$3" >> {json.dumps(str(marker))}\n'
        f'exec {json.dumps(sys.executable)} "$@"\n'
    )
    wrapper.chmod(0o755)

    real_run = subprocess.run
    find_calls: list[list[str]] = []
    find_returncode = 0
    find_stdout = f"{wrapper}\n"

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["uv", "python", "find"]:
            find_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, find_returncode, find_stdout, "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)

    out = build(api, python=host_python, platform=None, output=tmp_path / "out-ok")
    assert out.is_dir()
    capsys.readouterr()
    assert find_calls == [["uv", "python", "find", host_python]]
    invoked = marker.read_text().splitlines()
    assert len(invoked) == 1
    assert "-I" in invoked[0].split()

    # Passing a different `--python` forwards that version to `uv python find`.
    find_calls.clear()
    marker.unlink()
    real_install = build_module._install_dependencies

    def install_for_host(ws_root, vendor_dir, _py, plat, pkg=None):
        real_install(ws_root, vendor_dir, host_python, plat, pkg)

    monkeypatch.setattr(build_module, "_install_dependencies", install_for_host)
    out_other = build(api, python="3.13", platform=None, output=tmp_path / "out-313")
    assert out_other.is_dir()
    capsys.readouterr()
    assert find_calls == [["uv", "python", "find", "3.13"]]
    assert marker.is_file()

    # When `uv python find` finds no interpreter, skip with a message instead of
    # falling back to `sys.executable`, even when `--python` matches host_python.
    find_calls.clear()
    marker.unlink()
    find_returncode = 2
    find_stdout = ""
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    out_missing = build(
        api, python=host_python, platform=None, output=tmp_path / "out-no-interp"
    )
    assert out_missing.is_dir()
    skip_out = capsys.readouterr().out
    assert "skip" in skip_out.lower()
    assert host_python in skip_out
    assert find_calls == [["uv", "python", "find", host_python]]
    assert not marker.exists()


# @lat: [[tests#Build explain]]
def test_build_explain(tmp_path, isolated_imports, monkeypatch, capsys):
    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "explain-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(workspace, "pkg-leaf", "pkg_leaf", 'VAL = "leaf"\n')
    _write_workspace_pkg(
        workspace,
        "pkg-mid",
        "pkg_mid",
        'import pkg_leaf\nVAL = f"mid+{pkg_leaf.VAL}"\n',
        deps=["pkg-leaf"],
    )
    _write_workspace_pkg(
        workspace,
        "pkg-a",
        "pkg_a",
        'import pkg_mid\ndef run() -> str:\n    return f"a+{pkg_mid.VAL}"\n',
        deps=["pkg-mid"],
    )
    _write_workspace_pkg(workspace, "pkg-direct", "pkg_direct", 'NAME = "direct"\n')
    _write_workspace_pkg(
        workspace, "pkg-route-inc", "pkg_route_inc", 'NAME = "route-inc"\n'
    )
    _write_workspace_pkg(
        workspace, "pkg-proj-inc", "pkg_proj_inc", 'NAME = "proj-inc"\n'
    )
    _write_workspace_pkg(workspace, "pkg-only-beta", "pkg_only_beta", 'NAME = "beta"\n')

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = [\n"
        '    "chano",\n'
        '    "pkg-a",\n'
        '    "pkg-direct",\n'
        '    "pkg-route-inc",\n'
        '    "pkg-proj-inc",\n'
        '    "pkg-only-beta",\n'
        "]\n\n"
        "[tool.uv.sources]\n"
        f"chano = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
        "pkg-direct = { workspace = true }\n"
        "pkg-route-inc = { workspace = true }\n"
        "pkg-proj-inc = { workspace = true }\n"
        "pkg-only-beta = { workspace = true }\n"
    )
    api = workspace / "apps" / "api"
    write_project(
        api,
        {
            "pyproject.toml": (
                base_pyproject + '\n[tool.chano]\ninclude = ["pkg_proj_inc"]\n'
            ),
            "helpers/__init__.py": "",
            "helpers/a.py": (
                "import pkg_a\n\ndef run() -> str:\n    return pkg_a.run()\n"
            ),
            "app/alpha/route.py": (
                "import pkg_direct\n"
                "from helpers.a import run\n\n"
                'include = ["Pkg_Route_Inc"]\n\n'
                "def GET():\n"
                '    return {"direct": pkg_direct.NAME, "a": run()}\n'
            ),
            "app/beta/route.py": (
                "import pkg_only_beta\n\n"
                "def GET():\n"
                '    return {"beta": pkg_only_beta.NAME}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    for dist_name in (
        "pkg-leaf",
        "pkg-mid",
        "pkg-a",
        "pkg-direct",
        "pkg-route-inc",
        "pkg-proj-inc",
        "pkg-only-beta",
    ):
        monkeypatch.syspath_prepend(str(workspace / "packages" / dist_name / "src"))

    def parse_explain_sections(output: str) -> tuple[dict[str, str], list[str]]:
        included: dict[str, str] = {}
        excluded: list[str] = []
        section: str | None = None
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Included ("):
                section = "included"
                continue
            if stripped.startswith("Excluded ("):
                section = "excluded"
                continue
            if section == "included":
                if not line.startswith("    "):
                    section = None
                    continue
                if stripped == "(none)":
                    continue
                name, rest = stripped.split(None, 1)
                included[name] = rest
            elif section == "excluded":
                if not line.startswith("    "):
                    section = None
                    continue
                if stripped == "(none)":
                    continue
                excluded.append(stripped.split()[0])
        return included, excluded

    monkeypatch.chdir(api)
    out_alpha = tmp_path / "out-alpha"
    cli_main(
        [
            "build",
            "--platform",
            "native",
            "--output",
            str(out_alpha),
            "--explain",
            "alpha",
        ]
    )
    alpha_stdout = capsys.readouterr().out
    assert "Explain alpha:" in alpha_stdout
    inc, exc = parse_explain_sections(alpha_stdout)

    assert "always included" in inc["chano"]
    assert "always included" in inc["vercel-runtime"]
    assert "required by chano <- always included" in inc["msgspec"]
    assert "traced from app/alpha/route.py" in inc["pkg-direct"]
    assert "traced from app/alpha/route.py -> helpers/a.py" in inc["pkg-a"]
    assert (
        "required by pkg-a <- traced from app/alpha/route.py -> helpers/a.py"
        in inc["pkg-mid"]
    )
    assert (
        "required by pkg-mid <- pkg-a <- traced from app/alpha/route.py -> helpers/a.py"
        in inc["pkg-leaf"]
    )
    assert "listed in route include (app/alpha/route.py)" in inc["pkg-route-inc"]
    assert "listed in [tool.chano] include" in inc["pkg-proj-inc"]

    assert "pkg-only-beta" in exc
    assert "pkg-only-beta" not in inc
    assert exc == sorted(exc)
    alpha_vendor = out_alpha / "functions" / "alpha.func" / "_vendor"
    assert not (alpha_vendor / "pkg_only_beta").exists()
    assert "pkg-only-beta" not in build_module.index_store(alpha_vendor).distributions

    # Trailing `.func` suffix is normalized to match the function name.
    out_beta = tmp_path / "out-beta"
    build(api, platform=None, output=out_beta, explain="beta.func")
    beta_stdout = capsys.readouterr().out
    assert "Explain beta:" in beta_stdout
    beta_inc, beta_exc = parse_explain_sections(beta_stdout)
    assert "traced from app/beta/route.py" in beta_inc["pkg-only-beta"]
    assert "listed in [tool.chano] include" in beta_inc["pkg-proj-inc"]
    for excluded_name in (
        "pkg-a",
        "pkg-mid",
        "pkg-leaf",
        "pkg-direct",
        "pkg-route-inc",
    ):
        assert excluded_name in beta_exc
        assert excluded_name not in beta_inc

    # `tree_shake = false` explains every store distribution with none excluded.
    (api / "pyproject.toml").write_text(
        base_pyproject + "\n[tool.chano]\ntree_shake = false\n"
    )
    out_full = tmp_path / "out-full"
    build(api, platform=None, output=out_full, explain="alpha")
    full_stdout = capsys.readouterr().out
    assert "Explain alpha:" in full_stdout
    full_inc, full_exc = parse_explain_sections(full_stdout)
    assert full_exc == []
    assert "(none)" in full_stdout
    for dist_name in (
        "chano",
        "pkg-a",
        "pkg-mid",
        "pkg-leaf",
        "pkg-only-beta",
    ):
        assert "tree_shake = false" in full_inc[dist_name]

    # Unknown `--explain` function raises ValueError naming it and available functions,
    # before touching an existing output directory.
    sentinel = out_alpha / "keep.txt"
    sentinel.write_text("untouched")
    with pytest.raises(ValueError) as exc_info:
        cli_main(["build", "--output", str(out_alpha), "--explain", "missing"])
    msg = str(exc_info.value)
    assert "missing" in msg
    assert "alpha" in msg
    assert "beta" in msg
    assert sentinel.read_text() == "untouched"


def stream_scope(method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": "/s",
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }


def call_asgi(app, receive, method="GET", on_send=None) -> list[dict]:
    """Run one ``/s`` request through ``app``; return the sent messages.

    ``on_send(sent)`` runs after each message, to react to the stream.
    """
    scope = stream_scope(method)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)
        if on_send:
            on_send(sent)

    async def run():
        await asyncio.wait_for(app(scope, receive, send), timeout=5)

    asyncio.run(run())
    return sent


def stream_app(response_factory) -> Chano:
    app = Chano(docs=False)

    def handler():
        return response_factory()

    app.add_route("/s", handler, ["GET"])
    return app


async def wait_for_disconnect():
    """A ``receive`` that never disconnects until the response is done."""
    await asyncio.Event().wait()


def body_messages(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m["type"] == "http.response.body"]


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


def cookie_request(*cookie_headers: str) -> Request:
    scope = stream_scope()
    scope["headers"] = [
        (b"cookie", value.encode("latin-1")) for value in cookie_headers
    ]
    return Request(scope, wait_for_disconnect)


# @lat: [[tests#Request cookies]]
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ((), {}),
        (("",), {}),
        (("a=1",), {"a": "1"}),
        (("a=1; b=2",), {"a": "1", "b": "2"}),
        (("  a = 1 ;b=2;  ;",), {"a": "1", "b": "2"}),
        (("token=abc==; next=/x?y=1",), {"token": "abc==", "next": "/x?y=1"}),
        (('q="hello world"; e=""',), {"q": "hello world", "e": ""}),
        (('s="a\\"b\\\\c\\073d"',), {"s": 'a"b\\c;d'}),
        (("flag; a=1",), {"a": "1"}),
        (("a=1", "b=2; c=3"), {"a": "1", "b": "2", "c": "3"}),
        (("a=first; a=second",), {"a": "first"}),
    ],
)
def test_request_cookies(headers, expected):
    assert cookie_request(*headers).cookies == expected


# @lat: [[tests#Cookie parameters]]
def test_cookie_parameters():
    assert Cookie is params_module.Cookie
    app = Chano()

    def current_user(session: Annotated[str, Cookie()]) -> str:
        return f"user:{session}"

    def handler(
        user: Annotated[str, Depends(current_user)],
        count: Annotated[int, Cookie()] = 0,
        theme: Annotated[str | None, Cookie(alias="ui-theme")] = None,
    ):
        return {"user": user, "count": count, "theme": theme}

    app.add_route("/c", handler, ["GET"])
    request = make_client(app)

    response = request("GET", "/c", headers={"cookie": "session=s1"})
    assert response.json() == {"user": "user:s1", "count": 0, "theme": None}

    cookie = "session=s1; count=7; ui-theme=dark; theme=ignored"
    response = request("GET", "/c", headers={"cookie": cookie})
    assert response.json() == {"user": "user:s1", "count": 7, "theme": "dark"}

    response = request("GET", "/c", headers={"cookie": "count=many"})
    assert response.status_code == 422
    locs = [error["loc"] for error in response.json()["detail"]]
    assert locs == [["cookie", "session"], ["cookie", "count"]]

    parameters = app.openapi()["paths"]["/c"]["get"]["parameters"]
    assert [(p["name"], p["in"], p["required"]) for p in parameters] == [
        ("session", "cookie", True),
        ("count", "cookie", False),
        ("ui-theme", "cookie", False),
    ]


def sent_headers(response_factory) -> list[tuple[bytes, bytes]]:
    """The raw header list an app returning ``response_factory()`` sends."""
    sent = call_asgi(stream_app(response_factory), wait_for_disconnect)
    return sent[0]["headers"]


def set_cookie_headers(response_factory) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in sent_headers(response_factory)
        if key == b"set-cookie"
    ]


# @lat: [[tests#Set-cookie headers]]
def test_set_cookie_and_delete_cookie():
    def factory():
        response = Response("ok", headers={"X-Extra": "1"})
        response.set_cookie("session", "abc")
        response.set_cookie(
            "prefs",
            "a b",
            max_age=3600,
            expires=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
            path="/app",
            domain="example.com",
            secure=False,
            httponly=False,
            samesite="strict",
        )
        response.set_cookie("third", "x", samesite=None)
        response.delete_cookie("old", path="/app", domain="example.com")
        return response

    headers = sent_headers(factory)
    # Every set-cookie is its own tuple, after the single-valued headers.
    assert [key for key, _ in headers] == [
        b"x-extra",
        b"content-length",
        *[b"set-cookie"] * 4,
    ]
    assert headers[:2] == [(b"x-extra", b"1"), (b"content-length", b"2")]
    session, prefs, third, old = (value.decode() for _, value in headers[2:])
    assert session == "session=abc; HttpOnly; Path=/; SameSite=Lax; Secure"
    assert prefs.startswith('prefs="a b"; ')
    assert set(prefs.split("; ")[1:]) == {
        "Domain=example.com",
        "expires=Wed, 02 Jan 2030 03:04:05 GMT",
        "Max-Age=3600",
        "Path=/app",
        "SameSite=Strict",
    }
    assert third == "third=x; HttpOnly; Path=/; Secure"
    assert old.startswith('old=""; ')
    assert set(old.split("; ")[1:]) == {
        "Domain=example.com",
        "expires=Thu, 01 Jan 1970 00:00:00 GMT",
        "HttpOnly",
        "Max-Age=0",
        "Path=/app",
        "SameSite=Lax",
        "Secure",
    }

    response = Response()
    with pytest.raises(ValueError, match="samesite"):
        response.set_cookie("bad", samesite="sometimes")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError):
        response.set_cookie("bad key", "x")
    # delete_cookie has exactly the spec's signature.
    with pytest.raises(TypeError):
        response.delete_cookie("old", secure=False)  # ty: ignore[unknown-argument]


# @lat: [[tests#Set-cookie on every response type]]
@pytest.mark.parametrize(
    "make_response",
    [
        lambda: JSONResponse({}),
        lambda: StreamingResponse(iter([b"x"])),
        lambda: SSEResponse(iter(["x"])),
    ],
)
def test_set_cookie_on_every_response_type(make_response):
    def factory():
        response = make_response()
        response.set_cookie("a", "1")
        response.set_cookie("b", "2")
        return response

    a, b = set_cookie_headers(factory)
    assert a.startswith("a=1; ") and b.startswith("b=2; ")


# @lat: [[tests#Set-cookie encoding]]
@pytest.mark.parametrize(
    "kwargs",
    [
        {"value": "東京"},
        {"path": "/東京"},
        {"domain": "例え.jp"},
        {"expires": "Wed, 東京"},
        {"path": "/a; Domain=evil.com"},
        {"path": "/a\r\nX-Injected: 1"},
        {"domain": "example.com\n"},
    ],
)
def test_set_cookie_rejects_unencodable_values(kwargs):
    params: dict[str, Any] = {"value": "x", **kwargs}
    with pytest.raises(ValueError, match="cookie"):
        Response().set_cookie("city", **params)

    def handler():
        response = Response("ok")
        response.set_cookie("city", **params)
        return response

    # The error surfaces in the handler, so the client gets a whole 500
    # instead of a crash while sending headers.
    sent = call_asgi(stream_app(handler), wait_for_disconnect)
    assert sent[0]["status"] == 500
    assert body_messages(sent)[-1]["body"] == b"Internal Server Error"


# @lat: [[tests#Cookie round trip]]
@pytest.mark.parametrize(
    "value",
    ["plain", "", "a b", "x=y==", 'quo"te', "back\\slash", "semi;colon", "comma,é"],
)
def test_cookie_round_trip(value):
    app = Chano(docs=False)

    def login():
        response = Response("set")
        response.set_cookie("a", value)
        response.set_cookie("b", "second")
        return response

    def echo(
        request: Request, a: Annotated[str, Cookie()], b: Annotated[str, Cookie()]
    ):
        return {"a": a, "b": b, "cookies": request.cookies}

    app.add_route("/login", login, ["GET"])
    app.add_route("/echo", echo, ["GET"])
    request = make_client(app)

    set_cookies = request("GET", "/login").headers.get_list("set-cookie")
    assert len(set_cookies) == 2
    # Send back what a browser would: the name=value pair of each cookie.
    cookie = "; ".join(header.split("; ")[0] for header in set_cookies)
    response = request("GET", "/echo", headers={"cookie": cookie.encode("latin-1")})
    expected = {"a": value, "b": "second"}
    assert response.json() == {**expected, "cookies": expected}


def platform_request(headers: dict[str, str], client=("10.0.0.9", 5000)) -> Request:
    scope = stream_scope()
    scope["headers"] = [(k.encode(), v.encode()) for k, v in headers.items()]
    if client is not None:
        scope["client"] = client
    return Request(scope, wait_for_disconnect)


VERCEL_HEADERS = {
    "x-vercel-id": "iad1::abc12-1700000000000-deadbeef",
    "x-real-ip": "203.0.113.7",
    "x-forwarded-for": "198.51.100.1, 10.0.0.1",
    "x-vercel-ip-country": "US",
    "x-vercel-ip-country-region": "CA",
    "x-vercel-ip-city": "San%20Francisco",
    "x-vercel-ip-latitude": "37.7749",
    "x-vercel-ip-longitude": "-122.4194",
    "x-vercel-ip-postal-code": "94103",
}


# @lat: [[tests#Platform metadata from Vercel headers]]
def test_platform_metadata_from_vercel_headers():
    assert Geo is requests_module.Geo
    request = platform_request(VERCEL_HEADERS)
    assert request.id == "iad1::abc12-1700000000000-deadbeef"
    assert request.ip == "203.0.113.7"
    assert request.geo == Geo(
        country="US",
        region="CA",
        city="San Francisco",
        latitude="37.7749",
        longitude="-122.4194",
        postal_code="94103",
    )
    # Non-ASCII city names arrive percent-encoded (RFC 3986).
    assert platform_request({"x-vercel-ip-city": "S%C3%A3o%20Paulo"}).geo == Geo(
        city="São Paulo"
    )


# @lat: [[tests#Platform client IP sources]]
@pytest.mark.parametrize(
    ("headers", "client", "expected"),
    [
        (
            {"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.1.1.1"},
            None,
            "203.0.113.7",
        ),
        (
            {"x-forwarded-for": " 198.51.100.1 , 10.0.0.1, 10.0.0.2"},
            None,
            "198.51.100.1",
        ),
        ({"x-forwarded-for": "198.51.100.1"}, ("10.0.0.9", 1), "198.51.100.1"),
        ({}, ("10.0.0.9", 5000), "10.0.0.9"),
        ({}, None, None),
        ({}, (), None),
    ],
)
def test_platform_client_ip_sources(headers, client, expected):
    assert platform_request(headers, client=client).ip == expected


# @lat: [[tests#Platform metadata dev fallbacks]]
def test_platform_metadata_dev_fallbacks():
    request = platform_request({})
    request_id = request.id
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert uuid.UUID(hex=request_id).version == 4
    assert request.id == request_id
    assert platform_request({}).id != request_id
    assert request.ip == "10.0.0.9"
    assert request.geo == Geo()
    assert all(value is None for value in msgspec.structs.astuple(request.geo))


# @lat: [[tests#Platform metadata through create_app]]
def test_platform_metadata_through_create_app(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/route.py": (
                "import msgspec\n"
                "from chano import Request\n\n"
                "def GET(request: Request):\n"
                "    return {'id': request.id, 'ip': request.ip,\n"
                "            'geo': msgspec.structs.asdict(request.geo)}\n"
            )
        },
    )
    request = make_client(create_app(tmp_path / "app"))
    body = request("GET", "/", headers=VERCEL_HEADERS).json()
    assert body == {
        "id": "iad1::abc12-1700000000000-deadbeef",
        "ip": "203.0.113.7",
        "geo": {
            "country": "US",
            "region": "CA",
            "city": "San Francisco",
            "latitude": "37.7749",
            "longitude": "-122.4194",
            "postal_code": "94103",
        },
    }
    first, second = request("GET", "/").json(), request("GET", "/").json()
    assert re.fullmatch(r"[0-9a-f]{32}", first["id"])
    assert first["id"] != second["id"]
    assert first["ip"] == "127.0.0.1"
    assert first["geo"] == dict.fromkeys(body["geo"])


# @lat: [[tests#Platform client IP from repeated forwarded headers]]
def test_platform_ip_from_repeated_forwarded_headers():
    request = platform_request({}, client=None)
    request.scope["headers"] = [
        (b"X-Forwarded-For", b" 198.51.100.1, 10.0.0.2"),
        (b"x-forwarded-for", b"10.0.0.1"),
    ]
    assert request.ip == "198.51.100.1"
    # Request.headers keeps its last-value-wins behavior.
    assert request.headers["x-forwarded-for"] == "10.0.0.1"


# @lat: [[tests#Platform request id under concurrent reads]]
def test_platform_request_id_under_concurrent_reads():
    for _ in range(50):
        request = platform_request({})
        barrier = threading.Barrier(8)

        def read(request=request, barrier=barrier):
            barrier.wait()
            return request.id

        with ThreadPoolExecutor(8) as pool:
            ids = set(pool.map(lambda _: read(), range(8)))
        assert len(ids) == 1


SCHEMA_MODELS = """\
import msgspec


class Shared(msgspec.Struct):
    name: str


class Secret(msgspec.Struct):
    token: str


class SecretReply(msgspec.Struct):
    ok: bool
"""


# @lat: [[tests#Hidden routes leave the schema]]
def test_include_in_schema_false(tmp_path, isolated_imports):
    project = tmp_path / "hiding"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "hiding"\nversion = "0"\n',
            "schema_models.py": SCHEMA_MODELS,
            "app/items/route.py": (
                "from chano import openapi\n"
                "from schema_models import Secret, Shared\n\n"
                "@openapi(include_in_schema=True)\n"
                "def POST(item: Shared) -> Shared:\n    return item\n\n"
                "@openapi(include_in_schema=False)\n"
                "def DELETE(secret: Secret) -> dict[str, bool]:\n"
                "    return {'deleted': bool(secret.token)}\n"
            ),
            "app/internal/[key]/route.py": (
                "from chano import openapi\n"
                "from schema_models import Secret, SecretReply, Shared\n\n"
                "@openapi(include_in_schema=False)\n"
                "def GET(key: str, verbose: bool = False) -> Shared:\n"
                "    return Shared(name=key)\n\n"
                "@openapi(include_in_schema=False)\n"
                "def POST(key: str, secret: Secret) -> SecretReply:\n"
                "    return SecretReply(ok=secret.token == key)\n"
            ),
        },
    )
    app = create_app(project / "app")
    client = make_client(app)

    schema = client("GET", "/openapi.json").json()
    assert list(schema["paths"]) == ["/items"]
    assert list(schema["paths"]["/items"]) == ["post"]
    assert set(schema["components"]["schemas"]) == {"Shared"}
    assert "internal" not in json.dumps(schema)

    # Hidden is only about documentation: hidden methods still serve requests.
    assert client("DELETE", "/items", json={"token": "x"}).json() == {"deleted": True}
    assert client("GET", "/internal/abc").json() == {"name": "abc"}
    reply = client("POST", "/internal/abc", json={"token": "abc"})
    assert reply.json() == {"ok": True}
    assert client("POST", "/internal/abc", json={}).status_code == 422
    assert client("DELETE", "/internal/abc").status_code == 405

    static = {f.path: f.content for f in plan_build(project).static_files}
    built = json.loads(static["openapi.json"])
    assert (built["paths"], built["components"]) == (
        schema["paths"],
        schema["components"],
    )

    # A deployed hidden function serves its route too.
    route_app = create_route_app(project / "app", "internal/[key]")
    assert make_client(route_app)("GET", "/internal/x").json() == {"name": "x"}


# @lat: [[tests#Schema with only hidden routes]]
def test_include_in_schema_only_hidden_routes(tmp_path, isolated_imports):
    project = tmp_path / "secret"
    write_project(
        project,
        {
            "schema_models.py": SCHEMA_MODELS,
            "app/route.py": (
                "from chano import openapi\n"
                "from schema_models import Secret\n\n"
                "@openapi(include_in_schema=False)\n"
                "def POST(secret: Secret) -> Secret:\n    return secret\n"
            ),
        },
    )
    app = create_app(project / "app")
    assert app.openapi() == {
        "openapi": "3.1.0",
        "info": {"title": "Chano", "version": "0.1.0"},
        "paths": {},
    }
    assert make_client(app)("POST", "/", json={"token": "t"}).json() == {"token": "t"}


# @lat: [[tests#Invalid openapi metadata]]
@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"include_in_schema": "no"}, "include_in_schema"),
        ({"include_in_schema": 0}, "include_in_schema"),
        ({"deprecated": 1}, "deprecated"),
        ({"summary": 123}, "summary"),
        ({"description": 123}, "description"),
        ({"operation_id": 123}, "operation_id"),
        ({"response_description": 123}, "response_description"),
        ({"tags": "items"}, "tags"),
        ({"tags": [123]}, "tags"),
        ({"openapi_extra": ["x"]}, "openapi_extra"),
        ({"openapi_extra": {1: "x"}}, "openapi_extra"),
    ],
)
def test_openapi_decorator_validation(kwargs, field):
    with pytest.raises(TypeError, match=field):
        openapi(**kwargs)


# @lat: [[tests#OpenAPI decorator metadata]]
def test_openapi_decorator_metadata(tmp_path, isolated_imports):
    extra = {
        "x-rate-limit": 60,
        "responses": {"200": {"headers": {"X-Trace": {"schema": {"type": "string"}}}}},
    }
    app = Chano()

    @openapi(
        summary="List widgets",
        description="Returns every visible widget.",
        operation_id="listWidgets",
        tags=["widgets", "public"],
        deprecated=True,
        response_description="A list of widgets",
        openapi_extra=extra,
    )
    def list_widgets() -> list[str]:
        """Ignored docstring summary.

        Ignored docstring body.
        """
        return ["a"]

    extra["x-rate-limit"] = 999
    extra["responses"]["200"]["headers"]["X-Mutated"] = {}

    @openapi(tags=["outer"], openapi_extra={"x-a": 1, "nested": {"k": "outer"}})
    @openapi(
        tags=["inner"],
        summary="Stacked summary",
        deprecated=True,
        openapi_extra={"x-b": 2, "nested": {"k": "inner", "keep": True}},
    )
    def stacked() -> dict[str, bool]:
        return {"ok": True}

    app.add_route("/widgets", list_widgets, ["GET"])
    app.add_route("/stacked", stacked, ["GET"])

    schema = app.openapi()
    assert schema == app.openapi()

    op = schema["paths"]["/widgets"]["get"]
    assert op["summary"] == "List widgets"
    assert op["description"] == "Returns every visible widget."
    assert op["operationId"] == "listWidgets"
    assert op["tags"] == ["widgets", "public"]
    assert op["deprecated"] is True
    assert op["responses"]["200"]["description"] == "A list of widgets"
    assert op["responses"]["200"]["headers"] == {
        "X-Trace": {"schema": {"type": "string"}}
    }
    assert "content" in op["responses"]["200"]
    assert op["x-rate-limit"] == 60

    stacked_op = schema["paths"]["/stacked"]["get"]
    assert stacked_op["tags"] == ["outer"]
    assert stacked_op["summary"] == "Stacked summary"
    assert stacked_op["deprecated"] is True
    assert stacked_op["x-a"] == 1
    assert stacked_op["x-b"] == 2
    assert stacked_op["nested"] == {"k": "outer", "keep": True}

    # Module-level include_in_schema is no longer special.
    project = tmp_path / "module_var_ignored"
    write_project(
        project,
        {
            "app/route.py": (
                "include_in_schema = False\n\ndef GET() -> int:\n    return 1\n"
            )
        },
    )
    assert "/" in create_app(project / "app").openapi()["paths"]


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
def test_lazy_as_dependency(tmp_path, isolated_imports):
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
                "def GET(client: Annotated[dict, Depends(get_client)],\n"
                "        pool: Annotated[dict, Depends(get_pool)] = None):\n"
                "    return {'client': client, 'pool': pool, 'made': made}\n\n"
                "async def POST(pool: Annotated[dict, Depends(get_pool)],\n"
                "               client: Annotated[dict, Depends(get_client)]):\n"
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
def test_lazy_async_callable_instance(tmp_path, isolated_imports):
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
                "def GET(pool: Annotated[dict, Depends(get_pool)]):\n"
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


SECURE_ITEMS = """\
from typing import Annotated

from chano import Depends, HTTPBearer, HTTPException, raises

bearer = HTTPBearer()


@raises(401, "Invalid token")
def current_user(token: Annotated[str, Depends(bearer)]) -> str:
    if token != "secret":
        raise HTTPException(401, "Invalid token")
    return "scott"


def GET() -> list[str]:
    return ["public"]


@raises(403, "Not allowed")
def POST(name: str, user: Annotated[str, Depends(current_user)]) -> str:
    return f"{user}:{name}"
"""


# @lat: [[tests#Explicit security and responses]]
def test_explicit_security_and_responses(tmp_path, isolated_imports):
    project = tmp_path / "secure"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "secure"\nversion = "0"\n',
            "app/items/route.py": SECURE_ITEMS,
            "app/broken/route.py": (
                "from typing import Annotated\n"
                "from chano import Depends, HTTPBearer\n\n"
                "def explode(token: Annotated[str, Depends(HTTPBearer())]) -> str:\n"
                "    raise RuntimeError('called during schema generation')\n\n"
                "def GET(value: Annotated[str, Depends(explode)]) -> str:\n"
                "    return value\n"
            ),
            "app/internal/route.py": (
                "from typing import Annotated\n"
                "from chano import Depends, HTTPBearer, openapi, raises\n\n"
                "@openapi(include_in_schema=False)\n"
                "@raises(401, 'Internal only')\n"
                "def GET(\n"
                "    token: Annotated[str, Depends(HTTPBearer('internal'))],\n"
                ") -> str:\n"
                "    return token\n"
            ),
        },
    )
    client = make_client(create_app(project / "app"))
    schema = client("GET", "/openapi.json").json()

    assert schema["components"] == {
        "securitySchemes": {"HTTPBearer": {"type": "http", "scheme": "bearer"}}
    }
    items = schema["paths"]["/items"]
    assert "security" not in items["get"]
    assert list(items["get"]["responses"]) == ["200"]

    post = items["post"]
    assert post["security"] == [{"HTTPBearer": []}]
    assert [(p["name"], p["in"]) for p in post["parameters"]] == [("name", "query")]
    assert {code: r["description"] for code, r in post["responses"].items()} == {
        "200": "Successful Response",
        "401": "Invalid token",
        "403": "Not allowed",
        "422": "Validation Error",
    }

    # Declarations are read, not executed: the raising dependency is
    # documented, and its scheme's runtime 401 is not, since it is undeclared.
    broken = schema["paths"]["/broken"]["get"]
    assert broken["security"] == [{"HTTPBearer": []}]
    assert list(broken["responses"]) == ["200"]
    assert "internal" not in json.dumps(schema)

    static = {f.path: f.content for f in plan_build(project).static_files}
    built = json.loads(static["openapi.json"])
    assert (built["paths"], built["components"]) == (
        schema["paths"],
        schema["components"],
    )

    assert client("GET", "/items").json() == ["public"]
    ok = client("POST", "/items?name=x", headers={"Authorization": "Bearer secret"})
    assert ok.json() == "scott:x"
    wrong = client("POST", "/items?name=x", headers={"Authorization": "Bearer no"})
    assert (wrong.status_code, wrong.json()) == (401, {"detail": "Invalid token"})


# @lat: [[tests#HTTP bearer credentials]]
@pytest.mark.parametrize(
    ("authorization", "token"),
    [
        (None, None),
        ("", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("Basic dXNlcjpwYXNz", None),
        ("Bearertoken", None),
        ("Bearer abc.def", "abc.def"),
        ("bearer abc", "abc"),
        ("BEARER  abc ", "abc"),
    ],
)
def test_http_bearer_credentials(authorization, token):
    app = Chano(docs=False)
    bearer = HTTPBearer()

    def handler(value: Annotated[str, Depends(bearer)]) -> str:
        return value

    app.add_route("/", handler, methods=["GET"])
    headers = {} if authorization is None else {"Authorization": authorization}
    response = make_client(app)("GET", "/", headers=headers)
    if token is None:
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}
        assert response.headers["www-authenticate"] == "Bearer"
    else:
        assert response.json() == token


# @lat: [[tests#Declared responses must be errors]]
@pytest.mark.parametrize("status", [200, 204, 399, 600])
def test_raises_rejects_non_error_statuses(status):
    with pytest.raises(ValueError, match="status"):
        raises(status, "nope")


def declared_descriptions(handler) -> dict[str, str]:
    app = Chano()
    app.add_route("/", handler, methods=["GET"])
    responses = app.openapi()["paths"]["/"]["get"]["responses"]
    return {code: r["description"] for code, r in responses.items()}


# @lat: [[tests#Declared response precedence]]
def test_declared_response_precedence():
    @raises(401, "outer")
    @raises(401, "inner")
    @raises(403, "only")
    def stacked() -> None:
        return None

    assert declared_descriptions(stacked) == {
        "200": "Successful Response",
        "401": "outer",
        "403": "only",
    }

    @raises(401, "nested")
    @raises(403, "nested")
    @raises(404, "nested")
    def nested() -> None:
        return None

    def first(value: Annotated[None, Depends(nested)]) -> None:
        return value

    @raises(401, "direct")
    @raises(403, "direct")
    def second() -> None:
        return None

    @raises(403, "handler")
    def handler(
        a: Annotated[None, Depends(first)], b: Annotated[None, Depends(second)]
    ) -> None:
        return None

    # The handler beats its dependencies, and a direct dependency beats a
    # nested one even when the nested one is in an earlier parameter.
    assert declared_descriptions(handler) == {
        "200": "Successful Response",
        "401": "direct",
        "403": "handler",
        "404": "nested",
    }


# @lat: [[tests#Declared response statuses must be integers]]
@pytest.mark.parametrize("status", [401.5, "401", True, None])
def test_raises_rejects_non_integer_statuses(status):
    with pytest.raises(TypeError, match="status"):
        raises(status, "nope")


# @lat: [[tests#HTTP bearer scheme names]]
@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("HTTPBearer", True),
        ("api.v1-token_2", True),
        ("", False),
        ("my bearer", False),
        ("a/b", False),
        ("tokén", False),
        ("a:b", False),
    ],
)
def test_http_bearer_scheme_names(name, valid):
    if not valid:
        with pytest.raises(ValueError, match="name"):
            HTTPBearer(name)
        return
    bearer = HTTPBearer(name)

    def handler(token: Annotated[str, Depends(bearer)]) -> str:
        return token

    app = Chano()
    app.add_route("/", handler, methods=["GET"])
    assert list(app.openapi()["components"]["securitySchemes"]) == [name]


INHERITED_CHECKS = """\
from typing import Annotated

import deplog
from chano import Depends, HTTPBearer, HTTPException, raises

bearer = HTTPBearer()


@raises(401, "Invalid token")
def require_user(token: Annotated[str, Depends(bearer)], tenant: str = "main") -> str:
    deplog.EVENTS.append("user")
    if token not in ("admin", "member"):
        raise HTTPException(401, "Invalid token", {"www-authenticate": "Bearer"})
    return f"{token}@{tenant}"


def group_area() -> None:
    deplog.EVENTS.append("group")


def admin_area() -> None:
    deplog.EVENTS.append("admin-area")


@raises(403, "Admins only")
def require_admin(
    user: Annotated[str, Depends(require_user)], tenant: str = "main"
) -> str:
    deplog.EVENTS.append("admin")
    if not user.startswith("admin@"):
        raise HTTPException(403, "Admins only")
    return user
"""

INHERITED_GROUP_ERROR = """\
from chano import HTTPException, JSONResponse


def handle_error(request, exc):
    if isinstance(exc, HTTPException):
        body = {"detail": exc.detail, "via": "group"}
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
            "app/(group)/dependencies.py": (
                "from checks import group_area, require_user\n\n"
                "dependencies = [group_area, require_user]\n"
            ),
            "app/(group)/error.py": INHERITED_GROUP_ERROR,
            "app/(group)/admin/dependencies.py": (
                "from checks import admin_area, require_admin\n\n"
                "dependencies = (admin_area, require_admin)\n"
            ),
            "app/(group)/admin/route.py": (
                "import deplog\n"
                "from typing import Annotated\n"
                "from chano import Depends\n"
                "from checks import require_admin\n\n"
                "def GET(\n"
                "    admin: Annotated[str, Depends(require_admin)],\n"
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


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
    app.add_route("/", handler, methods=["GET"], dependencies=[[check]])
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
    @raises(401, "root")
    @raises(403, "root")
    @raises(404, "root")
    @raises(409, "root")
    @raises(410, "root")
    def root() -> None:
        return None

    @raises(401, "leaf first")
    @raises(403, "leaf first")
    def leaf_first() -> None:
        return None

    @raises(403, "leaf second")
    @raises(409, "leaf second")
    def leaf_second() -> None:
        return None

    @raises(404, "direct")
    @raises(410, "direct")
    def direct() -> None:
        return None

    @raises(410, "handler")
    def handler(value: Annotated[None, Depends(direct)]) -> None:
        return value

    app = Chano()
    app.add_route(
        "/", handler, methods=["GET"], dependencies=[[root], [leaf_first, leaf_second]]
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
        value: Annotated[int, Depends(shared)], x_key: Annotated[str, Header()] = ""
    ) -> int:
        return value

    def second(
        value: Annotated[int, Depends(shared)], x_key: Annotated[str, Header()] = ""
    ) -> int:
        return value

    def handler(
        a: Annotated[int, Depends(first)],
        b: Annotated[int, Depends(second)],
        q: int = 0,
    ):
        return a + b + q

    app = Chano()
    app.add_route("/", handler, methods=["GET"], dependencies=[[shared]])
    parameters = app.openapi()["paths"]["/"]["get"]["parameters"]
    assert [(p["name"], p["in"]) for p in parameters] == [
        ("q", "query"),
        ("x-key", "header"),
    ]
    assert make_client(app)("GET", "/?q=2", headers={"x-key": "k"}).json() == 6


# @lat: [[tests#Inherited typed dependencies#Invalid dependencies.py]]
@pytest.mark.parametrize(
    "source",
    [
        "",
        "dependencies = 'check'\n",
        "dependencies = [1]\n",
        "dependencies = {check}\n",
        "from chano import Depends\ndependencies = [Depends(check)]\n",
    ],
)
def test_invalid_dependencies_file(tmp_path, isolated_imports, source):
    write_project(
        tmp_path,
        {
            "app/dependencies.py": "def check():\n    return None\n\n" + source,
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    with pytest.raises(TypeError, match=r"dependencies.*dependencies\.py"):
        create_app(tmp_path / "app")
    with pytest.raises(TypeError, match=r"dependencies.*dependencies\.py"):
        create_route_app(tmp_path / "app", "index")


# @lat: [[tests#Inherited typed dependencies#Trace roots and bundling]]
def test_inherited_dependencies_trace_roots(tmp_path, isolated_imports, monkeypatch):
    store_files: dict[str, str] = {}
    for dist in (
        make_dist("chano", "chano"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("check-pkg", "check_pkg"),
    ):
        store_files.update(dist)

    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(vendor_dir, store_files)

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    project = tmp_path / "checks"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "checks"\nversion = "0"\n',
            "app/(group)/dependencies.py": (
                "from typing import Annotated\nfrom chano import Header, raises\n\n"
                "@raises(403, 'Forbidden')\n"
                "def check(x_key: Annotated[str, Header()]) -> str:\n"
                "    import check_pkg\n"
                "    return x_key\n\n"
                "dependencies = [check]\n"
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
        app / "(group)" / "dependencies.py",
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
            raise HTTPException(403, "denied")

    class Value:
        async def __call__(self) -> int:
            return 5

    async def deny_later() -> None:
        calls.append("later")
        raise HTTPException(403, "later")

    def handler(value: Annotated[int, Depends(Value())]) -> int:
        calls.append("handler")
        return value

    for check in (Deny(), lambda: deny_later()):
        app = Chano(docs=False)
        app.add_route("/", handler, methods=["GET"], dependencies=[[check]])
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
                "from chano import Header, HTTPException\n\n"
                "CALLS = []\n\n"
                "@dataclass\n"
                "class Role:\n"
                "    name: str\n\n"
                "    def __call__(\n"
                "        self, x_role: Annotated[str, Header()] = ''\n"
                "    ) -> str:\n"
                "        CALLS.append(self.name)\n"
                "        if x_role != self.name:\n"
                "            raise HTTPException(403, 'wrong role')\n"
                "        return x_role\n\n"
                "admin = Role('admin')  # eq=True without frozen: unhashable\n"
            ),
            "app/dependencies.py": (
                "from roles import admin\n\ndependencies = [admin]\n"
            ),
            "app/route.py": (
                "from typing import Annotated\n"
                "from chano import Depends, Header\n"
                "from roles import admin\n\n"
                "def GET(\n"
                "    role: Annotated[str, Depends(admin)],\n"
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
    app.add_route("/", handler, methods=["GET"], dependencies=[[check]])
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

    def handler(b: WholeB, x: Annotated[int, Depends(whole_check)]) -> list[int]:
        return [x, b.y]

    app = Chano()
    app.add_route("/", handler, methods=["POST"], dependencies=[[embedded_check]])
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
                raise HTTPException(403, "blocked")

    first, second = Gate("gate"), Gate("gate", block=True)
    assert first == second and hash(first) == hash(second)

    def handler() -> str:
        calls.append("handler")
        return "ok"

    app = Chano(docs=False)
    app.add_route("/", handler, methods=["GET"], dependencies=[[first], [second]])
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
    def handler(value: Annotated[int, Depends(guard.check)]) -> int:
        return value

    app = Chano(docs=False)
    app.add_route("/", handler, methods=["GET"], dependencies=[[guard.check]])
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
    app.add_route("/", handler, methods=["POST"], dependencies=[[check], [same_check]])
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
        Chano().add_route("/", handler, methods=["GET"], dependencies=[[check]])

    sources = {
        "sync": "def check():\n    yield\n",
        "async": "async def check():\n    yield\n",
    }
    for kind, source in sources.items():
        project = tmp_path / kind
        write_project(
            project,
            {
                "app/dependencies.py": source + "\ndependencies = [check]\n",
                "app/route.py": "def GET():\n    return {}\n",
            },
        )
        with pytest.raises(TypeError, match=r"generator.*dependencies\.py"):
            create_app(project / "app")
        with pytest.raises(TypeError, match=r"generator.*dependencies\.py"):
            create_route_app(project / "app", "index")


# @lat: [[tests#Inherited typed dependencies#Mixed list and scalar query inputs]]
def test_mixed_list_and_scalar_query_inputs_are_rejected():
    def check(q: list[int]) -> None:
        return None

    def handler(q: int = 0) -> int:
        return q

    app = Chano()
    app.add_route("/", handler, methods=["GET"], dependencies=[[check]])
    with pytest.raises(ValueError, match="'q'"):
        app.openapi()

    # The same shape everywhere is still one input.
    def lists(q: list[int]) -> list[int]:
        return q

    same = Chano()
    same.add_route("/", lists, methods=["GET"], dependencies=[[check]])
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
    app.add_route("/", handler, methods=["GET"], dependencies=[[check]])
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
    app.add_route("/", handler, methods=["POST"], dependencies=[[check]])
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

    def needs_wrapped(value: Annotated[Any, Depends(wrapped)]) -> None:
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
    for dependencies in cases:
        app = Chano(docs=False)
        app.add_route("/", handler, methods=["GET"], dependencies=dependencies)
        calls.clear()
        response = make_client(app)("GET", "/")
        assert response.status_code == 500
        assert "handler" not in calls


# @lat: [[tests#Lazy Pydantic model detection#Msgspec routes never import pydantic]]
def test_msgspec_routes_do_not_import_pydantic():
    script = """
import asyncio
import sys
from typing import Any

import httpx
import msgspec

attempts: list[str] = []


class BlockPydantic:
    def find_spec(self, fullname, path, target=None):
        if fullname == "pydantic" or fullname.startswith("pydantic."):
            attempts.append(fullname)
            raise AssertionError(f"unexpected import attempt: {fullname}")
        return None


sys.meta_path.insert(0, BlockPydantic())

from chano import Chano, SSEResponse


class Item(msgspec.Struct):
    name: str


def create(item: Item, limit: int = 10) -> dict[str, Any]:
    return {"name": item.name, "limit": limit}


def stream() -> SSEResponse:
    return SSEResponse([Item(name="tick")])


async def main() -> None:
    app = Chano()
    app.add_route("/items", create, methods=["POST"])
    app.add_route("/events", stream, methods=["GET"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        post = await http.post("/items?limit=5", json={"name": "widget"})
        assert post.status_code == 200
        assert post.json() == {"name": "widget", "limit": 5}
        events = await http.get("/events")
        assert events.status_code == 200
        assert 'data: {"name":"tick"}\\n\\n' in events.text
        schema = await http.get("/openapi.json")
        assert schema.status_code == 200


asyncio.run(main())
assert not attempts
assert "pydantic" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# @lat: [[tests#Lazy Pydantic model detection#BaseModel parameter infers body source]]
def test_pydantic_basemodel_infers_body_source(monkeypatch):
    fake_pydantic = types.ModuleType("pydantic")

    class BaseModel:
        pass

    fake_pydantic.__dict__["BaseModel"] = BaseModel
    monkeypatch.setitem(sys.modules, "pydantic", fake_pydantic)

    class Payload(BaseModel):
        title: str

    class Plain:
        pass

    def plain_model(payload: Payload) -> dict[str, Any]:
        return {}

    def annotated_model(payload: Annotated[Payload, "meta"]) -> dict[str, Any]:
        return {}

    def explicit_query(payload: Annotated[Payload, Query()]) -> dict[str, Any]:
        return {}

    def plain_class(plain: Plain) -> dict[str, Any]:
        return {}

    app = Chano(docs=False)
    app.add_route("/plain", plain_model, methods=["POST"])
    app.add_route("/annotated", annotated_model, methods=["POST"])
    app.add_route("/query", explicit_query, methods=["POST"])
    app.add_route("/non-model", plain_class, methods=["POST"])
    client = make_client(app)

    for path, expected_loc in (
        ("/plain", ["body"]),
        ("/annotated", ["body"]),
        ("/query", ["query", "payload"]),
        ("/non-model", ["query", "plain"]),
    ):
        response = client("POST", path)
        assert response.status_code == 422
        assert response.json() == {
            "detail": [{"loc": expected_loc, "msg": "Field required"}]
        }


# @lat: [[tests#Pydantic request validation#Body binding for BaseModel shapes]]
def test_pydantic_body_validation_and_binding():
    from pydantic import AfterValidator, BaseModel

    class Tag(BaseModel):
        label: str

    class Item(BaseModel):
        name: str
        count: int = 1
        tags: list[Tag] = []

    class Meta(BaseModel):
        source: str

    def strip_name(item: Item) -> Item:
        return item.model_copy(update={"name": item.name.strip()})

    def single(item: Item) -> dict[str, Any]:
        return {
            "name": item.name,
            "count": item.count,
            "tags": [tag.label for tag in item.tags],
        }

    def annotated(
        item: Annotated[Item, AfterValidator(strip_name)],
    ) -> dict[str, Any]:
        return {"name": item.name, "count": item.count}

    def embedded(item: Item, meta: Meta) -> dict[str, Any]:
        return {"name": item.name, "source": meta.source}

    def optional_body(
        item: Annotated[Item | None, Body()] = None,
    ) -> dict[str, Any]:
        return {"name": item.name if item is not None else None}

    default_item = Item(name="fallback", count=0)

    def defaulted_body(item: Item = default_item) -> dict[str, Any]:
        return {"name": item.name, "count": item.count}

    def list_body(items: Annotated[list[Item], Body()]) -> dict[str, Any]:
        return {"names": [item.name for item in items]}

    app = Chano(docs=False)
    app.add_route("/single", single, methods=["POST"])
    app.add_route("/annotated", annotated, methods=["POST"])
    app.add_route("/embedded", embedded, methods=["POST"])
    app.add_route("/optional", optional_body, methods=["POST"])
    app.add_route("/defaulted", defaulted_body, methods=["POST"])
    app.add_route("/list", list_body, methods=["POST"])
    client = make_client(app)

    res = client(
        "POST",
        "/single",
        json={"name": "widget", "count": 3, "tags": [{"label": "sale"}]},
    )
    assert res.status_code == 200
    assert res.json() == {"name": "widget", "count": 3, "tags": ["sale"]}

    res = client("POST", "/annotated", json={"name": "  padded  "})
    assert res.status_code == 200
    assert res.json() == {"name": "padded", "count": 1}

    res = client(
        "POST",
        "/embedded",
        json={"item": {"name": "widget", "count": 2}, "meta": {"source": "api"}},
    )
    assert res.status_code == 200
    assert res.json() == {"name": "widget", "source": "api"}

    res = client("POST", "/optional")
    assert res.status_code == 200
    assert res.json() == {"name": None}

    res = client("POST", "/optional", json={"name": "present"})
    assert res.status_code == 200
    assert res.json() == {"name": "present"}

    res = client("POST", "/defaulted")
    assert res.status_code == 200
    assert res.json() == {"name": "fallback", "count": 0}

    res = client("POST", "/list", json=[{"name": "a"}, {"name": "b", "count": 4}])
    assert res.status_code == 200
    assert res.json() == {"names": ["a", "b"]}


# @lat: [[tests#Pydantic request validation#Invalid payloads and malformed JSON]]
def test_pydantic_validation_errors_and_malformed_json():
    from pydantic import BaseModel
    from pydantic import Field as PydanticField

    class Tag(BaseModel):
        label: str = PydanticField(min_length=2)

    class Item(BaseModel):
        name: str
        count: int = PydanticField(ge=1)
        tags: list[Tag] = []

    class Meta(BaseModel):
        source: str

    def single(item: Item) -> dict[str, Any]:
        return {"name": item.name}

    def embedded(item: Item, meta: Meta) -> dict[str, Any]:
        return {"name": item.name, "source": meta.source}

    def list_body(items: Annotated[list[Item], Body()]) -> dict[str, Any]:
        return {"count": len(items)}

    app = Chano(docs=False)
    app.add_route("/single", single, methods=["POST"])
    app.add_route("/embedded", embedded, methods=["POST"])
    app.add_route("/list", list_body, methods=["POST"])
    client = make_client(app)

    # Nested field + array index + multiple errors in one model
    res = client(
        "POST",
        "/single",
        json={"count": 0, "tags": [{"label": "ok"}, {"label": "x"}]},
    )
    assert res.status_code == 422
    detail = res.json()["detail"]
    locs = [entry["loc"] for entry in detail]
    assert ["body", "name"] in locs
    assert ["body", "count"] in locs
    assert ["body", "tags", 1, "label"] in locs
    assert all(isinstance(entry["msg"], str) and entry["msg"] for entry in detail)

    # Top-level list[BaseModel] array index loc
    res = client(
        "POST",
        "/list",
        json=[{"name": "good", "count": 1}, {"name": "bad", "count": "nope"}],
    )
    assert res.status_code == 422
    assert any(entry["loc"] == ["body", 1, "count"] for entry in res.json()["detail"])

    # Embedded BaseModel strict validation & field-level loc
    res = client(
        "POST",
        "/embedded",
        json={"item": {"name": "widget", "count": "2"}},
    )
    assert res.status_code == 422
    embedded_locs = [entry["loc"] for entry in res.json()["detail"]]
    assert ["body", "item", "count"] in embedded_locs
    assert ["body", "meta"] in embedded_locs

    # Empty body for required BaseModel
    res = client("POST", "/single")
    assert res.status_code == 422
    assert res.json() == {"detail": [{"loc": ["body"], "msg": "Field required"}]}

    # Malformed JSON for single BaseModel body
    res = client(
        "POST",
        "/single",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 422
    malformed = res.json()["detail"]
    assert len(malformed) == 1
    assert malformed[0]["loc"] == ["body"]
    assert malformed[0]["msg"]


# @lat: [[tests#Pydantic request validation#Cached TypeAdapter and containers]]
def test_pydantic_type_adapter_cached_and_containers(monkeypatch):
    import pydantic
    from pydantic import BaseModel, model_validator

    class Item(BaseModel):
        name: str

    class QueryFilter(BaseModel):
        key: str
        value: int

        @model_validator(mode="before")
        @classmethod
        def parse_pair(cls, raw: Any) -> Any:
            if isinstance(raw, str) and ":" in raw:
                key, value = raw.split(":", 1)
                return {"key": key, "value": value}
            return raw

    created = 0
    real_adapter = pydantic.TypeAdapter

    def counting_adapter(annotation: Any) -> Any:
        nonlocal created
        created += 1
        return real_adapter(annotation)

    monkeypatch.setattr(pydantic, "TypeAdapter", counting_adapter)

    def handle(
        items: Annotated[dict[str, Item], Body()],
        filt: Annotated[QueryFilter, Query()],
    ) -> dict[str, Any]:
        return {
            "keys": sorted(items),
            "names": [items[k].name for k in sorted(items)],
            "filter": {"key": filt.key, "value": filt.value},
        }

    app = Chano(docs=False)
    app.add_route("/items", handle, methods=["POST"])
    # Built once per Pydantic-backed field during Endpoint construction.
    assert created == 2

    client = make_client(app)
    for _ in range(3):
        res = client(
            "POST",
            "/items?filt=min:5",
            json={"a": {"name": "alpha"}, "b": {"name": "beta"}},
        )
        assert res.status_code == 200
        assert res.json() == {
            "keys": ["a", "b"],
            "names": ["alpha", "beta"],
            "filter": {"key": "min", "value": 5},
        }

    bad = client("POST", "/items?filt=min:bad", json={"a": {"name": "alpha"}})
    assert bad.status_code == 422
    assert any(
        entry["loc"] == ["query", "filt", "value"] for entry in bad.json()["detail"]
    )
    # Never rebuilt per request.
    assert created == 2


# @lat: [[tests#Pydantic request validation#Specialized TypeAliasType and strict JSON]]
def test_pydantic_specialized_type_alias_and_strict_json_mode():
    from datetime import date

    from pydantic import BaseModel, ConfigDict

    class Item(BaseModel):
        count: int

    type Payload[T] = tuple[T, Item]

    class Event(BaseModel):
        model_config = ConfigDict(strict=True)
        day: date

    def alias_handler(
        payload: Annotated[Payload[str], Body()],
    ) -> dict[str, Any]:
        label, item = payload
        return {"label": label, "count": item.count}

    def strict_handler(event: Event) -> dict[str, Any]:
        return {"day": event.day.isoformat()}

    app = Chano(docs=False)
    app.add_route("/alias", alias_handler, methods=["POST"])
    app.add_route("/strict", strict_handler, methods=["POST"])
    client = make_client(app)

    res = client("POST", "/alias", json=["label", {"count": 7}])
    assert res.status_code == 200
    assert res.json() == {"label": "label", "count": 7}

    # In JSON mode (validate_json), ISO date strings satisfy strict=True date fields;
    # validate_python(..., strict=True) would reject a str.
    res = client("POST", "/strict", json={"day": "2026-11-21"})
    assert res.status_code == 200
    assert res.json() == {"day": "2026-11-21"}


# @lat: [[tests#Pydantic responses and OpenAPI#JSON and SSE serialization]]
def test_pydantic_response_and_sse_serialization():
    from datetime import date

    from pydantic import BaseModel

    class Tag(BaseModel):
        label: str

    class Item(BaseModel):
        name: str
        created: date
        tags: list[Tag] = []

    class Envelope(msgspec.Struct):
        ok: bool
        item: Item

    sample = Item(name="alpha", created=date(2026, 11, 21), tags=[Tag(label="v1")])
    expected = {
        "name": "alpha",
        "created": "2026-11-21",
        "tags": [{"label": "v1"}],
    }

    def single() -> Item:
        return sample

    def many() -> list[Item]:
        return [sample]

    def mapping() -> dict[str, Item]:
        return {"first": sample}

    def wrapped() -> Envelope:
        return Envelope(ok=True, item=sample)

    def events() -> SSEResponse:
        return SSEResponse(
            [
                sample,
                ServerSentEvent(data=Envelope(ok=True, item=sample), event="wrapped"),
            ]
        )

    app = Chano(docs=False)
    app.add_route("/single", single, methods=["GET"])
    app.add_route("/many", many, methods=["GET"])
    app.add_route("/mapping", mapping, methods=["GET"])
    app.add_route("/wrapped", wrapped, methods=["GET"])
    app.add_route("/events", events, methods=["GET"])
    client = make_client(app)

    res = client("GET", "/single")
    assert res.status_code == 200
    assert res.json() == expected

    res = client("GET", "/many")
    assert res.status_code == 200
    assert res.json() == [expected]

    res = client("GET", "/mapping")
    assert res.status_code == 200
    assert res.json() == {"first": expected}

    res = client("GET", "/wrapped")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "item": expected}

    res = client("GET", "/events")
    assert res.status_code == 200
    frames = [frame for frame in res.text.split("\n\n") if frame]
    assert len(frames) == 2
    assert json.loads(frames[0].removeprefix("data: ")) == expected
    event_line, data_line = frames[1].splitlines()
    assert event_line == "event: wrapped"
    assert json.loads(data_line.removeprefix("data: ")) == {
        "ok": True,
        "item": expected,
    }

    with pytest.raises(TypeError):
        _ = JSONResponse(object()).body


# @lat: [[tests#Pydantic responses and OpenAPI#Dev and build OpenAPI schemas]]
def test_pydantic_openapi_dev_and_build(tmp_path, isolated_imports, monkeypatch):
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    project = tmp_path / "pydantic_openapi_app"
    (project / "app" / "orders").mkdir(parents=True)
    (project / "app" / "ping").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "pydantic-openapi-app"\nversion = "0.1.0"\n'
    )
    (project / "app" / "orders" / "route.py").write_text(
        """
from typing import Annotated

from pydantic import BaseModel, Field

from chano import Body


class Tag(BaseModel):
    label: str


class Item(BaseModel):
    name: str
    tags: list[Tag] = []


class Order(BaseModel):
    id: int
    items: list[Item]


def POST(
    order: Annotated[Order, Field(description="Order to create")],
    notes: Annotated[Item | None, Body()] = None,
) -> list[Item]:
    \"\"\"Create an order.\"\"\"
    return order.items


def PUT(items: Annotated[list[Item], Body()]) -> Order:
    \"\"\"Replace order items.\"\"\"
    return Order(id=1, items=items)
"""
    )
    (project / "app" / "ping" / "route.py").write_text(
        """
import msgspec


class Pong(msgspec.Struct):
    ok: bool


def GET() -> Pong:
    return Pong(ok=True)
"""
    )

    app = create_app(project / "app", title=project.name)
    client = make_client(app)
    dev_res = client("GET", "/openapi.json")
    assert dev_res.status_code == 200
    dev_schema = dev_res.json()

    built = build(project, platform=None)
    openapi_file = built / "static" / "openapi.json"
    assert openapi_file.is_file()
    build_schema = json.loads(openapi_file.read_text())
    assert build_schema == dev_schema

    components = dev_schema["components"]["schemas"]
    assert {"Order", "Item", "Tag", "Pong"} <= set(components)
    assert components["Order"]["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert components["Item"]["properties"]["tags"]["items"] == {
        "$ref": "#/components/schemas/Tag"
    }

    post_op = dev_schema["paths"]["/orders"]["post"]
    post_body = post_op["requestBody"]["content"]["application/json"]["schema"]
    assert "$defs" not in post_body
    assert post_body["properties"]["order"]["$ref"] == "#/components/schemas/Order"
    assert post_body["properties"]["notes"]["anyOf"] == [
        {"$ref": "#/components/schemas/Item"},
        {"type": "null"},
    ]
    post_resp = post_op["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$defs" not in post_resp
    assert post_resp == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/Item"},
    }

    put_op = dev_schema["paths"]["/orders"]["put"]
    put_body = put_op["requestBody"]["content"]["application/json"]["schema"]
    assert "$defs" not in put_body
    assert put_body == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/Item"},
    }
    put_resp = put_op["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$defs" not in put_resp
    assert put_resp["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/Item"
    }

    ping_resp = dev_schema["paths"]["/ping"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert ping_resp == {"$ref": "#/components/schemas/Pong"}


# @lat: [[tests#Pydantic responses and OpenAPI#Component name collisions]]
def test_pydantic_openapi_component_collisions():
    from pydantic import BaseModel

    def make_struct_route() -> Any:
        class Payload(msgspec.Struct):
            struct_field: int

        def route(body: Payload) -> Payload:
            return body

        return route

    def make_model_route() -> Any:
        class Payload(BaseModel):
            model_field: str

        def route(body: Annotated[list[Payload], Body()]) -> list[Payload]:
            return body

        return route

    def make_other_model_route() -> Any:
        class Payload(BaseModel):
            other_field: bool

        def route(body: Annotated[list[Payload], Body()]) -> list[Payload]:
            return body

        return route

    app = Chano()
    app.add_route("/struct", make_struct_route(), methods=["POST"])
    app.add_route("/model", make_model_route(), methods=["POST"])
    app.add_route("/other", make_other_model_route(), methods=["POST"])
    client = make_client(app)

    res = client("GET", "/openapi.json")
    assert res.status_code == 200
    doc = res.json()
    schemas = doc["components"]["schemas"]

    def resolve(ref_schema: dict[str, Any]) -> dict[str, Any]:
        ref = ref_schema["$ref"]
        name = ref.removeprefix("#/components/schemas/")
        return schemas[name]

    struct_ref = doc["paths"]["/struct"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    model_ref = doc["paths"]["/model"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["items"]
    other_ref = doc["paths"]["/other"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["items"]

    assert len({struct_ref["$ref"], model_ref["$ref"], other_ref["$ref"]}) == 3
    assert set(resolve(struct_ref)["properties"]) == {"struct_field"}
    assert set(resolve(model_ref)["properties"]) == {"model_field"}
    assert set(resolve(other_ref)["properties"]) == {"other_field"}

    # Discriminated union mapping must also rewrite when a member collides.
    def make_struct_pet_route() -> Any:
        class Pet(msgspec.Struct):
            species: str

        def route(body: Pet) -> Pet:
            return body

        return route

    def make_union_route() -> Any:
        from typing import Literal

        from pydantic import Field

        class Pet(BaseModel):
            kind: Literal["pet"]
            name: str

        class Toy(BaseModel):
            kind: Literal["toy"]
            title: str

        def route(
            body: Annotated[Pet | Toy, Field(discriminator="kind"), Body()],
        ) -> dict[str, Any]:
            return body.model_dump(mode="json")

        return route

    union_app = Chano()
    union_app.add_route("/struct-pet", make_struct_pet_route(), methods=["POST"])
    union_app.add_route("/union", make_union_route(), methods=["POST"])
    union_doc = make_client(union_app)("GET", "/openapi.json").json()
    union_schemas = union_doc["components"]["schemas"]

    struct_pet_ref = union_doc["paths"]["/struct-pet"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["$ref"]
    union_body = union_doc["paths"]["/union"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    pet_mapping_ref = union_body["discriminator"]["mapping"]["pet"]
    one_of_refs = {entry["$ref"] for entry in union_body["oneOf"]}

    assert pet_mapping_ref != struct_pet_ref
    assert pet_mapping_ref in one_of_refs
    assert set(
        union_schemas[struct_pet_ref.removeprefix("#/components/schemas/")][
            "properties"
        ]
    ) == {"species"}
    assert set(
        union_schemas[pet_mapping_ref.removeprefix("#/components/schemas/")][
            "properties"
        ]
    ) == {"kind", "name"}


# @lat: [[tests#Pydantic responses and OpenAPI#Response serialization schema mode]]
def test_pydantic_openapi_response_serialization_mode():
    from pydantic import BaseModel, Field, field_serializer

    class User(BaseModel):
        display_name: str = Field(alias="displayName")
        score: int

        @field_serializer("score")
        def serialize_score(self, value: int) -> str:
            return f"{value}pts"

    def create_user(user: User) -> User:
        return user

    def batch_users(users: Annotated[list[User], Body()]) -> list[User]:
        return users

    app = Chano()
    app.add_route("/user", create_user, methods=["POST"])
    app.add_route("/users", batch_users, methods=["POST"])
    client = make_client(app)

    http_res = client("POST", "/user", json={"displayName": "Ada", "score": 42})
    assert http_res.status_code == 200
    assert http_res.json() == {"display_name": "Ada", "score": "42pts"}

    doc = client("GET", "/openapi.json").json()
    user_op = doc["paths"]["/user"]["post"]
    req_schema = user_op["requestBody"]["content"]["application/json"]["schema"]
    resp_schema = user_op["responses"]["200"]["content"]["application/json"]["schema"]

    assert set(req_schema["properties"]) == {"displayName", "score"}
    assert req_schema["properties"]["score"]["type"] == "integer"
    assert set(resp_schema["properties"]) == {"display_name", "score"}
    assert resp_schema["properties"]["score"]["type"] == "string"

    # When the same model appears as both request and response via $ref,
    # validation and serialization components stay distinct instead of clobbering.
    schemas = doc["components"]["schemas"]
    batch_op = doc["paths"]["/users"]["post"]
    batch_req_ref = batch_op["requestBody"]["content"]["application/json"]["schema"][
        "items"
    ]["$ref"]
    batch_resp_ref = batch_op["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["items"]["$ref"]
    assert batch_req_ref != batch_resp_ref
    batch_req = schemas[batch_req_ref.removeprefix("#/components/schemas/")]
    batch_resp = schemas[batch_resp_ref.removeprefix("#/components/schemas/")]
    assert set(batch_req["properties"]) == {"displayName", "score"}
    assert batch_req["properties"]["score"]["type"] == "integer"
    assert set(batch_resp["properties"]) == {"display_name", "score"}
    assert batch_resp["properties"]["score"]["type"] == "string"

    # ConfigDict(serialize_by_alias=True) keeps alias keys in both HTTP JSON
    # and the OpenAPI response schema.
    from pydantic import ConfigDict

    class Aliased(BaseModel):
        model_config = ConfigDict(serialize_by_alias=True)
        key_name: str = Field(alias="keyName")

    def echo_aliased(item: Aliased) -> Aliased:
        return item

    alias_app = Chano()
    alias_app.add_route("/aliased", echo_aliased, methods=["POST"])
    alias_client = make_client(alias_app)

    alias_http = alias_client("POST", "/aliased", json={"keyName": "example"})
    assert alias_http.status_code == 200
    assert alias_http.json() == {"keyName": "example"}

    alias_doc = alias_client("GET", "/openapi.json").json()
    alias_resp = alias_doc["paths"]["/aliased"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert set(alias_resp["properties"]) == {"keyName"}

    # Mixed models honor each model's own serialize_by_alias config per model.
    class Plain(BaseModel):
        key_name: str = Field(alias="keyName")

    class ChildAliased(BaseModel):
        model_config = ConfigDict(serialize_by_alias=True)
        other_key: str = Field(alias="otherKey")

    class Parent(BaseModel):
        parent_key: str = Field(alias="parentKey")
        child: ChildAliased
        trailing_key: str = Field(alias="trailingKey")

    def get_parent() -> Parent:
        return Parent(
            parentKey="p",
            child=ChildAliased(otherKey="c"),
            trailingKey="t",
        )

    def get_pair() -> tuple[Plain, ChildAliased]:
        return (Plain(keyName="x"), ChildAliased(otherKey="y"))

    mixed_app = Chano()
    mixed_app.add_route("/parent", get_parent, methods=["GET"])
    mixed_app.add_route("/pair", get_pair, methods=["GET"])
    mixed_client = make_client(mixed_app)

    assert mixed_client("GET", "/parent").json() == {
        "parent_key": "p",
        "child": {"otherKey": "c"},
        "trailing_key": "t",
    }
    assert mixed_client("GET", "/pair").json() == [
        {"key_name": "x"},
        {"otherKey": "y"},
    ]

    mixed_doc = mixed_client("GET", "/openapi.json").json()
    mixed_schemas = mixed_doc["components"]["schemas"]

    parent_resp = mixed_doc["paths"]["/parent"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert set(parent_resp["properties"]) == {"parent_key", "child", "trailing_key"}
    child_ref = parent_resp["properties"]["child"]["$ref"].removeprefix(
        "#/components/schemas/"
    )
    assert set(mixed_schemas[child_ref]["properties"]) == {"otherKey"}

    pair_resp = mixed_doc["paths"]["/pair"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    plain_ref, aliased_ref = (
        entry["$ref"].removeprefix("#/components/schemas/")
        for entry in pair_resp["prefixItems"]
    )
    assert set(mixed_schemas[plain_ref]["properties"]) == {"key_name"}
    assert set(mixed_schemas[aliased_ref]["properties"]) == {"otherKey"}


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
    tmp_path, isolated_imports, monkeypatch
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
    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(
            vendor_dir,
            {
                **make_dist("chano", "chano"),
                **make_dist("vercel-runtime", "vercel_runtime"),
                **make_dist("vercel", "vercel"),
            },
        )

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
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


def test_markers_require_annotated():
    def dep() -> int:
        return 1

    def bad_query(q: Any = Query()):  # noqa: B008
        return q

    def bad_header(h: Any = Header()):  # noqa: B008
        return h

    def bad_cookie(c: Any = Cookie()):  # noqa: B008
        return c

    def bad_body(b: Any = Body()):  # noqa: B008
        return b

    def bad_depends(d: Any = Depends(dep)):  # noqa: B008
        return d

    for fn in (bad_query, bad_header, bad_cookie, bad_body, bad_depends):
        app = Chano()
        with pytest.raises(TypeError, match="must be placed inside Annotated"):
            app.add_route("/", fn, methods=["GET"])

    query_fn: Any = Query
    with pytest.raises(TypeError):
        query_fn(10)


def test_annotated_query_meta_with_default():
    app = Chano()

    def search(limit: Annotated[int, msgspec.Meta(lt=100), Query()] = 10):
        return {"limit": limit}

    app.add_route("/items", search, methods=["GET"])
    client = make_client(app)

    assert client("GET", "/items").json() == {"limit": 10}
    assert client("GET", "/items?limit=42").json() == {"limit": 42}
    assert client("GET", "/items?limit=100").status_code == 422


# @lat: [[tests#Representations#Problem JSON subclassing JSONRepresentation]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_representation_problem_json(tmp_path, isolated_imports, factory):
    write_project(
        tmp_path,
        {
            "app/representation.py": (
                "from typing import Any\n"
                "import msgspec\n"
                "from chano import (\n"
                "    HTTPException,\n"
                "    JSONRepresentation,\n"
                "    Request,\n"
                "    RequestValidationError,\n"
                "    Response,\n"
                ")\n\n"
                "class ProblemDetail(msgspec.Struct):\n"
                "    title: str\n"
                "    status: int\n"
                "    detail: Any = None\n"
                "    instance: str | None = None\n\n"
                "class ProblemJSON(JSONRepresentation):\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        if status_code >= 400:\n"
                "            return 'application/problem+json'\n"
                "        return 'application/json'\n\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        if status_code >= 400:\n"
                "            if status_code == 410:\n"
                "                return None\n"
                "            return ProblemDetail\n"
                "        return returns\n\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response:\n"
                "        if not isinstance(value, Exception):\n"
                "            return super().render(request, value, status_code)\n"
                "        mt = self.media_type(status_code)\n"
                "        if isinstance(value, HTTPException):\n"
                "            problem = ProblemDetail(\n"
                "                title='HTTP Error',\n"
                "                status=value.status_code,\n"
                "                detail=value.detail,\n"
                "                instance=request.path,\n"
                "            )\n"
                "            return Response(\n"
                "                msgspec.json.encode(problem),\n"
                "                status_code=value.status_code,\n"
                "                headers=value.headers,\n"
                "                media_type=mt,\n"
                "            )\n"
                "        if isinstance(value, RequestValidationError):\n"
                "            problem = ProblemDetail(\n"
                "                title='Validation Error',\n"
                "                status=422,\n"
                "                detail=value.errors,\n"
                "                instance=request.path,\n"
                "            )\n"
                "            return Response(\n"
                "                msgspec.json.encode(problem),\n"
                "                status_code=422,\n"
                "                media_type=mt,\n"
                "            )\n"
                "        problem = ProblemDetail(\n"
                "            title='Internal Server Error',\n"
                "            status=500,\n"
                "            instance=request.path,\n"
                "        )\n"
                "        return Response(\n"
                "            msgspec.json.encode(problem),\n"
                "            status_code=500,\n"
                "            media_type=mt,\n"
                "        )\n\n"
                "representation = ProblemJSON()\n"
            ),
            "app/items/[item_id]/route.py": (
                "import msgspec\n"
                "from chano import HTTPException, PlainTextResponse, raises\n\n"
                "class Item(msgspec.Struct):\n"
                "    id: int\n"
                "    name: str\n\n"
                "@raises(404, 'Item missing')\n"
                "@raises(410, 'Item gone')\n"
                "def GET(item_id: int) -> Item:\n"
                "    if item_id == 0:\n"
                "        raise HTTPException(\n"
                "            404, 'No such item', {'x-reason': 'zero'}\n"
                "        )\n"
                "    if item_id < 0:\n"
                "        raise RuntimeError('negative id')\n"
                "    return Item(id=item_id, name='widget')\n\n"
                "def DELETE(item_id: int) -> PlainTextResponse:\n"
                "    return PlainTextResponse('deleted')\n"
            ),
        },
    )
    app_dir = tmp_path / "app"
    app = (
        create_app(app_dir)
        if factory == "dev"
        else create_route_app(app_dir, "items/[item_id]")
    )
    client = make_client(app)

    ok = client("GET", "/items/7")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "application/json"
    assert ok.json() == {"id": 7, "name": "widget"}

    # Explicit Response bypasses representation.render.
    deleted = client("DELETE", "/items/7")
    assert deleted.status_code == 200
    assert deleted.headers["content-type"].startswith("text/plain")
    assert deleted.text == "deleted"

    # HTTPException -> application/problem+json preserving headers.
    missing = client("GET", "/items/0")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.headers["x-reason"] == "zero"
    assert missing.json() == {
        "title": "HTTP Error",
        "status": 404,
        "detail": "No such item",
        "instance": "/items/0",
    }

    # RequestValidationError -> application/problem+json.
    bad = client("GET", "/items/not-an-int")
    assert bad.status_code == 422
    assert bad.headers["content-type"] == "application/problem+json"
    body = bad.json()
    assert body["title"] == "Validation Error"
    assert body["status"] == 422
    assert body["instance"] == "/items/not-an-int"
    assert body["detail"][0]["loc"] == ["path", "item_id"]

    # Method mismatch 405 -> application/problem+json with Allow header.
    wrong_method = client("POST", "/items/7")
    assert wrong_method.status_code == 405
    assert wrong_method.headers["content-type"] == "application/problem+json"
    assert wrong_method.headers["allow"] == "DELETE, GET"
    assert wrong_method.json() == {
        "title": "HTTP Error",
        "status": 405,
        "detail": "Method Not Allowed",
        "instance": "/items/7",
    }

    # Unhandled exception -> application/problem+json 500.
    boom = client("GET", "/items/-1")
    assert boom.status_code == 500
    assert boom.headers["content-type"] == "application/problem+json"
    assert boom.json()["title"] == "Internal Server Error"

    if factory == "dev":
        spec = app.openapi()
        get_op = spec["paths"]["/items/{item_id}"]["get"]
        assert get_op["responses"]["200"]["content"] == {
            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
        }
        assert get_op["responses"]["422"] == {
            "description": "Validation Error",
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetail"}
                }
            },
        }
        assert get_op["responses"]["404"] == {
            "description": "Item missing",
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetail"}
                }
            },
        }
        # body_type(410, None) returned None -> bodyless response keeping @raises text.
        assert get_op["responses"]["410"] == {"description": "Item gone"}
        assert "ProblemDetail" in spec["components"]["schemas"]

        # Response subclass return annotation keeps 200 bodyless.
        del_op = spec["paths"]["/items/{item_id}"]["delete"]
        assert "content" not in del_op["responses"]["200"]


# @lat: [[tests#Representations#Generic Struct envelope representation]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_representation_envelope_generic_struct(tmp_path, isolated_imports, factory):
    write_project(
        tmp_path,
        {
            "app/representation.py": (
                "from typing import Any\n"
                "import msgspec\n"
                "from chano import JSONRepresentation, Request, Response\n\n"
                "class Document[T](msgspec.Struct):\n"
                "    data: T\n\n"
                "class EnvelopeRepresentation(JSONRepresentation):\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        if status_code < 400:\n"
                "            return 'application/vnd.api+json'\n"
                "        return super().media_type(status_code)\n\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        if status_code < 400:\n"
                "            return Document[returns]\n"
                "        return super().body_type(status_code, returns)\n\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response:\n"
                "        if status_code >= 400 or isinstance(value, Exception):\n"
                "            return super().render(request, value, status_code)\n"
                "        return Response(\n"
                "            msgspec.json.encode(Document(data=value)),\n"
                "            status_code=status_code,\n"
                "            media_type=self.media_type(status_code),\n"
                "        )\n\n"
                "representation = EnvelopeRepresentation()\n"
            ),
            "app/items/route.py": (
                "import msgspec\n"
                "from chano import HTTPException, Response, raises\n\n"
                "class Item(msgspec.Struct):\n"
                "    id: int\n"
                "    name: str\n\n"
                "@raises(404, 'Not found')\n"
                "def GET(item_id: int) -> Item:\n"
                "    if item_id == 0:\n"
                "        raise HTTPException(404, 'missing')\n"
                "    return Item(id=item_id, name='bolt')\n\n"
                "def POST() -> Response:\n"
                "    return Response(b'raw', media_type='text/plain')\n"
            ),
        },
    )
    app_dir = tmp_path / "app"
    app = (
        create_app(app_dir) if factory == "dev" else create_route_app(app_dir, "items")
    )
    client = make_client(app)

    res = client("GET", "/items?item_id=3")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/vnd.api+json"
    assert res.json() == {"data": {"id": 3, "name": "bolt"}}

    # Default JSONRepresentation error handling still runs for errors.
    err = client("GET", "/items?item_id=0")
    assert err.status_code == 404
    assert err.headers["content-type"] == "application/json"
    assert err.json() == {"detail": "missing"}

    # Explicit Response bypasses envelope.
    raw = client("POST", "/items")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/plain")
    assert raw.text == "raw"

    if factory == "dev":
        spec = app.openapi()
        get_op = spec["paths"]["/items"]["get"]
        content = get_op["responses"]["200"]["content"]
        assert list(content) == ["application/vnd.api+json"]
        ref = content["application/vnd.api+json"]["schema"]["$ref"]
        doc_name = ref.removeprefix("#/components/schemas/")
        doc_schema = spec["components"]["schemas"][doc_name]
        assert doc_schema["properties"]["data"] == {"$ref": "#/components/schemas/Item"}
        assert "Item" in spec["components"]["schemas"]

        # Inherited JSONRepresentation.body_type documents 422 and @raises.
        assert get_op["responses"]["422"]["content"] == {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ValidationErrorDetail"}
            }
        }
        assert get_op["responses"]["404"] == {
            "description": "Not found",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorDetail"}
                }
            },
        }
        val_item = spec["components"]["schemas"]["ValidationErrorItem"]
        assert set(val_item["properties"]) == {"loc", "msg"}


# @lat: [[tests#Representations#Nearest wins and no-representation unchanged]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_representation_nearest_wins_and_default(tmp_path, isolated_imports, factory):
    write_project(
        tmp_path,
        {
            "app/api/representation.py": (
                "from typing import Any\n"
                "from chano import (\n"
                "    JSONRepresentation,\n"
                "    JSONResponse,\n"
                "    Request,\n"
                "    Response,\n"
                ")\n\n"
                "class V1Rep(JSONRepresentation):\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        if status_code < 400:\n"
                "            return 'application/vnd.v1+json'\n"
                "        return super().media_type(status_code)\n\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        if status_code < 400:\n"
                "            return dict[str, Any]\n"
                "        return super().body_type(status_code, returns)\n\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response:\n"
                "        if status_code >= 400 or isinstance(value, Exception):\n"
                "            return super().render(request, value, status_code)\n"
                "        return JSONResponse(\n"
                "            {'version': 'v1', 'payload': value},\n"
                "            status_code=status_code,\n"
                "            headers={'content-type': self.media_type(status_code)},\n"
                "        )\n\n"
                "representation = V1Rep()\n"
            ),
            "app/api/(v2)/representation.py": (
                "from typing import Any\n"
                "from chano import (\n"
                "    JSONRepresentation,\n"
                "    JSONResponse,\n"
                "    Request,\n"
                "    Response,\n"
                ")\n\n"
                "class V2Rep(JSONRepresentation):\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        if status_code < 400:\n"
                "            return 'application/vnd.v2+json'\n"
                "        return super().media_type(status_code)\n\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        if status_code < 400:\n"
                "            return dict[str, Any]\n"
                "        return super().body_type(status_code, returns)\n\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response:\n"
                "        if status_code >= 400 or isinstance(value, Exception):\n"
                "            return super().render(request, value, status_code)\n"
                "        return JSONResponse(\n"
                "            {'version': 'v2', 'payload': value},\n"
                "            status_code=status_code,\n"
                "            headers={'content-type': self.media_type(status_code)},\n"
                "        )\n\n"
                "representation = V2Rep()\n"
            ),
            "app/api/items/route.py": (
                "def GET() -> dict[str, int]:\n    return {'n': 1}\n"
            ),
            "app/api/(v2)/nested/route.py": (
                "def GET() -> dict[str, int]:\n    return {'n': 2}\n"
            ),
            "app/plain/route.py": (
                "from chano import HTTPException, raises\n\n"
                "@raises(404, 'Missing')\n"
                "def GET(n: int) -> dict[str, int]:\n"
                "    if n == 0:\n"
                "        raise HTTPException(404, 'nope')\n"
                "    return {'n': n}\n"
            ),
        },
    )
    app_dir = tmp_path / "app"

    def serve(path: str, function: str) -> httpx.Response:
        app = (
            create_app(app_dir)
            if factory == "dev"
            else create_route_app(app_dir, function)
        )
        return make_client(app)("GET", path)

    # Outer representation applies to /api/items.
    v1 = serve("/api/items", "api/items")
    assert v1.headers["content-type"] == "application/vnd.v1+json"
    assert v1.json() == {"version": "v1", "payload": {"n": 1}}

    # Nearest (route-group) representation replaces outer; no chaining or merging.
    v2 = serve("/api/nested", "api/nested")
    assert v2.headers["content-type"] == "application/vnd.v2+json"
    assert v2.json() == {"version": "v2", "payload": {"n": 2}}

    # Route outside any representation keeps default wire format and schema.
    plain = serve("/plain?n=5", "plain")
    assert plain.headers["content-type"] == "application/json"
    assert plain.json() == {"n": 5}
    plain_404 = serve("/plain?n=0", "plain")
    assert plain_404.status_code == 404
    assert plain_404.headers["content-type"] == "application/json"
    assert plain_404.json() == {"detail": "nope"}

    if factory == "dev":
        app = create_app(app_dir)
        # Unmatched path 404 stays default JSON mapping.
        unmatched = make_client(app)("GET", "/api/does-not-exist")
        assert unmatched.status_code == 404
        assert unmatched.headers["content-type"] == "application/json"
        assert unmatched.json() == {"detail": "Not Found"}

        spec = app.openapi()
        assert list(
            spec["paths"]["/api/items"]["get"]["responses"]["200"]["content"]
        ) == ["application/vnd.v1+json"]
        assert list(
            spec["paths"]["/api/nested"]["get"]["responses"]["200"]["content"]
        ) == ["application/vnd.v2+json"]
        plain_op = spec["paths"]["/plain"]["get"]
        assert plain_op["responses"]["200"]["content"] == {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": {"type": "integer"},
                }
            }
        }
        assert plain_op["responses"]["422"] == {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {"type": "array", "items": {"type": "object"}}
                        },
                    }
                }
            },
        }
        assert plain_op["responses"]["404"] == {"description": "Missing"}


# @lat: [[tests#Representations#Error boundaries run before representation error]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_representation_error_boundary_order(tmp_path, isolated_imports, factory):
    write_project(
        tmp_path,
        {
            "app/representation.py": (
                "from chano import (\n"
                "    JSONRepresentation,\n"
                "    JSONResponse,\n"
                "    Request,\n"
                "    Response,\n"
                ")\n\n"
                "class Rep(JSONRepresentation):\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        if status_code >= 400:\n"
                "            return 'application/problem+json'\n"
                "        return 'application/json'\n\n"
                "    def render(\n"
                "        self, request: Request, value, status_code: int\n"
                "    ) -> Response:\n"
                "        if isinstance(value, Exception):\n"
                "            payload = {\n"
                "                'by': 'representation',\n"
                "                'type': type(value).__name__,\n"
                "                'msg': str(value),\n"
                "            }\n"
                "            return JSONResponse(\n"
                "                payload,\n"
                "                status_code=502,\n"
                "                headers={\n"
                "                    'content-type': self.media_type(status_code),\n"
                "                },\n"
                "            )\n"
                "        if value == 'bad-success':\n"
                "            raise ValueError('success failed')\n"
                "        return JSONResponse(\n"
                "            {'ok': value}, status_code=status_code\n"
                "        )\n\n"
                "representation = Rep()\n"
            ),
            "app/error.py": (
                "from chano import HTTPException, JSONResponse\n\n"
                "def handle_error(request, exc):\n"
                "    if request.query_params.get('catch') == ['1']:\n"
                "        payload = {\n"
                "            'by': 'error.py',\n"
                "            'type': type(exc).__name__,\n"
                "            'msg': str(exc),\n"
                "        }\n"
                "        return JSONResponse(payload, status_code=418)\n"
                "    if request.query_params.get('replace') == ['1']:\n"
                "        raise HTTPException(409, 'replaced by error.py')\n"
                "    raise\n"
            ),
            "app/route.py": (
                "def GET(mode: str = 'ok'):\n"
                "    if mode == 'boom':\n"
                "        raise RuntimeError('handler failed')\n"
                "    if mode == 'bad-success':\n"
                "        return 'bad-success'\n"
                "    return mode\n"
            ),
        },
    )
    app_dir = tmp_path / "app"
    app = (
        create_app(app_dir) if factory == "dev" else create_route_app(app_dir, "index")
    )
    client = make_client(app)

    # error.py handles the error first when it returns a Response.
    caught = client("GET", "/?mode=boom&catch=1")
    assert caught.status_code == 418
    assert caught.json() == {
        "by": "error.py",
        "type": "RuntimeError",
        "msg": "handler failed",
    }

    # Re-raising in error.py falls through to representation.render.
    reraised = client("GET", "/?mode=boom")
    assert reraised.status_code == 502
    assert reraised.headers["content-type"] == "application/problem+json"
    assert reraised.json() == {
        "by": "representation",
        "type": "RuntimeError",
        "msg": "handler failed",
    }

    # Replacing the exception in error.py sends it to representation.render.
    replaced = client("GET", "/?mode=boom&replace=1")
    assert replaced.status_code == 502
    assert replaced.json() == {
        "by": "representation",
        "type": "HTTPException",
        "msg": "409: replaced by error.py",
    }

    # An exception raised by representation.render enters error.py first.
    success_err_caught = client("GET", "/?mode=bad-success&catch=1")
    assert success_err_caught.status_code == 418
    assert success_err_caught.json() == {
        "by": "error.py",
        "type": "ValueError",
        "msg": "success failed",
    }
    success_err_rep = client("GET", "/?mode=bad-success")
    assert success_err_rep.status_code == 502
    assert success_err_rep.json() == {
        "by": "representation",
        "type": "ValueError",
        "msg": "success failed",
    }


# @lat: [[tests#Representations#Invalid representation.py and conflicting paths]]
@pytest.mark.parametrize(
    "source",
    [
        "x = 1\n",
        "representation = 123\n",
        "from chano import JSONRepresentation\nrepresentation = JSONRepresentation\n",
        (
            "from chano import JSONResponse\n"
            "class Bad:\n"
            "    media_type = 'application/json'\n"
            "    response_type = JSONResponse\n"
            "    def body_type(self, code, r): return r\n"
            "    def render(self, req, val, code): ...\n"
            "representation = Bad()\n"
        ),
        (
            "from chano import JSONResponse\n"
            "class Bad:\n"
            "    response_type = JSONResponse\n"
            "    def media_type(self, code): return 'application/json'\n"
            "    body_type = 'not-callable'\n"
            "    def render(self, req, val, code): ...\n"
            "representation = Bad()\n"
        ),
        (
            "class Bad:\n"
            "    def media_type(self, code): return 'application/json'\n"
            "    def body_type(self, code, r): return r\n"
            "    def render(self, req, val, code): ...\n"
            "representation = Bad()\n"
        ),
        (
            "class Bad:\n"
            "    response_type = int\n"
            "    def media_type(self, code): return 'application/json'\n"
            "    def body_type(self, code, r): return r\n"
            "    def render(self, req, val, code): ...\n"
            "representation = Bad()\n"
        ),
        (
            "from chano import JSONResponse\n"
            "class Bad:\n"
            "    response_type = (JSONResponse, int)\n"
            "    def media_type(self, code): return 'application/json'\n"
            "    def body_type(self, code, r): return r\n"
            "    def render(self, req, val, code): ...\n"
            "representation = Bad()\n"
        ),
    ],
)
def test_representation_invalid_and_conflicting(tmp_path, isolated_imports, source):
    write_project(
        tmp_path,
        {
            "app/(group)/representation.py": source,
            "app/(group)/a/route.py": "def GET():\n    return {}\n",
        },
    )
    file = (tmp_path / "app" / "(group)" / "representation.py").resolve()
    for make_app in [
        lambda: create_app(tmp_path / "app"),
        lambda: create_route_app(tmp_path / "app", "a"),
    ]:
        with pytest.raises(TypeError, match=re.escape(str(file))):
            make_app()

    # Direct Chano.add_route rejects non-Representation values too.
    app = Chano(docs=False)
    bad_rep: Any = "bad"
    with pytest.raises(TypeError, match="representation"):
        app.add_route("/x", lambda: {}, ["GET"], representation=bad_rep)

    # Conflicting representations on one URL path raise ValueError.
    conflict_dir = tmp_path / "conflict"
    write_project(
        conflict_dir,
        {
            "app/(a)/representation.py": (
                "from chano import JSONRepresentation\n"
                "representation = JSONRepresentation()\n"
            ),
            "app/(a)/x/route.py": "def GET():\n    return {}\n",
            "app/(b)/x/route.py": "def POST():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="conflicting representation for route '/x'"):
        create_app(conflict_dir / "app")


# @lat: [[tests#Representations#Broken representation error and non-Response fallback]]
def test_representation_broken_fallback(capsys):
    class RaisingErrorRep(JSONRepresentation):
        def render(self, request: Request, value: Any, status_code: int) -> Response:
            if isinstance(value, Exception):
                raise RuntimeError("error renderer crashed")
            return super().render(request, value, status_code)

    class NonResponseRep(JSONRepresentation):
        def render(self, request: Request, value: Any, status_code: int) -> Any:
            if isinstance(value, Exception):
                return "not a response"
            return {"not": "a response"}

    class NonResponseSuccessRep(JSONRepresentation):
        def render(self, request: Request, value: Any, status_code: int) -> Any:
            if isinstance(value, Exception):
                return JSONResponse(
                    {"type": type(value).__name__, "msg": str(value)}, status_code=500
                )
            return 42

    def fail():
        raise HTTPException(404, "missing")

    def ok():
        return {"ok": True}

    app = Chano(docs=False)
    app.add_route("/raise", fail, ["GET"], representation=RaisingErrorRep())
    app.add_route("/nonresp", ok, ["GET"], representation=NonResponseRep())
    app.add_route(
        "/nonresp-success", ok, ["GET"], representation=NonResponseSuccessRep()
    )
    client = make_client(app)

    # Raising inside error representation.render falls back to error_response (500).
    res1 = client("GET", "/raise")
    assert res1.status_code == 500
    assert res1.text == "Internal Server Error"
    err1 = capsys.readouterr().err
    assert "error renderer crashed" in err1

    # Non-Response from normal representation.render raises TypeError naming the
    # representation and passes into error representation.render.
    res2 = client("GET", "/nonresp-success")
    assert res2.status_code == 500
    body2 = res2.json()
    assert body2["type"] == "TypeError"
    assert "NonResponseSuccessRep" in body2["msg"]

    # Non-Response from error representation.render raises TypeError naming the
    # representation and falls back to error_response (500).
    res3 = client("GET", "/nonresp")
    assert res3.status_code == 500
    assert res3.text == "Internal Server Error"
    err3 = capsys.readouterr().err
    assert "TypeError" in err3
    assert "NonResponseRep" in err3


# @lat: [[tests#Representations#Sync and async representations]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_representation_sync_and_async(tmp_path, isolated_imports, factory):
    assert isinstance(JSONRepresentation(), Representation)

    write_project(
        tmp_path,
        {
            "app/sync/representation.py": (
                "from typing import Any\n"
                "from chano import JSONResponse, Request, Response\n\n"
                "class SyncRep:\n"
                "    response_type = JSONResponse\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        return 'application/json'\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        return dict[str, Any] if status_code >= 400 else returns\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response:\n"
                "        if isinstance(value, Exception):\n"
                "            return JSONResponse(\n"
                "                {'mode': 'sync', 'err': str(value)}, status_code\n"
                "            )\n"
                "        return JSONResponse(\n"
                "            {'mode': 'sync', 'value': value}, status_code\n"
                "        )\n\n"
                "representation = SyncRep()\n"
            ),
            "app/sync/route.py": (
                "from chano import HTTPException\n"
                "def GET(fail: bool = False):\n"
                "    if fail:\n"
                "        raise HTTPException(400, 'bad')\n"
                "    return 'hi'\n"
            ),
            "app/async_rep/representation.py": (
                "import asyncio\n"
                "from typing import Any\n"
                "from chano import (\n"
                "    JSONRepresentation,\n"
                "    JSONResponse,\n"
                "    Request,\n"
                "    Response,\n"
                ")\n\n"
                "class AsyncRep(JSONRepresentation):\n"
                "    async def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response:\n"
                "        await asyncio.sleep(0)\n"
                "        if isinstance(value, Exception):\n"
                "            return JSONResponse(\n"
                "                {'mode': 'async', 'err': str(value)}, status_code\n"
                "            )\n"
                "        return JSONResponse(\n"
                "            {'mode': 'async', 'value': value}, status_code\n"
                "        )\n\n"
                "representation = AsyncRep()\n"
            ),
            "app/async_rep/route.py": (
                "from chano import HTTPException\n"
                "async def GET(fail: bool = False):\n"
                "    if fail:\n"
                "        raise HTTPException(400, 'bad')\n"
                "    return 'hi'\n"
            ),
        },
    )
    app_dir = tmp_path / "app"

    for name in ("sync", "async_rep"):
        expected = "sync" if name == "sync" else "async"
        app = (
            create_app(app_dir) if factory == "dev" else create_route_app(app_dir, name)
        )
        client = make_client(app)
        assert client("GET", f"/{name}").json() == {"mode": expected, "value": "hi"}
        err = client("GET", f"/{name}?fail=true")
        assert err.status_code == 400
        assert err.json() == {"mode": expected, "err": "400: bad"}


# @lat: [[tests#Representations#Build trace roots and bundling]]
def test_representation_build_trace_roots(tmp_path, isolated_imports, monkeypatch):
    store_files: dict[str, str] = {}
    for dist in (
        make_dist("chano", "chano"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("rep-pkg", "rep_pkg"),
    ):
        store_files.update(dist)

    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(vendor_dir, store_files)

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    project = tmp_path / "reps"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "reps"\nversion = "0"\n',
            "app/(api)/representation.py": (
                "from typing import Any\n"
                "import msgspec\n"
                "from chano import JSONRepresentation, Request, Response\n\n"
                "class Problem(msgspec.Struct):\n"
                "    detail: str\n\n"
                "class ApiRep(JSONRepresentation):\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        if status_code >= 400:\n"
                "            return 'application/problem+json'\n"
                "        return 'application/json'\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        return Problem if status_code >= 400 else returns\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response:\n"
                "        if isinstance(value, Exception):\n"
                "            import rep_pkg\n"
                "            return Response(\n"
                "                msgspec.json.encode(Problem(detail=str(value))),\n"
                "                status_code=500,\n"
                "                media_type=self.media_type(status_code),\n"
                "            )\n"
                "        return super().render(request, value, status_code)\n\n"
                "representation = ApiRep()\n"
            ),
            "app/(api)/items/route.py": (
                "from chano import raises\n\n"
                "@raises(404, 'Item not found')\n"
                "def GET(item_id: int) -> dict[str, int]:\n"
                "    return {'id': item_id}\n"
            ),
            "app/public/route.py": (
                "def GET() -> dict[str, bool]:\n    return {'ok': True}\n"
            ),
        },
    )
    app = (project / "app").resolve()
    plan = plan_build(project, platform=None)
    by_name = {spec.name: spec for spec in plan.functions}
    assert by_name["items"].trace_roots == (
        app / "(api)" / "items" / "route.py",
        app / "(api)" / "representation.py",
    )
    assert by_name["public"].trace_roots == (app / "public" / "route.py",)

    built = json.loads({f.path: f.content for f in plan.static_files}["openapi.json"])
    items_404 = built["paths"]["/items"]["get"]["responses"]["404"]
    assert items_404 == {
        "description": "Item not found",
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/Problem"}
            }
        },
    }
    assert "404" not in built["paths"]["/public"]["get"]["responses"]

    out = build(project, platform=None, output=tmp_path / "out")

    def bundled(function: str) -> set[str]:
        vendor = out / "functions" / f"{function}.func" / "_vendor"
        return set(build_module.index_store(vendor).distributions)

    assert bundled("items") == {"chano", "vercel-runtime", "rep-pkg"}
    assert bundled("public") == {"chano", "vercel-runtime"}


# @lat: [[tests#Representations#Type-keyed representations and Response passthrough]]
def test_type_keyed_representation_and_passthrough():
    class Envelope[T](msgspec.Struct):
        data: T

    class Item(msgspec.Struct):
        id: int

    class EnvelopeRep(JSONRepresentation):
        def media_type(self, status_code: int) -> str:
            if status_code < 400:
                return "application/vnd.api+json"
            return super().media_type(status_code)

        def body_type(self, status_code: int, returns: Any) -> Any:
            if status_code < 400:
                return Envelope[returns]
            return super().body_type(status_code, returns)

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return Response(
                msgspec.json.encode(Envelope(data=value)),
                status_code=status_code,
                media_type=self.media_type(status_code),
                headers={"x-rep": "envelope"},
            )

    class HTMLRep(JSONRepresentation):
        response_type = HTMLResponse

        def media_type(self, status_code: int) -> str:
            return "text/html" if status_code < 400 else super().media_type(status_code)

        def body_type(self, status_code: int, returns: Any) -> Any:
            if status_code < 400:
                return returns
            return super().body_type(status_code, returns)

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return HTMLResponse(
                f"<main>{value}</main>",
                status_code=status_code,
                headers={"x-rep": "html"},
            )

    class MultiRep(JSONRepresentation):
        response_type = (JSONResponse, PlainTextResponse)

        def media_type(self, status_code: int) -> str:
            if status_code < 400:
                return "text/plain"
            return super().media_type(status_code)

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return PlainTextResponse(f"wrapped:{value}", status_code=status_code)

    def plain() -> Item:
        return Item(id=1)

    def redirect() -> RedirectResponse:
        return RedirectResponse("/dest", status_code=302)

    def html() -> HTMLResponse[str]:
        return HTMLResponse("<p>hi</p>")

    def text() -> PlainTextResponse[str]:
        return PlainTextResponse("hello")

    def stream() -> StreamingResponse:
        return StreamingResponse(iter([b"a", b"b"]), media_type="text/plain")

    def sse() -> SSEResponse:
        return SSEResponse(["tick"])

    def raw_resp() -> Response[bytes]:
        return Response(b"raw", media_type="application/octet-stream")

    app = Chano(docs=False)
    rep = EnvelopeRep()
    app.add_route("/plain", plain, ["GET"], representation=rep)
    app.add_route("/redirect", redirect, ["GET"], representation=rep)
    app.add_route("/html", html, ["GET"], representation=rep)
    app.add_route("/text", text, ["GET"], representation=rep)
    app.add_route("/stream", stream, ["GET"], representation=rep)
    app.add_route("/sse", sse, ["GET"], representation=rep)
    app.add_route("/raw", raw_resp, ["GET"], representation=rep)

    # HTML-keyed representation: plain returns fall back to JSONResponse;
    # HTMLResponse matches; JSONResponse passes through untouched.
    html_rep = HTMLRep()
    app.add_route("/html-plain", plain, ["GET"], representation=html_rep)
    app.add_route("/html-page", html, ["GET"], representation=html_rep)
    app.add_route(
        "/html-json",
        lambda: JSONResponse({"raw": True}, status_code=201),
        ["GET"],
        representation=html_rep,
    )

    # Tuple response_type matches both JSONResponse (and plain) and PlainTextResponse.
    multi_rep = MultiRep()
    app.add_route("/multi-plain", lambda: "ok", ["GET"], representation=multi_rep)
    app.add_route("/multi-text", text, ["GET"], representation=multi_rep)
    app.add_route("/multi-html", html, ["GET"], representation=multi_rep)

    client = make_client(app)

    r_plain = client("GET", "/plain")
    assert r_plain.status_code == 200
    assert r_plain.headers["content-type"] == "application/vnd.api+json"
    assert r_plain.json() == {"data": {"id": 1}}

    r_redirect = client("GET", "/redirect")
    assert r_redirect.status_code == 302
    assert r_redirect.headers["location"] == "/dest"
    assert "x-rep" not in r_redirect.headers

    r_html = client("GET", "/html")
    assert r_html.status_code == 200
    assert r_html.headers["content-type"] == "text/html; charset=utf-8"
    assert r_html.text == "<p>hi</p>"

    r_text = client("GET", "/text")
    assert r_text.status_code == 200
    assert r_text.headers["content-type"] == "text/plain; charset=utf-8"
    assert r_text.text == "hello"

    r_stream = client("GET", "/stream")
    assert r_stream.status_code == 200
    assert r_stream.text == "ab"

    r_sse = client("GET", "/sse")
    assert r_sse.status_code == 200
    assert r_sse.headers["content-type"] == "text/event-stream"
    assert "data: tick" in r_sse.text

    r_raw = client("GET", "/raw")
    assert r_raw.status_code == 200
    assert r_raw.headers["content-type"] == "application/octet-stream"
    assert r_raw.content == b"raw"

    r_html_plain = client("GET", "/html-plain")
    assert r_html_plain.status_code == 200
    assert r_html_plain.headers["content-type"] == "application/json"
    assert r_html_plain.json() == {"id": 1}

    r_html_page = client("GET", "/html-page")
    assert r_html_page.status_code == 200
    assert r_html_page.headers["x-rep"] == "html"
    assert r_html_page.text == "<main><p>hi</p></main>"

    r_html_json = client("GET", "/html-json")
    assert r_html_json.status_code == 201
    assert r_html_json.json() == {"raw": True}
    assert "x-rep" not in r_html_json.headers

    assert client("GET", "/multi-plain").text == "wrapped:ok"
    assert client("GET", "/multi-text").text == "wrapped:hello"
    assert client("GET", "/multi-html").text == "<p>hi</p>"

    spec = app.openapi()
    # Non-matching HTMLResponse[str] emits text/html without EnvelopeRep wrapping.
    assert spec["paths"]["/html"]["get"]["responses"]["200"]["content"] == {
        "text/html": {"schema": {"type": "string"}}
    }
    # Non-matching plain return under HTMLRep falls back to application/json.
    assert spec["paths"]["/html-plain"]["get"]["responses"]["200"]["content"] == {
        "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
    }
    # Unparameterized RedirectResponse / StreamingResponse have no content schema.
    assert "content" not in spec["paths"]["/redirect"]["get"]["responses"]["200"]
    assert "content" not in spec["paths"]["/stream"]["get"]["responses"]["200"]


# @lat: [[tests#Representations#Generic JSONResponse and lazy rendering]]
def test_generic_json_response_with_representation_and_openapi():
    class Envelope[T](msgspec.Struct):
        data: T

    class Item(msgspec.Struct):
        id: int
        name: str

    class CustomObject:
        def __init__(self, label: str) -> None:
            self.label = label

    class EnvelopeRep(JSONRepresentation):
        def media_type(self, status_code: int) -> str:
            if status_code < 400:
                return "application/vnd.api+json"
            return super().media_type(status_code)

        def body_type(self, status_code: int, returns: Any) -> Any:
            if status_code >= 400:
                return super().body_type(status_code, returns)
            if returns is CustomObject:
                return Envelope[dict[str, str]]
            return Envelope[returns]

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            payload = (
                {"label": value.label} if isinstance(value, CustomObject) else value
            )
            return Response(
                msgspec.json.encode(Envelope(data=payload)),
                status_code=status_code,
                media_type=self.media_type(status_code),
                headers={"x-from-rep": "yes", "x-override": "rep"},
            )

    def create(item: Item) -> Annotated[JSONResponse[Item], Status(201)]:
        res = JSONResponse(
            item,
            status_code=201,
            headers={"X-Request-Id": "req-1", "X-Override": "handler"},
        )
        res.set_cookie("sid", "abc123", max_age=60)
        res.delete_cookie("old")
        return res

    def custom_ct() -> JSONResponse[Item]:
        return JSONResponse(
            Item(id=2, name="ct"),
            headers={"Content-Type": "application/vnd.api+json; profile=v2"},
        )

    def lazy_custom() -> JSONResponse[CustomObject]:
        # CustomObject is not msgspec-encodable; lazy rendering lets
        # EnvelopeRep.success handle result.content without failing eagerly.
        return JSONResponse(CustomObject("ok"), status_code=202)

    def unserializable() -> JSONResponse[Any]:
        return JSONResponse(object())

    app = Chano(docs=False)
    rep = EnvelopeRep()
    app.add_route("/wrapped", create, ["POST"], representation=rep)
    app.add_route("/custom-ct", custom_ct, ["GET"], representation=rep)
    app.add_route("/lazy", lazy_custom, ["GET"], representation=rep)
    app.add_route("/plain", create, ["POST"])
    app.add_route(
        "/broken",
        unserializable,
        ["GET"],
        error_handlers=[lambda req, exc: PlainTextResponse(type(exc).__name__, 500)],
    )
    client = make_client(app)

    res = client("POST", "/wrapped", json={"id": 1, "name": "widget"})
    assert res.status_code == 201
    assert res.headers["content-type"] == "application/vnd.api+json"
    assert res.headers["content-length"] == str(len(res.content))
    assert res.headers["x-from-rep"] == "yes"
    assert res.headers["x-request-id"] == "req-1"
    assert res.headers["x-override"] == "handler"
    cookies = res.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert any(c.startswith("sid=abc123;") for c in cookies)
    assert any(c.startswith('old="";') and "Max-Age=0" in c for c in cookies)
    assert res.json() == {"data": {"id": 1, "name": "widget"}}

    res_ct = client("GET", "/custom-ct")
    assert res_ct.status_code == 200
    assert res_ct.headers["content-type"] == "application/vnd.api+json; profile=v2"
    assert res_ct.json() == {"data": {"id": 2, "name": "ct"}}

    res_lazy = client("GET", "/lazy")
    assert res_lazy.status_code == 202
    assert res_lazy.json() == {"data": {"label": "ok"}}

    # Without representation, JSONResponse[Item] keeps cookies and headers.
    res_plain = client("POST", "/plain", json={"id": 3, "name": "bare"})
    assert res_plain.status_code == 201
    assert res_plain.headers["content-type"] == "application/json"
    assert res_plain.json() == {"id": 3, "name": "bare"}
    assert len(res_plain.headers.get_list("set-cookie")) == 2

    # Serialization failure on a non-intercepted JSONResponse hits error boundary.
    res_broken = client("GET", "/broken")
    assert res_broken.status_code == 500
    assert res_broken.text == "TypeError"

    spec = app.openapi()
    wrapped_201 = spec["paths"]["/wrapped"]["post"]["responses"]["201"]
    assert wrapped_201["description"] == "Created"
    assert wrapped_201["content"]["application/vnd.api+json"]["schema"] == {
        "$ref": "#/components/schemas/Envelope_Item_"
    }
    plain_201 = spec["paths"]["/plain"]["post"]["responses"]["201"]
    assert plain_201["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Item"
    }


# @lat: [[tests#Representations#Annotated Status unions and bodyless statuses]]
def test_annotated_status_unions_type_alias_and_bodyless():
    class Envelope[T](msgspec.Struct):
        data: T

    class Item(msgspec.Struct):
        id: int

    class Job(msgspec.Struct):
        job_id: str

    class AltItem(msgspec.Struct, tag=True):
        alt: bool

    class TaggedItem(msgspec.Struct, tag=True):
        id: int

    class EnvelopeRep(JSONRepresentation):
        def media_type(self, status_code: int) -> str:
            if status_code < 400:
                return "application/vnd.api+json"
            return super().media_type(status_code)

        def body_type(self, status_code: int, returns: Any) -> Any:
            if status_code < 400:
                return Envelope[returns]
            return super().body_type(status_code, returns)

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return Response(
                msgspec.json.encode(Envelope(data=value)),
                status_code=status_code,
                media_type=self.media_type(status_code),
            )

    type Created[T] = Annotated[T, Status(201, description="Resource created")]
    type NoContent = Annotated[None, Status(204)]
    type Outcome[T, U] = (
        Annotated[T, Status(201)] | Annotated[U, Status(202, description="Queued")]
    )

    def create_plain(override: bool = False) -> Created[Item]:
        if override:
            override_resp: Any = JSONResponse(Item(id=9), status_code=200)
            return override_resp
        return Item(id=1)

    def create_outcome(
        async_mode: bool = False,
    ) -> Outcome[JSONResponse[Item], JSONResponse[Job]]:
        if async_mode:
            return JSONResponse(Job(job_id="j1"), status_code=202)
        return JSONResponse(Item(id=2), status_code=201)

    def create_shared() -> (
        Annotated[TaggedItem, Status(201)] | Annotated[AltItem, Status(201)]
    ):
        return AltItem(alt=True)

    def delete_item() -> NoContent:
        return None

    def not_modified() -> Annotated[None, Status(304)]:
        return None

    def constrained() -> Annotated[str, msgspec.Meta(min_length=3), Status(201)]:
        return "abc"

    @representation(Raw)
    @status_code(201)
    def opted_out() -> Item:
        return Item(id=8)

    app = Chano(docs=False)
    rep = EnvelopeRep()
    app.add_route("/created", create_plain, ["POST"], representation=rep)
    app.add_route("/created-plain", create_plain, ["POST"])
    app.add_route("/outcome", create_outcome, ["POST"], representation=rep)
    app.add_route("/shared", create_shared, ["POST"], representation=rep)
    app.add_route("/delete", delete_item, ["DELETE"], representation=rep)
    app.add_route("/not-modified", not_modified, ["GET"], representation=rep)
    app.add_route("/constrained", constrained, ["POST"])
    app.add_route("/opted-out", opted_out, ["POST"], representation=rep)
    client = make_client(app)

    # Single unambiguous Status(201) applies to plain return (with and without rep).
    r1 = client("POST", "/created")
    assert r1.status_code == 201
    assert r1.headers["content-type"] == "application/vnd.api+json"
    assert r1.json() == {"data": {"id": 1}}

    r1_plain = client("POST", "/created-plain")
    assert r1_plain.status_code == 201
    assert r1_plain.json() == {"id": 1}

    # Explicit Response.status_code overrides annotated Status(201).
    r1_override = client("POST", "/created?override=true")
    assert r1_override.status_code == 200
    assert r1_override.json() == {"data": {"id": 9}}

    # Union statuses with explicit JSONResponse status codes.
    r_201 = client("POST", "/outcome")
    assert r_201.status_code == 201
    assert r_201.json() == {"data": {"id": 2}}
    r_202 = client("POST", "/outcome?async_mode=true")
    assert r_202.status_code == 202
    assert r_202.json() == {"data": {"job_id": "j1"}}

    # Shared status across union branches is unambiguous at runtime.
    r_shared = client("POST", "/shared")
    assert r_shared.status_code == 201
    assert r_shared.json() == {"data": {"type": "AltItem", "alt": True}}

    # Bodyless 204 and 304 send empty body with no content-type or content-length,
    # even under an envelope representation.
    r_del = client("DELETE", "/delete")
    assert r_del.status_code == 204
    assert r_del.content == b""
    assert "content-type" not in r_del.headers
    assert "content-length" not in r_del.headers

    r_304 = client("GET", "/not-modified")
    assert r_304.status_code == 304
    assert r_304.content == b""
    assert "content-type" not in r_304.headers

    # @representation(Raw) + @status_code(201) opts out while keeping 201.
    r_raw = client("POST", "/opted-out")
    assert r_raw.status_code == 201
    assert r_raw.headers["content-type"] == "application/json"
    assert r_raw.json() == {"id": 8}

    # OpenAPI checks.
    spec = app.openapi()
    created_resps = spec["paths"]["/created"]["post"]["responses"]
    assert "200" not in created_resps
    assert created_resps["201"]["description"] == "Resource created"
    assert created_resps["201"]["content"]["application/vnd.api+json"]["schema"] == {
        "$ref": "#/components/schemas/Envelope_Item_"
    }

    outcome_resps = spec["paths"]["/outcome"]["post"]["responses"]
    assert "200" not in outcome_resps
    assert set(outcome_resps) == {"201", "202", "422"}
    assert outcome_resps["201"]["description"] == "Created"
    assert outcome_resps["201"]["content"]["application/vnd.api+json"]["schema"] == {
        "$ref": "#/components/schemas/Envelope_Item_"
    }
    assert outcome_resps["202"]["description"] == "Queued"
    assert outcome_resps["202"]["content"]["application/vnd.api+json"]["schema"] == {
        "$ref": "#/components/schemas/Envelope_Job_"
    }

    # Shared-status union generates per-branch schemas under anyOf.
    shared_schema = spec["paths"]["/shared"]["post"]["responses"]["201"]["content"][
        "application/vnd.api+json"
    ]["schema"]
    assert shared_schema == {
        "anyOf": [
            {"$ref": "#/components/schemas/Envelope_TaggedItem_"},
            {"$ref": "#/components/schemas/Envelope_AltItem_"},
        ]
    }

    # Bodyless statuses omit content in OpenAPI even under an envelope representation.
    assert spec["paths"]["/delete"]["delete"]["responses"] == {
        "204": {"description": "No Content"}
    }
    assert spec["paths"]["/not-modified"]["get"]["responses"] == {
        "304": {"description": "Not Modified"}
    }

    # Non-Chano Annotated metadata (msgspec.Meta) is preserved.
    assert spec["paths"]["/constrained"]["post"]["responses"]["201"]["content"] == {
        "application/json": {"schema": {"type": "string", "minLength": 3}}
    }

    # Status / raises / representation validation errors.
    for bad_status in (99, 103, 199, 600):
        with pytest.raises(ValueError, match="status"):
            Status(bad_status)
    bad_types: list[Any] = ["201", True, 201.5, None]
    for bad_type in bad_types:
        with pytest.raises(TypeError, match="status"):
            Status(bad_type)
    bad_desc: Any = 123
    with pytest.raises(TypeError, match="description"):
        Status(201, description=bad_desc)
    with pytest.raises(TypeError, match="description"):
        raises(404, bad_desc)
    bad_rep: Any = "not-a-rep"
    with pytest.raises(TypeError, match="representation"):
        representation(bad_rep)


# @lat: [[tests#Representations#Decorator sugar with future annotations]]
def test_decorator_sugar_with_future_annotations(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/dependencies.py": (
                "from __future__ import annotations\n"
                "from typing import Annotated\n"
                "from chano import Header, HTTPException, Status\n\n"
                "def require_tenant(\n"
                "    x_tenant: Annotated[str, Header()],\n"
                ") -> Annotated[None, Status(403, description='Forbidden tenant')]:\n"
                "    if x_tenant != 'acme':\n"
                "        raise HTTPException(403, 'Forbidden')\n\n"
                "dependencies = [require_tenant]\n"
            ),
            "app/items/route.py": (
                "from __future__ import annotations\n"
                "from typing import Annotated, Any\n"
                "import msgspec\n"
                "from chano import (\n"
                "    Depends,\n"
                "    JSONRepresentation,\n"
                "    JSONResponse,\n"
                "    Request,\n"
                "    Response,\n"
                "    Status,\n"
                "    openapi,\n"
                "    raises,\n"
                "    representation,\n"
                "    status_code,\n"
                ")\n\n"
                "class Envelope[T](msgspec.Struct):\n"
                "    wrapped: T\n\n"
                "class WrapRep(JSONRepresentation):\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        if status_code < 400:\n"
                "            return Envelope[returns]\n"
                "        return super().body_type(status_code, returns)\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response[Any]:\n"
                "        if status_code >= 400 or isinstance(value, Exception):\n"
                "            return super().render(request, value, status_code)\n"
                "        return JSONResponse(\n"
                "            Envelope(wrapped=value), status_code=status_code\n"
                "        )\n\n"
                "def get_actor() -> Annotated[\n"
                "    Actor, Status(401, description='Missing actor')\n"
                "]:\n"
                "    return Actor(name='ada')\n\n"
                "@openapi(summary='Create item', tags=['items'])\n"
                "@raises(409, 'Outer conflict')\n"
                "@raises(409, 'Inner conflict')\n"
                "@status_code(201, description='Item created')\n"
                "@status_code(200)\n"
                "@representation(WrapRep())\n"
                "def POST(\n"
                "    item: Item,\n"
                "    actor: Annotated[Actor, Depends(get_actor)],\n"
                ") -> Item:\n"
                "    return item\n\n"
                "@status_code(204)\n"
                "@raises(404, 'Item missing')\n"
                "def DELETE():\n"
                "    return None\n\n"
                "@raises(404, 'Not found')\n"
                "def GET() -> (\n"
                "    Annotated[JSONResponse[Item], Status(200)]\n"
                "    | Annotated[None, Status(204)]\n"
                "):\n"
                "    return JSONResponse(Item(id=1, name='first'), status_code=200)\n\n"
                "class Actor(msgspec.Struct):\n"
                "    name: str\n\n"
                "class Item(msgspec.Struct):\n"
                "    id: int\n"
                "    name: str\n"
            ),
        },
    )
    app = create_app(tmp_path / "app")
    client = make_client(app)

    r_post = client(
        "POST", "/items", headers={"x-tenant": "acme"}, json={"id": 7, "name": "bolt"}
    )
    assert r_post.status_code == 201
    assert r_post.json() == {"wrapped": {"id": 7, "name": "bolt"}}

    r_del = client("DELETE", "/items", headers={"x-tenant": "acme"})
    assert r_del.status_code == 204
    assert r_del.content == b""

    r_get = client("GET", "/items", headers={"x-tenant": "acme"})
    assert r_get.status_code == 200
    assert r_get.json() == {"id": 1, "name": "first"}

    spec = app.openapi()
    post_op = spec["paths"]["/items"]["post"]
    assert post_op["summary"] == "Create item"
    assert post_op["tags"] == ["items"]
    assert set(post_op["responses"]) == {"201", "401", "403", "409", "422"}
    assert post_op["responses"]["201"]["description"] == "Item created"
    assert post_op["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Envelope_Item_"
    }
    assert post_op["responses"]["401"]["description"] == "Missing actor"
    assert post_op["responses"]["403"]["description"] == "Forbidden tenant"
    assert post_op["responses"]["409"]["description"] == "Outer conflict"

    del_op = spec["paths"]["/items"]["delete"]
    assert "content" not in del_op["responses"]["204"]
    assert del_op["responses"]["204"] == {"description": "No Content"}
    assert del_op["responses"]["404"]["description"] == "Item missing"
    assert del_op["responses"]["403"]["description"] == "Forbidden tenant"

    get_op = spec["paths"]["/items"]["get"]
    assert set(get_op["responses"]) == {"200", "204", "403", "404", "422"}
    assert get_op["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert "content" not in get_op["responses"]["204"]


# @lat: [[tests#Representations#Per-method representation overrides and Raw opt-out]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_per_method_representation_overrides_and_raw_opt_out(
    tmp_path, isolated_imports, factory
):
    write_project(
        tmp_path,
        {
            "app/error.py": ("def handle_error(request, exc):\n    raise\n"),
            "app/representation.py": (
                "from typing import Any\n"
                "import msgspec\n"
                "from chano import (\n"
                "    HTTPException,\n"
                "    JSONRepresentation,\n"
                "    JSONResponse,\n"
                "    Request,\n"
                "    Response,\n"
                ")\n\n"
                "class DefaultBody[T](msgspec.Struct):\n"
                "    source: str\n"
                "    data: T\n\n"
                "class DefaultErr(msgspec.Struct):\n"
                "    source: str\n"
                "    detail: Any\n\n"
                "class DefaultRep(JSONRepresentation):\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        if status_code >= 400:\n"
                "            return 'application/vnd.default-err+json'\n"
                "        return 'application/vnd.default+json'\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        if status_code >= 400:\n"
                "            return DefaultErr\n"
                "        return DefaultBody[returns]\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response[Any]:\n"
                "        if isinstance(value, Exception):\n"
                "            is_http = isinstance(value, HTTPException)\n"
                "            status = value.status_code if is_http else 500\n"
                "            detail = value.detail if is_http else str(value)\n"
                "            return JSONResponse(\n"
                "                DefaultErr(source='folder', detail=detail),\n"
                "                status_code=status,\n"
                "                media_type=self.media_type(status),\n"
                "            )\n"
                "        return JSONResponse(\n"
                "            DefaultBody(source='folder', data=value),\n"
                "            status_code=status_code,\n"
                "            media_type=self.media_type(status_code),\n"
                "        )\n\n"
                "representation = DefaultRep()\n"
            ),
            "app/items/route.py": (
                "from typing import Annotated, Any\n"
                "import msgspec\n"
                "from chano import (\n"
                "    HTTPException,\n"
                "    JSONRepresentation,\n"
                "    JSONResponse,\n"
                "    Raw,\n"
                "    Request,\n"
                "    Response,\n"
                "    Status,\n"
                "    raises,\n"
                "    representation,\n"
                "    status_code,\n"
                ")\n\n"
                "class Item(msgspec.Struct):\n"
                "    id: int\n\n"
                "class CustomBody[T](msgspec.Struct):\n"
                "    custom: T\n\n"
                "class CustomErr(msgspec.Struct):\n"
                "    custom_error: Any\n\n"
                "class CustomRep(JSONRepresentation):\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        if status_code >= 400:\n"
                "            return 'application/vnd.custom-err+json'\n"
                "        return 'application/vnd.custom+json'\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        if status_code >= 400:\n"
                "            return CustomErr\n"
                "        return CustomBody[returns]\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response[Any]:\n"
                "        if isinstance(value, Exception):\n"
                "            is_http = isinstance(value, HTTPException)\n"
                "            status = value.status_code if is_http else 500\n"
                "            detail = value.detail if is_http else str(value)\n"
                "            return JSONResponse(\n"
                "                CustomErr(custom_error=detail),\n"
                "                status_code=status,\n"
                "                media_type=self.media_type(status),\n"
                "            )\n"
                "        return JSONResponse(\n"
                "            CustomBody(custom=value),\n"
                "            status_code=status_code,\n"
                "            media_type=self.media_type(status_code),\n"
                "        )\n\n"
                "CUSTOM = CustomRep()\n\n"
                "@raises(404, 'Missing')\n"
                "def GET(fail: bool = False) -> Item:\n"
                "    if fail:\n"
                "        raise HTTPException(404, 'nope')\n"
                "    return Item(id=1)\n\n"
                "@representation(CUSTOM)\n"
                "@status_code(201)\n"
                "@raises(409, 'Conflict')\n"
                "def POST(fail: bool = False) -> Item:\n"
                "    if fail:\n"
                "        raise HTTPException(409, 'dup')\n"
                "    return Item(id=2)\n\n"
                "def PUT(\n"
                "    fail: bool = False,\n"
                ") -> Annotated[Item, CUSTOM, Status(400, description='Bad')]:\n"
                "    if fail:\n"
                "        raise HTTPException(400, 'bad')\n"
                "    return Item(id=3)\n\n"
                "@representation(None)\n"
                "@raises(404, 'Missing')\n"
                "def DELETE(fail: bool = False) -> Item:\n"
                "    if fail:\n"
                "        raise HTTPException(404, 'raw-missing')\n"
                "    return Item(id=4)\n\n"
                "def PATCH(\n"
                "    fail: bool = False,\n"
                ") -> Annotated[Item, Raw, Status(404, description='Missing')]:\n"
                "    if fail:\n"
                "        raise HTTPException(404, 'raw-patch')\n"
                "    return Item(id=5)\n"
            ),
        },
    )
    app_dir = tmp_path / "app"
    app = (
        create_app(app_dir) if factory == "dev" else create_route_app(app_dir, "items")
    )
    client = make_client(app)

    # GET inherits folder representation.py.
    r_get = client("GET", "/items")
    assert r_get.headers["content-type"] == "application/vnd.default+json"
    assert r_get.json() == {"source": "folder", "data": {"id": 1}}
    r_get_err = client("GET", "/items?fail=true")
    assert r_get_err.status_code == 404
    assert r_get_err.headers["content-type"] == "application/vnd.default-err+json"
    assert r_get_err.json() == {"source": "folder", "detail": "nope"}

    # POST uses @representation(CUSTOM) for both success and errors (past error.py).
    r_post = client("POST", "/items")
    assert r_post.status_code == 201
    assert r_post.headers["content-type"] == "application/vnd.custom+json"
    assert r_post.json() == {"custom": {"id": 2}}
    r_post_err = client("POST", "/items?fail=true")
    assert r_post_err.status_code == 409
    assert r_post_err.headers["content-type"] == "application/vnd.custom-err+json"
    assert r_post_err.json() == {"custom_error": "dup"}

    # PUT uses direct Annotated[Item, CUSTOM, Status(400, ...)] override.
    r_put = client("PUT", "/items")
    assert r_put.headers["content-type"] == "application/vnd.custom+json"
    assert r_put.json() == {"custom": {"id": 3}}
    r_put_err = client("PUT", "/items?fail=true")
    assert r_put_err.status_code == 400
    assert r_put_err.headers["content-type"] == "application/vnd.custom-err+json"
    assert r_put_err.json() == {"custom_error": "bad"}

    # DELETE (@representation(None)) and PATCH (Annotated[..., Raw]) opt out
    # of folder representation.py for both success and errors.
    r_del = client("DELETE", "/items")
    assert r_del.headers["content-type"] == "application/json"
    assert r_del.json() == {"id": 4}
    r_del_err = client("DELETE", "/items?fail=true")
    assert r_del_err.status_code == 404
    assert r_del_err.headers["content-type"] == "application/json"
    assert r_del_err.json() == {"detail": "raw-missing"}

    r_patch = client("PATCH", "/items")
    assert r_patch.headers["content-type"] == "application/json"
    assert r_patch.json() == {"id": 5}
    r_patch_err = client("PATCH", "/items?fail=true")
    assert r_patch_err.status_code == 404
    assert r_patch_err.headers["content-type"] == "application/json"
    assert r_patch_err.json() == {"detail": "raw-patch"}

    # 405 Method Not Allowed still uses folder representation.py.
    r_405 = client("OPTIONS", "/items")
    assert r_405.status_code == 405
    assert r_405.headers["content-type"] == "application/vnd.default-err+json"
    assert r_405.json()["source"] == "folder"

    if factory == "dev":
        ops = app.openapi()["paths"]["/items"]
        assert (
            "application/vnd.default+json" in ops["get"]["responses"]["200"]["content"]
        )
        assert (
            "application/vnd.default-err+json"
            in ops["get"]["responses"]["404"]["content"]
        )
        assert (
            "application/vnd.custom+json" in ops["post"]["responses"]["201"]["content"]
        )
        assert (
            "application/vnd.custom-err+json"
            in ops["post"]["responses"]["409"]["content"]
        )
        assert (
            "application/vnd.custom+json" in ops["put"]["responses"]["200"]["content"]
        )
        assert (
            "application/vnd.custom-err+json"
            in ops["put"]["responses"]["400"]["content"]
        )
        assert ops["delete"]["responses"]["200"]["content"] == {
            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
        }
        assert ops["delete"]["responses"]["404"] == {"description": "Missing"}
        assert ops["patch"]["responses"]["200"]["content"] == {
            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
        }
        assert ops["patch"]["responses"]["404"] == {"description": "Missing"}


# @lat: [[tests#Representations#Bodyless status suppression at wire boundary]]
def test_bodyless_status_suppression_at_wire_boundary():
    success_calls: list[Any] = []

    class TrackingRep(JSONRepresentation):
        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if isinstance(value, Exception):
                return super().render(request, value, status_code)
            success_calls.append(value)
            return JSONResponse({"wrapped": value}, status_code=status_code)

    @status_code(204)
    def plain_payload() -> Any:
        return "payload"

    def json_204() -> Annotated[JSONResponse[None], Status(204)]:
        res = JSONResponse(None, status_code=204, headers={"X-Tag": "ok"})
        res.set_cookie("seen", "1")
        return res

    started = False

    def stream_204() -> StreamingResponse:
        def gen():
            nonlocal started
            started = True
            yield b"never-sent"

        return StreamingResponse(gen(), status_code=204, headers={"X-Stream": "yes"})

    def raw_304() -> Response[bytes]:
        return Response(b"ignored", status_code=304, headers={"ETag": '"v1"'})

    app = Chano(docs=False)
    rep = TrackingRep()
    app.add_route("/plain-204", plain_payload, ["DELETE"], representation=rep)
    app.add_route("/json-204", json_204, ["DELETE"], representation=rep)
    app.add_route("/stream-204", stream_204, ["GET"])
    app.add_route("/raw-304", raw_304, ["GET"])
    client = make_client(app)

    r_plain = client("DELETE", "/plain-204")
    assert r_plain.status_code == 204
    assert r_plain.content == b""
    assert "content-length" not in r_plain.headers
    assert "content-type" not in r_plain.headers

    r_json = client("DELETE", "/json-204")
    assert r_json.status_code == 204
    assert r_json.content == b""
    assert "content-length" not in r_json.headers
    assert "content-type" not in r_json.headers
    assert r_json.headers["x-tag"] == "ok"
    assert any(c.startswith("seen=1;") for c in r_json.headers.get_list("set-cookie"))
    assert success_calls == []

    r_stream = client("GET", "/stream-204")
    assert r_stream.status_code == 204
    assert r_stream.content == b""
    assert "content-length" not in r_stream.headers
    assert r_stream.headers["x-stream"] == "yes"
    assert not started

    r_304 = client("GET", "/raw-304")
    assert r_304.status_code == 304
    assert r_304.content == b""
    assert "content-length" not in r_304.headers
    assert r_304.headers["etag"] == '"v1"'

    # Raw ASGI check for bodyless 204/205/304 stripping Content-Length and body.
    for code in (204, 205, 304):
        resp = Response(b"body", status_code=code, headers={"Content-Length": "4"})
        assert resp.body == b"body"
        sent: list[Any] = []

        async def send(msg: Any, _sent: list[Any] = sent) -> None:
            _sent.append(msg)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        asyncio.run(resp(receive, send))
        start, body_msg = sent[0], sent[1]
        assert start["status"] == code
        assert all(k != b"content-length" for k, _ in start["headers"])
        assert body_msg["body"] == b""

    # Changing status from 204 to 200 in middleware preserves the rendered body.
    async def promote_to_200(request: Request, call_next: Any) -> Response[Any]:
        resp = await call_next(request)
        _ = resp.body
        resp.status_code = 200
        return resp

    promote_app = Chano(docs=False)
    promote_app.add_route(
        "/promote",
        lambda: Response(b"kept", status_code=204, media_type="text/plain"),
        ["GET"],
        middleware=[promote_to_200],
    )
    r_promoted = make_client(promote_app)("GET", "/promote")
    assert r_promoted.status_code == 200
    assert r_promoted.text == "kept"
    assert r_promoted.headers["content-length"] == "4"

    # Explicit Content-Length is preserved on HEAD but overwritten on GET.
    def head_post() -> Response[bytes]:
        resp = Response()
        _ = resp.body
        resp.headers["content-length"] = "7"
        return resp

    def head_ctor() -> Response[bytes]:
        return Response(headers={"Content-Length": "11"})

    def bad_cl_get() -> Response[bytes]:
        return Response(b"x", headers={"Content-Length": "999"})

    head_app = Chano(docs=False)
    head_app.add_route("/head-post", head_post, ["HEAD"])
    head_app.add_route("/head-ctor", head_ctor, ["HEAD"])
    head_app.add_route("/bad-cl", bad_cl_get, ["GET", "HEAD"])
    head_client = make_client(head_app)

    r_head_post = head_client("HEAD", "/head-post")
    assert r_head_post.status_code == 200
    assert r_head_post.content == b""
    assert r_head_post.headers["content-length"] == "7"

    r_head_ctor = head_client("HEAD", "/head-ctor")
    assert r_head_ctor.status_code == 200
    assert r_head_ctor.content == b""
    assert r_head_ctor.headers["content-length"] == "11"

    r_get_cl = head_client("GET", "/bad-cl")
    assert r_get_cl.status_code == 200
    assert r_get_cl.content == b"x"
    assert r_get_cl.headers["content-length"] == "1"

    r_head_cl = head_client("HEAD", "/bad-cl")
    assert r_head_cl.status_code == 200
    assert r_head_cl.content == b""
    assert r_head_cl.headers["content-length"] == "999"


# @lat: [[tests#Representations#Middleware lazy response error boundary]]
def test_middleware_lazy_response_error_boundary(capsys):
    endpoint_error_called = False

    def handle_error(request: Request, exc: Exception) -> Response[Any]:
        nonlocal endpoint_error_called
        endpoint_error_called = True
        return PlainTextResponse("endpoint-error", status_code=418)

    async def bad_middleware(request: Request, call_next: Any) -> Response[Any]:
        return JSONResponse(object())

    app = Chano(docs=False)
    app.add_route(
        "/mw",
        lambda: {"ok": True},
        ["GET"],
        middleware=[bad_middleware],
        error_handlers=[handle_error],
    )
    client = make_client(app)

    res = client("GET", "/mw")
    assert res.status_code == 500
    assert res.text == "Internal Server Error"
    assert not endpoint_error_called
    assert "TypeError" in capsys.readouterr().err


# @lat: [[tests#Representations#Shared media collision and untagged struct union]]
def test_openapi_shared_media_collision_and_untagged_struct_union():
    class UntaggedA(msgspec.Struct):
        a: int

    class UntaggedB(msgspec.Struct):
        b: str

    class Envelope[T](msgspec.Struct):
        data: T

    class TextFromJSONRep(JSONRepresentation):
        def media_type(self, status_code: int) -> str:
            if status_code < 400:
                return "text/plain"
            return super().media_type(status_code)

        def body_type(self, status_code: int, returns: Any) -> Any:
            if status_code < 400:
                return str
            return super().body_type(status_code, returns)

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return PlainTextResponse(str(value), status_code=status_code)

    class EnvelopeRep(JSONRepresentation):
        def media_type(self, status_code: int) -> str:
            if status_code < 400:
                return "application/vnd.api+json"
            return super().media_type(status_code)

        def body_type(self, status_code: int, returns: Any) -> Any:
            if status_code < 400:
                return Envelope[returns]
            return super().body_type(status_code, returns)

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return JSONResponse(
                Envelope(data=value),
                status_code=status_code,
                media_type=self.media_type(status_code),
            )

    def mixed() -> JSONResponse[int] | PlainTextResponse[int]:
        return PlainTextResponse(1)

    def untagged() -> (
        Annotated[UntaggedA, Status(201)] | Annotated[UntaggedB, Status(201)]
    ):
        return UntaggedA(a=1)

    def dedup() -> (
        Annotated[UntaggedA, Status(200)] | Annotated[UntaggedA, Status(200)]
    ):
        return UntaggedA(a=1)

    app = Chano(docs=False)
    app.add_route("/mixed", mixed, ["GET"], representation=TextFromJSONRep())
    app.add_route("/untagged-raw", untagged, ["POST"])
    app.add_route("/untagged-env", untagged, ["POST"], representation=EnvelopeRep())
    app.add_route("/dedup", dedup, ["GET"])

    spec = app.openapi()
    assert spec["paths"]["/mixed"]["get"]["responses"]["200"]["content"] == {
        "text/plain": {"schema": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}
    }
    assert spec["paths"]["/untagged-raw"]["post"]["responses"]["201"]["content"] == {
        "application/json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/UntaggedA"},
                    {"$ref": "#/components/schemas/UntaggedB"},
                ]
            }
        }
    }
    assert spec["paths"]["/untagged-env"]["post"]["responses"]["201"]["content"] == {
        "application/vnd.api+json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/Envelope_UntaggedA_"},
                    {"$ref": "#/components/schemas/Envelope_UntaggedB_"},
                ]
            }
        }
    }
    assert spec["paths"]["/dedup"]["get"]["responses"]["200"]["content"] == {
        "application/json": {"schema": {"$ref": "#/components/schemas/UntaggedA"}}
    }


# @lat: [[tests#Representations#Unhashable rep alias and conflicting union reps]]
def test_unhashable_rep_in_alias_and_conflicting_union_reps():
    @dataclasses.dataclass
    class ConfigurableRep(JSONRepresentation):
        prefix: str = "ok"

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return JSONResponse(
                {"prefix": self.prefix, "value": value}, status_code=status_code
            )

    class Item(msgspec.Struct):
        id: int

    rep_a = ConfigurableRep("v1")
    rep_b = ConfigurableRep("v2")
    type Wrapped[T] = Annotated[T, Status(201), rep_a]

    def create() -> Wrapped[Item]:
        return Item(id=4)

    def agreed_a() -> Annotated[int, rep_a] | str:
        return 1

    def agreed_b() -> str | Annotated[int, rep_a]:
        return "hi"

    def conflict_raw_1() -> Annotated[int, rep_a] | Annotated[str, Raw]:
        return 1

    def conflict_raw_2() -> Annotated[str, Raw] | Annotated[int, rep_a]:
        return "a"

    def conflict_two() -> Annotated[int, rep_a] | Annotated[str, rep_b]:
        return 1

    app = Chano(docs=False)
    app.add_route("/wrapped", create, ["POST"])
    app.add_route("/agreed-a", agreed_a, ["GET"])
    app.add_route("/agreed-b", agreed_b, ["GET"])
    client = make_client(app)

    r = client("POST", "/wrapped")
    assert r.status_code == 201
    assert r.json() == {"prefix": "v1", "value": {"id": 4}}
    assert client("GET", "/agreed-a").json() == {"prefix": "v1", "value": 1}
    assert client("GET", "/agreed-b").json() == {"prefix": "v1", "value": "hi"}

    for bad_handler in (conflict_raw_1, conflict_raw_2, conflict_two):
        with pytest.raises(ValueError, match="conflicting representations"):
            app.add_route("/bad", bad_handler, ["GET"])


# @lat: [[tests#Representations#Header merge constructor cookies and post content-type]]
def test_header_merge_constructor_cookies_and_post_content_type():
    class CookieRep(JSONRepresentation):
        def media_type(self, status_code: int) -> str:
            if status_code < 400:
                return "application/vnd.api+json"
            return super().media_type(status_code)

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return Response(
                msgspec.json.encode({"data": value}),
                status_code=status_code,
                media_type=self.media_type(status_code),
                headers={"Set-Cookie": "from_rep=1; Path=/"},
            )

    def with_cookies() -> JSONResponse[dict[str, int]]:
        res = JSONResponse(
            {"x": 1},
            status_code=201,
            headers={"Set-Cookie": "from_handler=2; Path=/"},
        )
        res.set_cookie("extra", "3")
        return res

    def post_ct() -> JSONResponse[dict[str, int]]:
        res = JSONResponse({"x": 2})
        res.headers["content-type"] = "application/vnd.post+json"
        return res

    app = Chano(docs=False)
    rep = CookieRep()
    app.add_route("/cookies", with_cookies, ["POST"], representation=rep)
    app.add_route("/post-ct", post_ct, ["GET"], representation=rep)
    client = make_client(app)

    r_cookies = client("POST", "/cookies")
    assert r_cookies.status_code == 201
    assert r_cookies.headers["content-type"] == "application/vnd.api+json"
    assert r_cookies.json() == {"data": {"x": 1}}
    cookies = r_cookies.headers.get_list("set-cookie")
    assert len(cookies) == 3
    assert "from_rep=1; Path=/" in cookies
    assert "from_handler=2; Path=/" in cookies
    assert any(c.startswith("extra=3;") for c in cookies)

    r_ct = client("GET", "/post-ct")
    assert r_ct.status_code == 200
    assert r_ct.headers["content-type"] == "application/vnd.post+json"
    assert r_ct.json() == {"data": {"x": 2}}

    # Constructor set-cookie stays in response.headers so callers can pop it.
    plain = Response("ok", headers={"Set-Cookie": "temp=1"})
    assert plain.headers.pop("set-cookie", None) == "temp=1"
    assert "set-cookie" not in plain.headers


# @lat: [[tests#Representations#Decorated partial and nested ForwardRefs in OpenAPI]]
def test_decorated_partial_and_nested_forward_refs(tmp_path, isolated_imports):
    class Item(msgspec.Struct):
        id: int

    @raises(409, "Conflict")
    @status_code(201, description="Created item")
    def base(item_id: int) -> Item:
        return Item(id=item_id)

    bound = raises(410, "Gone")(functools.partial(base, 42))

    app = Chano(docs=False)
    app.add_route("/partial", bound, ["POST"])
    client = make_client(app)

    res = client("POST", "/partial")
    assert res.status_code == 201
    assert res.json() == {"id": 42}

    resps = app.openapi()["paths"]["/partial"]["post"]["responses"]
    assert resps["201"] == {
        "description": "Created item",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
        },
    }
    assert resps["409"] == {"description": "Conflict"}
    assert resps["410"] == {"description": "Gone"}

    # Nested ForwardRefs inside Annotated / unions skip only the unresolved
    # branch while preserving return metadata and resolved sibling branches.
    from typing import ForwardRef

    hidden_ref: Any = ForwardRef("Hidden")

    def only_hidden():
        return {"ok": True}

    only_hidden.__annotations__["return"] = Annotated[
        hidden_ref, "example", Status(201), Status(404, description="Missing")
    ]

    def mixed_hidden():
        return Item(id=7)

    mixed_hidden.__annotations__["return"] = (
        Annotated[hidden_ref, "example", Status(201)]
        | Annotated[list[hidden_ref], Status(201)]
        | Annotated[Item, Status(201)]
    )

    fwd_app = Chano(docs=False)
    fwd_app.add_route("/hidden", only_hidden, ["POST"])
    fwd_app.add_route("/mixed-hidden", mixed_hidden, ["POST"])
    fwd_spec = fwd_app.openapi()
    assert fwd_spec["paths"]["/hidden"]["post"]["responses"] == {
        "201": {"description": "Created"},
        "404": {"description": "Missing"},
    }
    assert fwd_spec["paths"]["/mixed-hidden"]["post"]["responses"]["201"] == {
        "description": "Created",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
        },
    }


# @lat: [[tests#Representations#Native PEP 649 TYPE_CHECKING return annotations]]
@pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="PEP 649 deferred annotations require Python 3.14+",
)
def test_pep649_type_checking_refs(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/tc/route.py": (
                "from typing import Annotated, Any, TYPE_CHECKING\n"
                "from chano import Depends, raises, status_code\n\n"
                "if TYPE_CHECKING:\n"
                "    class OnlyAtTypeCheck:\n"
                "        pass\n\n"
                "@raises(401, 'Unauthorized')\n"
                "def auth(token: str = 'ok') -> OnlyAtTypeCheck:\n"
                "    return token  # type: ignore[return-value]\n\n"
                "@status_code(201)\n"
                "@raises(404, 'Not found')\n"
                "def POST(\n"
                "    actor: Annotated[Any, Depends(auth)],\n"
                ") -> Annotated[OnlyAtTypeCheck, 'example']:\n"
                "    return {'actor': actor}  # type: ignore[return-value]\n"
            )
        },
    )
    tc_app = create_app(tmp_path / "app")
    tc_client = make_client(tc_app)
    r_tc = tc_client("POST", "/tc?token=ada")
    assert r_tc.status_code == 201
    assert r_tc.json() == {"actor": "ada"}

    tc_spec = tc_app.openapi()
    tc_resps = tc_spec["paths"]["/tc"]["post"]["responses"]
    assert tc_resps["201"] == {"description": "Created"}
    assert tc_resps["401"] == {"description": "Unauthorized"}
    assert tc_resps["404"] == {"description": "Not found"}
    assert "422" in tc_resps

    assert sys.version_info >= (3, 14)
    if sys.version_info >= (3, 14):
        import annotationlib

        mod = load_module(tmp_path / "app" / "tc" / "route.py")
        fwd = annotationlib.get_annotations(
            mod.POST, format=annotationlib.Format.FORWARDREF
        )
        assert "return" in fwd
        strs = annotationlib.get_annotations(
            mod.POST, format=annotationlib.Format.STRING
        )
        assert "OnlyAtTypeCheck" in strs["return"]


# @lat: [[tests#Representations#StatusRepresentation composition and dispatch]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_status_representation_composition_and_dispatch(
    tmp_path, isolated_imports, factory
):
    write_project(
        tmp_path,
        {
            "app/representation.py": (
                "from typing import Any\n"
                "import msgspec\n"
                "from chano import (\n"
                "    HTTPException,\n"
                "    JSONResponse,\n"
                "    Request,\n"
                "    RequestValidationError,\n"
                "    Response,\n"
                "    StatusRepresentation,\n"
                ")\n\n"
                "class Envelope[T](msgspec.Struct):\n"
                "    data: T\n\n"
                "class Problem(msgspec.Struct):\n"
                "    title: str\n"
                "    status: int\n"
                "    detail: Any = None\n\n"
                "class EnvelopeRep:\n"
                "    response_type = JSONResponse\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        return 'application/vnd.api+json'\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        return Envelope[returns]\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response[Any]:\n"
                "        return JSONResponse(\n"
                "            Envelope(data=value),\n"
                "            status_code=status_code,\n"
                "            media_type=self.media_type(status_code),\n"
                "        )\n\n"
                "class ProblemRep:\n"
                "    response_type = JSONResponse\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        return 'application/problem+json'\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        return Problem\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response[Any]:\n"
                "        if isinstance(value, HTTPException):\n"
                "            detail = value.detail\n"
                "            headers = value.headers\n"
                "        elif isinstance(value, RequestValidationError):\n"
                "            detail = value.errors\n"
                "            headers = None\n"
                "        else:\n"
                "            detail = str(value)\n"
                "            headers = None\n"
                "        problem = Problem(\n"
                "            title='Error', status=status_code, detail=detail\n"
                "        )\n"
                "        return JSONResponse(\n"
                "            problem,\n"
                "            status_code=status_code,\n"
                "            headers=headers,\n"
                "            media_type=self.media_type(status_code),\n"
                "        )\n\n"
                "class ConflictRep:\n"
                "    response_type = JSONResponse\n"
                "    def media_type(self, status_code: int) -> str:\n"
                "        return 'application/vnd.conflict+json'\n"
                "    def body_type(self, status_code: int, returns: Any) -> Any:\n"
                "        return dict[str, Any]\n"
                "    def render(\n"
                "        self, request: Request, value: Any, status_code: int\n"
                "    ) -> Response[Any]:\n"
                "        return JSONResponse(\n"
                "            {'conflict': True, 'status': status_code},\n"
                "            status_code=status_code,\n"
                "            media_type=self.media_type(status_code),\n"
                "        )\n\n"
                "representation = StatusRepresentation(\n"
                "    {\n"
                "        409: ConflictRep(),\n"
                "        frozenset({200, 201}): EnvelopeRep(),\n"
                "        range(400, 600): ProblemRep(),\n"
                "    }\n"
                ")\n"
            ),
            "app/items/route.py": (
                "import msgspec\n"
                "from chano import HTTPException\n"
                "from chano.status import Conflict, Created, NotFound\n\n"
                "class Item(msgspec.Struct):\n"
                "    id: int\n\n"
                "def POST(item_id: int) -> Created[Conflict[NotFound[Item]]]:\n"
                "    if item_id == 0:\n"
                "        raise HTTPException(404, 'missing')\n"
                "    if item_id == -1:\n"
                "        raise HTTPException(409, 'dup')\n"
                "    return Item(id=item_id)\n"
            ),
        },
    )
    app_dir = tmp_path / "app"
    app = (
        create_app(app_dir) if factory == "dev" else create_route_app(app_dir, "items")
    )
    client = make_client(app)

    ok = client("POST", "/items?item_id=7")
    assert ok.status_code == 201
    assert ok.headers["content-type"] == "application/vnd.api+json"
    assert ok.json() == {"data": {"id": 7}}

    not_found = client("POST", "/items?item_id=0")
    assert not_found.status_code == 404
    assert not_found.headers["content-type"] == "application/problem+json"
    assert not_found.json() == {"title": "Error", "status": 404, "detail": "missing"}

    conflict = client("POST", "/items?item_id=-1")
    assert conflict.status_code == 409
    assert conflict.headers["content-type"] == "application/vnd.conflict+json"
    assert conflict.json() == {"conflict": True, "status": 409}

    bad = client("POST", "/items?item_id=abc")
    assert bad.status_code == 422
    assert bad.headers["content-type"] == "application/problem+json"
    assert bad.json()["status"] == 422

    if factory == "dev":
        resps = app.openapi()["paths"]["/items"]["post"]["responses"]
        assert set(resps) == {"201", "404", "409", "422"}
        assert "application/vnd.api+json" in resps["201"]["content"]
        assert "application/problem+json" in resps["404"]["content"]
        assert "application/vnd.conflict+json" in resps["409"]["content"]
        assert "application/problem+json" in resps["422"]["content"]


# @lat: [[tests#Representations#StatusRepresentation defaults and validation]]
def test_status_representation_defaults_response_types_and_validation():
    class HTMLOnlyRep:
        response_type = HTMLResponse

        def media_type(self, status_code: int) -> str:
            return "text/html"

        def body_type(self, status_code: int, returns: Any) -> Any:
            return returns

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            return HTMLResponse(f"<p>{value}</p>", status_code=status_code)

    class ErrOnlyRep(JSONRepresentation):
        def media_type(self, status_code: int) -> str:
            return "application/problem+json"

        def body_type(self, status_code: int, returns: Any) -> Any:
            return dict[str, str]

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            return JSONResponse(
                {"err": str(value)},
                status_code=status_code,
                media_type=self.media_type(status_code),
            )

    # Empty StatusRepresentation with default=None falls back to JSONResponse.
    empty_rep = StatusRepresentation({}, default=None)
    assert isinstance(empty_rep, Representation)
    assert empty_rep.response_type is JSONResponse
    assert empty_rep.media_type(200) == "application/json"
    assert empty_rep.body_type(200, int) is int
    assert empty_rep.body_type(404, None) is None

    # Heterogeneous response_types: 200 matches HTMLResponse only; 4xx matches JSON.
    mixed = StatusRepresentation(
        {range(200, 400): HTMLOnlyRep(), range(400, 600): ErrOnlyRep()},
        default=None,
    )
    assert mixed.response_type == (HTMLResponse, JSONResponse)

    # Nested StatusRepresentation with default=None leaves unmatched 4xx on default.
    nested = StatusRepresentation(
        {range(200, 400): StatusRepresentation({200: HTMLOnlyRep()}, default=None)},
        default=None,
    )

    def plain_item(q: int = 1) -> dict[str, int]:
        if q == 0:
            raise HTTPException(404, "missing")
        return {"q": q}

    def html_page() -> HTMLResponse[str]:
        return HTMLResponse("hello")

    def html_err() -> HTMLResponse[str]:
        return HTMLResponse("nope", status_code=400)

    @raises(404, "Missing")
    def nested_plain(q: int = 1) -> dict[str, int]:
        if q == 0:
            raise HTTPException(404, "x")
        return {"q": q}

    app = Chano(docs=False)
    app.add_route("/plain", plain_item, ["GET"], representation=mixed)
    app.add_route("/html", html_page, ["GET"], representation=mixed)
    app.add_route("/html-err", html_err, ["GET"], representation=mixed)
    app.add_route("/nested-html", html_page, ["GET"], representation=nested)
    app.add_route("/nested-plain", nested_plain, ["GET"], representation=nested)
    client = make_client(app)

    # Plain JSON return on 200 does not match HTMLOnlyRep -> falls back to JSONResponse.
    r_plain = client("GET", "/plain")
    assert r_plain.status_code == 200
    assert r_plain.headers["content-type"] == "application/json"
    assert r_plain.json() == {"q": 1}

    # HTMLResponse on 200 matches HTMLOnlyRep.
    r_html = client("GET", "/html")
    assert r_html.status_code == 200
    assert r_html.text == "<p>hello</p>"

    # HTMLResponse on 400 resolves to ErrOnlyRep (JSON) -> passes through untouched.
    r_html_err = client("GET", "/html-err")
    assert r_html_err.status_code == 400
    assert r_html_err.text == "nope"

    # Exception on /plain resolves to ErrOnlyRep.
    r_err = client("GET", "/plain?q=0")
    assert r_err.status_code == 404
    assert r_err.headers["content-type"] == "application/problem+json"

    # Nested StatusRepresentation resolves 200 -> HTMLOnlyRep and 404 -> default.
    assert client("GET", "/nested-html").text == "<p>hello</p>"
    r_nested_404 = client("GET", "/nested-plain?q=0")
    assert r_nested_404.status_code == 404
    assert r_nested_404.headers["content-type"] == "application/json"
    assert r_nested_404.json() == {"detail": "x"}

    # OpenAPI reflects per-status response_type matching and default=None fallback.
    spec = app.openapi()
    assert spec["paths"]["/plain"]["get"]["responses"]["200"]["content"] == {
        "application/json": {
            "schema": {"type": "object", "additionalProperties": {"type": "integer"}}
        }
    }
    assert (
        "application/problem+json"
        in (spec["paths"]["/plain"]["get"]["responses"]["422"]["content"])
    )
    assert spec["paths"]["/nested-plain"]["get"]["responses"]["404"] == {
        "description": "Missing"
    }
    assert (
        "application/json"
        in (spec["paths"]["/nested-plain"]["get"]["responses"]["422"]["content"])
    )

    # Validation errors on StatusRepresentation arguments.
    bad_routes: Any = "not-a-mapping"
    with pytest.raises(TypeError, match="mapping"):
        StatusRepresentation(bad_routes)
    bad_key: Any = "200"
    with pytest.raises(TypeError, match="keys"):
        StatusRepresentation({bad_key: JSONRepresentation()})
    bad_bool_key: Any = True
    with pytest.raises(TypeError, match="keys"):
        StatusRepresentation({bad_bool_key: JSONRepresentation()})
    bad_val: Any = "not-a-rep"
    with pytest.raises(TypeError, match="values"):
        StatusRepresentation({200: bad_val})
    with pytest.raises(TypeError, match="default"):
        StatusRepresentation({}, default=bad_val)


# @lat: [[tests#Representations#Unified Status aliases and additive error metadata]]
def test_unified_status_aliases_and_additive_error_metadata():
    from chano import status as status_mod
    from chano.representation import parse_return
    from chano.status import (
        OK,
        Accepted,
        BadGateway,
        BadRequest,
        Conflict,
        Created,
        Forbidden,
        Found,
        Gone,
        InternalServerError,
        MovedPermanently,
        NoContent,
        NotFound,
        NotModified,
        PermanentRedirect,
        SeeOther,
        ServiceUnavailable,
        TemporaryRedirect,
        TooManyRequests,
        Unauthorized,
        UnprocessableContent,
    )

    class Item(msgspec.Struct):
        id: int

    class User(msgspec.Struct):
        name: str

    # Every alias in chano.status expands to Annotated[..., Status(code)].
    expected_codes = {
        OK: 200,
        Created: 201,
        Accepted: 202,
        MovedPermanently: 301,
        Found: 302,
        SeeOther: 303,
        TemporaryRedirect: 307,
        PermanentRedirect: 308,
        BadRequest: 400,
        Unauthorized: 401,
        Forbidden: 403,
        NotFound: 404,
        Conflict: 409,
        Gone: 410,
        UnprocessableContent: 422,
        TooManyRequests: 429,
        InternalServerError: 500,
        BadGateway: 502,
        ServiceUnavailable: 503,
    }
    for alias, code in expected_codes.items():

        def _probe() -> Any:
            return None

        _probe.__annotations__["return"] = alias[Item]
        spec = parse_return(_probe)
        if code < 400:
            assert spec.default_status == code
            assert spec.error_statuses == {}
        else:
            assert spec.default_status is None
            assert spec.branches[0].status_code == 200
            assert code in spec.error_statuses

    assert set(status_mod.__all__) >= {
        "OK",
        "Created",
        "Accepted",
        "NoContent",
        "NotModified",
        "NotFound",
        "Unauthorized",
        "Forbidden",
        "Conflict",
        "UnprocessableContent",
    }

    # Dependency with < 400 (Created) and >= 400 (Unauthorized) only propagates 401.
    def require_user() -> Created[Unauthorized[User]]:
        return User(name="ada")

    # Inherited check with both < 400 and >= 400 only propagates 403.
    def require_admin() -> Accepted[Forbidden[None]]:
        return None

    # NotFound[Item] -> 200 Item + 404 Not Found.
    def get_item(user: Annotated[User, Depends(require_user)]) -> NotFound[Item]:
        return Item(id=1)

    # Created[NotFound[Item]] and NotFound[Created[Item]] -> 201 Item + 404 Not Found.
    def create_a() -> Created[NotFound[Item]]:
        return Item(id=2)

    def create_b() -> NotFound[Created[Item]]:
        return Item(id=3)

    # NotFound[NoContent] -> 204 bodyless + 404 Not Found.
    def delete_item() -> NotFound[NoContent]:
        return None

    # NotModified -> 304 bodyless.
    def check_cache() -> NotModified:
        return None

    # @raises sugar produces the same Status metadata as Annotated[..., Status(...)].
    @raises(409, "Already exists")
    def with_sugar() -> Created[Item]:
        return Item(id=4)

    def with_direct() -> Annotated[
        Created[Item], Status(409, description="Already exists")
    ]:
        return Item(id=4)

    assert parse_return(with_sugar) == parse_return(with_direct)

    # Explicit description in dependency fills in descriptionless NotFound[Item].
    @raises(404, "Detailed missing item")
    def find_item() -> Item:
        return Item(id=5)

    def get_with_dep_desc(item: Annotated[Item, Depends(find_item)]) -> NotFound[Item]:
        return item

    app = Chano(docs=False)
    app.add_route("/get", get_item, ["GET"], dependencies=[[require_admin]])
    app.add_route("/create-a", create_a, ["POST"])
    app.add_route("/create-b", create_b, ["POST"])
    app.add_route("/delete", delete_item, ["DELETE"])
    app.add_route("/cache", check_cache, ["GET"])
    app.add_route("/dep-desc", get_with_dep_desc, ["GET"])
    client = make_client(app)

    # Runtime statuses: NotFound[Item] stays 200; Created[NotFound[Item]] is 201.
    r_get = client("GET", "/get")
    assert r_get.status_code == 200
    assert r_get.json() == {"id": 1}

    assert client("POST", "/create-a").status_code == 201
    assert client("POST", "/create-b").status_code == 201
    assert client("DELETE", "/delete").status_code == 204
    assert client("GET", "/cache").status_code == 304

    openapi = app.openapi()
    get_resps = openapi["paths"]["/get"]["get"]["responses"]
    # Neither Created (201) nor Accepted (202) leaked from deps/checks.
    assert set(get_resps) == {"200", "401", "403", "404"}
    assert get_resps["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert get_resps["401"] == {"description": "Unauthorized"}
    assert get_resps["403"] == {"description": "Forbidden"}
    assert get_resps["404"] == {"description": "Not Found"}

    for path in ("/create-a", "/create-b"):
        resps = openapi["paths"][path]["post"]["responses"]
        assert set(resps) == {"201", "404"}
        assert resps["201"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/Item"
        }
        assert resps["404"] == {"description": "Not Found"}

    assert openapi["paths"]["/delete"]["delete"]["responses"] == {
        "204": {"description": "No Content"},
        "404": {"description": "Not Found"},
    }
    assert openapi["paths"]["/cache"]["get"]["responses"] == {
        "304": {"description": "Not Modified"}
    }
    assert openapi["paths"]["/dep-desc"]["get"]["responses"]["404"] == {
        "description": "Detailed missing item"
    }


# @lat: [[tests#Representations#Nested aliases None rep ambiguous returns and 205]]
def test_nested_alias_none_representation_ambiguous_and_205(capsys):
    from chano.representation import parse_return
    from chano.status import Accepted, Created, NotFound, ResetContent

    class Envelope[T](msgspec.Struct):
        data: T

    class EnvelopeRep(JSONRepresentation):
        def body_type(self, status_code: int, returns: Any) -> Any:
            if status_code < 400:
                return Envelope[returns]
            return super().body_type(status_code, returns)

        def render(
            self, request: Request, value: Any, status_code: int
        ) -> Response[Any]:
            if status_code >= 400 or isinstance(value, Exception):
                return super().render(request, value, status_code)
            return JSONResponse(Envelope(data=value), status_code=status_code)

    # 1. Nested repeated TypeAliasType expands completely; cycles still terminate.
    def nested_alias() -> NotFound[NotFound[Created[str]]]:
        return "created"

    spec_nested = parse_return(nested_alias)
    assert spec_nested.default_status == 201
    assert spec_nested.branches[0].payload_type is str
    assert spec_nested.error_statuses == {404: None}

    type RecLoop[T] = Annotated[  # ty: ignore[cyclic-type-alias-definition]
        RecLoop[T], Status(201)
    ]

    def recursive_alias() -> RecLoop[str]:
        return "ok"  # type: ignore[return-value]

    spec_rec = parse_return(recursive_alias)
    assert spec_rec.default_status == 201

    # 2. OpenAPI None with active representation vs JSONRepresentation vs bodyless 205.
    env_rep = EnvelopeRep()

    def none_enveloped() -> Annotated[None, env_rep]:
        return None

    def none_json_rep() -> Annotated[None, JSONRepresentation()]:
        return None

    def reset_plain() -> ResetContent:
        return None

    def reset_enveloped() -> Annotated[ResetContent, env_rep]:
        return None

    # 4. Ambiguous multi-status union rejects plain return with TypeError.
    def ambiguous(explicit: bool = False) -> Created[str] | Accepted[str]:
        if explicit:
            resp: Any = JSONResponse("ok", status_code=201)
            return resp
        return "plain"

    # 5. Status(103) is rejected with ValueError.
    with pytest.raises(ValueError, match="status"):
        Status(103)

    app = Chano(docs=False)
    app.add_route("/nested", nested_alias, ["POST"])
    app.add_route("/none-env", none_enveloped, ["GET"])
    app.add_route("/none-json", none_json_rep, ["GET"])
    app.add_route("/reset", reset_plain, ["POST"])
    app.add_route("/reset-env", reset_enveloped, ["POST"])
    app.add_route("/ambiguous", ambiguous, ["POST"])
    client = make_client(app)

    r_nested = client("POST", "/nested")
    assert r_nested.status_code == 201
    assert r_nested.json() == "created"

    r_none_env = client("GET", "/none-env")
    assert r_none_env.status_code == 200
    assert r_none_env.json() == {"data": None}

    for path in ("/reset", "/reset-env"):
        r_reset = client("POST", path)
        assert r_reset.status_code == 205
        assert r_reset.content == b""
        assert "content-length" not in r_reset.headers
        assert "content-type" not in r_reset.headers

    r_ambig_plain = client("POST", "/ambiguous")
    assert r_ambig_plain.status_code == 500
    err_out = capsys.readouterr().err
    assert "TypeError" in err_out
    assert "201, 202" in err_out
    assert "explicit Response" in err_out

    r_ambig_explicit = client("POST", "/ambiguous?explicit=true")
    assert r_ambig_explicit.status_code == 201
    assert r_ambig_explicit.json() == "ok"

    schema = app.openapi()
    nested_resps = schema["paths"]["/nested"]["post"]["responses"]
    assert set(nested_resps) == {"201", "404"}
    assert nested_resps["201"]["content"] == {
        "application/json": {"schema": {"type": "string"}}
    }

    none_env_200 = schema["paths"]["/none-env"]["get"]["responses"]["200"]
    assert "content" in none_env_200
    env_ref = none_env_200["content"]["application/json"]["schema"]["$ref"]
    env_name = env_ref.removeprefix("#/components/schemas/")
    assert schema["components"]["schemas"][env_name]["properties"]["data"] == {
        "type": "null"
    }

    assert "content" not in schema["paths"]["/none-json"]["get"]["responses"]["200"]
    assert schema["paths"]["/reset"]["post"]["responses"] == {
        "205": {"description": "Reset Content"}
    }
    assert schema["paths"]["/reset-env"]["post"]["responses"] == {
        "205": {"description": "Reset Content"}
    }
