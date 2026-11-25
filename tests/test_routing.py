import json
import sys
from pathlib import Path
from typing import Annotated, Any

import msgspec
import pytest

from chano import (
    Body,
    Chano,
    ChanoConfig,
    Cookie,
    Depends,
    Header,
    Query,
    Request,
    create_app,
    create_route_app,
    load_config,
)
from chano.config import normalize_base_path
from chano.openapi import swagger_html
from chano.routing import discover_routes, load_module
from tests.helpers import (
    AUTH,
    make_client,
    raw_get,
    write_project,
)


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
    assert put["responses"]["401"] == {
        "description": "Unauthorized",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorDetail"}}
        },
    }
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


# @lat: [[tests#Project config#Defaults and keys]]
def test_project_config_defaults_and_keys(tmp_path):
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

    with pytest.raises(ValueError, match="invalid base_path"):
        ChanoConfig(base_path="/a\x00b")
    # functions/svc.func would be read as a function, not a directory.
    with pytest.raises(ValueError, match="invalid base_path"):
        normalize_base_path("/svc.func")
    assert normalize_base_path("/svc.funcs/x.func.d") == "/svc.funcs/x.func.d"


# @lat: [[tests#Project config#Base path normalization]]
@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("", ""),
        ("/", ""),
        ("/api/", "/api"),
        ("/api", "/api"),
        ("/v1/api", "/v1/api"),
        ("/v1+/a-b_c.d~e!,;=@", "/v1+/a-b_c.d~e!,;=@"),
    ],
)
def test_project_config_base_path_normalization(raw, normalized):
    assert ChanoConfig(base_path=raw).base_path == normalized


# @lat: [[tests#Project config#Invalid tool.chano table]]
@pytest.mark.parametrize(
    ("table", "error"),
    [
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
    ],
)
def test_project_config_invalid_chano_table(tmp_path, table, error):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f"[tool.chano]\n{table}\n")
    with pytest.raises(ValueError, match=error):
        load_config(pyproject)


# @lat: [[tests#Project config#Invalid tool table]]
@pytest.mark.parametrize(
    ("toml", "error"),
    [
        ("tool = true", "`tool` must be a table"),
        ("[tool]\nchano = true", "`tool.chano` must be a table"),
    ],
    ids=["tool-not-table", "tool-chano-not-table"],
)
def test_project_config_invalid_tool_table(tmp_path, toml, error):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f"{toml}\n")
    with pytest.raises(ValueError, match=rf"invalid \[tool\.chano\] .*{error}"):
        load_config(pyproject)


def test_swagger_html_escapes_title_and_url():
    page = swagger_html("<b>&", '/x"y</script><!--')
    assert "<title>&lt;b&gt;&amp;</title>" in page
    script = page.split("<script>\n", 1)[1].split("\n</script>", 1)[0]
    assert "<" not in script
    assert 'url: "/x\\"y\\u003c/script>\\u003c!--"' in script
    url = script.split("url: ", 1)[1].split(", dom_id", 1)[0]
    assert json.loads(url) == '/x"y</script><!--'


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


# @lat: [[tests#Root path reconstruction#Request path joining]]
@pytest.mark.parametrize(
    ("scope", "full"),
    [
        ({"path": "/", "root_path": ""}, "/"),
        ({"path": "/api/users", "root_path": ""}, "/api/users"),
        ({"path": "/", "root_path": "/api"}, "/api"),
        ({"path": "/", "root_path": "/api/"}, "/api"),
        ({"path": "/users", "root_path": "/api"}, "/api/users"),
        ({"path": "/apiary", "root_path": "/api"}, "/api/apiary"),
        ({"path": "/api/users", "root_path": "/api"}, "/api/api/users"),
        ({"path": "/x"}, "/x"),
    ],
)
def test_request_path_reconstruction(scope, full):
    async def receive():
        return {"type": "http.request"}

    assert Request(scope, receive).path == full


