import json
import subprocess
import sys
from pathlib import Path

import pytest

from chano import build as build_module
from chano.build import build, plan_build
from chano.cli import main as cli_main
from tests.helpers import (
    HOST_PYTHON,
    invoke,
    make_dist,
    write_project,
)


def _write_workspace_pkg(
    workspace: Path,
    dist_name: str,
    module_name: str,
    source: str,
    *,
    deps: list[str] | None = None,
) -> None:
    dep_list = deps or []
    sources_block = ""
    if dep_list:
        entries = "\n".join(f"{dep} = {{ workspace = true }}" for dep in dep_list)
        sources_block = f"\n[tool.uv.sources]\n{entries}\n"
    write_project(
        workspace / "packages" / dist_name,
        {
            "pyproject.toml": (
                f'[project]\nname = "{dist_name}"\nversion = "0.1.0"\n'
                f'requires-python = ">=3.13"\n'
                f"dependencies = {json.dumps(dep_list)}\n\n"
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
                f"{sources_block}"
            ),
            f"src/{module_name}/__init__.py": source,
        },
    )


# @lat: [[tests#Per-function tree-shaking]]
def test_tree_shaking_workspace_build(tmp_path, isolated_imports, monkeypatch, capsys):
    from chano.build import index_store

    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "shaken-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(workspace, "pkg-trans", "pkg_trans", 'VALUE = "from-trans"\n')
    _write_workspace_pkg(
        workspace,
        "pkg-a",
        "pkg_a",
        (
            "import pkg_trans\n\n"
            'def describe() -> str:\n    return f"pkg-a+{pkg_trans.VALUE}"\n'
        ),
        deps=["pkg-trans"],
    )
    _write_workspace_pkg(
        workspace,
        "pkg-b",
        "pkg_b",
        'def describe() -> str:\n    return "pkg-b"\n',
    )
    _write_workspace_pkg(
        workspace,
        "pkg-c",
        "pkg_c",
        'def describe() -> str:\n    return "pkg-c"\n',
    )
    _write_workspace_pkg(
        workspace,
        "pkg-d",
        "pkg_d",
        'def describe() -> str:\n    return "pkg-d"\n',
    )

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.13"\n'
        'dependencies = ["chano", "pkg-a", "pkg-b", "pkg-c", "pkg-d"]\n\n'
        "[tool.uv.sources]\n"
        f"chano = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
        "pkg-b = { workspace = true }\n"
        "pkg-c = { workspace = true }\n"
        "pkg-d = { workspace = true }\n"
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": base_pyproject,
            "helpers/__init__.py": "",
            "helpers/chain.py": (
                "from . import leaf\n"
                "from .cycle import TAG\n\n"
                "def run() -> str:\n"
                '    return f"{leaf.run()}:{TAG}"\n'
            ),
            "helpers/cycle.py": (
                'TAG = "ok"\n\n'
                "def touch():\n"
                "    from . import chain\n"
                "    return chain\n"
            ),
            "helpers/leaf.py": (
                "import pkg_a\n\ndef run() -> str:\n    return pkg_a.describe()\n"
            ),
            "app/alpha/route.py": (
                "import typing\n"
                "from typing import TYPE_CHECKING\n"
                "from helpers.chain import run\n\n"
                "if TYPE_CHECKING:\n"
                "    import pkg_d\n"
                "if typing.TYPE_CHECKING:\n"
                "    import pkg_b\n\n"
                "def GET():\n"
                '    return {"result": run()}\n'
            ),
            "app/beta/route.py": (
                "def GET():\n"
                "    import pkg_b\n"
                '    return {"result": pkg_b.describe()}\n'
            ),
            "app/dynamic/route.py": (
                "import importlib\n"
                "from importlib import import_module\n\n"
                "def GET():\n"
                '    b = importlib.import_module("pkg_b")\n'
                '    c = import_module("pkg_c")\n'
                '    return {"b": b.describe(), "c": c.describe()}\n'
            ),
            "app/dunder/route.py": (
                "def GET():\n"
                '    c = __import__("pkg_c")\n'
                '    return {"c": c.describe()}\n'
            ),
            "app/included/route.py": (
                "import importlib\n\n"
                'include = ["Pkg_D"]\n\n'
                "def GET():\n"
                '    mod = importlib.import_module("".join(["pkg", "_d"]))\n'
                '    return {"d": mod.describe()}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    for dist_name in ("pkg-trans", "pkg-a", "pkg-b", "pkg-c", "pkg-d"):
        monkeypatch.syspath_prepend(str(workspace / "packages" / dist_name / "src"))

    api = workspace / "apps" / "api"
    plan = plan_build(api, platform=None, output=tmp_path / "out-shaken")
    by_name = {spec.name: spec for spec in plan.functions}
    assert by_name["alpha"].trace_roots == (
        (api / "app" / "alpha" / "route.py").resolve(),
    )
    assert by_name["alpha"].include == ()
    assert by_name["included"].include == ("pkg-d",)

    shaken = build(
        api, python=HOST_PYTHON, platform=None, output=tmp_path / "out-shaken"
    )
    report_out = capsys.readouterr().out

    def dists_for(out_dir: Path, func_name: str) -> set[str]:
        return set(
            index_store(
                out_dir / "functions" / f"{func_name}.func" / "_vendor"
            ).distributions
        )

    always = {"chano", "msgspec", "vercel-runtime"}

    alpha_dists = dists_for(shaken, "alpha")
    beta_dists = dists_for(shaken, "beta")
    dynamic_dists = dists_for(shaken, "dynamic")
    dunder_dists = dists_for(shaken, "dunder")
    included_dists = dists_for(shaken, "included")

    assert always | {"pkg-a", "pkg-trans"} <= alpha_dists
    assert not ({"pkg-b", "pkg-c", "pkg-d"} & alpha_dists)

    assert always | {"pkg-b"} <= beta_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-c", "pkg-d"} & beta_dists)

    assert always | {"pkg-b", "pkg-c"} <= dynamic_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-d"} & dynamic_dists)

    assert always | {"pkg-c"} <= dunder_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-b", "pkg-d"} & dunder_dists)

    assert always | {"pkg-d"} <= included_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-b", "pkg-c"} & included_dists)

    # Size report reflects each function's actual bundled distribution count.
    for func_name, expected_dists in (
        ("alpha", alpha_dists),
        ("beta", beta_dists),
        ("dynamic", dynamic_dists),
        ("dunder", dunder_dists),
        ("included", included_dists),
    ):
        line = next(
            row for row in report_out.splitlines() if row.strip().startswith(func_name)
        )
        assert f"({len(expected_dists)} distributions)" in line

    # Shared files across functions are still hard-linked.
    alpha_chano = (
        shaken / "functions" / "alpha.func" / "_vendor" / "chano" / "__init__.py"
    )
    beta_chano = (
        shaken / "functions" / "beta.func" / "_vendor" / "chano" / "__init__.py"
    )
    assert alpha_chano.stat().st_ino == beta_chano.stat().st_ino

    # Every tree-shaken function executes standalone via vc_handler (python -S).
    assert json.loads(invoke(shaken, "alpha", "/alpha")["body"]) == {
        "result": "pkg-a+from-trans:ok"
    }
    assert json.loads(invoke(shaken, "beta", "/beta")["body"]) == {"result": "pkg-b"}
    assert json.loads(invoke(shaken, "dynamic", "/dynamic")["body"]) == {
        "b": "pkg-b",
        "c": "pkg-c",
    }
    assert json.loads(invoke(shaken, "dunder", "/dunder")["body"]) == {"c": "pkg-c"}
    assert json.loads(invoke(shaken, "included", "/included")["body"]) == {"d": "pkg-d"}

    # Project-level `include` adds its normalized distribution to every function.
    (api / "pyproject.toml").write_text(
        base_pyproject + '\n[tool.chano]\ninclude = ["Pkg_D"]\n'
    )
    with_proj_inc = build(
        api, python=HOST_PYTHON, platform=None, output=tmp_path / "out-proj-inc"
    )
    capsys.readouterr()
    beta_with_inc = dists_for(with_proj_inc, "beta")
    assert always | {"pkg-b", "pkg-d"} <= beta_with_inc
    assert not ({"pkg-a", "pkg-trans", "pkg-c"} & beta_with_inc)
    assert json.loads(invoke(with_proj_inc, "beta", "/beta")["body"]) == {
        "result": "pkg-b"
    }

    # `tree_shake = false` restores the full store into every function.
    (api / "pyproject.toml").write_text(
        base_pyproject + "\n[tool.chano]\ntree_shake = false\n"
    )
    full = build(api, python=HOST_PYTHON, platform=None, output=tmp_path / "out-full")
    full_report = capsys.readouterr().out
    all_expected = always | {"pkg-a", "pkg-trans", "pkg-b", "pkg-c", "pkg-d"}
    for func_name in ("alpha", "beta", "dynamic", "dunder", "included"):
        func_dists = dists_for(full, func_name)
        assert all_expected <= func_dists
        line = next(
            row for row in full_report.splitlines() if row.strip().startswith(func_name)
        )
        assert f"({len(func_dists)} distributions)" in line
    assert json.loads(invoke(full, "alpha", "/alpha")["body"]) == {
        "result": "pkg-a+from-trans:ok"
    }
    assert json.loads(invoke(full, "beta", "/beta")["body"]) == {"result": "pkg-b"}


# @lat: [[tests#Import tracer and closure]]
def test_trace_and_closure_edge_cases(
    tmp_path, isolated_imports, monkeypatch, stub_build_install, capsys
):
    from chano.build import index_store

    store_files: dict[str, str] = {}
    for dist in (
        make_dist("chano", "chano", requires=["msgspec>=0.20"]),
        make_dist("msgspec", "msgspec"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("vercel", "vercel", requires=["vf-dep"]),
        make_dist("vf-dep", "vf_dep"),
        make_dist("cyc-a", "cyc_a", requires=["cyc-b"]),
        make_dist("cyc-b", "cyc_b", requires=["cyc-a"]),
        make_dist("ns-one", "ns_one", top_level="ns_shared\n"),
        make_dist("ns-two", "ns_two", top_level="ns_shared\n"),
        make_dist("else-pkg", "else_pkg"),
        make_dist("not-tc-pkg", "not_tc_pkg"),
        make_dist("tc-only", "tc_only"),
        make_dist("rel-pkg", "rel_pkg"),
        make_dist("init-pkg", "init_pkg"),
        make_dist("alias-pkg", "alias_pkg"),
        make_dist(
            "huge-pkg",
            "huge_pkg",
            extra_files={"huge_pkg/weights.bin": "x" * 16_384},
        ),
    ):
        store_files.update(dist)

    stub_build_install(store_files)

    project = tmp_path / "edge"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "edge"\nversion = "0"\n',
            # Namespace-package project helper directory (no __init__.py).
            "ns_helpers/sub.py": (
                "def use():\n    import rel_pkg\n    return rel_pkg\n"
            ),
            # Package whose __init__.py itself imports init_pkg.
            "pkg_helpers/__init__.py": (
                "def init_use():\n    import init_pkg\n    return init_pkg\n"
            ),
            "pkg_helpers/mod.py": (
                "def use():\n"
                "    import importlib as il\n"
                "    from importlib import import_module as im\n"
                '    il.import_module(name="alias_pkg")\n'
                '    im(".leaf", __package__)\n'
            ),
            "pkg_helpers/leaf.py": ("def use():\n    import cyc_a\n    return cyc_a\n"),
            "pkg_helpers/shared.py": (
                "def use():\n    import ns_shared\n    return ns_shared\n"
            ),
            "pkg_helpers/sub/__init__.py": "",
            "pkg_helpers/sub/deep.py": (
                "from ..shared import use as shared_use\n\n"
                "def run():\n    return shared_use()\n"
            ),
            "app/traced/route.py": (
                "import os\n"
                "from typing import TYPE_CHECKING\n"
                "from ns_helpers import sub\n"
                "from pkg_helpers.mod import use\n"
                "from pkg_helpers.sub.deep import run\n\n"
                "if TYPE_CHECKING:\n"
                "    import tc_only\n"
                "else:\n"
                "    def runtime_else():\n"
                "        import else_pkg\n"
                "        return else_pkg\n\n"
                "if not TYPE_CHECKING:\n"
                "    def runtime_not_tc():\n"
                "        import not_tc_pkg\n"
                "        return not_tc_pkg\n"
                "else:\n"
                "    import tc_only\n\n"
                "if TYPE_CHECKING and True:\n"
                "    import tc_only\n\n"
                "def GET():\n"
                "    if False:\n"
                "        import unknown_not_in_store\n"
                '    return {"ok": True}\n'
            ),
            "app/heavy/route.py": (
                'def GET():\n    import huge_pkg\n    return {"ok": True}\n'
            ),
            "app/tiny/route.py": ("def GET():\n    return {'ok': True}\n"),
        },
    )

    monkeypatch.setattr(build_module, "SIZE_WARN_BYTES", 8_192)
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 1024 * 1024)
    with pytest.warns(UserWarning) as record:
        out = build(project, platform=None, output=tmp_path / "out")
    capsys.readouterr()

    # Only `heavy` exceeds SIZE_WARN_BYTES; `tiny` and `traced` stay below it.
    warn_messages = [str(item.message) for item in record]
    assert len(warn_messages) == 1
    assert "'heavy'" in warn_messages[0]
    assert "huge-pkg" in warn_messages[0]

    always_with_vf = {
        "chano",
        "msgspec",
        "vercel-runtime",
        "vercel",
        "vf-dep",
    }

    traced_dists = set(
        index_store(out / "functions" / "traced.func" / "_vendor").distributions
    )
    assert traced_dists == always_with_vf | {
        "rel-pkg",
        "init-pkg",
        "alias-pkg",
        "cyc-a",
        "cyc-b",
        "ns-one",
        "ns-two",
        "else-pkg",
        "not-tc-pkg",
    }
    assert "tc-only" not in traced_dists
    assert "huge-pkg" not in traced_dists

    tiny_dists = set(
        index_store(out / "functions" / "tiny.func" / "_vendor").distributions
    )
    assert tiny_dists == always_with_vf

    heavy_dists = set(
        index_store(out / "functions" / "heavy.func" / "_vendor").distributions
    )
    assert heavy_dists == always_with_vf | {"huge-pkg"}

    # Invalid route-level `include` raises ValueError during plan_build.
    for i, bad_include in enumerate(
        ('include = "pkg-a"\n', "include = [123]\n", 'include = [""]\n')
    ):
        bad_proj = tmp_path / f"bad-{i}"
        write_project(
            bad_proj,
            {
                "pyproject.toml": '[project]\nname = "bad"\nversion = "0"\n',
                "app/route.py": bad_include + "def GET():\n    return {}\n",
            },
        )
        with pytest.raises(ValueError, match="include"):
            plan_build(bad_proj)


