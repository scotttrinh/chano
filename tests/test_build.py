import json
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from chano import (
    ChanoConfig,
    create_app,
    create_route_app,
)
from chano import _report as report_module
from chano.build import build, emit_build, plan_build
from chano.cli import main as cli_main
from chano.openapi import swagger_html
from tests.helpers import (
    EXAMPLE,
    FULLSTACK,
    HOST_PYTHON,
    invoke,
    make_client,
    make_dist,
    write_project,
)


# @lat: [[tests#Build plan]]
def test_build_plan(tmp_path, isolated_project_imports):
    project = tmp_path / "shop"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "shop"\nversion = "0"\n',
            "app/(admin)/orders/route.py": "def GET():\n    return []\n",
            "app/orders/[order_id]/route.py": (
                "max_duration = 30\nmemory = 1024\nregions = ['iad1']\n\n"
                "def GET(order_id: int):\n    return {'id': order_id}\n"
            ),
            "public/css/site.css": "body {}\n",
        },
    )

    plan = plan_build(project, python="3.13")

    assert plan.project_dir == plan.workspace_root == project
    assert plan.package is None
    assert plan.app_dir == project / "app"
    assert plan.public_dir == project / "public"
    assert plan.output_dir == project / ".vercel" / "output"
    assert not plan.output_dir.exists()

    assert [f.name for f in plan.functions] == ["orders", "orders/[order_id]"]
    assert [f.route.url_path for f in plan.functions if f.route is not None] == [
        "/orders",
        "/orders/{order_id}",
    ]
    orders, order = plan.functions
    assert orders.route is not None
    assert order.route is not None
    assert plan.config == ChanoConfig()
    assert "create_route_app(" in order.entrypoint
    assert (
        "'orders/[order_id]',\n    base_path='',\n    after_backend=\"wait_until\",\n)"
        in order.entrypoint
    )
    assert order.vc_config == {
        "runtime": "python3.13",
        "handler": "vc__handler__python.vc_handler",
        "supportsResponseStreaming": True,
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "maxDuration": 30,
        "memory": 1024,
        "regions": ["iad1"],
    }
    assert set(orders.vc_config) == {
        "runtime",
        "handler",
        "supportsResponseStreaming",
        "environment",
    }
    assert orders.trace_roots == (orders.route.file,)
    assert orders.include == ()
    assert order.trace_roots == (order.route.file,)
    assert order.include == ()

    static = {f.path: f.content for f in plan.static_files}
    assert set(static) == {"openapi.json", "docs.html"}
    assert set(json.loads(static["openapi.json"])["paths"]) == {
        "/orders",
        "/orders/{order_id}",
    }
    assert "swagger" in static["docs.html"].lower()

    assert plan.routes == (
        {"handle": "filesystem"},
        {"src": "^/docs$", "dest": "/docs.html"},
        {"src": "^/orders/([^/]+)$", "dest": "/orders/[order_id]"},
    )


# @lat: [[tests#Build emit]]
def test_emit_build_preserves_public_and_config_isolation(
    tmp_path, isolated_project_imports, stub_build_install
):
    project = tmp_path / "snap"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "snap"\nversion = "0"\n',
            "snap_config.py": (
                "REGIONS = ['iad1']\n\n"
                "class Regions(list):\n"
                "    def __deepcopy__(self, memo):\n"
                "        raise TypeError('no deepcopy')\n"
            ),
            "app/a/route.py": "from snap_config import REGIONS as regions\n",
            "app/b/route.py": (
                "from pathlib import Path\nfrom snap_config import REGIONS\n"
                "REGIONS.append('sfo1')\n"
                "(Path(__file__).parents[2] / 'public' / 'late.txt').write_text('x')\n"
            ),
            "app/c/route.py": (
                "from snap_config import Regions\nregions = Regions(['cdg1'])\n"
            ),
            "app/d/route.py": "regions = {1: 'first', '1': 'second'}\n",
            "assets/logo.svg": "<svg/>",
        },
    )
    (project / "public" / "empty").mkdir(parents=True)
    (project / "public" / "empty").chmod(0o750)
    (project / "public" / "linked").symlink_to(project / "assets")

    plan = plan_build(project)
    assert plan.functions[0].vc_config["regions"] == ["iad1"]
    assert plan.functions[2].vc_config["regions"] == ["cdg1"]

    stub_build_install()
    output = emit_build(plan)

    static = output / "static"
    assert (static / "linked" / "logo.svg").read_text() == "<svg/>"
    assert (static / "empty").stat().st_mode & 0o777 == 0o750
    # Like the original builder, public/ is copied after route imports.
    assert (static / "late.txt").read_text() == "x"
    functions = output / "functions"
    assert (functions / "b.func" / "app" / "b" / "route.py").is_file()
    a_config = json.loads((functions / "a.func" / ".vc-config.json").read_text())
    assert a_config["regions"] == ["iad1"]
    c_config = json.loads((functions / "c.func" / ".vc-config.json").read_text())
    assert c_config["regions"] == ["cdg1"]
    d_raw = (functions / "d.func" / ".vc-config.json").read_text()
    assert d_raw.count('"1":') == 2

    # public/ created during route imports is also discovered at emit time.
    late_public = tmp_path / "late_public"
    write_project(
        late_public,
        {
            "pyproject.toml": '[project]\nname = "late"\nversion = "0"\n',
            "app/late/route.py": (
                "from pathlib import Path\n"
                "pub = Path(__file__).parents[2] / 'public'\n"
                "pub.mkdir(exist_ok=True)\n"
                "(pub / 'late.txt').write_text('created')\n"
            ),
        },
    )
    late_output = emit_build(plan_build(late_public))
    assert (late_output / "static" / "late.txt").read_text() == "created"


