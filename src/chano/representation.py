"""Response and error representations that pair runtime rendering with OpenAPI types."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol, TypeGuard, runtime_checkable

import msgspec

from chano.requests import Request
from chano.responses import JSONResponse, Response


class ValidationErrorItem(msgspec.Struct):
    """One field-level validation failure in a default 422 response."""

    loc: list[str | int]
    msg: str


class ValidationErrorDetail(msgspec.Struct):
    """Default 422 validation error response body."""

    detail: list[ValidationErrorItem]


class ErrorDetail(msgspec.Struct):
    """Default ``HTTPException`` error response body."""

    detail: Any


@runtime_checkable
class Representation(Protocol):
    """Controls wire rendering and OpenAPI schemas for a route subtree.

    Each runtime transformation (``success`` / ``error``) is paired with the
    corresponding type transformation (``success_type`` / ``error_type``) so
    generated OpenAPI stays in agreement with the bytes sent on the wire.
    """

    media_type: str
    error_media_type: str

    def success_type(self, returns: Any) -> Any:
        """Transform a handler's return annotation into the wire body annotation."""
        ...

    def success(self, request: Request, value: Any) -> Response | Awaitable[Response]:
        """Turn a non-``Response`` handler return value into a ``Response``."""
        ...

    def error_type(self, status_code: int) -> Any:
        """Return the error body annotation for ``status_code``, or ``None``."""
        ...

    def error(self, request: Request, exc: Exception) -> Response | Awaitable[Response]:
        """Turn an uncaught endpoint exception into a ``Response``."""
        ...


def is_representation(value: Any) -> TypeGuard[Representation]:
    """Return True if ``value`` is a representation object implementing the protocol."""
    if isinstance(value, type):
        return False
    return (
        isinstance(getattr(value, "media_type", None), str)
        and isinstance(getattr(value, "error_media_type", None), str)
        and callable(getattr(value, "success_type", None))
        and callable(getattr(value, "success", None))
        and callable(getattr(value, "error_type", None))
        and callable(getattr(value, "error", None))
    )


class JSONRepresentation:
    """Default JSON representation matching Chano's built-in wire format.

    Subclass this to customize only success rendering (e.g. an envelope) or
    only error rendering (e.g. RFC 9457 Problem JSON) while keeping the other
    half at the default behavior.
    """

    media_type: str = "application/json"
    error_media_type: str = "application/json"

    def success_type(self, returns: Any) -> Any:
        return returns

    def success(self, request: Request, value: Any) -> Response:
        return JSONResponse(value)

    def error_type(self, status_code: int) -> Any:
        if status_code == 422:
            return ValidationErrorDetail
        return ErrorDetail

    def error(self, request: Request, exc: Exception) -> Response:
        from chano.app import error_response

        return error_response(exc)
