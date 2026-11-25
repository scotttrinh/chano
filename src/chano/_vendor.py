"""uv workspace detection, dependency vendoring, and function layout constants."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from chano.config import PYPROJECT, read_pyproject

UV_LOCK = "uv.lock"
VENDOR_DIR = "_vendor"
ENTRYPOINT = "index.py"

# Vercel's Python functions run on Amazon Linux 2023 (glibc 2.34).
DEFAULT_PLATFORM = "x86_64-manylinux_2_28"
DEFAULT_PYTHON = "3.13"

# Shared `_vendor/` sys.path setup used by both the deployed trampoline and
# the build-time import check so their import resolution cannot drift.
_VENDOR_SETUP_SOURCE = f"""\
_vendor = os.path.join(_here, "{VENDOR_DIR}")
site.addsitedir(_vendor)
while _vendor in sys.path:
    sys.path.remove(_vendor)
sys.path.insert(1 if sys.path and sys.path[0] in ("", _here) else 0, _vendor)
importlib.invalidate_caches()
"""


def _is_uv_workspace(directory: Path) -> bool:
    pyproject = directory / PYPROJECT
    if not (directory / UV_LOCK).is_file() or not pyproject.is_file():
        return False
    tool = read_pyproject(pyproject).get("tool")
    if not isinstance(tool, Mapping):
        return False
    uv = tool.get("uv")
    return isinstance(uv, Mapping) and isinstance(uv.get("workspace"), Mapping)


def _find_workspace_root(project_dir: Path) -> Path | None:
    """Nearest directory from ``project_dir`` upward declaring
    ``[tool.uv.workspace]`` alongside ``uv.lock``, or ``None``.

    Stops walking up at any directory with its own ``uv.lock`` that does not
    declare ``[tool.uv.workspace]``.
    """
    for directory in (project_dir, *project_dir.parents):
        if (directory / UV_LOCK).is_file():
            return directory if _is_uv_workspace(directory) else None
    return None


def _project_name(pyproject: Path) -> str | None:
    project = read_pyproject(pyproject).get("project")
    if not isinstance(project, Mapping):
        return None
    name = project.get("name")
    return name if isinstance(name, str) and name.strip() else None


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed:\n{result.stderr}")


def _install_dependencies(
    workspace_root: Path,
    vendor_dir: Path,
    python: str,
    platform: str | None,
    package: str | None = None,
) -> None:
    """Vendor the project's locked dependencies plus Vercel's Python runtime."""
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        export = ["uv", "export"]
        if package is not None:
            export += ["--package", package]
        export += [
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-editable",
            "--no-hashes",
            "--quiet",
            "-o",
            str(requirements),
        ]
        _run(export, cwd=workspace_root)
        cmd = [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--target",
            str(vendor_dir),
            "--python-version",
            python,
            "-r",
            str(requirements),
            "vercel-runtime",
            "vercel>=0.10.0",
        ]
        if platform:
            cmd += ["--python-platform", platform]
        _run(cmd, cwd=workspace_root)
