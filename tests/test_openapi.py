import json

import pytest

from chano import (
    Chano,
    Depends,
    HTTPBearer,
    create_app,
    create_route_app,
    openapi,
)
from chano.build import plan_build
from chano.status import (
    Forbidden,
    NotFound,
    Unauthorized,
)
from tests.helpers import (
    make_client,
    write_project,
)

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
def test_include_in_schema_false(tmp_path, isolated_project_imports):
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
    assert set(schema["components"]["schemas"]) == {"Shared", "ValidationErrorDetail"}
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
def test_include_in_schema_only_hidden_routes(tmp_path, isolated_project_imports):
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
def test_openapi_decorator_metadata(tmp_path, isolated_project_imports):
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


SECURE_ITEMS = """\
from chano import Depends, HTTPBearer
from chano.status import Forbidden, Unauthorized

bearer = HTTPBearer()


class InvalidToken(Unauthorized):
    \"\"\"Invalid token\"\"\"


class NotAllowed(Forbidden):
    \"\"\"Not allowed\"\"\"


def current_user(token: str = Depends(bearer)) -> str | InvalidToken:
    if token != "secret":
        return InvalidToken("Invalid token")
    return "scott"


def GET() -> list[str]:
    return ["public"]


def POST(name: str, user: str = Depends(current_user)) -> str | NotAllowed:
    return f"{user}:{name}"
"""


# @lat: [[tests#Explicit security and responses]]
def test_explicit_security_and_responses(tmp_path, isolated_project_imports):
    project = tmp_path / "secure"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "secure"\nversion = "0"\n',
            "app/items/route.py": SECURE_ITEMS,
            "app/broken/route.py": (
                "from chano import Depends, HTTPBearer\n\n"
                "def explode(token: str = Depends(HTTPBearer())) -> str:\n"
                "    raise RuntimeError('called during schema generation')\n\n"
                "def GET(value: str = Depends(explode)) -> str:\n"
                "    return value\n"
            ),
            "app/internal/route.py": (
                "from chano import Depends, HTTPBearer, openapi\n\n"
                "@openapi(include_in_schema=False)\n"
                "def GET(token: str = Depends(HTTPBearer('internal'))) -> str:\n"
                "    return token\n"
            ),
        },
    )
    client = make_client(create_app(project / "app"))
    schema = client("GET", "/openapi.json").json()

    assert schema["components"]["securitySchemes"] == {
        "HTTPBearer": {"type": "http", "scheme": "bearer"}
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
    # documented, and HTTPBearer's own 401 is documented without a decorator.
    broken = schema["paths"]["/broken"]["get"]
    assert broken["security"] == [{"HTTPBearer": []}]
    assert list(broken["responses"]) == ["200", "401"]
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

    def handler(value: str = Depends(bearer)) -> str:
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


def declared_descriptions(handler) -> dict[str, str]:
    app = Chano()
    app.add_route("/", handler, methods=["GET"])
    responses = app.openapi()["paths"]["/"]["get"]["responses"]
    return {code: r["description"] for code, r in responses.items()}


# @lat: [[tests#Declared response precedence]]
def test_declared_response_precedence():
    class Nested401(Unauthorized):
        """nested"""

    class Nested403(Forbidden):
        """nested"""

    class Nested404(NotFound):
        """nested"""

    class Direct401(Unauthorized):
        """direct"""

    class Direct403(Forbidden):
        """direct"""

    class Handler403(Forbidden):
        """handler"""

    def nested() -> None | Nested401 | Nested403 | Nested404:
        return None

    def first(value: None = Depends(nested)) -> None:
        return value

    def second() -> None | Direct401 | Direct403:
        return None

    def handler(
        a: None = Depends(first), b: None = Depends(second)
    ) -> None | Handler403:
        return None

    # The handler beats its dependencies, and a direct dependency beats a
    # nested one even when the nested one is in an earlier parameter.
    assert declared_descriptions(handler) == {
        "200": "Successful Response",
        "401": "direct",
        "403": "handler",
        "404": "nested",
    }


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

    def handler(token: str = Depends(bearer)) -> str:
        return token

    app = Chano()
    app.add_route("/", handler, methods=["GET"])
    assert list(app.openapi()["components"]["securitySchemes"]) == [name]