# @lat: [[tests#Build output layout]]
@pytest.mark.slow
def test_build_output_layout(output):
    config = json.loads((output / "config.json").read_text())
    assert config["version"] == 3
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/docs$", "dest": "/docs.html"},
        {"src": "^/api/items/([^/]+)$", "dest": "/api/items/[item_id]"},
    ]

    static = output / "static"
    assert (static / "hello.txt").is_file()
    assert "swagger" in (static / "docs.html").read_text().lower()
    schema = json.loads((static / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/", "/api/hello", "/api/items/{item_id}"}

    functions = output / "functions"
    assert {p.relative_to(functions).as_posix() for p in functions.rglob("*.func")} == {
        "index.func",
        "api/hello.func",
        "api/items/[item_id].func",
    }
    func = functions / "api" / "items" / "[item_id].func"
    vc_config = json.loads((func / ".vc-config.json").read_text())
    assert vc_config["runtime"] == f"python{HOST_PYTHON}"
    assert plan_build(EXAMPLE).python == "3.13"
    assert vc_config["handler"] == "vc__handler__python.vc_handler"
    assert vc_config["maxDuration"] == 10
    assert (func / "_vendor" / "chano").is_dir()
    assert (func / "_vendor" / "msgspec").is_dir()
    assert (func / "_vendor" / "vercel").is_dir()
    assert not any(p.name.startswith("uvicorn") for p in (func / "_vendor").iterdir())
    assert not (func / "public").exists()

    hello_config = (functions / "api" / "hello.func" / ".vc-config.json").read_text()
    assert "maxDuration" not in json.loads(hello_config)


# @lat: [[tests#Built function invocation]]
@pytest.mark.slow
def test_built_functions_handle_their_own_route(output):
    response = invoke(output, "api/items/[item_id]", "/api/items/42?q=1")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"item_id": 42, "verbose": False}

    response = invoke(output, "index", "/")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"message": "Hello from Chano"}

    # Each function only knows its own route.
    for path in ("/api/items/42", "/docs"):
        miss = invoke(output, "api/hello", path)
        assert miss["statusCode"] == 404
        assert json.loads(miss["body"]) == {"detail": "Not Found"}


# @lat: [[tests#Prefixed build output]]
@pytest.mark.slow
def test_prefixed_build_output(prefixed_project, prefixed_output):
    output = prefixed_output
    assert output == prefixed_project / ".vercel" / "output"

    config = json.loads((output / "config.json").read_text())
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/api/docs$", "dest": "/api/docs.html"},
        {"src": "^/api/users/([^/]+)$", "dest": "/api/users/[id]"},
    ]

    static = output / "static"
    assert {p.relative_to(static).as_posix() for p in static.rglob("*")} == {
        "api",
        "api/hello.txt",
        "api/docs.html",
        "api/openapi.json",
    }
    docs = (static / "api" / "docs.html").read_text()
    assert docs == swagger_html(prefixed_project.name, "/api/openapi.json")
    schema = json.loads((static / "api" / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/api", "/api/users", "/api/users/{id}"}

    functions = output / "functions"
    assert {p.relative_to(functions).as_posix() for p in functions.rglob("*.func")} == {
        "api.func",
        "api/users.func",
        "api/users/[id].func",
    }

    response = invoke(output, "api", "/api")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"route": "root"}
    response = invoke(output, "api/users/[id]", "/api/users/5")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"id": 5, "path": "/api/users/5"}
    miss = invoke(output, "api/users", "/users")
    assert miss["statusCode"] == 404
    assert json.loads(miss["body"]) == {"detail": "Not Found"}


# @lat: [[tests#Route module isolation]]
def test_route_module_isolation(tmp_path, isolated_project_imports):
    for name in ["one", "two"]:
        write_project(
            tmp_path / name,
            {"app/route.py": f"def GET():\n    return {{'project': {name!r}}}\n"},
        )
    for name in ["one", "two"]:
        response = make_client(create_app(tmp_path / name / "app"))("GET", "/")
        assert response.json() == {"project": name}

    # "v1+" and "v1+/!" functions: distinct files, distinct modules.
    project = tmp_path / "punct"
    write_project(
        project,
        {
            "pyproject.toml": '[tool.chano]\nbase_path = "/v1+"\n',
            "app/route.py": "def GET():\n    return {'route': 'root'}\n",
            "app/!/route.py": "def GET():\n    return {'route': 'bang'}\n",
        },
    )
    client = make_client(create_app(project / "app"))
    assert client("GET", "/v1+").json() == {"route": "root"}
    assert client("GET", "/v1+/!").json() == {"route": "bang"}


