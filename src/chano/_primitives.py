"""Shared helpers for file-based primitives (routes, queues, schedules, workflows)."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from chano.params import is_generator_callable
from chano.requests import Message, Receive, Request, Scope, Send


def load_module(path: Path) -> ModuleType:
    """Import a route or segment file once per process, keyed only by its
    resolved path: files from different projects never share a
    ``sys.modules`` entry, and one file reached from several routes or
    prefixes reuses its module. A failed import leaves no partial module
    behind, so a retry runs the file again."""
    digest = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]
    name = f"chano_routes._{digest}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def discover_app_files(
    app_dir: str | Path,
    filename: str,
    *,
    label: str,
) -> list[tuple[str, Path]]:
    """Find all ``filename`` modules under ``app_dir`` as sorted ``(name, file)`` pairs.

    Strips ``(group)`` directory segments, rejects ``[param]`` dynamic segments,
    and falls back to ``"index"`` at the root of ``app_dir``.
    """
    app_path = Path(app_dir).resolve()
    if not app_path.is_dir():
        raise FileNotFoundError(f"app directory not found: {app_path}")

    project_root = str(app_path.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    discovered: list[tuple[str, Path]] = []
    for file in app_path.rglob(filename):
        parts = file.parent.relative_to(app_path).parts
        name_parts: list[str] = []
        for part in parts:
            if part.startswith("(") and part.endswith(")"):
                continue
            if part.startswith("[") and part.endswith("]"):
                raise ValueError(
                    f"invalid {label} path {file}: {filename} cannot live inside "
                    f"dynamic segment {part!r}"
                )
            name_parts.append(part)
        name = "/".join(name_parts) or "index"
        discovered.append((name, file))
    return sorted(discovered, key=lambda item: (item[0], item[1]))


def validate_handler(
    module: ModuleType,
    attr: str,
    file: Path,
    *,
    kind_plural: str,
) -> Callable[..., Any]:
    """Read and validate a non-generator callable handler attribute from ``module``."""
    handler = getattr(module, attr, None)
    if not callable(handler):
        raise TypeError(
            f"invalid `{attr}` in {file}: expected a callable handler, got {handler!r}"
        )
    if is_generator_callable(handler):
        raise TypeError(
            f"invalid `{attr}` in {file}: generator functions are not "
            f"supported as {kind_plural}"
        )
    return handler


def synthetic_post_request(
    path: str,
    *,
    body: bytes = b"",
    default_headers: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Request:
    """Build a synthetic ASGI ``POST`` ``Request`` for in-process invocation."""
    merged: dict[str, str] = dict(default_headers) if default_headers else {}
    if headers:
        for key, value in headers.items():
            merged[key.lower()] = value
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in merged.items()
        ],
    }
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


async def handle_noop_lifespan(receive: Receive, send: Send) -> None:
    """Acknowledge ASGI ``lifespan`` startup and shutdown events."""
    while True:
        message = await receive()
        await send({"type": f"{message['type']}.complete"})
        if message["type"] == "lifespan.shutdown":
            return


def headers_from_scope(scope: Scope) -> dict[str, str]:
    """Decode ASGI ``scope["headers"]`` into a lowercased ``dict[str, str]``."""
    return {
        raw_k.decode("latin-1").lower(): raw_v.decode("latin-1")
        for raw_k, raw_v in scope.get("headers", ())
    }


def is_missing_module(exc: ModuleNotFoundError, *roots: str) -> bool:
    """Return whether ``exc`` reports one of ``roots`` (or a submodule) missing."""
    name = exc.name or ""
    return any(name == root or name.startswith(f"{root}.") for root in roots)


def try_import_optional(name: str) -> Any | None:
    """Import ``name`` if available, returning ``None`` when its top-level package
    is absent while letting unrelated ``ModuleNotFoundError`` propagate."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if is_missing_module(exc, name.split(".", 1)[0]):
            return None
        raise
