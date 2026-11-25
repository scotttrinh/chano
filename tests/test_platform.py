import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import msgspec
import pytest

from chano import (
    Geo,
    Request,
    create_app,
)
from chano import requests as requests_module
from tests.helpers import (
    make_client,
    stream_scope,
    wait_for_disconnect,
    write_project,
)


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