# @lat: [[tests#Symlinked app directory]]
def test_symlinked_app_directory(tmp_path, isolated_project_imports):
    project = tmp_path / "linked"
    write_project(
        project,
        {
            "pyproject.toml": (
                '[project]\nname = "linked"\nversion = "0"\n\n'
                '[tool.chano]\nbase_path = "/api"\n'
            ),
            "src/app/users/route.py": "def GET():\n    return {'route': 'users'}\n",
        },
    )
    (project / "app").symlink_to(project / "src" / "app")

    app = create_app(project / "app")
    assert app.base_path == "/api"
    assert make_client(app)("GET", "/api/users").json() == {"route": "users"}
    route_app = create_route_app(project / "app", "api/users")
    assert route_app.base_path == "/api"

    plan = plan_build(project)
    assert plan.config.base_path == "/api"
    assert [f.name for f in plan.functions] == ["api/users"]


# @lat: [[tests#Prefixed build without public]]
def test_prefixed_build_without_public(
    tmp_path, isolated_project_imports, stub_build_install
):
    stub_build_install()
    files = {
        "pyproject.toml": (
            '[project]\nname = "nopub"\nversion = "0"\n\n'
            '[tool.chano]\nbase_path = "/api"\n'
        ),
        "app/route.py": "def GET():\n    return {}\n",
    }
    project = tmp_path / "nopub"
    write_project(project, files)
    static = emit_build(plan_build(project)) / "static"
    assert {p.relative_to(static).as_posix() for p in static.rglob("*")} == {
        "api",
        "api/docs.html",
        "api/openapi.json",
    }

    late = tmp_path / "latepub"
    write_project(
        late,
        files
        | {
            "app/route.py": (
                "from pathlib import Path\n"
                "pub = Path(__file__).parents[1] / 'public'\n"
                "pub.mkdir(exist_ok=True)\n"
                "(pub / 'late.txt').write_text('created')\n"
            )
        },
    )
    plan = plan_build(late)
    assert plan.public_dir is None
    static = emit_build(plan) / "static"
    assert (static / "api" / "late.txt").read_text() == "created"
    assert (static / "api" / "docs.html").is_file()


# @lat: [[tests#Custom build output directory#Custom output directory]]
def test_custom_build_output_directory(
    tmp_path, isolated_project_imports, monkeypatch, stub_build_install, capsys
):
    stub_build_install()
    project = tmp_path / "custom"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "custom"\nversion = "0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
            "public/hello.txt": "hi\n",
        },
    )

    # plan_build accepts a custom output path without creating it.
    external = tmp_path / "artifacts" / "out"
    plan = plan_build(project, output=external)
    assert plan.output_dir == external.resolve()
    assert not external.exists()

    # build() writes to the custom directory and leaves .vercel/output untouched.
    result = build(project, output=external)
    assert result == external.resolve()
    assert (external / "config.json").is_file()
    assert (external / "static" / "hello.txt").read_text() == "hi\n"
    assert (external / "functions" / "index.func" / "index.py").is_file()
    assert not (project / ".vercel").exists()

    # Output inside project_dir does not copy itself or temporary directories
    # into function payloads, even across repeated builds.
    nested = project / "dist" / "vercel-output"
    for _ in range(2):
        built = build(project, output=nested)
        assert built == nested.resolve()
        func = nested / "functions" / "index.func"
        assert (func / "app" / "route.py").is_file()
        assert not (func / "dist" / "vercel-output").exists()
        assert not list(func.rglob(".chano-*"))

    direct = project / "out"
    built = build(project, output=direct)
    assert built == direct.resolve()
    assert not (direct / "functions" / "index.func" / "out").exists()

    # CLI `--output` builds into the requested directory (relative to cwd).
    monkeypatch.chdir(project)
    cli_main(["build", "--output", "cli-out", "--platform", "native"])
    cli_out = project / "cli-out"
    assert (cli_out / "config.json").is_file()
    assert (cli_out / "functions" / "index.func" / "index.py").is_file()
    assert not (cli_out / "functions" / "index.func" / "cli-out").exists()
    assert "cli-out" in capsys.readouterr().out
    assert not (project / ".vercel").exists()


# @lat: [[tests#Custom build output directory#Rejects overlapping output directory]]
@pytest.mark.parametrize(
    "relative_output",
    [".", "..", "app", "app/out", "public", "public/out"],
    ids=["project-root", "parent-dir", "app", "inside-app", "public", "inside-public"],
)
def test_build_rejects_overlapping_output_directory(
    tmp_path, isolated_project_imports, relative_output
):
    project = tmp_path / "custom"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "custom"\nversion = "0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
            "public/hello.txt": "hi\n",
        },
    )
    bad = (project / relative_output).resolve()
    with pytest.raises(ValueError, match="output directory"):
        plan_build(project, output=bad)
    assert (project / "app" / "route.py").is_file()
    assert (project / "public" / "hello.txt").is_file()