def lazy_middleware(*modules: str) -> str:
    """Source for a pass-through ``boundary.py`` importing ``modules`` lazily,
    so planning never needs them installed."""
    imports = "".join(f"    import {module}\n" for module in modules)
    return (
        "from chano import Boundary\n\n"
        "async def middleware(request, call_next):\n"
        f"{imports}"
        "    return await call_next(request)\n\n"
        "boundary = Boundary(wrap=[middleware])\n"
    )


# @lat: [[tests#Segment files as trace roots]]
def test_segment_files_are_trace_roots(
    tmp_path, isolated_imports, stub_build_install, capsys
):
    store_files: dict[str, str] = {}
    for dist in (
        make_dist("chano", "chano"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("root-mw-pkg", "root_mw_pkg"),
        make_dist("admin-mw-pkg", "admin_mw_pkg"),
        make_dist("admin-err-pkg", "admin_err_pkg"),
        make_dist("audit-pkg", "audit_pkg"),
        make_dist("public-mw-pkg", "public_mw_pkg"),
    ):
        store_files.update(dist)

    stub_build_install(store_files)

    route = "def GET():\n    return {}\n"
    project = tmp_path / "segments"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "segments"\nversion = "0"\n',
            "helpers/__init__.py": "",
            "helpers/audit.py": "import audit_pkg\n",
            "app/boundary.py": lazy_middleware("root_mw_pkg"),
            "app/route.py": route,
            "app/admin/boundary.py": (
                "from chano import Boundary\n\n"
                "async def middleware(request, call_next):\n"
                "    import admin_mw_pkg\n"
                "    import helpers.audit\n"
                "    return await call_next(request)\n\n"
                "def handle_error(request, exc):\n"
                "    import admin_err_pkg\n"
                "    raise exc\n\n"
                "boundary = Boundary(recover=[handle_error], wrap=[middleware])\n"
            ),
            "app/admin/route.py": route,
            "app/admin/(team)/members/route.py": route,
            "app/admin/settings/[id]/route.py": route,
            "app/public/boundary.py": lazy_middleware("public_mw_pkg"),
            "app/public/route.py": route,
            "app/(site)/about/route.py": route,
        },
    )
    app = (project / "app").resolve()

    plan = plan_build(project, platform=None)
    by_name = {spec.name: spec for spec in plan.functions}
    admin_segments = (
        app / "boundary.py",
        app / "admin" / "boundary.py",
    )
    for name in ("admin", "admin/members", "admin/settings/[id]"):
        spec = by_name[name]
        assert spec.route is not None
        assert spec.trace_roots == (spec.route.file, *admin_segments)
    assert by_name["public"].trace_roots == (
        app / "public" / "route.py",
        app / "boundary.py",
        app / "public" / "boundary.py",
    )
    for name in ("index", "about"):
        spec = by_name[name]
        assert spec.route is not None
        assert spec.trace_roots == (spec.route.file, app / "boundary.py")

    out = build(
        project, platform=None, output=tmp_path / "out", explain="admin/members"
    )
    explain_out = capsys.readouterr().out

    def bundled(function: str) -> set[str]:
        vendor = out / "functions" / f"{function}.func" / "_vendor"
        return set(build_module.index_store(vendor).distributions)

    base = {"chano", "vercel-runtime", "root-mw-pkg"}
    admin_only = {"admin-mw-pkg", "admin-err-pkg", "audit-pkg"}
    for name in ("admin", "admin/members", "admin/settings/[id]"):
        assert bundled(name) == base | admin_only
    assert bundled("public") == base | {"public-mw-pkg"}
    for name in ("index", "about"):
        assert bundled(name) == base

    reasons = {
        line.split()[0]: line for line in explain_out.splitlines() if "traced" in line
    }
    assert reasons.keys() == {"root-mw-pkg"} | admin_only
    assert reasons["root-mw-pkg"].endswith("traced from app/boundary.py")
    assert reasons["admin-mw-pkg"].endswith("traced from app/admin/boundary.py")
    assert reasons["admin-err-pkg"].endswith("traced from app/admin/boundary.py")
    assert reasons["audit-pkg"].endswith(
        "traced from app/admin/boundary.py -> helpers/audit.py"
    )
    excluded = explain_out.split("Excluded (", 1)[1].split()
    assert "public-mw-pkg" in excluded