# @lat: [[tests#Root path reconstruction#Routing with root_path]]
def test_root_path_reconstruction(prefixed_project, isolated_imports):
    app = create_app(prefixed_project / "app")

    # What vercel-runtime's prefix strip produces for both /api and /api/.
    assert make_client(app, "/api")("GET", "/").json() == {"route": "root"}
    # Without a strip, /api/ is a literal miss.
    miss = make_client(app, "")("GET", "/api/")
    assert miss.status_code == 404
    assert miss.json() == {"detail": "Not Found"}
    assert make_client(app, "/api")("GET", "/users").json() == {"route": "users"}
    assert make_client(app, "/api/")("GET", "/users/3").json() == {
        "id": 3,
        "path": "/api/users/3",
    }
    # No startswith check: a stripped path may itself begin with the prefix.
    miss = make_client(app, "/api")("GET", "/apiary")
    assert miss.status_code == 404
    assert miss.json() == {"detail": "Not Found"}
    response = make_client(app, "/api")("GET", "/api/users")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}

    route_app = create_route_app(prefixed_project / "app", "api/users/[id]")
    route_client = make_client(route_app, "/api")
    assert route_client("GET", "/users/3").json() == {"id": 3, "path": "/api/users/3"}
    for path in ("/", "/api/users/3", "/apiary/x"):
        response = route_client("GET", path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


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


@pytest.mark.parametrize(
    "marker",
    [Query, Header, Cookie, Body],
    ids=["Query", "Header", "Cookie", "Body"],
)
def test_markers_require_annotated(marker):
    def bad(value: Any = marker()):  # noqa: B008
        return value

    app = Chano()
    with pytest.raises(TypeError, match="must be placed inside Annotated"):
        app.add_route("/", bad, methods=["GET"])


def test_query_rejects_positional_argument():
    query_fn: Any = Query
    with pytest.raises(TypeError, match="positional"):
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


type _QueryTags = list[str]


# @lat: [[tests#List query parameters with unions and aliases]]
def test_list_query_unions_and_type_aliases():
    from typing import Optional

    app = Chano()

    def union_pipe(tags: Annotated[list[str] | None, Query()] = None):
        return {"tags": tags}

    def union_optional(tags: Optional[list[str]] = None):  # noqa: UP045
        return {"tags": tags}

    def alias_direct(tags: _QueryTags):
        return {"tags": tags}

    def alias_optional(tags: Annotated[_QueryTags | None, Query()] = None):
        return {"tags": tags}

    app.add_route("/pipe", union_pipe, methods=["GET"])
    app.add_route("/optional", union_optional, methods=["GET"])
    app.add_route("/alias", alias_direct, methods=["GET"])
    app.add_route("/alias-opt", alias_optional, methods=["GET"])
    client = make_client(app)

    for path in ("/pipe", "/optional", "/alias", "/alias-opt"):
        resp = client("GET", f"{path}?tags=a&tags=b")
        assert resp.status_code == 200, (path, resp.text)
        assert resp.json() == {"tags": ["a", "b"]}

        single = client("GET", f"{path}?tags=only")
        assert single.status_code == 200, (path, single.text)
        assert single.json() == {"tags": ["only"]}

    for path in ("/pipe", "/optional", "/alias-opt"):
        absent = client("GET", path)
        assert absent.status_code == 200
        assert absent.json() == {"tags": None}

    def dep_q(q: str) -> str:
        return q

    def mixed(
        q: Annotated[str | list[str], Query()],
        from_dep: str = Depends(dep_q),
    ):
        return [q, from_dep]

    app.add_route("/mixed", mixed, methods=["GET"])
    mixed_resp = client("GET", "/mixed?q=a")
    assert mixed_resp.status_code == 200
    assert mixed_resp.json() == ["a", "a"]

    openapi_resp = client("GET", "/openapi.json")
    assert openapi_resp.status_code == 200
    assert "/mixed" in openapi_resp.json()["paths"]


# @lat: [[tests#Embedded body fields#Malformed or non-object JSON]]
@pytest.mark.parametrize(
    "raw_body",
    [b"{bad", b"[1, 2]", b"null"],
    ids=["malformed-json", "json-array", "json-null"],
)
def test_embedded_body_malformed_or_non_object_json(raw_body):
    app = Chano()

    def create(
        name: Annotated[str, Body()],
        count: Annotated[int, Body()],
    ):
        return {"name": name, "count": count}

    app.add_route("/items", create, methods=["POST"])
    client = make_client(app)

    resp = client(
        "POST",
        "/items",
        content=raw_body,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert len(detail) == 1, detail
    assert detail[0]["loc"] == ["body"]


# @lat: [[tests#Embedded body fields#Empty body reports missing fields]]
def test_embedded_body_empty_reports_missing_fields():
    app = Chano()

    def create(
        name: Annotated[str, Body()],
        count: Annotated[int, Body()],
    ):
        return {"name": name, "count": count}

    app.add_route("/items", create, methods=["POST"])
    client = make_client(app)

    empty = client("POST", "/items", content=b"")
    assert empty.status_code == 422
    assert empty.json()["detail"] == [
        {"loc": ["body", "name"], "msg": "Field required"},
        {"loc": ["body", "count"], "msg": "Field required"},
    ]
