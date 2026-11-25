from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from chano import Chano

EXAMPLE = Path(__file__).parent.parent / "examples" / "hello"
FULLSTACK = Path(__file__).parent.parent / "examples" / "fullstack"
AUTH = {"Authorization": "Bearer secret"}
HOST_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"

FACTORIES = [
    pytest.param("dev", id="create_app"),
    pytest.param("route", id="create_route_app"),
]

PREFIXED_FILES = {
    "pyproject.toml": (
        '[project]\nname = "prefixed"\nversion = "0"\n\n'
        '[tool.chano]\nbase_path = "/api/"\n'
    ),
    "app/route.py": "def GET():\n    return {'route': 'root'}\n",
    "app/users/route.py": "def GET():\n    return {'route': 'users'}\n",
    "app/users/[id]/route.py": (
        "from chano import Request\n\n"
        "def GET(id: int, request: Request):\n"
        "    return {'id': id, 'path': request.path}\n"
    ),
    "public/hello.txt": "prefixed static\n",
}

INVOKE = """
import json, sys
from vc__handler__python import vc_handler
event = {"body": json.dumps({"method": "GET", "path": sys.argv[1], "headers": {}})}
print(json.dumps(vc_handler(event, None)))
"""


def make_client(app: Any, root_path: str = "") -> Callable[..., httpx.Response]:
    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=app, root_path=root_path)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                return await http.request(method, url, **kwargs)

        return asyncio.run(send())

    return request


def raw_get(app: Any, path: str, root_path: str = "") -> tuple[int, bytes]:
    """Call ``app`` with an unnormalized ``path``, which httpx would clean up."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": root_path,
        "headers": [],
        "query_string": b"",
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return sent[0]["status"], body


def write_project(project: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return project


def make_dist(
    name: str,
    module: str,
    *,
    requires: list[str] | None = None,
    extra_files: dict[str, str] | None = None,
    top_level: str | None = None,
) -> dict[str, str]:
    """Files for a fake installed distribution: a package, `METADATA` and
    `RECORD`, ready for `write_project` into a store."""
    dist_info = f"{name.replace('-', '_')}-1.0.dist-info"
    meta_lines = ["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0"]
    for req in requires or ():
        meta_lines.append(f"Requires-Dist: {req}")
    files: dict[str, str] = {
        f"{module}/__init__.py": f"NAME = {name!r}\n",
        **(extra_files or {}),
    }
    record_lines = [f"{path},," for path in sorted(files)]
    record_lines.append(f"{dist_info}/METADATA,,")
    record_lines.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/METADATA"] = "\n".join(meta_lines) + "\n"
    if top_level is not None:
        files[f"{dist_info}/top_level.txt"] = top_level
        record_lines.append(f"{dist_info}/top_level.txt,,")
    files[f"{dist_info}/RECORD"] = "\n".join(record_lines) + "\n"
    return files


def invoke(output: Path, function: str, path: str) -> Any:
    """Invoke a built function the way Vercel does, using only vendored deps."""
    result = subprocess.run(
        [sys.executable, "-S", "-c", INVOKE, path],
        cwd=output / "functions" / f"{function}.func",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def stream_scope(method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": "/s",
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }


def call_asgi(
    app: Any, receive: Any, method: str = "GET", on_send: Any = None
) -> list[dict]:
    """Run one ``/s`` request through ``app``; return the sent messages.

    ``on_send(sent)`` runs after each message, to react to the stream.
    """
    scope = stream_scope(method)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)
        if on_send:
            on_send(sent)

    async def run():
        await asyncio.wait_for(app(scope, receive, send), timeout=5)

    asyncio.run(run())
    return sent


async def wait_for_disconnect():
    """A ``receive`` that never disconnects until the response is done."""
    await asyncio.Event().wait()


def body_messages(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m["type"] == "http.response.body"]


def stream_app(response_factory: Any) -> Chano:
    app = Chano(docs=False)

    def handler():
        return response_factory()

    app.add_route("/s", handler, ["GET"])
    return app
