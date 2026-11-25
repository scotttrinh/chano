from __future__ import annotations

import json
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from chano._vendor import _VENDOR_SETUP_SOURCE

_PLATFORM_ALIASES: Mapping[str, tuple[str, str]] = {
    "linux": ("linux", "x86_64"),
    "macos": ("darwin", "aarch64"),
    "windows": ("windows", "x86_64"),
}

_ARCH_ALIASES: Mapping[str, str] = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "i386": "i686",
    "i686": "i686",
    "x86": "i686",
}

_IMPORT_CHECK_PREFIX = "__CHANO_IMPORT_ERROR__:"

_IMPORT_CHECK_SCRIPT = f"""\
import importlib
import json
import os
import site
import sys

_here = sys.argv[1]
sys.path.insert(0, _here)
{_VENDOR_SETUP_SOURCE}
try:
    importlib.import_module("index")
except ImportError as exc:
    payload = json.dumps({{"name": exc.name, "msg": str(exc)}})
    print(f"{_IMPORT_CHECK_PREFIX}{{payload}}", file=sys.stderr)
    sys.exit(1)
"""


def _normalize_arch(arch: str) -> str:
    lowered = arch.lower()
    return _ARCH_ALIASES.get(lowered, lowered)


def _host_os_arch() -> tuple[str, str]:
    if sys.platform.startswith("linux"):
        host_os = "linux"
    elif sys.platform == "darwin":
        host_os = "darwin"
    elif sys.platform in ("win32", "cygwin"):
        host_os = "windows"
    else:
        host_os = sys.platform
    return host_os, _normalize_arch(platform.machine())


def _parse_target_platform(target_platform: str) -> tuple[str, str] | None:
    """Parse a ``uv --python-platform`` value into ``(os, arch)``, or ``None``."""
    lowered = target_platform.strip().lower()
    if not lowered:
        return None
    if lowered in _PLATFORM_ALIASES:
        return _PLATFORM_ALIASES[lowered]
    parts = lowered.split("-")
    if len(parts) < 2:
        return None
    arch = _normalize_arch(parts[0])
    rest = parts[1:]
    if "android" in rest or "ios" in rest or "simulator" in rest:
        return None
    if "darwin" in rest:
        return ("darwin", arch)
    if "windows" in rest:
        return ("windows", arch)
    if "linux" in rest or any(part.startswith("manylinux") for part in rest):
        return ("linux", arch)
    return None


def _platform_matches_host(target_platform: str | None) -> bool:
    """Return whether ``target_platform`` (or ``None`` for native) matches the
    current host OS and CPU architecture."""
    if target_platform is None:
        return True
    parsed = _parse_target_platform(target_platform)
    return parsed is not None and parsed == _host_os_arch()


def _find_target_python(python: str, cwd: Path) -> Path | None:
    """Resolve a target-version Python interpreter via ``uv python find``."""
    try:
        result = subprocess.run(
            ["uv", "python", "find", python],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return Path(path) if path else None


def _parse_import_check_error(stderr: str) -> tuple[str | None, str] | None:
    """Extract ``(module_name, message)`` emitted by ``_IMPORT_CHECK_SCRIPT``."""
    for line in reversed(stderr.splitlines()):
        if line.startswith(_IMPORT_CHECK_PREFIX):
            try:
                payload = json.loads(line[len(_IMPORT_CHECK_PREFIX) :])
            except json.JSONDecodeError:
                return None
            if isinstance(payload, dict):
                name = payload.get("name")
                msg = payload.get("msg")
                return (
                    name if isinstance(name, str) and name else None,
                    msg if isinstance(msg, str) else "",
                )
    return None


def _check_one_function(name: str, func_dir: Path, interpreter: Path) -> None:
    """Verify ``func_dir/index.py`` imports in an isolated target subprocess."""
    result = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-S",
            "-B",
            "-c",
            _IMPORT_CHECK_SCRIPT,
            str(func_dir),
        ],
        cwd=func_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    parsed = _parse_import_check_error(result.stderr)
    if parsed is not None:
        missing, detail = parsed
        reason = (
            f"missing module {missing!r} ({detail})"
            if missing
            else detail or "ImportError"
        )
        raise RuntimeError(
            f"Build-time import check failed for function {name!r}: "
            f"{reason}. "
            "Add the missing distribution to `[tool.chano] include` in "
            "pyproject.toml or module-level `include` in the route."
        )
    detail = (result.stderr or result.stdout).strip()
    raise RuntimeError(
        f"Build-time import check failed for function {name!r}:\n{detail}"
    )


def _check_function_imports(
    target_platform: str | None,
    python: str,
    workspace_root: Path,
    func_dirs: Sequence[tuple[str, Path]],
) -> None:
    """Run the build-time import check for each built function when the target
    platform matches the host and a target interpreter is available."""
    if not func_dirs:
        return
    if not _platform_matches_host(target_platform):
        host_os, host_arch = _host_os_arch()
        print(
            f"Skipping build-time import check: target platform {target_platform!r} "
            f"does not match host ({host_os}/{host_arch})."
        )
        return
    interpreter = _find_target_python(python, workspace_root)
    if interpreter is None:
        print(
            f"Skipping build-time import check: no Python {python} interpreter "
            f"found via `uv python find {python}`."
        )
        return
    for name, func_dir in func_dirs:
        _check_one_function(name, func_dir, interpreter)
