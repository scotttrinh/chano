from __future__ import annotations

import sys
import sysconfig
from contextlib import contextmanager
from pathlib import Path

import pytest

from chano import build as build_module
from chano import create_app
from chano.build import build
from tests.helpers import (
    EXAMPLE,
    HOST_PYTHON,
    PREFIXED_FILES,
    make_client,
    write_project,
)

_INSTALLED_ROOTS = tuple(
    Path(p).resolve()
    for key in ("stdlib", "platstdlib", "purelib", "platlib")
    if (p := sysconfig.get_paths().get(key))
)


def _is_under_installed_roots(path_str: str) -> bool:
    path = Path(path_str).resolve()
    return any(path.is_relative_to(root) for root in _INSTALLED_ROOTS)


def _is_installed_module(mod: object) -> bool:
    spec = getattr(mod, "__spec__", None)
    if spec is None:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    if spec.has_location and isinstance(spec.origin, str):
        return _is_under_installed_roots(spec.origin)
    if spec.submodule_search_locations:
        paths = list(spec.submodule_search_locations)
        return bool(paths) and all(_is_under_installed_roots(p) for p in paths)
    return False


@contextmanager
def _isolated_import_scope():
    """Restore sys.path, bytecode setting, and non-installed sys.modules entries."""
    before_path = list(sys.path)
    before_bytecode = sys.dont_write_bytecode
    before_modules = set(sys.modules)
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        # Classify before mutating sys.path or sys.modules: namespace packages
        # evaluate submodule_search_locations dynamically via their parent.
        to_evict = [
            name
            for name in set(sys.modules) - before_modules
            if not _is_installed_module(sys.modules.get(name))
        ]
        sys.path[:] = before_path
        sys.dont_write_bytecode = before_bytecode
        for name in to_evict:
            sys.modules.pop(name, None)


@pytest.fixture
def isolated_imports():
    """Restore sys.path and evict non-installed modules added to sys.modules.

    Modules from the installed Python environment (stdlib, site-packages,
    built-in, and frozen, including namespace packages) are kept so native
    extensions are never re-imported. Route modules, temp-project modules,
    examples, and synthetic modules are evicted.
    """
    with _isolated_import_scope():
        yield


@pytest.fixture
def stub_build_install(monkeypatch: pytest.MonkeyPatch):
    """Stub dependency installation (optionally populating fake dists) and skip
    the post-build import check."""

    def apply(store_files: dict[str, str] | None = None) -> None:
        if store_files is None:
            monkeypatch.setattr(
                build_module, "_install_dependencies", lambda *args: None
            )
        else:

            def _install(_workspace, vendor_dir, *_args, **_kwargs):
                write_project(vendor_dir, store_files)

            monkeypatch.setattr(build_module, "_install_dependencies", _install)
        monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    return apply


@pytest.fixture(scope="module")
def example_app():
    return create_app(EXAMPLE / "app", public_dir=EXAMPLE / "public")


@pytest.fixture(scope="module")
def client(example_app):
    return make_client(example_app)


@pytest.fixture(scope="module")
def output():
    # Vendor for this machine so the function can be invoked locally.
    return build(EXAMPLE, python=HOST_PYTHON, platform=None)


@pytest.fixture(scope="module")
def prefixed_project(tmp_path_factory: pytest.TempPathFactory):
    project = tmp_path_factory.mktemp("prefixed")
    write_project(project, PREFIXED_FILES)
    return project


@pytest.fixture(scope="module")
def prefixed_output(prefixed_project):
    install = build_module._install_dependencies

    def install_like_example(
        workspace_root, vendor_dir, python, platform, package=None
    ):
        # Same dependencies as examples/hello, which has a lock to export.
        install(EXAMPLE, vendor_dir, python, platform)

    with _isolated_import_scope(), pytest.MonkeyPatch.context() as patch:
        patch.setattr(build_module, "_install_dependencies", install_like_example)
        return build(prefixed_project, python=HOST_PYTHON, platform=None)
