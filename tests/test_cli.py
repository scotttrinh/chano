from __future__ import annotations

import builtins
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chano.cli import dev_app
from chano.cli import main as cli_main
from tests.helpers import make_client, write_project


# @lat: [[tests#CLI#Dev app factory]]
def test_cli_dev_app_serves_routes_and_static(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_project_imports: Any
):
    project = write_project(
        tmp_path / "devproj",
        {
            "pyproject.toml": '[project]\nname = "devproj"\nversion = "0.1.0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
            "public/hello.txt": "hello dev\n",
        },
    )
    monkeypatch.chdir(project)

    app = dev_app()
    client = make_client(app)
    assert client("GET", "/").json() == {"ok": True}
    assert client("GET", "/hello.txt").text == "hello dev\n"


# @lat: [[tests#CLI#Dev command delegates to uvicorn]]
def test_cli_dev_command_delegates_to_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_project_imports: Any
):
    project = write_project(
        tmp_path / "devcmd",
        {
            "pyproject.toml": '[project]\nname = "devcmd"\nversion = "0.1.0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
        },
    )
    monkeypatch.chdir(project)

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_run(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=fake_run))
    cli_main(["dev", "--host", "0.0.0.0", "--port", "9001"])
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("chano.cli:dev_app",)
    assert kwargs["factory"] is True
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9001
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == [str(project)]
    expected_includes = (
        ["*.py", "pyproject.toml"] if importlib.util.find_spec("watchfiles") else None
    )
    assert kwargs["reload_includes"] == expected_includes


# @lat: [[tests#CLI#Dev propagates non-uvicorn import errors]]
def test_cli_dev_propagates_non_uvicorn_import_error(
    monkeypatch: pytest.MonkeyPatch,
):
    real_import = builtins.__import__

    def failing_import(name: str, *import_args: Any, **import_kwargs: Any) -> Any:
        if name == "uvicorn":
            raise ModuleNotFoundError("missing dep", name="broken_dep")
        return real_import(name, *import_args, **import_kwargs)

    monkeypatch.delitem(sys.modules, "uvicorn", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing_import)
    with pytest.raises(ModuleNotFoundError, match="missing dep"):
        cli_main(["dev"])


# @lat: [[tests#CLI#Empty queue and schedule lists]]
def test_cli_empty_queue_and_schedule_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], isolated_project_imports: Any
):
    project = write_project(
        tmp_path / "emptyproj",
        {
            "pyproject.toml": '[project]\nname = "emptyproj"\nversion = "0.1.0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
        },
    )

    cli_main(["queue", "list", "--app", str(project / "app")])
    assert capsys.readouterr().out == "(no queue consumers)\n"

    cli_main(["schedule", "list", "--app", str(project / "app")])
    assert capsys.readouterr().out == "(no schedules)\n"


# @lat: [[tests#CLI#Queue and schedule result formatting]]
def test_cli_result_formatting_for_none_response_and_non_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_project_imports: Any,
):
    none_marker = tmp_path / "none.marker"
    empty_marker = tmp_path / "empty.marker"
    monkeypatch.setenv("CHANO_NONE_MARKER", str(none_marker))
    monkeypatch.setenv("CHANO_EMPTY_MARKER", str(empty_marker))

    project = write_project(
        tmp_path / "fmtproj",
        {
            "pyproject.toml": '[project]\nname = "fmtproj"\nversion = "0.1.0"\n',
            "app/none_job/schedule.py": (
                "import os\n"
                "from pathlib import Path\n"
                'schedule = "0 * * * *"\n'
                "def RUN():\n"
                '    Path(os.environ["CHANO_NONE_MARKER"]).write_text("ran-none")\n'
                "    return None\n"
            ),
            "app/resp_job/schedule.py": (
                "from chano import Response\n"
                'schedule = "0 * * * *"\n'
                'def RUN():\n    return Response("plain text")\n'
            ),
            "app/empty_resp_job/schedule.py": (
                "import os\n"
                "from pathlib import Path\n"
                "from chano import Response\n"
                'schedule = "0 * * * *"\n'
                "def RUN():\n"
                '    Path(os.environ["CHANO_EMPTY_MARKER"]).write_text("ran-empty")\n'
                '    return Response(b"")\n'
            ),
            "app/opaque_job/schedule.py": (
                'schedule = "0 * * * *"\n'
                "class _Opaque:\n"
                '    def __str__(self):\n        return "opaque-value"\n'
                "def RUN():\n    return _Opaque()\n"
            ),
        },
    )
    app_arg = str(project / "app")

    cli_main(["schedule", "run", "none_job", "--app", app_arg])
    assert none_marker.read_text() == "ran-none"
    assert capsys.readouterr().out == ""

    cli_main(["schedule", "run", "resp_job", "--app", app_arg])
    assert capsys.readouterr().out == "plain text\n"

    cli_main(["schedule", "run", "empty_resp_job", "--app", app_arg])
    assert empty_marker.read_text() == "ran-empty"
    assert capsys.readouterr().out == ""

    cli_main(["schedule", "run", "opaque_job", "--app", app_arg])
    assert capsys.readouterr().out == "opaque-value\n"


# @lat: [[tests#CLI#Invalid arguments exit with usage error]]
@pytest.mark.parametrize(
    "argv",
    [[], ["unknown"], ["queue"], ["schedule"]],
    ids=[
        "no-args",
        "unknown-command",
        "queue-missing-subcommand",
        "schedule-missing-subcommand",
    ],
)
def test_cli_error_exits(argv: list[str], capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc_info:
        cli_main(argv)
    assert exc_info.value.code == 2
    assert "usage: chano" in capsys.readouterr().err