# @lat: [[tests#Build-time import check]]
def test_build_import_check_catches_computed_import(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    repo_root = Path(__file__).parent.parent.resolve()
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    workspace = tmp_path / "check-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(
        workspace, "pkg-a", "pkg_a", 'def value() -> str:\n    return "from-pkg-a"\n'
    )

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.13"\n'
        'dependencies = ["chano", "pkg-a"]\n\n'
        "[tool.uv.sources]\n"
        f"chano = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
    )
    computed_route = (
        "import importlib\n\n"
        'MOD = importlib.import_module("".join(["pkg", "_a"]))\n\n'
        "def GET():\n"
        '    return {"value": MOD.value()}\n'
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": base_pyproject,
            "app/ok/route.py": 'def GET():\n    return {"ok": True}\n',
            "app/computed/route.py": computed_route,
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)
    monkeypatch.syspath_prepend(str(workspace / "packages" / "pkg-a" / "src"))
    monkeypatch.setenv("PYTHONPATH", str(workspace / "packages" / "pkg-a" / "src"))

    api = workspace / "apps" / "api"

    # Without `include`, the computed import is missed by static tracing and
    # caught by the build-time import check.
    with pytest.raises(RuntimeError) as exc_info:
        build(api, python=host_python, platform=None, output=tmp_path / "out-fail")
    message = str(exc_info.value)
    assert "computed" in message
    assert "pkg_a" in message
    assert "include" in message
    capsys.readouterr()

    # Adding route-level `include` bundles pkg-a and lets the check pass.
    (api / "app" / "computed" / "route.py").write_text(
        'include = ["pkg-a"]\n' + computed_route
    )
    for mod_name in [k for k in sys.modules if k.startswith("chano_routes.")]:
        sys.modules.pop(mod_name, None)
    out_route = build(
        api, python=host_python, platform=None, output=tmp_path / "out-route-inc"
    )
    capsys.readouterr()
    assert not list((out_route / "functions").rglob("__pycache__"))
    assert json.loads(invoke(out_route, "computed", "/computed")["body"]) == {
        "value": "from-pkg-a"
    }

    # Project-level `[tool.chano] include` also satisfies the check.
    (api / "app" / "computed" / "route.py").write_text(computed_route)
    for mod_name in [k for k in sys.modules if k.startswith("chano_routes.")]:
        sys.modules.pop(mod_name, None)
    (api / "pyproject.toml").write_text(
        base_pyproject + '\n[tool.chano]\ninclude = ["pkg-a"]\n'
    )
    out_proj = build(
        api, python=host_python, platform=None, output=tmp_path / "out-proj-inc"
    )
    capsys.readouterr()
    assert json.loads(invoke(out_proj, "computed", "/computed")["body"]) == {
        "value": "from-pkg-a"
    }


