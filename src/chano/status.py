"""Named HTTP status outcomes and failures."""

from __future__ import annotations

from typing import Any, overload

from chano.responses import ErrorDetail, Failure, Outcome


class _BodylessOutcome(Outcome[None]):
    def __init__(self, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(None, headers=headers)


class OK[T](Outcome[T]):
    status = 200


class Created[T](Outcome[T]):
    status = 201


class Accepted[T = None](Outcome[T]):
    status = 202

    @overload
    def __init__(
        self: Accepted[None],
        content: None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self, content: T, *, headers: dict[str, str] | None = None
    ) -> None: ...
    def __init__(
        self, content: Any = None, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(content, headers=headers)


class NoContent(_BodylessOutcome):
    status = 204


class SeeOther(Outcome[None]):
    status = 303

    def __init__(self, location: str, *, headers: dict[str, str] | None = None) -> None:
        merged = {key.lower(): value for key, value in (headers or {}).items()}
        merged["location"] = location
        super().__init__(None, headers=merged)


class NotModified(_BodylessOutcome):
    status = 304


class BadRequest[T = ErrorDetail](Failure[T]):
    status = 400


class Unauthorized[T = ErrorDetail](Failure[T]):
    status = 401


class Forbidden[T = ErrorDetail](Failure[T]):
    status = 403


class NotFound[T = ErrorDetail](Failure[T]):
    status = 404


class MethodNotAllowed[T = ErrorDetail](Failure[T]):
    status = 405


class Conflict[T = ErrorDetail](Failure[T]):
    status = 409


class Gone[T = ErrorDetail](Failure[T]):
    status = 410


class UnprocessableContent[T = ErrorDetail](Failure[T]):
    status = 422


class TooManyRequests[T = ErrorDetail](Failure[T]):
    status = 429


class InternalServerError[T = ErrorDetail](Failure[T]):
    status = 500


class ServiceUnavailable[T = ErrorDetail](Failure[T]):
    status = 503


__all__ = [
    "Accepted",
    "BadRequest",
    "Conflict",
    "Created",
    "Forbidden",
    "Gone",
    "InternalServerError",
    "MethodNotAllowed",
    "NoContent",
    "NotFound",
    "NotModified",
    "OK",
    "SeeOther",
    "ServiceUnavailable",
    "TooManyRequests",
    "Unauthorized",
    "UnprocessableContent",
]