# @lat: [[tests#Locked lockfile]]
@pytest.mark.slow
def test_stale_lockfile_fails_build(tmp_path, isolated_project_imports):
    project = tmp_path / "stale"
    pyproject = (
        '[project]\nname = "stale"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = []\n'
    )
    write_project(
        project,
        {
            "pyproject.toml": pyproject,
            "app/route.py": "def GET():\n    return {'ok': True}\n",
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=project, check=True)
    lock_before = (project / "uv.lock").read_text()

    # Drift pyproject.toml from uv.lock without re-locking.
    (project / "pyproject.toml").write_text(
        pyproject.replace("dependencies = []", 'dependencies = ["msgspec"]')
    )
    with pytest.raises(RuntimeError, match=r"uv\.lock"):
        build(project, platform=None, output=tmp_path / "out")
    assert (project / "uv.lock").read_text() == lock_before

    # Missing uv.lock also fails instead of resolving on the fly.
    (project / "uv.lock").unlink()
    with pytest.raises(RuntimeError, match=r"uv\.lock"):
        build(project, platform=None, output=tmp_path / "out2")


# @lat: [[tests#Deterministic build output]]
def test_deterministic_build_output(
    tmp_path, isolated_project_imports, stub_build_install
):
    stub_build_install()
    project = tmp_path / "det"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "det"\nversion = "0"\n',
            "app/z/route.py": "def GET():\n    return {'z': 1}\n",
            "app/a/route.py": "def GET():\n    return {'a': 1}\n",
            "app/items/[id]/route.py": (
                "import msgspec\n\n"
                "max_duration = 10\n"
                "memory = 512\n"
                "regions = ['iad1']\n\n"
                "class Item(msgspec.Struct):\n"
                "    name: str\n\n"
                "def PUT(id: int, item: Item) -> Item:\n"
                "    return item\n"
            ),
            "public/hello.txt": "hello\n",
        },
    )

    out1 = build(project, output=tmp_path / "out1")
    out2 = build(project, output=tmp_path / "out2")

    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert snapshot(out1) == snapshot(out2)

    config_text = (out1 / "config.json").read_text()
    assert config_text == json.dumps(json.loads(config_text), indent=2, sort_keys=True)

    openapi_text = (out1 / "static" / "openapi.json").read_text()
    assert openapi_text == json.dumps(json.loads(openapi_text), sort_keys=True)

    for vc_config in sorted((out1 / "functions").rglob(".vc-config.json")):
        text = vc_config.read_text()
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True)


