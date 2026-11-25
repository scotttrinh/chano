from datetime import UTC, datetime
from typing import Annotated, Any

import pytest

from chano import (
    Chano,
    Cookie,
    Depends,
    JSONResponse,
    Request,
    Response,
    SSEResponse,
    StreamingResponse,
)
from chano import params as params_module
from tests.helpers import (
    body_messages,
    call_asgi,
    make_client,
    stream_app,
    stream_scope,
    wait_for_disconnect,
)


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
        user: str = Depends(current_user),
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
    with pytest.raises(ValueError, match="invalid cookie name"):
        response.set_cookie("bad key", "x")
    # delete_cookie has exactly the spec's signature.
    with pytest.raises(TypeError, match="secure"):
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