# @lat: [[tests#Import check platform matching]]
def test_build_import_check_skips_non_matching_platform(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    project = tmp_path / "plat"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "plat"\nversion = "0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
        },
    )
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)

    uv_calls: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["uv", "python", "find"]:
            uv_calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(build_module.subprocess, "run", spy_run)

    # On macOS arm64, DEFAULT_PLATFORM (x86_64-manylinux_2_28) skips the check.
    monkeypatch.setattr(build_module, "_host_os_arch", lambda: ("darwin", "aarch64"))
    out = build(project, output=tmp_path / "out-darwin")
    assert out.is_dir()
    skipped_out = capsys.readouterr().out
    assert "skip" in skipped_out.lower()
    assert build_module.DEFAULT_PLATFORM in skipped_out
    assert uv_calls == []

    # On Linux x86_64, a non-matching platform (e.g. aarch64-apple-darwin) also skips.
    monkeypatch.setattr(build_module, "_host_os_arch", lambda: ("linux", "x86_64"))
    out_linux_skip = build(
        project, platform="aarch64-apple-darwin", output=tmp_path / "out-linux-skip"
    )
    assert out_linux_skip.is_dir()
    linux_skip_out = capsys.readouterr().out
    assert "skip" in linux_skip_out.lower()
    assert "aarch64-apple-darwin" in linux_skip_out
    assert uv_calls == []

    # On Linux x86_64, DEFAULT_PLATFORM matches the host so the check runs
    # (and fails here because `_install_dependencies` was stubbed without chano).
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    with pytest.raises(RuntimeError) as exc_info:
        build(
            project,
            python=host_python,
            platform=build_module.DEFAULT_PLATFORM,
            output=tmp_path / "out-linux-match",
        )
    assert "index" in str(exc_info.value)
    assert "chano" in str(exc_info.value)
    assert uv_calls == [["uv", "python", "find", host_python]]