# @lat: [[tests#Workspace discovery]]
def test_workspace_root_discovery(tmp_path, isolated_project_imports):
    # Non-workspace project falls back to project_dir, even under an ancestor
    # with pyproject.toml or [tool.uv.workspace] lacking uv.lock.
    outer = tmp_path / "outer"
    write_project(
        outer,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["apps/*"]\n',
            "apps/solo/pyproject.toml": '[project]\nname = "solo"\nversion = "0"\n',
            "apps/solo/app/route.py": "def GET():\n    return {}\n",
        },
    )
    solo = (outer / "apps" / "solo").resolve()
    solo_plan = plan_build(solo)
    assert solo_plan.workspace_root == solo
    assert solo_plan.package is None

    # Adding uv.lock at the ancestor makes it the workspace root.
    (outer / "uv.lock").write_text("version = 1\n")
    solo_ws_plan = plan_build(solo)
    assert solo_ws_plan.workspace_root == outer.resolve()
    assert solo_ws_plan.package == "solo"

    # Nearest ancestor with both [tool.uv.workspace] and uv.lock wins.
    inner = outer / "sub"
    write_project(
        inner,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["svc", "unnamed"]\n',
            "svc/pyproject.toml": '[project]\nname = "svc"\nversion = "0"\n',
            "svc/app/route.py": "def GET():\n    return {}\n",
        },
    )
    svc = (inner / "svc").resolve()
    # inner lacks uv.lock, so outer wins.
    svc_outer_plan = plan_build(svc)
    assert svc_outer_plan.workspace_root == outer.resolve()
    assert svc_outer_plan.package == "svc"
    # Once inner also has uv.lock, inner (nearest) wins.
    (inner / "uv.lock").write_text("version = 1\n")
    svc_inner_plan = plan_build(svc)
    assert svc_inner_plan.workspace_root == inner.resolve()
    assert svc_inner_plan.package == "svc"

    # A workspace member without [project].name cannot be exported by package name.
    unnamed = inner / "unnamed"
    write_project(
        unnamed,
        {
            "pyproject.toml": '[tool.chano]\nbase_path = "/api"\n',
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="name"):
        plan_build(unnamed)

    # A standalone project with its own uv.lock under a workspace root stops
    # walking up and resolves to itself.
    standalone = outer / "standalone"
    write_project(
        standalone,
        {
            "pyproject.toml": '[project]\nname = "standalone"\nversion = "0"\n',
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    standalone_plan = plan_build(standalone)
    assert standalone_plan.workspace_root == standalone.resolve()
    assert standalone_plan.package is None

    # An ancestor with uv.lock but without [tool.uv.workspace] is not a
    # workspace root and stops walking up before reaching an outer workspace.
    plain_parent = outer / "plain_parent"
    write_project(
        plain_parent,
        {
            "pyproject.toml": '[project]\nname = "parent"\nversion = "0"\n',
            "uv.lock": "version = 1\n",
            "child/pyproject.toml": '[project]\nname = "child"\nversion = "0"\n',
            "child/app/route.py": "def GET():\n    return {}\n",
        },
    )
    child = (plain_parent / "child").resolve()
    child_plan = plan_build(child)
    assert child_plan.workspace_root == child
    assert child_plan.package is None

    # A workspace root that is itself a Chano project resolves to itself,
    # while a virtual workspace root without [project].name is rejected.
    root_svc = tmp_path / "root_svc"
    write_project(
        root_svc,
        {
            "pyproject.toml": (
                '[project]\nname = "root-svc"\nversion = "0"\n\n'
                "[tool.uv.workspace]\nmembers = []\n"
            ),
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    root_svc_plan = plan_build(root_svc)
    assert root_svc_plan.workspace_root == root_svc.resolve()
    assert root_svc_plan.package == "root-svc"

    virtual_root = tmp_path / "virtual_root"
    write_project(
        virtual_root,
        {
            "pyproject.toml": "[tool.uv.workspace]\nmembers = []\n",
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="name"):
        plan_build(virtual_root)


# @lat: [[tests#Workspace build]]
@pytest.mark.slow
def test_workspace_build(tmp_path, isolated_project_imports, monkeypatch):
    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "workspace"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
            "packages/core/pyproject.toml": (
                '[project]\nname = "core"\nversion = "0.1.0"\n'
                'requires-python = ">=3.13"\ndependencies = []\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
            ),
            "packages/core/src/core/__init__.py": (
                'def greet(who: str) -> str:\n    return f"hello {who} from core"\n'
            ),
            "packages/unused/pyproject.toml": (
                '[project]\nname = "unused"\nversion = "0.1.0"\n'
                'requires-python = ">=3.13"\ndependencies = []\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
            ),
            "packages/unused/src/unused/__init__.py": "VALUE = 1\n",
            "apps/worker/pyproject.toml": (
                '[project]\nname = "worker"\nversion = "0.1.0"\n'
                'requires-python = ">=3.13"\ndependencies = ["unused"]\n\n'
                "[tool.uv.sources]\nunused = { workspace = true }\n"
            ),
            "apps/api/pyproject.toml": (
                '[project]\nname = "api"\nversion = "0.1.0"\n'
                'requires-python = ">=3.13"\n'
                'dependencies = ["chano", "core"]\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n\n'
                "[tool.hatch.build.targets.wheel]\n"
                'packages = ["src/api_pkg"]\n\n'
                "[tool.uv.sources]\n"
                f"chano = {{ path = {json.dumps(repo_root.as_posix())},"
                " editable = true }\n"
                "core = { workspace = true }\n"
            ),
            "apps/api/src/api_pkg/__init__.py": 'MARKER = "api"\n',
            "apps/api/app/route.py": (
                "from core import greet\n\n"
                'def GET(name: str = "world"):\n'
                '    return {"greeting": greet(name)}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    # Mirror the workspace environment during planning so route.py can import core;
    # invoke() runs in a fresh `python -S` subprocess with only _vendor/ on sys.path.
    monkeypatch.syspath_prepend(str(workspace / "packages" / "core" / "src"))

    api = workspace / "apps" / "api"
    plan = plan_build(api, platform=None)
    assert plan.project_dir == api.resolve()
    assert plan.workspace_root == workspace.resolve()
    assert plan.package == "api"

    output = build(api, python=HOST_PYTHON, platform=None)
    assert output == api.resolve() / ".vercel" / "output"

    vendor = output / "functions" / "index.func" / "_vendor"
    assert (vendor / "core" / "__init__.py").is_file()
    assert not (vendor / "unused").exists()
    assert not (vendor / "api_pkg").exists()

    response = invoke(output, "index", "/?name=chano")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"greeting": "hello chano from core"}


# @lat: [[tests#Full-stack example build]]
@pytest.mark.slow
def test_fullstack_example_build(tmp_path, isolated_project_imports):
    backend = FULLSTACK / "backend"
    vercel = json.loads((FULLSTACK / "vercel.json").read_text())
    assert vercel["services"]["web"] == {"root": "frontend/", "framework": "nextjs"}
    assert vercel["services"]["api"] == {
        "root": "backend/",
        "framework": "vite",
        "buildCommand": "uv run --frozen chano build",
        "devCommand": "uv run --frozen chano dev --port $PORT",
    }
    assert vercel["rewrites"] == [
        {"source": "/api/:path*", "destination": {"service": "api"}},
        {"source": "/(.*)", "destination": {"service": "web"}},
    ]
    assert (FULLSTACK / ".python-version").read_text().strip() == "3.13"
    assert (backend / ".python-version").read_text().strip() == "3.13"

    out = tmp_path / "output"
    plan = plan_build(backend, platform=None, output=out)
    assert plan.project_dir == backend.resolve()
    assert plan.workspace_root == FULLSTACK.resolve()
    assert plan.package == "fullstack-api"
    assert plan.config.base_path == "/api"

    output = build(backend, python=HOST_PYTHON, platform=None, output=out)
    assert output == out.resolve()

    config = json.loads((output / "config.json").read_text())
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/api/docs$", "dest": "/api/docs.html"},
        {"src": "^/api/items/([^/]+)$", "dest": "/api/items/[item_id]"},
    ]

    docs_html = output / "static" / "api" / "docs.html"
    assert docs_html.is_file()
    assert '"/api/openapi.json"' in docs_html.read_text()
    schema = json.loads((output / "static" / "api" / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/api", "/api/hello", "/api/items/{item_id}"}
    assert (output / "static" / "api" / "hello.txt").read_text().strip() == (
        "A static file served from /api/hello.txt"
    )

    for func in ("api", "api/hello", "api/items/[item_id]"):
        func_dir = output / "functions" / f"{func}.func"
        assert (func_dir / ".vc-config.json").is_file()
        assert not any(
            p.name.startswith("uvicorn") for p in (func_dir / "_vendor").iterdir()
        )

    root_res = invoke(output, "api", "/api")
    assert root_res["statusCode"] == 200
    assert json.loads(root_res["body"]) == {
        "service": "chano",
        "message": "Hello from Chano",
    }

    hello_res = invoke(output, "api/hello", "/api/hello?name=vercel")
    assert hello_res["statusCode"] == 200
    assert json.loads(hello_res["body"]) == {"hello": "vercel"}

    item_res = invoke(output, "api/items/[item_id]", "/api/items/42")
    assert item_res["statusCode"] == 200
    assert json.loads(item_res["body"]) == {"item_id": 42}


# @lat: [[tests#Dev requires uvicorn]]
def test_dev_requires_uvicorn(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["dev"])
    message = str(exc_info.value)
    assert "uvicorn" in message
    assert "chano[dev]" in message


# @lat: [[tests#Store index]]
@pytest.mark.slow
def test_store_index(tmp_path, output):
    from chano.build import index_store, normalize_dist_name

    assert normalize_dist_name("My_Cool.Pkg--Name") == "my-cool-pkg-name"
    assert normalize_dist_name("msgspec") == "msgspec"

    # Missing store directory returns an empty index.
    empty = index_store(tmp_path / "missing")
    assert empty.distributions == {}
    assert empty.by_import == {}

    # Synthetic store covering RECORD, top_level.txt, Requires-Dist, namespace
    # packages, single-file modules, excluded directories, and escaped paths.
    store = tmp_path / "_vendor"
    write_project(
        store,
        {
            "Alpha_Pkg-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\n"
                "Name: Alpha_Pkg\n"
                "Version: 1.0\n"
                "Requires-Dist: Beta.Lib[extra1, extra2]>=2.0,<3; "
                "python_version >= '3.12'\n"
                "Requires-Dist: Gamma_Tool (==1.5)\n"
                "Requires-Dist: delta-dep @ https://example.com/delta.whl ; "
                "extra == 'dev'\n"
                "Requires-Dist: z\n"
            ),
            "Alpha_Pkg-1.0.dist-info/RECORD": (
                "alpha/__init__.py,sha256=abc,10\n"
                "alpha/sub.py,sha256=def,20\n"
                "alpha_helper.py,sha256=ghi,15\n"
                "ns_shared/alpha_part.py,sha256=jkl,5\n"
                "bin/alpha-cli,sha256=mno,30\n"
                "Alpha_Pkg-1.0.dist-info/METADATA,sha256=pqr,100\n"
                "Alpha_Pkg-1.0.dist-info/RECORD,,\n"
                "Alpha_Pkg-1.0.data/purelib/ignored.py,sha256=stu,10\n"
                "__pycache__/alpha_helper.cpython-312.pyc,sha256=vwx,10\n"
                "../../outside.py,sha256=yz,10\n"
                "/abs/outside.py,sha256=ab,10\n"
                '"alpha/data,file.txt",sha256=cd,4\n'
            ),
            "Alpha_Pkg-1.0.dist-info/top_level.txt": "\nalpha_extra\nns_top/sub\n",
            "alpha/__init__.py": "x = 1\n",
            "alpha/sub.py": "y = 2\n",
            "alpha/data,file.txt": "1234",
            "alpha_helper.py": "z = 3\n",
            "ns_shared/alpha_part.py": "a = 1\n",
            "bin/alpha-cli": "#!/usr/bin/env python\n",
            "Beta.Lib-2.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: Beta.Lib\nVersion: 2.0\n"
            ),
            "Beta.Lib-2.0.dist-info/RECORD": (
                "ns_shared/beta_part.py,sha256=aaa,8\n"
                "beta_ext.cpython-312-linux.so,sha256=bbb,12\n"
                "Beta.Lib-2.0.dist-info/METADATA,sha256=ccc,50\n"
                "Beta.Lib-2.0.dist-info/RECORD,,\n"
            ),
            "ns_shared/beta_part.py": "b = 2\n",
            "beta_ext.cpython-312-linux.so": "0123456789ab",
        },
    )

    index = index_store(store)
    assert set(index.distributions) == {"alpha-pkg", "beta-lib"}

    alpha = index.distributions["alpha-pkg"]
    assert alpha.name == "alpha-pkg"
    assert alpha.dist_info == "Alpha_Pkg-1.0.dist-info"
    assert alpha.requires == frozenset({"beta-lib", "gamma-tool", "delta-dep", "z"})
    assert alpha.top_level == frozenset(
        {"alpha", "alpha_helper", "ns_shared", "alpha_extra", "ns_top"}
    )
    assert "alpha/__init__.py" in alpha.files
    assert "alpha/data,file.txt" in alpha.files
    assert "bin/alpha-cli" in alpha.files
    assert "../../outside.py" not in alpha.files
    assert "/abs/outside.py" not in alpha.files
    expected_alpha_size = sum(
        (store / p).lstat().st_size
        for p in (
            "alpha/__init__.py",
            "alpha/sub.py",
            "alpha/data,file.txt",
            "alpha_helper.py",
            "ns_shared/alpha_part.py",
            "bin/alpha-cli",
            "Alpha_Pkg-1.0.dist-info/METADATA",
            "Alpha_Pkg-1.0.dist-info/RECORD",
            "Alpha_Pkg-1.0.dist-info/top_level.txt",
        )
    )
    assert alpha.size == expected_alpha_size

    beta = index.distributions["beta-lib"]
    assert beta.top_level == frozenset({"ns_shared", "beta_ext"})
    assert beta.requires == frozenset()

    assert index.by_import["alpha"] == ("alpha-pkg",)
    assert index.by_import["alpha_helper"] == ("alpha-pkg",)
    assert index.by_import["alpha_extra"] == ("alpha-pkg",)
    assert index.by_import["ns_shared"] == ("alpha-pkg", "beta-lib")
    assert index.by_import["beta_ext"] == ("beta-lib",)
    assert "bin" not in index.by_import

    # Real vendored store from examples/hello.
    real_index = index_store(output / "functions" / "index.func" / "_vendor")
    assert {"chano", "msgspec", "vercel-runtime"} <= set(real_index.distributions)
    chano_dist = real_index.distributions["chano"]
    assert chano_dist.top_level == frozenset({"chano"})
    assert "msgspec" in chano_dist.requires
    assert "chano/__init__.py" in chano_dist.files
    assert chano_dist.size > 0
    assert real_index.by_import["msgspec"] == ("msgspec",)
    assert real_index.by_import["vercel_runtime"] == ("vercel-runtime",)


# @lat: [[tests#Build size report and limits]]
def test_build_size_report_and_limits(
    tmp_path, isolated_project_imports, monkeypatch, stub_build_install, capsys
):
    assert report_module.SIZE_WARN_BYTES == 250 * 1024 * 1024
    assert report_module.SIZE_LIMIT_BYTES == 500 * 1024 * 1024

    project = tmp_path / "sized"
    write_project(
        project,
        {
            "pyproject.toml": (
                '[project]\nname = "sized"\nversion = "0"\n\n'
                '[tool.chano]\ninclude = ["big-pkg", "med-pkg", "small-pkg"]\n'
            ),
            "app/route.py": "def GET():\n    return {'ok': True}\n",
            "app/ping/route.py": "def GET():\n    return {'ping': True}\n",
        },
    )

    stub_build_install(
        {
            "big_pkg-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: Big_Pkg\nVersion: 1.0\n"
            ),
            "big_pkg-1.0.dist-info/RECORD": (
                "big_pkg/__init__.py,,\n"
                "big_pkg/blob.bin,,\n"
                "big_pkg-1.0.dist-info/METADATA,,\n"
                "big_pkg-1.0.dist-info/RECORD,,\n"
            ),
            "big_pkg/__init__.py": "",
            "big_pkg/blob.bin": "x" * 8_192,
            "med_pkg-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: med-pkg\nVersion: 1.0\n"
            ),
            "med_pkg-1.0.dist-info/RECORD": (
                "med_pkg/__init__.py,,\n"
                "med_pkg/blob.bin,,\n"
                "med_pkg-1.0.dist-info/METADATA,,\n"
                "med_pkg-1.0.dist-info/RECORD,,\n"
            ),
            "med_pkg/__init__.py": "",
            "med_pkg/blob.bin": "y" * 2_048,
            "small_pkg-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: small-pkg\nVersion: 1.0\n"
            ),
            "small_pkg-1.0.dist-info/RECORD": (
                "small_pkg/__init__.py,,\n"
                "small_pkg-1.0.dist-info/METADATA,,\n"
                "small_pkg-1.0.dist-info/RECORD,,\n"
            ),
            "small_pkg/__init__.py": "z = 1\n",
        }
    )

    # Under thresholds: prints each function's size and distribution count, no warnings.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = build(project, platform=None, output=tmp_path / "out-ok")
    assert out.is_dir()
    assert caught == []
    report_out = capsys.readouterr().out
    assert "index" in report_out
    assert "ping" in report_out
    assert "KB" in report_out
    assert "3 distributions" in report_out

    # Above warn threshold (and below fail limit): emits warning naming largest
    # distributions in descending size order, and build still succeeds.
    monkeypatch.setattr(report_module, "SIZE_WARN_BYTES", 4_096)
    monkeypatch.setattr(report_module, "SIZE_LIMIT_BYTES", 1024 * 1024)
    with pytest.warns(UserWarning, match="big-pkg") as record:
        out_warn = build(project, platform=None, output=tmp_path / "out-warn")
    assert out_warn.is_dir()
    warn_messages = [str(item.message) for item in record]
    assert len(warn_messages) == 2
    for func_name in ("index", "ping"):
        msg = next(m for m in warn_messages if f"'{func_name}'" in m)
        assert "big-pkg" in msg
        assert "med-pkg" in msg
        assert msg.index("big-pkg") < msg.index("med-pkg")
    capsys.readouterr()

    # Above fail limit: build fails with RuntimeError naming function and largest dists.
    monkeypatch.setattr(report_module, "SIZE_LIMIT_BYTES", 4_096)
    with pytest.raises(RuntimeError) as exc_info:
        build(project, platform=None, output=tmp_path / "out-fail")
    error_message = str(exc_info.value)
    assert "index" in error_message
    assert "big-pkg" in error_message
    assert "med-pkg" in error_message
    capsys.readouterr()

    # CLI build prints the per-function size report alongside the build summary.
    monkeypatch.setattr(report_module, "SIZE_WARN_BYTES", 1024 * 1024)
    monkeypatch.setattr(report_module, "SIZE_LIMIT_BYTES", 2 * 1024 * 1024)
    monkeypatch.chdir(project)
    cli_main(["build", "--platform", "native", "--output", str(tmp_path / "cli-out")])
    cli_out = capsys.readouterr().out
    assert "index" in cli_out
    assert "ping" in cli_out
    assert "3 distributions" in cli_out
    assert "Build output written to" in cli_out


# @lat: [[tests#Root-only build ignores preserve nested public and tests]]
@pytest.mark.parametrize(
    (
        "files",
        "symlinks",
        "function_name",
        "request_path",
        "expected_body",
        "expected_missing",
        "expected_static",
    ),
    [
        pytest.param(
            {
                "pyproject.toml": '[project]\nname = "nested"\nversion = "0.1.0"\n',
                "app/public/route.py": (
                    "from mylib.tests import helper\n"
                    'def GET():\n    return {"from": helper.VALUE}\n'
                ),
                "app/public/.secret": "nope",
                "mylib/__init__.py": "",
                "mylib/tests/__init__.py": "",
                "mylib/tests/helper.py": 'VALUE = "nested-ok"\n',
                "public/hello.txt": "static-file",
                "tests/test_root.py": "assert True\n",
                "node_modules/pkg/index.js": "module.exports = 1;\n",
                "frontend/node_modules/pkg/index.js": "module.exports = 2;\n",
                "uv.lock": "version = 1\n",
                "subpkg/uv.lock": "version = 1\n",
            },
            [],
            "public",
            "/public",
            {"from": "nested-ok"},
            (
                "app/public/.secret",
                "public",
                "tests",
                "node_modules",
                "frontend/node_modules",
                "uv.lock",
                "subpkg/uv.lock",
            ),
            ("hello.txt", "static-file"),
            id="nested-public-tests-and-node-modules",
        ),
        pytest.param(
            {
                "pyproject.toml": '[project]\nname = "dangling"\nversion = "0.1.0"\n',
                "app/route.py": 'def GET():\n    return {"ok": True}\n',
                "public/ok.txt": "ok",
            },
            [("subpkg/tests/fixtures/dangling-link", "missing-target")],
            "index",
            "/",
            {"ok": True},
            ("subpkg/tests/fixtures/dangling-link",),
            ("ok.txt", "ok"),
            id="dangling-symlink-in-nested-tests",
        ),
        pytest.param(
            {
                "pyproject.toml": '[project]\nname = "overlap"\nversion = "0.1.0"\n',
                "app/images/route.py": 'def GET():\n    return {"route": "images"}\n',
                "app/images/logo.txt": "logo",
            },
            [("public", "app/images")],
            "images",
            "/images",
            {"route": "images"},
            ("public",),
            ("logo.txt", "logo"),
            id="public-symlink-into-app",
        ),
        pytest.param(
            {
                "pyproject.toml": '[project]\nname = "sharedpkg"\nversion = "0.1.0"\n',
                "app/images/route.py": (
                    "from shared.required import VALUE\n"
                    'def GET():\n    return {"shared": VALUE}\n'
                ),
                "shared/__init__.py": "",
                "shared/required.py": 'VALUE = "from-shared"\n',
                "shared/logo.txt": "shared-logo",
            },
            [("public", "shared")],
            "images",
            "/images",
            {"shared": "from-shared"},
            ("public",),
            ("logo.txt", "shared-logo"),
            id="public-symlink-to-imported-package",
        ),
    ],
)
def test_build_ignores_root_only_preserves_nested_public_and_tests(
    tmp_path: Path,
    monkeypatch: Any,
    isolated_project_imports: Any,
    stub_build_install: Any,
    files: dict[str, str],
    symlinks: list[tuple[str, str]],
    function_name: str,
    request_path: str,
    expected_body: dict[str, Any],
    expected_missing: tuple[str, ...],
    expected_static: tuple[str, str],
):
    import shutil

    project = tmp_path / "proj"
    write_project(project, files)
    for link_rel, target_rel in symlinks:
        link_path = project / link_rel
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(project / target_rel)

    stub_build_install(
        {
            **make_dist("chano", "chano"),
            **make_dist("vercel-runtime", "vercel_runtime"),
            **make_dist("vercel", "vercel"),
        }
    )
    monkeypatch.syspath_prepend(str(project))
    out = build(project, platform=None, output=tmp_path / "out")

    func_dir = out / "functions" / f"{function_name}.func"
    for rel in expected_missing:
        assert not (func_dir / rel).exists(), rel
    static_rel, static_text = expected_static
    assert (out / "static" / static_rel).read_text() == static_text

    shutil.rmtree(project)
    runner = (
        "import asyncio, json, sys, httpx, index\n"
        "async def run():\n"
        "    async with httpx.AsyncClient(\n"
        "        transport=httpx.ASGITransport(app=index.app),\n"
        '        base_url="http://test",\n'
        "    ) as client:\n"
        "        r = await client.get(sys.argv[1])\n"
        '        print(json.dumps({"status": r.status_code, "body": r.json()}))\n'
        "asyncio.run(run())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", runner, request_path],
        cwd=func_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "status": 200,
        "body": expected_body,
    }
