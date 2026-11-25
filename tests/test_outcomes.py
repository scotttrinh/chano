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
    Chano,
    Depends,
    ErrorDetail,
    Failure,
    HTTPBearer,
    Outcome,
    Request,
    RequestValidationError,
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


class StrNotFound(NotFound[str]):
    """Plain string missing error."""


class ObservingFailure[T = ErrorDetail](NotFound[T]):
    def __init__(self, content: Any, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(content, headers=headers)
        self.seen_content = self.content
        self.seen_body_len = len(self.body)


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
    app.add_route("/x", handler, ["GET"], error_handlers=[handle_error])
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
    app.add_route("/x", handler, ["GET"], dependencies=[[check_gate]])
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

    def mixin_not_found() -> MixinNotFound:
        return MixinNotFound("gone")

    def dep_ok() -> str | OK[int]:
        return "ok"

    @openapi(response_description="Custom 200")
    def dep_200_override(x: str = Depends(dep_ok)) -> Created[int]:
        return Created(len(x))

    app = Chano()
    app.add_route("/items/{item_id}", get_item, ["GET"], dependencies=[[guard]])
    app.add_route("/items", create_item, ["POST"])
    app.add_route("/items/{item_id}", delete_item, ["DELETE"])
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

    # DELETE /items/{item_id}: 204 bodyless, 404 ErrorDetail, 422, no 200.
    del_res = schema["paths"]["/items/{item_id}"]["delete"]["responses"]
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

    # An error.py recovering to < 400 acknowledges both returned and raised failures.
    (app_dir / "error.py").write_text(
        "from chano.status import NoContent\n\n"
        "def handle_error(request, exc):\n"
        "    return NoContent()\n"
    )
    loaded_recovered = load_queue(discover_queues(app_dir)[0])
    recovered_subscriber = _make_queue_subscriber(loaded_recovered, "local")
    asyncio.run(deliver(recovered_subscriber, {"mode": "return"}))
    asyncio.run(deliver(recovered_subscriber, {"mode": "raise"}))


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
