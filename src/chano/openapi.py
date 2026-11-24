"""Generate an OpenAPI 3.1 document, and a Swagger UI page to browse it."""

from __future__ import annotations

import copy
import html
import inspect
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ForwardRef,
    Literal,
    TypeAliasType,
    get_args,
    get_origin,
)

import msgspec

from chano._pydantic import json_schema as pydantic_json_schema
from chano._pydantic import pydantic_adapter
from chano.params import Endpoint, RequestInput
from chano.representation import (
    ReturnBranch,
    matches_response_type,
    resolve_representation,
)
from chano.responses import JSONResponse, is_bodyless_status

if TYPE_CHECKING:
    from chano.app import Chano
    from chano.representation import Representation

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


_VALIDATION_ERROR = {
    "description": "Validation Error",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "detail": {"type": "array", "items": {"type": "object"}}
                },
            }
        }
    },
}


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
    (schema,), definitions = msgspec.json.schema_components(
        [annotation], ref_template=_REF
    )
    return _merge_components(schema, definitions, components)


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


def _contains_forward_ref(annotation: Any, seen: frozenset[int] = frozenset()) -> bool:
    if isinstance(annotation, ForwardRef):
        return True
    if isinstance(annotation, TypeAliasType):
        if id(annotation) in seen:
            return False
        return _contains_forward_ref(annotation.__value__, seen | {id(annotation)})
    origin = get_origin(annotation)
    if origin is None:
        return False
    if origin is Annotated:
        inner, *_ = get_args(annotation)
        return _contains_forward_ref(inner, seen)
    if (
        isinstance(origin, TypeAliasType)
        and id(origin) not in seen
        and _contains_forward_ref(origin.__value__, seen | {id(origin)})
    ):
        return True
    return any(_contains_forward_ref(arg, seen) for arg in get_args(annotation))


def _status_description(
    status_code: int, explicit: str | None, fallback: str | None
) -> str:
    if explicit is not None:
        return explicit
    if fallback is not None:
        return fallback
    if status_code == 200:
        return "Successful Response"
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Successful Response"


def _error_openapi_response(
    description: str,
    status: int,
    representation: Representation,
    components: dict[str, Any],
) -> dict[str, Any]:
    entry: dict[str, Any] = {"description": description}
    error_annotation = representation.body_type(status, None)
    if error_annotation is not None:
        entry["content"] = {
            representation.media_type(status): {
                "schema": _schema(error_annotation, components, mode="serialization")
            }
        }
    return entry


def _operation(
    method: str,
    path: str,
    endpoint: Endpoint,
    components: dict[str, Any],
    security_schemes: dict[str, Any],
    representation: Representation | None = None,
) -> dict[str, Any]:
    info = openapi_info(endpoint.func)
    doc = inspect.getdoc(endpoint.func) or ""
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

    fields = endpoint.request_inputs()
    parameters = [
        {
            "name": field.alias,
            "in": field.source,
            "required": field.required,
            "schema": _input_schema(field, components),
        }
        for field in fields
        if field.source != "body"
    ]
    if parameters:
        operation["parameters"] = parameters

    body = [field for field in fields if field.source == "body"]
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
    if body:
        operation["requestBody"] = {
            "required": any(field.required for field in body),
            "content": {"application/json": {"schema": body_schema}},
        }

    return_spec = endpoint.return_spec
    active_rep = (
        return_spec.representation if return_spec.has_representation else representation
    )
    grouped: dict[int, list[ReturnBranch]] = {}
    for branch in return_spec.branches:
        grouped.setdefault(branch.status_code, []).append(branch)

    responses: dict[int, Any] = {}
    for status, branches in grouped.items():
        explicit_desc = next(
            (b.description for b in branches if b.description is not None), None
        )
        fallback_desc = (
            info.response_description
            if info is not None
            and info.response_description is not None
            and (status == 200 or len(grouped) == 1)
            else None
        )
        entry: dict[str, Any] = {
            "description": _status_description(status, explicit_desc, fallback_desc)
        }
        if not is_bodyless_status(status):
            media_entries: dict[str, list[tuple[Any, bool]]] = {}
            for branch in branches:
                payload = branch.payload_type
                if payload is inspect.Signature.empty:
                    continue
                effective_cls = (
                    JSONResponse if branch.response_cls is None else branch.response_cls
                )
                if active_rep is not None and matches_response_type(
                    active_rep, effective_cls, status
                ):
                    media_entries.setdefault(active_rep.media_type(status), []).append(
                        (payload, True)
                    )
                elif effective_cls.media_type is not None:
                    media_entries.setdefault(effective_cls.media_type, []).append(
                        (payload, False)
                    )
            content: dict[str, Any] = {}
            for media_type, items in media_entries.items():
                if not any(uses_rep for _, uses_rep in items) and all(
                    p in (None, type(None)) for p, _ in items
                ):
                    continue
                schemas: list[dict[str, Any]] = []
                for payload, uses_rep in items:
                    if _contains_forward_ref(payload):
                        continue
                    if uses_rep and active_rep is not None:
                        rep_payload = None if payload is type(None) else payload
                        body_type = active_rep.body_type(status, rep_payload)
                    else:
                        body_type = payload
                    if body_type is inspect.Signature.empty or _contains_forward_ref(
                        body_type
                    ):
                        continue
                    if body_type in (None, type(None)) and (
                        uses_rep or payload not in (None, type(None))
                    ):
                        continue
                    branch_schema = _schema(body_type, components, mode="serialization")
                    if branch_schema not in schemas:
                        schemas.append(branch_schema)
                if schemas:
                    content[media_type] = {
                        "schema": (
                            schemas[0] if len(schemas) == 1 else {"anyOf": schemas}
                        )
                    }
            if content:
                entry["content"] = content
        responses[status] = entry

    if fields and 422 not in responses:
        if active_rep is None or resolve_representation(active_rep, 422) is None:
            responses[422] = _VALIDATION_ERROR
        else:
            responses[422] = _error_openapi_response(
                "Validation Error", 422, active_rep, components
            )
    for status, description in endpoint.declared_responses().items():
        if status in responses:
            continue
        if active_rep is None or resolve_representation(active_rep, status) is None:
            responses[status] = {"description": description}
        else:
            responses[status] = _error_openapi_response(
                description, status, active_rep, components
            )
    operation["responses"] = {str(k): v for k, v in sorted(responses.items())}

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
        representation = app.representations.get(path)
        # Components are collected only from documented operations, so a
        # schema or scheme used solely by hidden ones is left out too.
        operations = {
            method.lower(): _operation(
                method,
                path,
                endpoint,
                components,
                security_schemes,
                representation,
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