# @lat: [[tests#Import check target interpreter]]
def test_build_import_check_uses_uv_python_find(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    repo_root = Path(__file__).parent.parent.resolve()
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    workspace = tmp_path / "interp-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["apps/*"]\n',
        },
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": (
                '[project]\nname = "api"\nversion = "0.1.0"\n'
                'requires-python = ">=3.13"\n'
                'dependencies = ["chano"]\n\n'
                "[tool.uv.sources]\n"
                f"chano = {{ path = {json.dumps(repo_root.as_posix())}, "
                "editable = true }\n"
            ),
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)
    api = workspace / "apps" / "api"

    # Build a wrapper script at a distinct path so we can prove the import check
    # invokes the interpreter returned by `uv python find`, not `sys.executable`.
    marker = tmp_path / "invocations.log"
    wrapper = tmp_path / "resolved-python"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s %s\\n" "$1" "$2" "$3" >> {json.dumps(str(marker))}\n'
        f'exec {json.dumps(sys.executable)} "$@"\n'
    )
    wrapper.chmod(0o755)

    real_run = subprocess.run
    find_calls: list[list[str]] = []
    find_returncode = 0
    find_stdout = f"{wrapper}\n"

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["uv", "python", "find"]:
            find_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, find_returncode, find_stdout, "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)

    out = build(api, python=host_python, platform=None, output=tmp_path / "out-ok")
    assert out.is_dir()
    capsys.readouterr()
    assert find_calls == [["uv", "python", "find", host_python]]
    invoked = marker.read_text().splitlines()
    assert len(invoked) == 1
    assert "-I" in invoked[0].split()

    # Passing a different `--python` forwards that version to `uv python find`.
    find_calls.clear()
    marker.unlink()
    real_install = build_module._install_dependencies

    def install_for_host(ws_root, vendor_dir, _py, plat, pkg=None):
        real_install(ws_root, vendor_dir, host_python, plat, pkg)

    monkeypatch.setattr(build_module, "_install_dependencies", install_for_host)
    out_other = build(api, python="3.13", platform=None, output=tmp_path / "out-313")
    assert out_other.is_dir()
    capsys.readouterr()
    assert find_calls == [["uv", "python", "find", "3.13"]]
    assert marker.is_file()

    # When `uv python find` finds no interpreter, skip with a message instead of
    # falling back to `sys.executable`, even when `--python` matches host_python.
    find_calls.clear()
    marker.unlink()
    find_returncode = 2
    find_stdout = ""
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    out_missing = build(
        api, python=host_python, platform=None, output=tmp_path / "out-no-interp"
    )
    assert out_missing.is_dir()
    skip_out = capsys.readouterr().out
    assert "skip" in skip_out.lower()
    assert host_python in skip_out
    assert find_calls == [["uv", "python", "find", host_python]]
    assert not marker.exists()


# @lat: [[tests#Build explain]]
def test_build_explain(tmp_path, isolated_imports, monkeypatch, capsys):
    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "explain-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(workspace, "pkg-leaf", "pkg_leaf", 'VAL = "leaf"\n')
    _write_workspace_pkg(
        workspace,
        "pkg-mid",
        "pkg_mid",
        'import pkg_leaf\nVAL = f"mid+{pkg_leaf.VAL}"\n',
        deps=["pkg-leaf"],
    )
    _write_workspace_pkg(
        workspace,
        "pkg-a",
        "pkg_a",
        'import pkg_mid\ndef run() -> str:\n    return f"a+{pkg_mid.VAL}"\n',
        deps=["pkg-mid"],
    )
    _write_workspace_pkg(workspace, "pkg-direct", "pkg_direct", 'NAME = "direct"\n')
    _write_workspace_pkg(
        workspace, "pkg-route-inc", "pkg_route_inc", 'NAME = "route-inc"\n'
    )
    _write_workspace_pkg(
        workspace, "pkg-proj-inc", "pkg_proj_inc", 'NAME = "proj-inc"\n'
    )
    _write_workspace_pkg(workspace, "pkg-only-beta", "pkg_only_beta", 'NAME = "beta"\n')

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.13"\n'
        "dependencies = [\n"
        '    "chano",\n'
        '    "pkg-a",\n'
        '    "pkg-direct",\n'
        '    "pkg-route-inc",\n'
        '    "pkg-proj-inc",\n'
        '    "pkg-only-beta",\n'
        "]\n\n"
        "[tool.uv.sources]\n"
        f"chano = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
        "pkg-direct = { workspace = true }\n"
        "pkg-route-inc = { workspace = true }\n"
        "pkg-proj-inc = { workspace = true }\n"
        "pkg-only-beta = { workspace = true }\n"
    )
    api = workspace / "apps" / "api"
    write_project(
        api,
        {
            "pyproject.toml": (
                base_pyproject + '\n[tool.chano]\ninclude = ["pkg_proj_inc"]\n'
            ),
            "helpers/__init__.py": "",
            "helpers/a.py": (
                "import pkg_a\n\ndef run() -> str:\n    return pkg_a.run()\n"
            ),
            "app/alpha/route.py": (
                "import pkg_direct\n"
                "from helpers.a import run\n\n"
                'include = ["Pkg_Route_Inc"]\n\n'
                "def GET():\n"
                '    return {"direct": pkg_direct.NAME, "a": run()}\n'
            ),
            "app/beta/route.py": (
                "import pkg_only_beta\n\n"
                "def GET():\n"
                '    return {"beta": pkg_only_beta.NAME}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    for dist_name in (
        "pkg-leaf",
        "pkg-mid",
        "pkg-a",
        "pkg-direct",
        "pkg-route-inc",
        "pkg-proj-inc",
        "pkg-only-beta",
    ):
        monkeypatch.syspath_prepend(str(workspace / "packages" / dist_name / "src"))

    def parse_explain_sections(output: str) -> tuple[dict[str, str], list[str]]:
        included: dict[str, str] = {}
        excluded: list[str] = []
        section: str | None = None
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Included ("):
                section = "included"
                continue
            if stripped.startswith("Excluded ("):
                section = "excluded"
                continue
            if section == "included":
                if not line.startswith("    "):
                    section = None
                    continue
                if stripped == "(none)":
                    continue
                name, rest = stripped.split(None, 1)
                included[name] = rest
            elif section == "excluded":
                if not line.startswith("    "):
                    section = None
                    continue
                if stripped == "(none)":
                    continue
                excluded.append(stripped.split()[0])
        return included, excluded

    monkeypatch.chdir(api)
    out_alpha = tmp_path / "out-alpha"
    cli_main(
        [
            "build",
            "--platform",
            "native",
            "--output",
            str(out_alpha),
            "--explain",
            "alpha",
        ]
    )
    alpha_stdout = capsys.readouterr().out
    assert "Explain alpha:" in alpha_stdout
    inc, exc = parse_explain_sections(alpha_stdout)

    assert "always included" in inc["chano"]
    assert "always included" in inc["vercel-runtime"]
    assert "required by chano <- always included" in inc["msgspec"]
    assert "traced from app/alpha/route.py" in inc["pkg-direct"]
    assert "traced from app/alpha/route.py -> helpers/a.py" in inc["pkg-a"]
    assert (
        "required by pkg-a <- traced from app/alpha/route.py -> helpers/a.py"
        in inc["pkg-mid"]
    )
    assert (
        "required by pkg-mid <- pkg-a <- traced from app/alpha/route.py -> helpers/a.py"
        in inc["pkg-leaf"]
    )
    assert "listed in route include (app/alpha/route.py)" in inc["pkg-route-inc"]
    assert "listed in [tool.chano] include" in inc["pkg-proj-inc"]

    assert "pkg-only-beta" in exc
    assert "pkg-only-beta" not in inc
    assert exc == sorted(exc)
    alpha_vendor = out_alpha / "functions" / "alpha.func" / "_vendor"
    assert not (alpha_vendor / "pkg_only_beta").exists()
    assert "pkg-only-beta" not in build_module.index_store(alpha_vendor).distributions

    # Trailing `.func` suffix is normalized to match the function name.
    out_beta = tmp_path / "out-beta"
    build(api, platform=None, output=out_beta, explain="beta.func")
    beta_stdout = capsys.readouterr().out
    assert "Explain beta:" in beta_stdout
    beta_inc, beta_exc = parse_explain_sections(beta_stdout)
    assert "traced from app/beta/route.py" in beta_inc["pkg-only-beta"]
    assert "listed in [tool.chano] include" in beta_inc["pkg-proj-inc"]
    for excluded_name in (
        "pkg-a",
        "pkg-mid",
        "pkg-leaf",
        "pkg-direct",
        "pkg-route-inc",
    ):
        assert excluded_name in beta_exc
        assert excluded_name not in beta_inc

    # `tree_shake = false` explains every store distribution with none excluded.
    (api / "pyproject.toml").write_text(
        base_pyproject + "\n[tool.chano]\ntree_shake = false\n"
    )
    out_full = tmp_path / "out-full"
    build(api, platform=None, output=out_full, explain="alpha")
    full_stdout = capsys.readouterr().out
    assert "Explain alpha:" in full_stdout
    full_inc, full_exc = parse_explain_sections(full_stdout)
    assert full_exc == []
    assert "(none)" in full_stdout
    for dist_name in (
        "chano",
        "pkg-a",
        "pkg-mid",
        "pkg-leaf",
        "pkg-only-beta",
    ):
        assert "tree_shake = false" in full_inc[dist_name]

    # Unknown `--explain` function raises ValueError naming it and available functions,
    # before touching an existing output directory.
    sentinel = out_alpha / "keep.txt"
    sentinel.write_text("untouched")
    with pytest.raises(ValueError) as exc_info:
        cli_main(["build", "--output", str(out_alpha), "--explain", "missing"])
    msg = str(exc_info.value)
    assert "missing" in msg
    assert "alpha" in msg
    assert "beta" in msg
    assert sentinel.read_text() == "untouched"
