"""Rejected Outcomes and checked Depends mistakes — each marked line must fail `ty`."""

from __future__ import annotations

import msgspec

from chano import Depends, HTTPBearer
from chano.status import Created, Forbidden, NotFound, Unauthorized


class Item(msgspec.Struct):
    id: int
    name: str


class User(msgspec.Struct):
    name: str


class Problem(msgspec.Struct):
    title: str


class Document[T](msgspec.Struct):
    data: T


class ItemNotFound(NotFound[Problem]):
    """No item has this ID."""


class ApiCreated[T](Created[Document[T]]):
    def __init__(self, data: T, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(Document(data), headers=headers)


bearer = HTTPBearer()


def current_user(token: str = Depends(bearer)) -> User | Unauthorized:
    return User(name="scott")


def undeclared_failure(item_id: int) -> Item | ItemNotFound:
    if item_id == 0:
        return Forbidden("nope")  # type-error
    return Item(id=item_id, name="x")


def wrong_status_class(item: Item) -> Created[Item]:
    return NotFound("wrong")  # type-error


def wrong_body_for_status(item_id: int) -> Item | ItemNotFound:
    if item_id == 0:
        return ItemNotFound("not a Problem")  # type-error
    return Item(id=item_id, name="x")


def body_paired_with_wrong_status(item: Item) -> Created[Item] | NotFound[Problem]:
    return Created(Problem(title="wrong branch"))  # type-error


def bare_value_where_created_declared(item: Item) -> Created[Item]:
    return item  # type-error


def raw_created_where_envelope_declared(item: Item) -> ApiCreated[Item]:
    return Created(item)  # type-error


def wrong_depends_param_type(user: str = Depends(current_user)) -> str:  # type-error
    return user
