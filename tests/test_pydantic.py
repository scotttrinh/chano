import json
import subprocess
import sys
import types
from typing import Annotated, Any

import msgspec
import pytest

from chano import (
    Body,
    Chano,
    JSONResponse,
    Query,
    ServerSentEvent,
    SSEResponse,
    create_app,
)
from chano.build import build
from tests.helpers import (
    make_client,
)


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

    with pytest.raises(
        TypeError, match="Encoding objects of type object is unsupported"
    ):
        JSONResponse(object())


# @lat: [[tests#Pydantic responses and OpenAPI#Dev and build OpenAPI schemas]]
def test_pydantic_openapi_dev_and_build(
    tmp_path, isolated_project_imports, stub_build_install
):
    stub_build_install()
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
