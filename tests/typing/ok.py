"""Valid Outcomes and checked Depends usage — must pass `ty check`."""

from __future__ import annotations

import msgspec

from chano import Depends, ErrorDetail, HTTPBearer, Request, lazy
from chano.status import (
    OK,
    Accepted,
    Created,
    Forbidden,
    NoContent,
    NotFound,
    NotModified,
    SeeOther,
    Unauthorized,
)


class Item(msgspec.Struct):
    id: int
    name: str


class User(msgspec.Struct):
    name: str


class Problem(msgspec.Struct):
    title: str
    status: int


class Document[T](msgspec.Struct):
    data: T


class ItemNotFound(NotFound[Problem]):
    """No item has this ID."""


class ApiCreated[T](Created[Document[T]]):
    def __init__(self, data: T, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(Document(data), headers=headers)


class ApiNotFound(NotFound[Problem]):
    media_type = "application/problem+json"

    def __init__(self, title: str) -> None:
        super().__init__(Problem(title=title, status=self.status))


bearer = HTTPBearer()


def current_user(token: str = Depends(bearer)) -> User | Unauthorized:
    if token != "secret":
        return Unauthorized("Not authenticated")
    return User(name="scott")


async def async_user(token: str = Depends(bearer)) -> User | Unauthorized:
    if token != "secret":
        return Unauthorized("Not authenticated")
    return User(name="scott")


def redirect_guard(request: Request) -> User | SeeOther:
    if not request.headers.get("x-session"):
        return SeeOther("/login")
    return User(name="scott")


def make_config() -> dict[str, str]:
    return {"env": "prod"}


get_config = lazy(make_config)


def get_item(item_id: int, user: User = Depends(current_user)) -> Item | ItemNotFound:
    if item_id == 0:
        return ItemNotFound(Problem(title="No such item", status=404))
    return Item(id=item_id, name=user.name)


async def get_item_async(
    item_id: int,
    user: User = Depends(async_user),
    cfg: dict[str, str] = Depends(get_config),
) -> OK[Item] | NotFound:
    if item_id == 0:
        return NotFound("gone")
    return OK(Item(id=item_id, name=f"{user.name}:{cfg['env']}"))


def create_item(item: Item) -> Created[Item]:
    return Created(item, headers={"location": f"/items/{item.id}"})


def create_enveloped(item: Item) -> ApiCreated[Item] | ApiNotFound:
    if not item.name:
        return ApiNotFound("missing name")
    return ApiCreated(item)


def delete_item(item_id: int) -> NoContent | NotFound:
    if item_id == 0:
        raise NotFound("gone")
    return NoContent()


def accept_job(item: Item) -> Accepted[Item] | Accepted[None]:
    if item.id == 0:
        return Accepted()
    return Accepted(item)


def check_etag() -> NotModified | Forbidden:
    return NotModified()


def guarded(user: User = Depends(redirect_guard)) -> User:
    return user


def explicit_error_detail() -> NotFound[ErrorDetail]:
    return NotFound(ErrorDetail(detail="gone"))
