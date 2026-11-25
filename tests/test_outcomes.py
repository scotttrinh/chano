"""Tests for Outcomes, Failure, checked Depends, OpenAPI return annotations,
queue failure retries, and static typing fixtures."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import httpx
import msgspec
import pytest

from chano import (
    Boundary,
    CallNext,
    Chano,
    Depends,
    ErrorDetail,
    Failure,
    HTTPBearer,
    Outcome,
    Request,
    RequestValidationError,
    Response,
    create_app,
    openapi,
)
from chano.queues import _make_queue_subscriber, discover_queues, load_queue
from chano.status import (
    OK,
    Accepted,
    BadRequest,
    Created,
    Forbidden,
    NoContent,
    NotFound,
    NotModified,
    SeeOther,
    ServiceUnavailable,
    Unauthorized,
    UnprocessableContent,
)


def make_client(app: Any):
    def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    return request


class Item(msgspec.Struct):
    id: int
    name: str


class User(msgspec.Struct):
    name: str


class Problem(msgspec.Struct):
    title: str
    status: int = 404


class Document[T](msgspec.Struct):
    data: T


class ItemNotFound(NotFound[Problem]):
    """No item has this ID."""


class ApiCreated[T](Created[Document[T]]):
    def __init__(self, data: T, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(Document(data), headers=headers)


class ApiNotFound(NotFound[Problem]):
    """Problem-formatted missing resource."""

    media_type = "application/problem+json"

    def __init__(self, title: str) -> None:
        super().__init__(Problem(title=title, status=self.status))


class EarlyHints(Outcome[None]):
    status = 103


class Bodyless204WithArg(Outcome[str]):
    status = 204


type GetResult = Item | ItemNotFound
type Enveloped[T] = ApiCreated[T] | ApiNotFound
type DefaultResult[T = int] = Created[T] | NotFound
type ChainedDefault[T = int, U = T] = Created[U] | NotFound
type AliasDetail = ErrorDetail
type PlainUnion = int | str
type MissingAlias = NotFound
type ReplacementAlias = Created[str]


class StrNotFound(NotFound[str]):
    """Plain string missing error."""


class ObservingFailure[T = ErrorDetail](NotFound[T]):
    def __init__(self, content: Any, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(content, headers=headers)
        self.seen_content = self.content
        self.seen_body_len = len(self.body)


class NestedFailure[T = ErrorDetail](NotFound[T]):
    def __init__(
        self,
        content: Any,
        *,
        make_inner: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(content, headers=headers)
        self.inner = NestedFailure("inner") if make_inner else None


class MultiCreated[T = str, U = T](Created[U]):
    pass


class MultiFailure[T = str, U = T](NotFound[U]):
    pass


class GenericBase[T]:
    pass


class GenericMixin(GenericBase[int]):
    pass


class MixinNotFound(GenericMixin, NotFound):
    pass


class UnhashableMetaCreated[T](Created[Annotated[T, {"note": "unhashable"}]]):
    pass


# @lat: [[tests#Outcomes#Returned and raised outcomes]]
def test_outcomes_status_body_headers_cookies_and_bodyless():
    with pytest.raises(TypeError, match="status"):
        Outcome("x")
    with pytest.raises(TypeError, match="status"):
        Failure("x")

    app = Chano(docs=False)

    def created() -> Created[Item]:
        res = Created(Item(id=1, name="book"), headers={"location": "/items/1"})
        res.set_cookie("session", "abc")
        return res

    def accepted(empty: bool = False) -> Accepted[Item] | Accepted[None]:
        if empty:
            return Accepted()
        return Accepted(Item(id=2, name="job"))

    def no_content() -> NoContent:
        res = NoContent(headers={"x-done": "1"})
        res.delete_cookie("session")
        return res

    def not_modified() -> NotModified:
        return NotModified(headers={"etag": '"v1"'})

    def ok_none() -> OK[None]:
        return OK(None, headers={"x-empty": "yes"})

    def early() -> EarlyHints:
        return EarlyHints(None, headers={"link": "</a.css>; rel=preload"})

    def custom_204() -> Bodyless204WithArg:
        return Bodyless204WithArg("ignored")

    def fail_return() -> Item | NotFound:
        err = NotFound("gone", headers={"x-reason": "missing"})
        err.set_cookie("err_cookie", "1")
        return err

    def fail_raise() -> Item | NotFound:
        err = NotFound("gone", headers={"x-reason": "missing"})
        err.set_cookie("err_cookie", "1")
        raise err

    def fail_struct() -> Item | ApiNotFound:
        return ApiNotFound("No such item")

    app.add_route("/created", created, ["POST"])
    app.add_route("/accepted", accepted, ["POST"])
    app.add_route("/no-content", no_content, ["DELETE"])
    app.add_route("/not-modified", not_modified, ["GET"])
    app.add_route("/ok-none", ok_none, ["GET"])
    app.add_route("/early", early, ["GET"])
    app.add_route("/custom-204", custom_204, ["GET"])
    app.add_route("/fail-return", fail_return, ["GET"])
    app.add_route("/fail-raise", fail_raise, ["GET"])
    app.add_route("/fail-struct", fail_struct, ["GET"])

    client = make_client(app)

    r = client("POST", "/created")
    assert r.status_code == 201
    assert r.json() == {"id": 1, "name": "book"}
    assert r.headers["location"] == "/items/1"
    assert r.headers["content-type"] == "application/json"
    assert "session=abc" in r.headers["set-cookie"]

    r = client("POST", "/accepted")
    assert (r.status_code, r.json()) == (202, {"id": 2, "name": "job"})
    r = client("POST", "/accepted?empty=true")
    assert (r.status_code, r.content) == (202, b"")
    assert "content-type" not in r.headers

    r = client("DELETE", "/no-content")
    assert (r.status_code, r.content) == (204, b"")
    assert r.headers["x-done"] == "1"
    assert "content-type" not in r.headers
    assert "content-length" not in r.headers
    assert "Max-Age=0" in r.headers["set-cookie"]

    r = client("GET", "/not-modified")
    assert (r.status_code, r.content) == (304, b"")
    assert r.headers["etag"] == '"v1"'
    assert "content-type" not in r.headers
    assert "content-length" not in r.headers

    r = client("GET", "/ok-none")
    assert (r.status_code, r.content) == (200, b"")
    assert r.headers["x-empty"] == "yes"
    assert "content-type" not in r.headers

    r = client("GET", "/early")
    assert (r.status_code, r.content) == (103, b"")
    assert "content-type" not in r.headers
    assert "content-length" not in r.headers

    r = client("GET", "/custom-204")
    assert (r.status_code, r.content) == (204, b"")
    assert "content-type" not in r.headers

    # Returned and raised failures produce identical responses.
    r_ret = client("GET", "/fail-return")
    r_raise = client("GET", "/fail-raise")
    for res in (r_ret, r_raise):
        assert res.status_code == 404
        assert res.json() == {"detail": "gone"}
        assert res.headers["x-reason"] == "missing"
        assert "err_cookie=1" in res.headers["set-cookie"]

    r_prob = client("GET", "/fail-struct")
    assert r_prob.status_code == 404
    assert r_prob.headers["content-type"] == "application/problem+json"
    assert r_prob.json() == {"title": "No such item", "status": 404}

    # Failure[str] and subclasses of Failure[str] preserve raw str payloads, while
    # default NotFound("gone") and NotFound[ErrorDetail]("gone") wrap in ErrorDetail.
    raw_generic = NotFound[str]("gone")
    assert raw_generic.content == "gone"
    assert raw_generic.body == b'"gone"'
    assert raw_generic.headers["content-length"] == str(len(b'"gone"'))

    raw_subclass = StrNotFound("gone")
    assert raw_subclass.content == "gone"
    assert raw_subclass.body == b'"gone"'

    wrapped_default = NotFound("gone")
    assert wrapped_default.content == ErrorDetail(detail="gone")
    assert json.loads(wrapped_default.body) == {"detail": "gone"}

    wrapped_explicit = NotFound[ErrorDetail]("gone")
    assert wrapped_explicit.content == ErrorDetail(detail="gone")
    assert json.loads(wrapped_explicit.body) == {"detail": "gone"}

    # Generic subclass observes the resolved payload during __init__ (before return),
    # and Annotated / TypeAliasType wrappers around ErrorDetail still wrap strings.
    observed = ObservingFailure[str]("gone")
    assert observed.seen_content == "gone"
    assert observed.seen_body_len == len(b'"gone"')

    wrapped_ann = NotFound[Annotated[ErrorDetail, "marker"]]("gone")
    assert wrapped_ann.content == ErrorDetail(detail="gone")
    assert json.loads(wrapped_ann.body) == {"detail": "gone"}

    wrapped_alias = NotFound[AliasDetail]("gone")
    assert wrapped_alias.content == ErrorDetail(detail="gone")
    assert json.loads(wrapped_alias.body) == {"detail": "gone"}

    # Nested same-class construction inside __init__ does not inherit outer alias.
    nested_outer = NestedFailure[str]("outer", make_inner=True)
    assert nested_outer.content == "outer"
    assert nested_outer.body == b'"outer"'
    assert nested_outer.inner is not None
    assert nested_outer.inner.content == ErrorDetail(detail="inner")
    assert nested_outer.inner.body == b'{"detail":"inner"}'

    # Partially specialized class with default referencing earlier TypeVar.
    multi_err = MultiFailure[ErrorDetail]("x")
    assert multi_err.content == ErrorDetail(detail="x")
    assert multi_err.body == b'{"detail":"x"}'

    # RequestValidationError is a Failure keeping .errors and the same wire bytes.
    rve = RequestValidationError([{"loc": ["query", "x"], "msg": "Field required"}])
    assert isinstance(rve, Failure)
    assert isinstance(rve, UnprocessableContent)
    assert rve.status_code == 422
    assert rve.errors == [{"loc": ["query", "x"], "msg": "Field required"}]
    assert json.loads(rve.body) == {"detail": rve.errors}


# @lat: [[tests#Outcomes#Returned failures reach error boundaries]]
def test_returned_failure_reaches_error_boundary():
    seen: list[str] = []

    def handle_error(request: Request, exc: Exception):
        seen.append(type(exc).__name__)
        assert isinstance(exc, Failure)
        return BadRequest(
            f"caught:{exc.content.detail}"
            if isinstance(exc.content, ErrorDetail)
            else "caught"
        )

    def handler(mode: str = "return") -> Item | NotFound:
        if mode == "return":
            return NotFound("ret")
        raise NotFound("exc")

    app = Chano(docs=False)
    app.add_route("/x", handler, ["GET"], boundaries=[Boundary(recover=[handle_error])])
    client = make_client(app)

    r1 = client("GET", "/x?mode=return")
    assert (r1.status_code, r1.json()) == (400, {"detail": "caught:ret"})
    r2 = client("GET", "/x?mode=raise")
    assert (r2.status_code, r2.json()) == (400, {"detail": "caught:exc"})
    assert seen == ["NotFound", "NotFound"]


# @lat: [[tests#Checked Depends#Short-circuiting and caching]]
def test_depends_short_circuit_nested_and_cached():
    calls: list[str] = []

    def get_session(request: Request) -> str | Unauthorized | SeeOther:
        calls.append("session")
        mode = request.headers.get("x-mode", "ok")
        if mode == "unauth":
            return Unauthorized("no session")
        if mode == "redirect":
            return SeeOther("/login")
        return "sess-1"

    def get_user(session: str = Depends(get_session)) -> User:
        calls.append("user")
        return User(name=f"user:{session}")

    def later_dep() -> str:
        calls.append("later")
        return "later"

    def check_gate(request: Request) -> None | SeeOther:
        calls.append("check")
        if request.headers.get("x-gate") == "redirect":
            return SeeOther("/gate-login")
        return None

    def handler(
        session: str = Depends(get_session),
        user: User = Depends(get_user),
        extra: str = Depends(later_dep),
    ) -> dict[str, str]:
        calls.append("handler")
        return {"session": session, "user": user.name, "extra": extra}

    app = Chano(docs=False)
    app.add_route("/x", handler, ["GET"], boundaries=[Boundary(guards=[check_gate])])
    client = make_client(app)

    # Normal path: shared dependency cached once.
    calls.clear()
    r = client("GET", "/x")
    assert (r.status_code, r.json()) == (
        200,
        {"session": "sess-1", "user": "user:sess-1", "extra": "later"},
    )
    assert calls == ["check", "session", "user", "later", "handler"]

    # Dependency returning a Failure short-circuits outer deps and handler.
    calls.clear()
    r = client("GET", "/x", headers={"x-mode": "unauth"})
    assert (r.status_code, r.json()) == (401, {"detail": "no session"})
    assert calls == ["check", "session"]

    # Dependency returning a SeeOther redirect short-circuits outer deps and handler.
    calls.clear()
    r = client("GET", "/x", headers={"x-mode": "redirect"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert r.content == b""
    assert calls == ["check", "session"]

    # Inherited check returning a Response short-circuits before dependencies/handler.
    calls.clear()
    r = client("GET", "/x", headers={"x-gate": "redirect"})
    assert r.status_code == 303
    assert r.headers["location"] == "/gate-login"
    assert calls == ["check"]


# @lat: [[tests#Outcomes#OpenAPI from return annotations]]
def test_openapi_from_return_annotations():
    bearer = HTTPBearer()

    def dep_auth(token: str = Depends(bearer)) -> User | Forbidden:
        return User(name=token)

    def dep_alt_404() -> str | NotFound:
        return "ok"

    def guard() -> None | SeeOther:
        return None

    @openapi(response_description="Fetched item")
    def get_item(
        item_id: Annotated[int, msgspec.Meta(gt=0)],
        user: User = Depends(dep_auth),
        alt: str = Depends(dep_alt_404),
    ) -> GetResult:
        return Item(id=item_id, name=f"{user.name}:{alt}")

    def create_item(item: Item) -> Enveloped[Item]:
        return ApiCreated(item)

    def delete_item(item_id: int) -> NoContent | NotFound:
        return NoContent()

    def multi_200() -> Item | User:
        return Item(id=1, name="x")

    def bare_bearer(token: str = Depends(bearer)) -> str:
        return token

    def annotated_outcome() -> Annotated[Created[int], "marker"]:
        return Created(1)

    def annotated_alias() -> Annotated[Enveloped[Item], "marker"]:
        return ApiCreated(Item(id=1, name="x"))

    def unhashable_meta() -> UnhashableMetaCreated[int]:
        return UnhashableMetaCreated(1)

    def default_alias() -> DefaultResult:
        return Created(1)

    def chained_default() -> ChainedDefault:
        return Created(1)

    def chained_override() -> ChainedDefault[str]:
        return Created("ok")

    def annotated_union() -> Annotated[int | str, msgspec.Meta(description="Keep me")]:
        return 1

    def annotated_plain_alias() -> Annotated[
        PlainUnion, msgspec.Meta(description="Alias desc")
    ]:
        return "x"

    def annotated_struct_union() -> Annotated[
        Item | User, msgspec.Meta(description="Either model")
    ]:
        return Item(id=1, name="x")

    def multi_created() -> MultiCreated[int]:
        return MultiCreated(1)

    def mixin_not_found() -> MixinNotFound:
        return MixinNotFound("gone")

    def dep_ok() -> str | OK[int]:
        return "ok"

    @openapi(response_description="Custom 200")
    def dep_200_override(x: str = Depends(dep_ok)) -> Created[int]:
        return Created(len(x))

    app = Chano()
    app.add_route(
        "/items/{item_id}", get_item, ["GET"], boundaries=[Boundary(guards=[guard])]
    )
    app.add_route("/items", create_item, ["POST"])
    app.add_route("/delete/{item_id}", delete_item, ["DELETE"])
    app.add_route("/multi", multi_200, ["GET"])
    app.add_route("/bearer", bare_bearer, ["GET"])
    app.add_route("/annotated-outcome", annotated_outcome, ["POST"])
    app.add_route("/annotated-alias", annotated_alias, ["POST"])
    app.add_route("/unhashable-meta", unhashable_meta, ["POST"])
    app.add_route("/default-alias", default_alias, ["POST"])
    app.add_route("/chained-default", chained_default, ["POST"])
    app.add_route("/chained-override", chained_override, ["POST"])
    app.add_route("/annotated-union", annotated_union, ["GET"])
    app.add_route("/annotated-plain-alias", annotated_plain_alias, ["GET"])
    app.add_route("/annotated-struct-union", annotated_struct_union, ["GET"])
    app.add_route("/multi-created", multi_created, ["POST"])
    app.add_route("/mixin-not-found", mixin_not_found, ["GET"])
    app.add_route("/dep-200", dep_200_override, ["POST"])

    schema = app.openapi()
    schemas = schema["components"]["schemas"]

    # GET /items/{item_id}: 200 (with @openapi response_description), 303 (guard),
    # 401 (nested HTTPBearer without decorator), 403 (dep_auth), 404 (anyOf
    # ItemNotFound's Problem + dep_alt_404's ErrorDetail), 422 (has request inputs).
    get_res = schema["paths"]["/items/{item_id}"]["get"]["responses"]
    assert list(get_res) == ["200", "303", "401", "403", "404", "422"]
    assert get_res["200"]["description"] == "Fetched item"
    assert get_res["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert get_res["303"] == {"description": "See Other"}
    assert get_res["401"]["description"] == "Unauthorized"
    assert get_res["401"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorDetail"
    }
    assert get_res["403"]["description"] == "Forbidden"
    assert get_res["404"]["description"] == "No item has this ID."
    assert get_res["404"]["content"]["application/json"]["schema"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/Problem"},
            {"$ref": "#/components/schemas/ErrorDetail"},
        ]
    }
    assert get_res["422"]["description"] == "Validation Error"
    assert get_res["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ValidationErrorDetail"
    }

    # POST /items: envelope subclass resolves ApiCreated[Item] -> Document[Item] at 201,
    # ApiNotFound -> application/problem+json at 404, no invented 200.
    post_res = schema["paths"]["/items"]["post"]["responses"]
    assert list(post_res) == ["201", "404", "422"]
    assert post_res["201"]["description"] == "Created"
    doc_ref = post_res["201"]["content"]["application/json"]["schema"]["$ref"]
    doc_name = doc_ref.rsplit("/", 1)[-1]
    assert schemas[doc_name]["properties"]["data"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert post_res["404"]["description"] == "Problem-formatted missing resource."
    assert post_res["404"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/Problem"
    }

    # DELETE /delete/{item_id}: 204 bodyless, 404 ErrorDetail, 422, no 200.
    del_res = schema["paths"]["/delete/{item_id}"]["delete"]["responses"]
    assert list(del_res) == ["204", "404", "422"]
    assert del_res["204"] == {"description": "No Content"}
    assert "content" not in del_res["204"]

    # GET /multi: two plain return types form anyOf on 200, no 422 (no inputs).
    multi_res = schema["paths"]["/multi"]["get"]["responses"]
    assert list(multi_res) == ["200"]
    assert multi_res["200"]["content"]["application/json"]["schema"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/Item"},
            {"$ref": "#/components/schemas/User"},
        ]
    }

    # GET /bearer: HTTPBearer documents 401 with no decorator, no 422.
    bearer_res = schema["paths"]["/bearer"]["get"]["responses"]
    assert list(bearer_res) == ["200", "401"]
    assert bearer_res["401"]["description"] == "Unauthorized"

    # Annotated outcomes, Annotated aliases, unhashable metadata, alias defaults,
    # generic-mixin subclasses, and @openapi(response_description) over dep 200.
    ann_out = schema["paths"]["/annotated-outcome"]["post"]["responses"]
    assert list(ann_out) == ["201"]
    assert ann_out["201"]["content"]["application/json"]["schema"] == {
        "type": "integer"
    }

    ann_alias = schema["paths"]["/annotated-alias"]["post"]["responses"]
    assert list(ann_alias) == ["201", "404"]

    unhash = schema["paths"]["/unhashable-meta"]["post"]["responses"]
    assert list(unhash) == ["201"]
    assert unhash["201"]["content"]["application/json"]["schema"] == {"type": "integer"}

    def_alias = schema["paths"]["/default-alias"]["post"]["responses"]
    assert list(def_alias) == ["201", "404"]
    assert def_alias["201"]["content"]["application/json"]["schema"] == {
        "type": "integer"
    }
    assert def_alias["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorDetail"
    }

    chained_res = schema["paths"]["/chained-default"]["post"]["responses"]
    assert chained_res["201"]["content"]["application/json"]["schema"] == {
        "type": "integer"
    }
    chained_str = schema["paths"]["/chained-override"]["post"]["responses"]
    assert chained_str["201"]["content"]["application/json"]["schema"] == {
        "type": "string"
    }

    ann_union = schema["paths"]["/annotated-union"]["get"]["responses"]["200"]
    assert ann_union["content"]["application/json"]["schema"] == {
        "anyOf": [{"type": "integer"}, {"type": "string"}],
        "description": "Keep me",
    }
    ann_plain = schema["paths"]["/annotated-plain-alias"]["get"]["responses"]["200"]
    assert ann_plain["content"]["application/json"]["schema"] == {
        "anyOf": [{"type": "integer"}, {"type": "string"}],
        "description": "Alias desc",
    }

    ann_structs = schema["paths"]["/annotated-struct-union"]["get"]["responses"]["200"]
    assert ann_structs["content"]["application/json"]["schema"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/Item"},
            {"$ref": "#/components/schemas/User"},
        ],
        "description": "Either model",
    }

    multi_res = schema["paths"]["/multi-created"]["post"]["responses"]
    assert list(multi_res) == ["201"]
    assert multi_res["201"]["content"]["application/json"]["schema"] == {
        "type": "integer"
    }

    mixin_res = schema["paths"]["/mixin-not-found"]["get"]["responses"]
    assert list(mixin_res) == ["404"]
    assert mixin_res["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorDetail"
    }

    dep_200 = schema["paths"]["/dep-200"]["post"]["responses"]
    assert list(dep_200) == ["200", "201"]
    assert dep_200["200"]["description"] == "Custom 200"


# @lat: [[tests#Outcomes#Queue retry on returned and raised Failure]]
def test_queue_retry_on_returned_and_raised_failure(tmp_path):
    app_dir = tmp_path / "app"
    q_dir = app_dir / "jobs"
    q_dir.mkdir(parents=True)
    (q_dir / "queue.py").write_text(
        "from chano.status import ServiceUnavailable\n\n"
        'topic = "jobs"\n\n'
        "def CONSUME(payload: dict[str, str]):\n"
        '    mode = payload.get("mode")\n'
        '    if mode == "return":\n'
        '        return ServiceUnavailable("try later")\n'
        '    if mode == "raise":\n'
        '        raise ServiceUnavailable("try later")\n'
        '    return {"ok": True}\n'
    )

    loaded = load_queue(discover_queues(app_dir)[0])
    subscriber = _make_queue_subscriber(loaded, "local")

    async def deliver(sub: Any, payload: dict[str, str]) -> None:
        await sub(payload)

    # Normal delivery succeeds (returns None -> ack).
    asyncio.run(deliver(subscriber, {"mode": "ok"}))

    # Both returned and raised Failure raise out of the subscriber (triggers retry).
    for mode in ("return", "raise"):
        with pytest.raises(ServiceUnavailable):
            asyncio.run(deliver(subscriber, {"mode": mode}))

    # An ancestor boundary.py with guards, recover, and wrap is ignored by
    # queue consumers: both returned and raised failures still raise to retry.
    (app_dir / "boundary.py").write_text(
        "from chano import Boundary, RedirectResponse, Request\n"
        "from chano.status import NoContent\n\n"
        "def redirect_guard():\n"
        "    return RedirectResponse('/login')\n\n"
        "def recover_all(request: Request, exc: Exception):\n"
        "    return NoContent()\n\n"
        "async def never_wrap(request: Request, call_next):\n"
        "    raise AssertionError('wrap must not run on queues')\n\n"
        "boundary = Boundary(\n"
        "    guards=[redirect_guard], recover=[recover_all], wrap=[never_wrap]\n"
        ")\n"
    )
    loaded_with_boundary = load_queue(discover_queues(app_dir)[0])
    bounded_subscriber = _make_queue_subscriber(loaded_with_boundary, "local")
    asyncio.run(deliver(bounded_subscriber, {"mode": "ok"}))
    for mode in ("return", "raise"):
        with pytest.raises(ServiceUnavailable):
            asyncio.run(deliver(bounded_subscriber, {"mode": mode}))


class ApiUnprocessable(UnprocessableContent[Problem]):
    """Problem-formatted validation error."""

    media_type = "application/problem+json"

    def __init__(self, title: str) -> None:
        super().__init__(Problem(title=title, status=self.status))


# @lat: [[tests#Boundary#Recover matching and order]]
def test_boundary_recover_matching_order_and_wrappers():
    events: list[str] = []

    def recover_default_failure(
        request: Request, exc: Failure[ErrorDetail]
    ) -> ApiNotFound:
        events.append("leaf:Failure[ErrorDetail]")
        return ApiNotFound(f"reformatted:{exc.content.detail}")

    async def recover_lookup(request: Request, exc: LookupError) -> ApiNotFound:
        events.append("leaf:LookupError")
        if str(exc) == "'pass'":
            raise
        return ApiNotFound(f"lookup:{exc}")

    def recover_key_error(request: Request, exc: KeyError) -> BadRequest:
        events.append("root:KeyError")
        return BadRequest(f"root-key:{exc}")

    async def wrap_root(request: Request, call_next: CallNext) -> Response:
        events.append("wrap>")
        if request.headers.get("x-wrap-fail"):
            raise RuntimeError("wrap boom")
        res = await call_next(request)
        events.append(f"<wrap:{res.status_code}")
        return res

    def handler(mode: str = "default-fail") -> Item | NotFound | ApiNotFound:
        if mode == "default-fail":
            return NotFound("missing")
        if mode == "problem-fail":
            return ApiNotFound("already problem")
        if mode == "key":
            raise KeyError("k")
        if mode == "pass":
            raise KeyError("pass")
        return Item(id=1, name="ok")

    root_b = Boundary(recover=[recover_key_error], wrap=[wrap_root])
    leaf_b = Boundary(recover=[recover_default_failure, recover_lookup])

    app = Chano(docs=False)
    app.add_route("/x", handler, ["GET"], boundaries=[root_b, leaf_b])
    client = make_client(app)

    # Failure[ErrorDetail] matches returned NotFound("missing")...
    events.clear()
    r = client("GET", "/x?mode=default-fail")
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json() == {"title": "reformatted:missing", "status": 404}
    assert events == ["wrap>", "leaf:Failure[ErrorDetail]", "<wrap:404"]

    # ...and leaves ApiNotFound (Failure[Problem]) alone (sent as-is).
    events.clear()
    r = client("GET", "/x?mode=problem-fail")
    assert r.status_code == 404
    assert r.json() == {"title": "already problem", "status": 404}
    assert events == ["wrap>", "<wrap:404"]

    # Async LookupError recover catches KeyError (nearest first).
    events.clear()
    r = client("GET", "/x?mode=key")
    assert r.status_code == 404
    assert r.json() == {"title": "lookup:'k'", "status": 404}
    assert events == ["wrap>", "leaf:LookupError", "<wrap:404"]

    # Bare raise in leaf passes outward to root's KeyError recover.
    events.clear()
    r = client("GET", "/x?mode=pass")
    assert r.status_code == 400
    assert r.json() == {"detail": "root-key:'pass'"}
    assert events == ["wrap>", "leaf:LookupError", "root:KeyError", "<wrap:400"]

    # Wrapper errors skip recover and get the default 500 mapping.
    events.clear()
    r = client("GET", "/x", headers={"x-wrap-fail": "1"})
    assert r.status_code == 500
    assert events == ["wrap>"]

    # Failure[Any] matches any Failure without failing isinstance(..., Any).
    def recover_any_failure(request: Request, exc: Failure[Any]) -> BadRequest:
        return BadRequest(f"any:{exc.status_code}")

    any_app = Chano(docs=False)
    any_app.add_route(
        "/any", handler, ["GET"], boundaries=[Boundary(recover=[recover_any_failure])]
    )
    any_client = make_client(any_app)
    r_any1 = any_client("GET", "/any?mode=default-fail")
    assert (r_any1.status_code, r_any1.json()) == (400, {"detail": "any:404"})
    r_any2 = any_client("GET", "/any?mode=problem-fail")
    assert (r_any2.status_code, r_any2.json()) == (400, {"detail": "any:404"})


# @lat: [[tests#Boundary#OpenAPI recover replacement]]
def test_boundary_openapi_recover_replaces_422():
    def invalid(request: Request, error: RequestValidationError) -> ApiUnprocessable:
        return ApiUnprocessable("invalid input")

    def missing(request: Request, error: LookupError) -> ApiNotFound:
        return ApiNotFound(str(error))

    def handler(item: Item) -> Item:
        return item

    app = Chano()
    app.add_route(
        "/items", handler, ["POST"], boundaries=[Boundary(recover=[invalid, missing])]
    )
    schema = app.openapi()
    responses = schema["paths"]["/items"]["post"]["responses"]
    assert list(responses) == ["200", "422"]
    assert responses["422"]["description"] == "Problem-formatted validation error."
    assert responses["422"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/Problem"
    }
    assert "ValidationErrorDetail" not in schema["components"]["schemas"]

    # Earlier non-documentable catch-all stops later NotFound -> Created rewrite,
    # and non-Failure Outcomes (OK[str]) are never rewritten by recover.
    def catch_all(request: Request, error: Exception) -> Forbidden:
        return Forbidden("blocked")

    def not_found_to_created(request: Request, error: NotFound) -> Created[Item]:
        return Created(Item(id=0, name="fallback"))

    def ok_to_forbidden(request: Request, error: OK[str]) -> Forbidden:
        return Forbidden("never")

    def ok_or_missing() -> OK[str] | NotFound:
        return OK("hi")

    app2 = Chano()
    app2.add_route(
        "/prec",
        ok_or_missing,
        ["GET"],
        boundaries=[
            Boundary(recover=[catch_all, not_found_to_created, ok_to_forbidden])
        ],
    )
    prec_res = app2.openapi()["paths"]["/prec"]["get"]["responses"]
    assert list(prec_res) == ["200", "404"]
    assert prec_res["200"]["content"]["application/json"]["schema"] == {
        "type": "string"
    }

    # PEP 695 aliases on recover parameter/return and union payloads
    # (NotFound[str | None] matching NotFound[object]) rewrite OpenAPI.
    def recover_aliased(request: Request, error: MissingAlias) -> ReplacementAlias:
        return Created("recovered")

    def missing_handler() -> NotFound:
        return NotFound("gone")

    app3 = Chano()
    app3.add_route(
        "/aliased",
        missing_handler,
        ["GET"],
        boundaries=[Boundary(recover=[recover_aliased])],
    )
    aliased_res = app3.openapi()["paths"]["/aliased"]["get"]["responses"]
    assert list(aliased_res) == ["201"]
    assert aliased_res["201"]["content"]["application/json"]["schema"] == {
        "type": "string"
    }

    def recover_obj_payload(request: Request, error: NotFound[object]) -> Created[str]:
        return Created("obj")

    def union_payload_handler() -> NotFound[str | None]:
        return NotFound[str | None]("gone")

    app4 = Chano()
    app4.add_route(
        "/union-payload",
        union_payload_handler,
        ["GET"],
        boundaries=[Boundary(recover=[recover_obj_payload])],
    )
    union_res = app4.openapi()["paths"]["/union-payload"]["get"]["responses"]
    assert list(union_res) == ["201"]
    assert union_res["201"]["content"]["application/json"]["schema"] == {
        "type": "string"
    }


# @lat: [[tests#Boundary#Local schedule runner ignores boundary]]
def test_local_schedule_runner_ignores_boundary(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "ret").mkdir(parents=True)
    (app_dir / "exc").mkdir(parents=True)
    (app_dir / "boundary.py").write_text(
        "from chano import Boundary, RedirectResponse, Request\n"
        "from chano.status import OK, NotFound\n\n"
        "def redirect_guard():\n"
        "    return RedirectResponse('/login')\n\n"
        "def recover_missing(request: Request, exc: NotFound) -> OK[dict[str, str]]:\n"
        "    return OK({'recovered': exc.content.detail})\n\n"
        "async def never_wrap(request: Request, call_next):\n"
        "    raise AssertionError('wrap must not run on schedules')\n\n"
        "boundary = Boundary(\n"
        "    guards=[redirect_guard], recover=[recover_missing], wrap=[never_wrap]\n"
        ")\n"
    )
    (app_dir / "ret" / "schedule.py").write_text(
        "from chano.status import NotFound\n\n"
        'schedule = "0 * * * *"\n\n'
        "def RUN():\n"
        '    return NotFound("returned")\n'
    )
    (app_dir / "exc" / "schedule.py").write_text(
        "from chano.status import NotFound\n\n"
        'schedule = "0 * * * *"\n\n'
        "def RUN():\n"
        '    raise NotFound("raised")\n'
    )

    app = create_app(app_dir)
    res_ret = asyncio.run(app.run_schedule("ret"))
    assert isinstance(res_ret, NotFound)
    assert res_ret.content.detail == "returned"

    with pytest.raises(NotFound) as exc_info:
        asyncio.run(app.run_schedule("exc"))
    assert exc_info.value.content.detail == "raised"


# @lat: [[tests#Outcomes#Static typing fixtures]]
def test_typing_fixtures():
    repo_root = Path(__file__).parent.parent
    ok_file = repo_root / "tests" / "typing" / "ok.py"
    bad_file = repo_root / "tests" / "typing" / "bad.py"

    ok_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ty",
            "check",
            "--output-format",
            "concise",
            str(ok_file),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ok_result.returncode == 0, ok_result.stdout + ok_result.stderr

    expected_lines = {
        lineno
        for lineno, line in enumerate(bad_file.read_text().splitlines(), start=1)
        if "# type-error" in line
    }
    assert expected_lines, "bad.py must contain marked # type-error lines"

    bad_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ty",
            "check",
            "--output-format",
            "concise",
            str(bad_file),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert bad_result.returncode != 0
    reported_lines = {
        int(match.group(1))
        for match in re.finditer(
            r"bad\.py:(\d+):\d+:\s+error\[", bad_result.stdout + bad_result.stderr
        )
    }
    assert reported_lines == expected_lines, bad_result.stdout + bad_result.stderr


type _MyOK = OK[int]
type _GenericOK[T] = OK[T]


# @lat: [[tests#Outcomes#Outcome type aliases]]
def test_outcome_type_alias_route_and_resolve_content():
    from chano.responses import resolve_outcome_content

    app = Chano()

    def get_number() -> _MyOK:
        return OK(42)

    app.add_route("/num", get_number, methods=["GET"])
    client = make_client(app)

    resp = client("GET", "/num")
    assert resp.status_code == 200
    assert resp.json() == 42

    schema = app.openapi()
    ok_schema = schema["paths"]["/num"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert ok_schema == {"type": "integer"}

    assert resolve_outcome_content(_MyOK) is int
    assert resolve_outcome_content(_GenericOK[str]) is str
