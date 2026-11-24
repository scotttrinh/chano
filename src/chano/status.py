"""Standard PEP 695 HTTP status type aliases backed by ``Status(...)``.

Use ``< 400`` aliases to declare success/redirect return statuses and
``>= 400`` aliases to attach additive error statuses to a return type::

    from chano.status import Created, NoContent, NotFound

    def POST(item: CreateItem) -> Created[Item]: ...
    def GET(item_id: str) -> NotFound[Item]: ...
    def DELETE(item_id: str) -> NotFound[NoContent]: ...
"""

from typing import Annotated

from chano.representation import Status

type OK[T] = Annotated[T, Status(200)]
type Created[T] = Annotated[T, Status(201)]
type Accepted[T] = Annotated[T, Status(202)]
type NoContent = Annotated[None, Status(204)]
type ResetContent = Annotated[None, Status(205)]
type PartialContent[T] = Annotated[T, Status(206)]

type MovedPermanently[T] = Annotated[T, Status(301)]
type Found[T] = Annotated[T, Status(302)]
type SeeOther[T] = Annotated[T, Status(303)]
type NotModified = Annotated[None, Status(304)]
type TemporaryRedirect[T] = Annotated[T, Status(307)]
type PermanentRedirect[T] = Annotated[T, Status(308)]

type BadRequest[T] = Annotated[T, Status(400)]
type Unauthorized[T] = Annotated[T, Status(401)]
type PaymentRequired[T] = Annotated[T, Status(402)]
type Forbidden[T] = Annotated[T, Status(403)]
type NotFound[T] = Annotated[T, Status(404)]
type MethodNotAllowed[T] = Annotated[T, Status(405)]
type NotAcceptable[T] = Annotated[T, Status(406)]
type RequestTimeout[T] = Annotated[T, Status(408)]
type Conflict[T] = Annotated[T, Status(409)]
type Gone[T] = Annotated[T, Status(410)]
type PreconditionFailed[T] = Annotated[T, Status(412)]
type ContentTooLarge[T] = Annotated[T, Status(413)]
type UnsupportedMediaType[T] = Annotated[T, Status(415)]
type UnprocessableContent[T] = Annotated[T, Status(422)]
type TooManyRequests[T] = Annotated[T, Status(429)]

type InternalServerError[T] = Annotated[T, Status(500)]
type BadGateway[T] = Annotated[T, Status(502)]
type ServiceUnavailable[T] = Annotated[T, Status(503)]
type GatewayTimeout[T] = Annotated[T, Status(504)]

__all__ = [
    "Accepted",
    "BadGateway",
    "BadRequest",
    "Conflict",
    "ContentTooLarge",
    "Created",
    "Forbidden",
    "Found",
    "GatewayTimeout",
    "Gone",
    "InternalServerError",
    "MethodNotAllowed",
    "MovedPermanently",
    "NoContent",
    "NotAcceptable",
    "NotFound",
    "NotModified",
    "OK",
    "PartialContent",
    "PaymentRequired",
    "PermanentRedirect",
    "PreconditionFailed",
    "RequestTimeout",
    "ResetContent",
    "SeeOther",
    "ServiceUnavailable",
    "TemporaryRedirect",
    "TooManyRequests",
    "Unauthorized",
    "UnprocessableContent",
    "UnsupportedMediaType",
]
