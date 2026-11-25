from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qs, unquote

import msgspec

from chano.exceptions import ClientDisconnect

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

_COOKIE_ESCAPE = re.compile(r"\\(?:([0-3][0-7]{2})|(.))")


def _unquote(value: str) -> str:
    """Undo the quoting ``http.cookies`` applies to values: surrounding
    double quotes, backslash escapes and ``\\ooo`` octal escapes."""
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value
    return _COOKIE_ESCAPE.sub(
        lambda m: chr(int(m[1], 8)) if m[1] else m[2], value[1:-1]
    )


class Geo(msgspec.Struct, frozen=True):
    """Requester location from Vercel's ``x-vercel-ip-*`` headers.

    ``region`` is the ISO 3166-2 region from ``x-vercel-ip-country-region``
    (``@vercel/functions`` calls it ``countryRegion``), not the Vercel edge
    region. Every field is ``None`` when its header is absent, as in dev.
    """

    country: str | None = None
    region: str | None = None
    city: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    postal_code: str | None = None


class Request:
    """An incoming HTTP request, wrapping the ASGI scope."""

    def __init__(self, scope: Scope, receive: Receive) -> None:
        self.scope = scope
        self._receive = receive
        self._body: bytes | None = None
        self._stream_consumed = False
        self._receive_lock = asyncio.Lock()
        self._pending_message: Message | None = None
        self._body_done = asyncio.Event()
        self._disconnected = False
        self.path_params: dict[str, str] = {}
        # Generated eagerly so concurrent first reads of ``id`` agree.
        self._generated_id = uuid.uuid4().hex

    @property
    def method(self) -> str:
        return self.scope["method"]

    @property
    def path(self) -> str:
        """The full public path, ``root_path + path``.

        ``scope["path"]`` is relative to ``scope["root_path"]`` (as
        ``vercel-runtime``'s prefix strip writes it), so there is no
        ``startswith`` check. At the prefix root, ``path == "/"``, the full
        path is ``root_path`` alone.
        """
        root_path = self.scope.get("root_path", "").rstrip("/")
        path = self.scope["path"]
        if root_path and path == "/":
            return root_path
        return f"{root_path}{path}"

    @property
    def headers(self) -> dict[str, str]:
        """Request headers, keyed by lower-case name."""
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in self.scope.get("headers", [])
        }

    @property
    def cookies(self) -> dict[str, str]:
        """Cookies from every ``cookie`` header, keyed by name.

        Quoted values are unquoted. When a name repeats, the first value wins:
        browsers send the cookie with the most specific path first.
        """
        cookies: dict[str, str] = {}
        for key, value in self.scope.get("headers", []):
            if key.lower() != b"cookie":
                continue
            for pair in value.decode("latin-1").split(";"):
                name, sep, raw = pair.partition("=")
                name = name.strip()
                if sep and name and name not in cookies:
                    cookies[name] = _unquote(raw.strip())
        return cookies

    def _first_header(self, name: bytes) -> str | None:
        """The first ``name`` header in ``scope["headers"]`` order.

        ``headers`` keeps the last of repeated field lines; platform metadata
        needs the first.
        """
        for key, value in self.scope.get("headers", []):
            if key.lower() == name:
                return value.decode("latin-1")
        return None

    @property
    def id(self) -> str:
        """The ``x-vercel-id`` header, else a UUID4 hex string generated once
        per request."""
        return self._first_header(b"x-vercel-id") or self._generated_id

    @property
    def ip(self) -> str | None:
        """The client IP: ``x-real-ip``, else the first ``x-forwarded-for``
        entry, else the ASGI ``client`` host.

        Outside Vercel the headers come from the client, so never trust them
        for authorization.
        """
        if real_ip := (self._first_header(b"x-real-ip") or "").strip():
            return real_ip
        forwarded = self._first_header(b"x-forwarded-for") or ""
        if first := forwarded.split(",")[0].strip():
            return first
        client = self.scope.get("client")
        return client[0] if client else None

    @property
    def geo(self) -> Geo:
        """Requester location from the ``x-vercel-ip-*`` headers; the city is
        percent-decoded."""
        headers = self.headers
        city = headers.get("x-vercel-ip-city")
        return Geo(
            country=headers.get("x-vercel-ip-country"),
            region=headers.get("x-vercel-ip-country-region"),
            city=None if city is None else unquote(city),
            latitude=headers.get("x-vercel-ip-latitude"),
            longitude=headers.get("x-vercel-ip-longitude"),
            postal_code=headers.get("x-vercel-ip-postal-code"),
        )

    @property
    def query_params(self) -> dict[str, list[str]]:
        """Query parameters; each name maps to every value given for it."""
        query = self.scope.get("query_string", b"").decode("latin-1")
        return parse_qs(query, keep_blank_values=True)

    def stream(self) -> AsyncIterator[bytes]:
        if self._body is not None:
            return self._cached_stream()
        if self._stream_consumed:
            raise RuntimeError("Stream consumed")
        self._stream_consumed = True
        return self._stream_body()

    async def _cached_stream(self) -> AsyncIterator[bytes]:
        if self._body:
            yield self._body

    async def _stream_body(self) -> AsyncIterator[bytes]:
        try:
            while True:
                if self._pending_message is not None:
                    message = self._pending_message
                    self._pending_message = None
                else:
                    async with self._receive_lock:
                        if self._pending_message is not None:
                            message = self._pending_message
                            self._pending_message = None
                        elif self._disconnected:
                            raise ClientDisconnect()
                        else:
                            message = await self._receive()
                            if message.get("type") == "http.disconnect":
                                self._disconnected = True
                msg_type = message.get("type")
                if msg_type == "http.request":
                    body = message.get("body", b"")
                    more_body = bool(message.get("more_body", False))
                    if not more_body:
                        self._body_done.set()
                    if body:
                        yield body
                    if not more_body:
                        return
                elif msg_type == "http.disconnect":
                    self._disconnected = True
                    raise ClientDisconnect()
                else:
                    raise RuntimeError(f"Unexpected ASGI message: {msg_type!r}")
        finally:
            self._body_done.set()

    async def body(self) -> bytes:
        if self._body is None:
            self._body = b"".join([chunk async for chunk in self.stream()])
        return self._body

    async def _response_receive(self) -> Message:
        while True:
            if self._disconnected:
                return {"type": "http.disconnect"}
            if (
                self._stream_consumed or self._pending_message is not None
            ) and not self._body_done.is_set():
                await self._body_done.wait()
                continue
            async with self._receive_lock:
                if self._disconnected:
                    return {"type": "http.disconnect"}
                if (
                    self._stream_consumed or self._pending_message is not None
                ) and not self._body_done.is_set():
                    continue
                message = await self._receive()
                msg_type = message.get("type")
                if msg_type == "http.disconnect":
                    self._disconnected = True
                    return message
                if (
                    msg_type == "http.request"
                    and self._body is None
                    and not self._body_done.is_set()
                ):
                    self._pending_message = message
                    if not message.get("more_body", False):
                        self._body_done.set()
                return message

    async def json(self) -> Any:
        return json.loads(await self.body())
