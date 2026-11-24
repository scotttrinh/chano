"""Response and error representations plus unified ``Annotated`` return metadata."""

from __future__ import annotations

import functools
import inspect
import operator
import sys
import types
from collections.abc import Awaitable, Callable, Container, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import (
    Annotated,
    Any,
    ParamSpec,
    Protocol,
    TypeAliasType,
    TypeGuard,
    TypeVar,
    TypeVarTuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

import msgspec

from chano.requests import Request
from chano.responses import JSONResponse, Response

_EMPTY: Any = inspect.Signature.empty


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


def _is_response_type(value: Any) -> bool:
    if isinstance(value, type):
        return issubclass(value, Response)
    if isinstance(value, tuple):
        return all(
            isinstance(item, type) and issubclass(item, Response) for item in value
        )
    return False


@runtime_checkable
class Representation(Protocol):
    """Controls wire rendering and OpenAPI schemas for matching responses.

    Each runtime transformation (``render``) is paired with the corresponding
    media type and body type transformations (``media_type`` / ``body_type``)
    keyed by ``status_code`` so generated OpenAPI stays in agreement with the
    bytes sent on the wire. ``response_type`` declares which ``Response``
    subclass(es) this representation handles; non-``Response`` handler returns
    are treated as implicit ``JSONResponse``.
    """

    @property
    def response_type(
        self,
    ) -> type[Response[Any]] | tuple[type[Response[Any]], ...]:
        """``Response`` subclass(es) handled by this representation."""
        ...

    def media_type(self, status_code: int) -> str:
        """Return the media type for ``status_code``."""
        ...

    def body_type(self, status_code: int, returns: Any) -> Any:
        """Transform a payload annotation (or ``None``) for ``status_code``."""
        ...

    def render(
        self, request: Request, value: Any, status_code: int
    ) -> Response[Any] | Awaitable[Response[Any]]:
        """Render a payload or exception into a ``Response`` for ``status_code``."""
        ...


def is_representation(value: Any) -> TypeGuard[Representation]:
    """Return True if ``value`` is a representation object implementing the protocol."""
    if isinstance(value, type):
        return False
    return (
        _is_response_type(getattr(value, "response_type", None))
        and callable(getattr(value, "media_type", None))
        and callable(getattr(value, "body_type", None))
        and callable(getattr(value, "render", None))
    )


def resolve_representation(
    representation: Representation | None, status_code: int
) -> Representation | None:
    """Resolve nested ``StatusRepresentation``s to the leaf for ``status_code``."""
    while isinstance(representation, StatusRepresentation):
        representation = representation._resolve(status_code)
    return representation


def matches_response_type(
    representation: Representation,
    response_cls: type[Response[Any]],
    status_code: int | None = None,
) -> bool:
    """Return True if ``representation`` handles ``response_cls``."""
    if status_code is not None and isinstance(representation, StatusRepresentation):
        resolved = resolve_representation(representation, status_code)
        if resolved is None:
            return False
        return issubclass(response_cls, resolved.response_type)
    return issubclass(response_cls, representation.response_type)


class JSONRepresentation:
    """Default JSON representation matching Chano's built-in wire format.

    Subclass this to customize rendering or OpenAPI schemas for all or a subset
    of status codes, or compose specialized representations with
    ``StatusRepresentation``.
    """

    response_type: type[Response[Any]] | tuple[type[Response[Any]], ...] = JSONResponse

    def media_type(self, status_code: int) -> str:
        return "application/json"

    def body_type(self, status_code: int, returns: Any) -> Any:
        if status_code >= 400:
            if status_code == 422:
                return ValidationErrorDetail
            return ErrorDetail
        return returns

    def render(self, request: Request, value: Any, status_code: int) -> Response[Any]:
        if isinstance(value, Exception):
            from chano.app import error_response

            return error_response(value)
        return JSONResponse(value, status_code=status_code)


_DEFAULT_REPRESENTATION: Representation = JSONRepresentation()


def _is_status_matcher(key: Any) -> bool:
    if isinstance(key, bool):
        return False
    if isinstance(key, int):
        return True
    return isinstance(key, Container) and not isinstance(key, str | bytes)


class StatusRepresentation:
    """Route ``status_code`` to a matching child ``Representation``.

    Keys in ``routes`` may be individual ``int`` status codes, ``range``s, or
    any ``Container[int]`` (such as a ``frozenset`` or ``tuple``), checked in
    insertion order. Unmatched status codes fall back to ``default``
    (``JSONRepresentation()`` by default, or ``None`` for default Chano
    passthrough).
    """

    def __init__(
        self,
        routes: Mapping[int | Container[int], Representation],
        *,
        default: Representation | None = _DEFAULT_REPRESENTATION,
    ) -> None:
        if not isinstance(routes, Mapping):
            raise TypeError(
                f"StatusRepresentation() expected a mapping, got {routes!r}"
            )
        entries: list[tuple[int | Container[int], Representation]] = []
        for matcher, rep in routes.items():
            if not _is_status_matcher(matcher):
                raise TypeError(
                    "StatusRepresentation() keys must be int or int containers, "
                    f"got {matcher!r}"
                )
            if not is_representation(rep):
                raise TypeError(
                    "StatusRepresentation() values must be Representation "
                    f"instances, got {rep!r}"
                )
            entries.append((matcher, rep))
        if default is not None and not is_representation(default):
            raise TypeError(
                "StatusRepresentation() `default` must be a Representation "
                f"or None, got {default!r}"
            )
        self._routes: tuple[tuple[int | Container[int], Representation], ...] = tuple(
            entries
        )
        self._default: Representation | None = default

        types_list: list[type[Response[Any]]] = []
        children: list[Representation] = [rep for _, rep in entries]
        if default is not None:
            children.append(default)
        for child in children:
            rt = child.response_type
            if isinstance(rt, tuple):
                types_list.extend(rt)
            else:
                types_list.append(rt)
        unique_types = tuple(dict.fromkeys(types_list))
        self.response_type: type[Response[Any]] | tuple[type[Response[Any]], ...] = (
            unique_types if unique_types else JSONResponse
        )

    def _resolve(self, status_code: int) -> Representation | None:
        for matcher, rep in self._routes:
            if isinstance(matcher, int):
                if status_code == matcher:
                    return rep
            elif status_code in matcher:
                return rep
        return self._default

    def media_type(self, status_code: int) -> str:
        rep = self._resolve(status_code)
        if rep is None:
            return "application/json"
        return rep.media_type(status_code)

    def body_type(self, status_code: int, returns: Any) -> Any:
        rep = self._resolve(status_code)
        if rep is None:
            return returns
        return rep.body_type(status_code, returns)

    def render(
        self, request: Request, value: Any, status_code: int
    ) -> Response[Any] | Awaitable[Response[Any]]:
        rep = self._resolve(status_code)
        if rep is None:
            if isinstance(value, Exception):
                from chano.app import error_response

                return error_response(value)
            return JSONResponse(value, status_code=status_code)
        return rep.render(request, value, status_code)


def default_status_description(status_code: int, description: str | None) -> str:
    """Return ``description`` if set, else the HTTP phrase for ``status_code``."""
    if description is not None:
        return description
    if status_code == 200:
        return "Successful Response"
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error Response" if status_code >= 400 else "Successful Response"


class _RawType:
    """Sentinel placed in ``Annotated[T, Raw]`` to opt out of ``representation.py``."""

    def __repr__(self) -> str:
        return "Raw"


Raw = _RawType()


@dataclass(frozen=True, slots=True)
class Status:
    """Declare an HTTP status code and optional OpenAPI description on a return type."""

    status_code: int
    description: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError(
                f"Status() takes an int status code, got {self.status_code!r}"
            )
        if not 200 <= self.status_code <= 599:
            raise ValueError(
                "Status() takes a valid final HTTP status code (200-599), "
                f"got {self.status_code}"
            )
        if self.description is not None and not isinstance(self.description, str):
            raise TypeError(
                f"Status() `description` must be a str, got {self.description!r}"
            )


def _unwrap_partial(func: Any) -> Any:
    while isinstance(func, functools.partial):
        func = func.func
    return func


def _wrap_return_annotation[F: Callable[..., Any]](func: F, meta: Any) -> F:
    """Wrap ``func``'s return annotation in ``Annotated[..., meta]``.

    A missing return annotation is initialized to ``Annotated[None, meta]``.
    On Python 3.14+ (PEP 649), wrapping ``__annotate__`` keeps forward
    references deferred without triggering eager evaluation at decoration time.
    """
    annotate = getattr(func, "__annotate__", None)
    if callable(annotate):
        if sys.version_info >= (3, 14):
            import annotationlib

            def _annotate(
                fmt: Any,
                _orig: Any = annotate,
                _meta: Any = meta,
                _owner: Any = func,
            ) -> Any:
                if fmt == annotationlib.Format.STRING:
                    anns = dict(
                        annotationlib.call_annotate_function(_orig, fmt, owner=_owner)
                    )
                    ret = anns.get("return")
                    ret_str = ret if ret is not None else "None"
                    anns["return"] = f"Annotated[{ret_str}, {_meta!r}]"
                    return anns
                if fmt not in (
                    annotationlib.Format.VALUE,
                    annotationlib.Format.FORWARDREF,
                ):
                    raise NotImplementedError
                anns = dict(
                    annotationlib.call_annotate_function(_orig, fmt, owner=_owner)
                )
                ret = anns.get("return")
                anns["return"] = Annotated[ret, _meta]
                return anns

        else:  # pragma: no cover - Python <3.14 has no __annotate__

            def _annotate(fmt: Any, _orig: Any = annotate, _meta: Any = meta) -> Any:
                if fmt not in (1, 2):
                    raise NotImplementedError
                anns = dict(_orig(fmt))
                ret = anns.get("return")
                anns["return"] = Annotated[ret, _meta]
                return anns

        target: Any = func
        target.__annotate__ = _annotate
        return func

    if isinstance(func, functools.partial):
        own = func.__dict__.get("__annotations__")
        anns = dict(own) if isinstance(own, dict) else {}
        if "return" in anns:
            ret = anns["return"]
        else:
            inherited = _raw_return_annotation_chain(func.func)
            ret = None if inherited is _EMPTY else inherited
        anns["return"] = Annotated[ret, meta]
        target = func
        target.__annotations__ = anns
        return func

    if not inspect.isroutine(func) and not isinstance(func, type):
        own = getattr(func, "__dict__", {}).get("__annotations__")
        if own is not None:
            anns = dict(own)
        else:
            call_ret = _raw_return_annotation(type(func).__call__)
            anns = {"return": call_ret} if call_ret is not _EMPTY else {}
    else:
        anns = dict(getattr(func, "__annotations__", {}))
    ret = anns.get("return")
    anns["return"] = Annotated[ret, meta]
    target = func
    target.__annotations__ = anns
    return func


def status_code[F: Callable[..., Any]](
    code: int, *, description: str | None = None
) -> Callable[[F], F]:
    """Attach ``Status(code, description=description)`` to a handler's return type."""
    meta = Status(code, description=description)

    def decorate(func: F) -> F:
        return _wrap_return_annotation(func, meta)

    return decorate


def raises[F: Callable[..., Any]](
    status_code: int, description: str
) -> Callable[[F], F]:
    """Document that a handler or dependency may respond with an error.

    ``@raises(401, "Not authenticated")`` wraps the function's return
    annotation in ``Annotated[..., Status(401, description="Not authenticated")]``.
    Stack it for several statuses; when stacked decorators repeat a status,
    the outermost one wins. It only documents: raise ``HTTPException`` to
    send the response.
    """
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        raise TypeError(f"raises() takes an int status code, got {status_code!r}")
    if not 400 <= status_code <= 599:
        raise ValueError(f"raises() takes an error status code, got {status_code}")
    if not isinstance(description, str):
        raise TypeError(f"raises() `description` must be a str, got {description!r}")
    meta = Status(status_code, description=description)

    def decorate(func: F) -> F:
        return _wrap_return_annotation(func, meta)

    return decorate


def representation[F: Callable[..., Any]](
    rep: Representation | _RawType | None,
) -> Callable[[F], F]:
    """Attach a per-handler ``Representation`` override or ``None``/``Raw`` opt-out."""
    if rep is None or rep is Raw:
        meta: Any = Raw
    elif is_representation(rep):
        meta = rep
    else:
        raise TypeError(
            f"representation() expected a Representation, Raw, or None, got {rep!r}"
        )

    def decorate(func: F) -> F:
        return _wrap_return_annotation(func, meta)

    return decorate


@dataclass(frozen=True, slots=True)
class ReturnBranch:
    """One response branch parsed from a handler's return annotation."""

    status_code: int
    description: str | None
    payload_type: Any
    response_cls: type[Response[Any]] | None
    explicit_status: bool


@dataclass(frozen=True, slots=True)
class ReturnSpec:
    """Return-level metadata extracted from a callable's return annotation."""

    branches: tuple[ReturnBranch, ...]
    error_statuses: dict[int, str | None]
    representation: Representation | None
    has_representation: bool

    @property
    def default_status(self) -> int | None:
        """Unambiguous ``< 400`` status from ``Status(...)``, or ``None``."""
        statuses = {branch.status_code for branch in self.branches}
        if len(statuses) == 1 and any(b.explicit_status for b in self.branches):
            return next(iter(statuses))
        return None

    @property
    def has_ambiguous_status(self) -> bool:
        """True when multiple distinct ``< 400`` statuses are declared."""
        return len({branch.status_code for branch in self.branches}) > 1


def _has_own_return_annotation(obj: Any) -> bool:
    annotate = getattr(obj, "__annotate__", None)
    if callable(annotate) and sys.version_info >= (3, 14):
        import annotationlib

        anns = annotationlib.get_annotations(
            obj, format=annotationlib.Format.FORWARDREF
        )
        return "return" in anns
    own = getattr(obj, "__dict__", {}).get("__annotations__")
    if isinstance(own, dict):
        return "return" in own
    return False


def _raw_return_annotation(target: Any) -> Any:
    annotate = getattr(target, "__annotate__", None)
    if callable(annotate) and sys.version_info >= (3, 14):
        import annotationlib

        anns = annotationlib.get_annotations(
            target, format=annotationlib.Format.FORWARDREF
        )
        return anns.get("return", _EMPTY)
    anns = getattr(target, "__annotations__", None)
    if isinstance(anns, dict):
        return anns.get("return", _EMPTY)
    return _EMPTY


def _raw_return_annotation_chain(obj: Any) -> Any:
    while isinstance(obj, functools.partial):
        own = obj.__dict__.get("__annotations__")
        if isinstance(own, dict) and "return" in own:
            return own["return"]
        obj = obj.func
    if (
        not inspect.isroutine(obj)
        and not isinstance(obj, type)
        and callable(obj)
        and not _has_own_return_annotation(obj)
    ):
        obj = obj.__call__
    raw = _raw_return_annotation(obj)
    if raw is not _EMPTY:
        return raw
    unwrapped = inspect.unwrap(obj)
    if unwrapped is not obj:
        return _raw_return_annotation(unwrapped)
    return _EMPTY


def _eval_return_annotation(raw: Any, globalns: dict[str, Any] | None) -> Any:
    if raw is _EMPTY:
        return _EMPTY

    def _stub() -> Any:
        pass

    _stub.__annotations__ = {"return": raw}
    try:
        hints = get_type_hints(_stub, globalns=globalns, include_extras=True)
    except (NameError, TypeError, AttributeError):
        return raw
    return hints.get("return", raw)


def get_return_annotation(func: Callable[..., Any]) -> Any:
    """Resolve ``func``'s return annotation, including forward refs in ``Annotated``."""
    base: Any = _unwrap_partial(func)
    if (
        not inspect.isroutine(base)
        and not isinstance(base, type)
        and callable(base)
        and not _has_own_return_annotation(base)
    ):
        base = base.__call__
    unwrapped = inspect.unwrap(base)

    globalns = getattr(unwrapped, "__globals__", None)
    if globalns is None and (mod := getattr(unwrapped, "__module__", None)):
        module = sys.modules.get(mod)
        globalns = module.__dict__ if module is not None else None

    if isinstance(func, functools.partial):
        raw = _raw_return_annotation_chain(func)
        return _eval_return_annotation(raw, globalns)

    target: Any = base
    if target is not unwrapped and not _has_own_return_annotation(target):
        target = unwrapped
    try:
        hints = get_type_hints(target, globalns=globalns, include_extras=True)
    except (TypeError, AttributeError):
        return _EMPTY
    except NameError:
        raw = _raw_return_annotation(target)
        return _eval_return_annotation(raw, globalns)
    return hints.get("return", _EMPTY)


def _substitute_type_vars(ann: Any, mapping: dict[Any, Any]) -> Any:
    if isinstance(ann, TypeVar | ParamSpec | TypeVarTuple) and ann in mapping:
        return mapping[ann]
    origin = get_origin(ann)
    if origin is None:
        return ann
    args = get_args(ann)
    if origin is Annotated:
        inner, *meta = args
        new_inner = _substitute_type_vars(inner, mapping)
        if new_inner is inner:
            return ann
        return Annotated[(new_inner, *meta)]
    if origin is types.UnionType or origin is Union:
        new_args = tuple(_substitute_type_vars(arg, mapping) for arg in args)
        if new_args == args:
            return ann
        return operator.getitem(Union, new_args)
    new_args = tuple(_substitute_type_vars(arg, mapping) for arg in args)
    if new_args == args:
        return ann
    if hasattr(ann, "copy_with"):
        return ann.copy_with(new_args)
    return origin[new_args[0] if len(new_args) == 1 else new_args]


_MAX_ALIAS_DEPTH = 64


def _safe_alias_arg(arg: Any) -> Any:
    try:
        hash(arg)
        return arg
    except TypeError:
        origin = get_origin(arg)
        if origin is not None:
            return (id(origin), tuple(_safe_alias_arg(a) for a in get_args(arg)))
        return id(arg)


def _expand_alias(ann: Any, seen: frozenset[Any]) -> tuple[Any, frozenset[Any]]:
    if len(seen) >= _MAX_ALIAS_DEPTH:
        return ann, seen
    if isinstance(ann, TypeAliasType):
        key: Any = (id(ann), ())
        if key in seen:
            return ann, seen
        return ann.__value__, seen | {key}
    origin = get_origin(ann)
    if isinstance(origin, TypeAliasType):
        args = get_args(ann)
        key = (id(origin), tuple(_safe_alias_arg(arg) for arg in args))
        if key in seen:
            return ann, seen
        mapping = dict(zip(origin.__type_params__, args, strict=False))
        return _substitute_type_vars(origin.__value__, mapping), seen | {key}
    return ann, seen


def _is_chano_metadata(item: Any) -> bool:
    return isinstance(item, Status) or item is Raw or is_representation(item)


def _unwrap_response(ann: Any) -> tuple[type[Response[Any]] | None, Any]:
    if isinstance(ann, type) and issubclass(ann, Response):
        return ann, _EMPTY
    origin = get_origin(ann)
    if isinstance(origin, type) and issubclass(origin, Response):
        args = get_args(ann)
        return origin, args[0] if args else _EMPTY
    return None, ann


def _has_chano_or_response(ann: Any, seen: frozenset[Any]) -> bool:
    if ann is _EMPTY:
        return False
    origin = get_origin(ann)
    if origin is Annotated:
        inner, *meta = get_args(ann)
        if any(_is_chano_metadata(item) for item in meta):
            return True
        return _has_chano_or_response(inner, seen)
    if isinstance(ann, TypeAliasType) or isinstance(origin, TypeAliasType):
        expanded, next_seen = _expand_alias(ann, seen)
        if expanded is ann:
            return False
        return _has_chano_or_response(expanded, next_seen)
    if origin is types.UnionType or origin is Union:
        return any(_has_chano_or_response(arg, seen) for arg in get_args(ann))
    response_cls, _ = _unwrap_response(ann)
    return response_cls is not None


def _reattach_meta(payload: Any, extra_meta: tuple[Any, ...]) -> Any:
    if not extra_meta or payload in (_EMPTY, None, type(None)):
        return payload
    return Annotated[(payload, *extra_meta)]


def _collect_return(
    ann: Any,
    inherited_status: Status | None,
    extra_meta: tuple[Any, ...],
    error_statuses_out: dict[int, str | None],
    rep_state: list[Any],
    branches_out: list[ReturnBranch],
    seen: frozenset[Any],
) -> None:
    if ann is _EMPTY:
        branches_out.append(
            ReturnBranch(
                status_code=(
                    inherited_status.status_code
                    if inherited_status is not None
                    else 200
                ),
                description=(
                    inherited_status.description
                    if inherited_status is not None
                    else None
                ),
                payload_type=_EMPTY,
                response_cls=None,
                explicit_status=inherited_status is not None,
            )
        )
        return

    origin = get_origin(ann)
    if origin is Annotated:
        inner, *meta = get_args(ann)
        local_status: Status | None = None
        local_error_statuses: list[Status] = []
        local_reps: list[Any] = []
        other_meta: list[Any] = []
        for item in meta:
            if isinstance(item, Status):
                if item.status_code < 400:
                    local_status = item
                else:
                    local_error_statuses.append(item)
            elif item is Raw or is_representation(item):
                local_reps.append(item)
            else:
                other_meta.append(item)

        effective_status = (
            inherited_status if inherited_status is not None else local_status
        )
        combined_extra = (*other_meta, *extra_meta)
        _collect_return(
            inner,
            effective_status,
            combined_extra,
            error_statuses_out,
            rep_state,
            branches_out,
            seen,
        )
        # Apply outer error Status / Representation / Raw after inner so outer wins.
        for err_status in local_error_statuses:
            if (
                err_status.description is not None
                or err_status.status_code not in error_statuses_out
            ):
                error_statuses_out[err_status.status_code] = err_status.description
        for rep_item in local_reps:
            rep_state[0] = True
            rep_state[1] = None if rep_item is Raw else rep_item
        return

    if isinstance(ann, TypeAliasType) or isinstance(origin, TypeAliasType):
        expanded, next_seen = _expand_alias(ann, seen)
        if expanded is not ann and _has_chano_or_response(expanded, next_seen):
            _collect_return(
                expanded,
                inherited_status,
                extra_meta,
                error_statuses_out,
                rep_state,
                branches_out,
                next_seen,
            )
            return

    if origin is types.UnionType or origin is Union:
        args = get_args(ann)
        if any(_has_chano_or_response(arg, seen) for arg in args):
            declared_reps: list[Representation | None] = []
            for arg in args:
                branch_rep_state: list[Any] = [False, None]
                _collect_return(
                    arg,
                    inherited_status,
                    extra_meta,
                    error_statuses_out,
                    branch_rep_state,
                    branches_out,
                    seen,
                )
                if branch_rep_state[0]:
                    declared_reps.append(branch_rep_state[1])
            if declared_reps:
                first = declared_reps[0]
                if any(rep is not first and rep != first for rep in declared_reps[1:]):
                    raise ValueError(
                        "conflicting representations in return type union: "
                        "apply a single Representation or Raw outside the union"
                    )
                rep_state[0] = True
                rep_state[1] = first
            return

    response_cls, payload = _unwrap_response(ann)
    branches_out.append(
        ReturnBranch(
            status_code=(
                inherited_status.status_code if inherited_status is not None else 200
            ),
            description=(
                inherited_status.description if inherited_status is not None else None
            ),
            payload_type=_reattach_meta(payload, extra_meta),
            response_cls=response_cls,
            explicit_status=inherited_status is not None,
        )
    )


def parse_return(func: Callable[..., Any]) -> ReturnSpec:
    """Extract ``Status``, ``Representation``/``Raw``, and payload types."""
    ann = get_return_annotation(func)
    error_statuses_out: dict[int, str | None] = {}
    rep_state: list[Any] = [False, None]
    branches_out: list[ReturnBranch] = []
    _collect_return(
        ann, None, (), error_statuses_out, rep_state, branches_out, frozenset()
    )
    return ReturnSpec(
        branches=tuple(branches_out),
        error_statuses=error_statuses_out,
        representation=rep_state[1],
        has_representation=rep_state[0],
    )
