"""Generate an OpenAPI 3.1 document, and a Swagger UI page to browse it."""

from __future__ import annotations

import copy
import functools
import html
import inspect
import json
import operator
import re
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

import msgspec

from chano._pydantic import json_schema as pydantic_json_schema
from chano._pydantic import pydantic_adapter
from chano.exceptions import RequestValidationError
from chano.params import Endpoint, RequestInput
from chano.responses import (
    Failure,
    Outcome,
    Response,
    _substitute_typevars,
    _type_param_mapping,
    _unwrap_type,
    resolve_outcome_content,
)

if TYPE_CHECKING:
    from chano.app import Boundary, Chano

_REF = "#/components/schemas/{name}"
_PYDANTIC_REF = "#/components/schemas/{model}"
_OPENAPI = "__chano_openapi__"


@dataclass(frozen=True, slots=True)
class OpenAPIInfo:
    """OpenAPI operation metadata attached to a route handler via ``@openapi``."""

    include_in_schema: bool | None = None
    summary: str | None = None
    description: str | None = None
    operation_id: str | None = None
    tags: tuple[str, ...] | None = None
    deprecated: bool | None = None
    response_description: str | None = None
    openapi_extra: dict[str, Any] | None = None


def _deep_merge(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {k: copy.deepcopy(v) for k, v in base.items()}
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _check_optional_bool(name: str, value: Any) -> None:
    if value is not None and not isinstance(value, bool):
        raise TypeError(f"openapi() `{name}` must be a bool, got {value!r}")


def _check_optional_str(name: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"openapi() `{name}` must be a str, got {value!r}")


def openapi[F: Callable[..., Any]](
    *,
    include_in_schema: bool | None = None,
    summary: str | None = None,
    description: str | None = None,
    operation_id: str | None = None,
    tags: Sequence[str] | None = None,
    deprecated: bool | None = None,
    response_description: str | None = None,
    openapi_extra: Mapping[str, Any] | None = None,
) -> Callable[[F], F]:
    """Attach OpenAPI operation metadata to a route handler.

    ``@openapi`` only affects the generated OpenAPI document and ``/docs``;
    it never changes request dispatch or response behavior. When stacked,
    the outermost decorator wins for each specified field, and
    ``openapi_extra`` mappings are deep-merged with outer keys winning.
    """
    _check_optional_bool("include_in_schema", include_in_schema)
    _check_optional_bool("deprecated", deprecated)
    _check_optional_str("summary", summary)
    _check_optional_str("description", description)
    _check_optional_str("operation_id", operation_id)
    _check_optional_str("response_description", response_description)

    normalized_tags: tuple[str, ...] | None = None
    if tags is not None:
        if (
            not isinstance(tags, Sequence)
            or isinstance(tags, str | bytes)
            or not all(isinstance(tag, str) for tag in tags)
        ):
            raise TypeError(f"openapi() `tags` must be a sequence of str, got {tags!r}")
        normalized_tags = tuple(tags)

    normalized_extra: dict[str, Any] | None = None
    if openapi_extra is not None:
        if not isinstance(openapi_extra, Mapping) or not all(
            isinstance(key, str) for key in openapi_extra
        ):
            raise TypeError(
                "openapi() `openapi_extra` must be a mapping with str keys, "
                f"got {openapi_extra!r}"
            )
        normalized_extra = _deep_merge({}, openapi_extra)

    def decorate(func: F) -> F:
        existing = openapi_info(func)
        if existing is None:
            merged = OpenAPIInfo(
                include_in_schema=include_in_schema,
                summary=summary,
                description=description,
                operation_id=operation_id,
                tags=normalized_tags,
                deprecated=deprecated,
                response_description=response_description,
                openapi_extra=normalized_extra,
            )
        else:
            if existing.openapi_extra is not None and normalized_extra is not None:
                merged_extra: dict[str, Any] | None = _deep_merge(
                    existing.openapi_extra, normalized_extra
                )
            elif normalized_extra is not None:
                merged_extra = normalized_extra
            else:
                merged_extra = existing.openapi_extra
            merged = OpenAPIInfo(
                include_in_schema=(
                    include_in_schema
                    if include_in_schema is not None
                    else existing.include_in_schema
                ),
                summary=summary if summary is not None else existing.summary,
                description=(
                    description if description is not None else existing.description
                ),
                operation_id=(
                    operation_id if operation_id is not None else existing.operation_id
                ),
                tags=normalized_tags if normalized_tags is not None else existing.tags,
                deprecated=(
                    deprecated if deprecated is not None else existing.deprecated
                ),
                response_description=(
                    response_description
                    if response_description is not None
                    else existing.response_description
                ),
                openapi_extra=merged_extra,
            )
        setattr(func, _OPENAPI, merged)
        return func

    return decorate


def openapi_info(func: Callable[..., Any]) -> OpenAPIInfo | None:
    """Return ``@openapi`` metadata attached to ``func``, if any."""
    info = getattr(func, _OPENAPI, None)
    return info if isinstance(info, OpenAPIInfo) else None


def _split_annotated(annotation: Any) -> tuple[Any, tuple[Any, ...]]:
    meta: list[Any] = []
    while get_origin(annotation) is Annotated:
        annotation, *rest = get_args(annotation)
        meta.extend(rest)
    return annotation, tuple(meta)


def _wrap_annotated(annotation: Any, meta: tuple[Any, ...]) -> Any:
    return Annotated[(annotation, *meta)] if meta else annotation


def _iter_union_members(annotation: Any) -> Iterator[Any]:
    inner, meta = _split_annotated(annotation)
    if isinstance(inner, TypeAliasType):
        expanded = _substitute_typevars(inner.__value__, _type_param_mapping(inner, ()))
        yield from _iter_union_members(_wrap_annotated(expanded, meta))
        return
    origin = get_origin(inner)
    if isinstance(origin, TypeAliasType):
        expanded = _substitute_typevars(
            origin.__value__, _type_param_mapping(origin, get_args(inner))
        )
        yield from _iter_union_members(_wrap_annotated(expanded, meta))
        return
    if origin in (types.UnionType, Union):
        members = [m for arg in get_args(inner) for m in _iter_union_members(arg)]
        if meta:
            plain = [
                m
                for m in members
                if _outcome_class(m) is None and not _is_response_type(m)
            ]
            other = [
                m
                for m in members
                if _outcome_class(m) is not None or _is_response_type(m)
            ]
            if plain:
                yield _wrap_annotated(functools.reduce(operator.or_, plain), meta)
            yield from other
            return
        yield from members
        return
    if _outcome_class(inner) is not None or _is_response_type(inner):
        yield inner
        return
    yield _wrap_annotated(inner, meta)


def _outcome_class(annotation: Any) -> type[Outcome[Any]] | None:
    inner, _ = _split_annotated(annotation)
    cls = get_origin(inner) or inner
    return cls if isinstance(cls, type) and issubclass(cls, Outcome) else None


def _is_response_type(annotation: Any) -> bool:
    inner, _ = _split_annotated(annotation)
    cls = get_origin(inner) or inner
    return isinstance(cls, type) and issubclass(cls, Response)


def _outcome_description(cls: type[Outcome[Any]]) -> str:
    own_doc = cls.__dict__.get("__doc__")
    if isinstance(own_doc, str) and own_doc.strip():
        return inspect.cleandoc(own_doc)
    try:
        return HTTPStatus(cls.status).phrase
    except ValueError:
        return f"HTTP {cls.status}"


def _add_schema(entry: dict[str, Any], media_type: str, schema: dict[str, Any]) -> None:
    content = entry.setdefault("content", {})
    if media_type not in content:
        content[media_type] = {"schema": schema}
        return
    existing = content[media_type]["schema"]
    if existing == schema:
        return
    variants = list(existing["anyOf"]) if list(existing) == ["anyOf"] else [existing]
    if schema not in variants:
        variants.append(schema)
    content[media_type]["schema"] = {"anyOf": variants}


def _add_outcome(
    responses: dict[int, dict[str, Any]],
    annotation: Any,
    components: dict[str, Any],
) -> None:
    cls = _outcome_class(annotation)
    status = getattr(cls, "status", None) if cls is not None else None
    if cls is None or not isinstance(status, int) or isinstance(status, bool):
        return
    entry = responses.setdefault(status, {"description": _outcome_description(cls)})
    content_type = resolve_outcome_content(annotation)
    if (
        100 <= status < 200
        or status in (204, 304)
        or content_type in (None, type(None))
    ):
        return
    _add_schema(
        entry,
        cls.media_type or "application/json",
        _schema(content_type, components, mode="serialization"),
    )


def _rewrite_refs(node: Any, renames: dict[str, str]) -> Any:
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value in renames:
                result[key] = renames[value]
            elif key == "discriminator" and isinstance(value, dict):
                disc = _rewrite_refs(value, renames)
                if isinstance(mapping := disc.get("mapping"), dict):
                    disc["mapping"] = {
                        k: renames.get(v, v) if isinstance(v, str) else v
                        for k, v in mapping.items()
                    }
                result[key] = disc
            else:
                result[key] = _rewrite_refs(value, renames)
        return result
    if isinstance(node, list):
        return [_rewrite_refs(item, renames) for item in node]
    return node


def _merge_components(
    schema: dict[str, Any],
    definitions: dict[str, Any],
    components: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``definitions`` into ``components`` without overwriting distinct
    same-named schemas, rewriting ``$ref`` targets when a suffix is allocated."""
    if not definitions:
        return schema

    name_map = {name: name for name in definitions}
    reserved = set(components) | set(definitions)
    while True:
        renames = {
            f"#/components/schemas/{old}": f"#/components/schemas/{new}"
            for old, new in name_map.items()
            if old != new
        }
        rewritten = {
            name_map[old]: _rewrite_refs(def_schema, renames)
            for old, def_schema in definitions.items()
        }
        changed = False
        for old, current in list(name_map.items()):
            if current in components and components[current] != rewritten[current]:
                n = 2
                while f"{old}_{n}" in reserved:
                    n += 1
                fresh = f"{old}_{n}"
                reserved.add(fresh)
                name_map[old] = fresh
                changed = True
        if not changed:
            components.update(rewritten)
            return _rewrite_refs(schema, renames)


def _schema(
    annotation: Any,
    components: dict[str, Any],
    *,
    mode: Literal["validation", "serialization"] = "validation",
) -> dict[str, Any]:
    if (adapter := pydantic_adapter(annotation)) is not None:
        schema = pydantic_json_schema(adapter, ref_template=_PYDANTIC_REF, mode=mode)
        defs = schema.pop("$defs", None) or {}
        return _merge_components(schema, defs, components)
    try:
        (schema,), definitions = msgspec.json.schema_components(
            [annotation], ref_template=_REF
        )
        return _merge_components(schema, definitions, components)
    except TypeError:
        inner, meta = _split_annotated(annotation)
        if get_origin(inner) not in (types.UnionType, Union):
            raise
        variants: list[dict[str, Any]] = []
        for branch in get_args(inner):
            branch_schema = _schema(branch, components, mode=mode)
            if branch_schema not in variants:
                variants.append(branch_schema)
        result: dict[str, Any] = (
            variants[0] if len(variants) == 1 else {"anyOf": variants}
        )
        if meta:
            result = {**result, **msgspec.json.schema(_wrap_annotated(Any, meta))}
        return result


def _input_schema(
    request_input: RequestInput, components: dict[str, Any]
) -> dict[str, Any]:
    """The schema every consumer of ``request_input`` accepts: its one
    distinct schema, or the ``allOf`` of them, since each one validates."""
    schemas: list[dict[str, Any]] = []
    for annotation in request_input.annotations:
        if (schema := _schema(annotation, components)) not in schemas:
            schemas.append(schema)
    return schemas[0] if len(schemas) == 1 else {"allOf": schemas}


def _has_fixed_status(cls: type[Outcome[Any]] | None) -> bool:
    status = getattr(cls, "status", None) if cls is not None else None
    return isinstance(status, int) and not isinstance(status, bool)


def _recover_rules(boundaries: Sequence[Boundary]) -> list[tuple[Any, Any | None]]:
    rules: list[tuple[Any, Any | None]] = []
    for boundary in reversed(boundaries):
        for fn in boundary.recover:
            sig = inspect.signature(fn, eval_str=True)
            params = list(sig.parameters.values())
            p_ann = (
                Exception
                if len(params) < 2 or params[1].annotation is inspect.Parameter.empty
                else params[1].annotation
            )
            p_unwrapped = _unwrap_type(p_ann)
            r_unwrapped = _unwrap_type(sig.return_annotation)
            replacement = (
                r_unwrapped
                if _has_fixed_status(_outcome_class(p_unwrapped))
                and _has_fixed_status(_outcome_class(r_unwrapped))
                else None
            )
            rules.append((p_unwrapped, replacement))
    return rules


def _payload_matches(actual: Any, expected: type) -> bool:
    actual = _unwrap_type(actual)
    if actual is Any:
        return expected is object
    origin = get_origin(actual)
    if origin in (types.UnionType, Union):
        return all(_payload_matches(arg, expected) for arg in get_args(actual))
    actual_cls = origin if isinstance(origin, type) else actual
    return isinstance(actual_cls, type) and issubclass(actual_cls, expected)


def _failure_matches_param(outcome_ann: Any, param_ann: Any) -> bool:
    out_cls = _outcome_class(outcome_ann)
    if out_cls is None or not issubclass(out_cls, Failure):
        return False
    unwrapped = _unwrap_type(param_ann)
    if unwrapped is Any or unwrapped is object:
        return True
    origin = get_origin(unwrapped)
    if origin in (types.UnionType, Union):
        return any(_failure_matches_param(outcome_ann, a) for a in get_args(unwrapped))
    param_cls = origin if isinstance(origin, type) else unwrapped
    if not isinstance(param_cls, type) or not issubclass(out_cls, param_cls):
        return False
    if origin is not None and issubclass(param_cls, Outcome):
        args = get_args(unwrapped)
        if len(args) == 1:
            expected = _unwrap_type(args[0])
            if expected is not Any and isinstance(expected, type):
                return _payload_matches(resolve_outcome_content(outcome_ann), expected)
    return True


def _apply_recover_rules(
    annotation: Any, rules: Sequence[tuple[Any, Any | None]]
) -> Any:
    for param_ann, replacement in rules:
        if _failure_matches_param(annotation, param_ann):
            return annotation if replacement is None else replacement
    return annotation


def _operation_metadata(
    method: str,
    path: str,
    info: OpenAPIInfo | None,
    doc: str,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "summary": (
            info.summary
            if info is not None and info.summary is not None
            else doc.split("\n")[0] or f"{method} {path}"
        ),
        "operationId": (
            info.operation_id
            if info is not None and info.operation_id is not None
            else re.sub(r"\W+", "_", f"{method}_{path}".lower()).strip("_")
        ),
    }
    if info is not None and info.description is not None:
        operation["description"] = info.description
    elif "\n" in doc:
        operation["description"] = doc
    if info is not None and info.tags:
        operation["tags"] = list(info.tags)
    if info is not None and info.deprecated:
        operation["deprecated"] = True
    return operation


def _operation_parameters(
    fields: Sequence[RequestInput],
    components: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "name": field.alias,
            "in": field.source,
            "required": field.required,
            "schema": _input_schema(field, components),
        }
        for field in fields
        if field.source != "body"
    ]


def _operation_request_body(
    fields: Sequence[RequestInput],
    components: dict[str, Any],
) -> dict[str, Any] | None:
    body = [field for field in fields if field.source == "body"]
    if not body:
        return None
    # Each whole-body field decodes the entire body; embedded ones are keys
    # of one JSON object. A request must satisfy all of them at once.
    parts: list[dict[str, Any]] = []
    for field in body:
        if field.embed:
            continue
        if (part := _input_schema(field, components)) not in parts:
            parts.append(part)
    if embedded := [field for field in body if field.embed]:
        parts.append(
            {
                "type": "object",
                "properties": {f.alias: _input_schema(f, components) for f in embedded},
                "required": [f.alias for f in embedded if f.required],
            }
        )
    body_schema = parts[0] if len(parts) == 1 else {"allOf": parts}
    return {
        "required": any(field.required for field in body),
        "content": {"application/json": {"schema": body_schema}},
    }


def _add_handler_returns(
    responses: dict[int, dict[str, Any]],
    returns: Any,
    default_200_desc: str,
    rules: Sequence[tuple[Any, Any | None]],
    components: dict[str, Any],
) -> None:
    if returns in (inspect.Signature.empty, None):
        responses[200] = {"description": default_200_desc}
        return
    plain: list[Any] = []
    has_other_response = False
    for member in _iter_union_members(returns):
        if _outcome_class(member) is not None:
            _add_outcome(responses, _apply_recover_rules(member, rules), components)
        elif _is_response_type(member):
            has_other_response = True
        else:
            plain.append(member)
    if plain:
        entry = responses.setdefault(200, {"description": default_200_desc})
        if not (len(plain) == 1 and plain[0] is type(None)):
            for member in plain:
                _add_schema(
                    entry,
                    "application/json",
                    _schema(member, components, mode="serialization"),
                )
    elif has_other_response and 200 not in responses:
        responses[200] = {"description": default_200_desc}


def _operation_responses(
    endpoint: Endpoint,
    fields: Sequence[RequestInput],
    info: OpenAPIInfo | None,
    components: dict[str, Any],
    boundaries: Sequence[Boundary],
) -> dict[str, Any]:
    rules = _recover_rules(boundaries)
    responses: dict[int, dict[str, Any]] = {}
    default_200_desc = (
        info.response_description
        if info is not None and info.response_description is not None
        else "Successful Response"
    )
    returns = inspect.signature(endpoint.func, eval_str=True).return_annotation
    _add_handler_returns(responses, returns, default_200_desc, rules, components)
    if not responses:
        responses[200] = {"description": default_200_desc}

    for dep in endpoint.response_endpoints():
        if dep is endpoint:
            continue
        dep_returns = inspect.signature(dep.func, eval_str=True).return_annotation
        if dep_returns in (inspect.Signature.empty, None):
            continue
        for member in _iter_union_members(dep_returns):
            if _outcome_class(member) is not None:
                _add_outcome(responses, _apply_recover_rules(member, rules), components)

    if info is not None and info.response_description is not None and 200 in responses:
        responses[200]["description"] = info.response_description
    if fields:
        _add_outcome(
            responses, _apply_recover_rules(RequestValidationError, rules), components
        )
    return {str(k): v for k, v in sorted(responses.items())}


def _operation(
    method: str,
    path: str,
    endpoint: Endpoint,
    components: dict[str, Any],
    security_schemes: dict[str, Any],
    boundaries: Sequence[Boundary] = (),
) -> dict[str, Any]:
    info = openapi_info(endpoint.func)
    doc = inspect.getdoc(endpoint.func) or ""
    operation = _operation_metadata(method, path, info, doc)

    fields = endpoint.request_inputs()
    if parameters := _operation_parameters(fields, components):
        operation["parameters"] = parameters
    if (request_body := _operation_request_body(fields, components)) is not None:
        operation["requestBody"] = request_body

    operation["responses"] = _operation_responses(
        endpoint, fields, info, components, boundaries
    )

    # All dependencies run, so every scheme is required together: one
    # requirement object naming each of them.
    schemes = {scheme.name: scheme for scheme in endpoint.security_schemes()}
    if schemes:
        operation["security"] = [{name: [] for name in schemes}]
        for name, scheme in schemes.items():
            security_schemes[name] = scheme.openapi()
    if info is not None and info.openapi_extra is not None:
        operation = _deep_merge(operation, info.openapi_extra)
    return operation


def openapi_schema(app: Chano) -> dict[str, Any]:
    components: dict[str, Any] = {}
    security_schemes: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    for path, _, endpoints in app.routes:
        # Components are collected only from documented operations, so a
        # schema or scheme used solely by hidden ones is left out too.
        boundaries = app.boundaries.get(path, ())
        operations = {
            method.lower(): _operation(
                method, path, endpoint, components, security_schemes, boundaries
            )
            for method, endpoint in endpoints.items()
            if (path, method) not in app.hidden_operations
        }
        if operations:
            paths[path.replace(":path}", "}")] = operations
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": app.title, "version": app.version},
        "paths": paths,
    }
    if components:
        schema["components"] = {"schemas": components}
    if security_schemes:
        schema.setdefault("components", {})["securitySchemes"] = security_schemes
    return schema


def swagger_html(title: str, openapi_url: str = "/openapi.json") -> str:
    cdn = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"
    # A JS string literal that cannot close the inline <script> early.
    url = json.dumps(openapi_url).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{cdn}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{cdn}/swagger-ui-bundle.js"></script>
<script>
SwaggerUIBundle({{ url: {url}, dom_id: "#swagger-ui" }});
</script>
</body>
</html>
"""
