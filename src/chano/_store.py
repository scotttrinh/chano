"""Dependency store indexing from installed ``*.dist-info`` metadata."""

from __future__ import annotations

import csv
import os
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from email.parser import HeaderParser
from pathlib import Path

_DIST_NAME_SEP = re.compile(r"[-_.]+")
_REQUIRES_DIST_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def normalize_dist_name(name: str) -> str:
    """Normalize a distribution name per PEP 503."""
    return _DIST_NAME_SEP.sub("-", name).lower()


@dataclass(frozen=True)
class Distribution:
    """One installed distribution indexed from a ``*.dist-info`` directory."""

    name: str
    """PEP 503 normalized distribution name."""
    dist_info: str
    """Relative path of the ``*.dist-info`` directory inside the store."""
    files: tuple[str, ...]
    """Relative POSIX paths inside the store listed in ``RECORD``."""
    top_level: frozenset[str]
    """Top-level import names provided by this distribution."""
    requires: frozenset[str]
    """PEP 503 normalized distribution names from ``Requires-Dist`` headers."""
    size: int
    """Total uncompressed size in bytes of this distribution's files."""


@dataclass(frozen=True)
class StoreIndex:
    """Index of every distribution installed in the shared dependency store."""

    distributions: Mapping[str, Distribution]
    """Normalized distribution name -> ``Distribution``."""
    by_import: Mapping[str, tuple[str, ...]]
    """Top-level import name -> sorted normalized distribution names."""


def _parse_requires_dist(value: str) -> str | None:
    """Extract and normalize the distribution name from a ``Requires-Dist`` header."""
    match = _REQUIRES_DIST_NAME.match(value)
    return normalize_dist_name(match.group(1)) if match else None


def _read_record(record_path: Path) -> tuple[str, ...]:
    """Read in-store relative POSIX file paths listed in a ``RECORD`` CSV."""
    if not record_path.is_file():
        return ()
    files: set[str] = set()
    with record_path.open(encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.reader(handle):
            if not row or not row[0].strip():
                continue
            raw = row[0].replace("\\", "/")
            if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
                continue
            rel = posixpath.normpath(raw)
            if rel in (".", "..") or rel.startswith("../"):
                continue
            files.add(rel)
    return tuple(sorted(files))


def _top_level_imports(files: tuple[str, ...], dist_info: Path) -> frozenset[str]:
    """Derive top-level import names from ``RECORD`` paths and ``top_level.txt``."""
    names: set[str] = set()
    for rel in files:
        parts = rel.split("/")
        if len(parts) == 1:
            filename = parts[0]
            if filename.endswith(".py"):
                stem = filename[:-3]
                if stem.isidentifier() and stem != "__init__":
                    names.add(stem)
            elif filename.endswith((".so", ".pyd")):
                stem = filename.split(".", 1)[0]
                if stem.isidentifier():
                    names.add(stem)
        else:
            top = parts[0]
            if (
                top == "__pycache__"
                or top.endswith((".dist-info", ".data"))
                or not top.isidentifier()
                or "__pycache__" in parts
            ):
                continue
            if parts[-1].endswith((".py", ".so", ".pyd")):
                names.add(top)

    top_level_txt = dist_info / "top_level.txt"
    if top_level_txt.is_file():
        for line in top_level_txt.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            entry = line.strip()
            if not entry:
                continue
            top = entry.replace("\\", "/").split("/", 1)[0].strip()
            if top.isidentifier():
                names.add(top)

    return frozenset(names)


def _distribution_size(store_dir: Path, dist_info: Path, files: tuple[str, ...]) -> int:
    """Total uncompressed size in bytes of a distribution's files and ``.dist-info``."""
    seen: set[str] = set()
    total = 0
    for rel in files:
        if rel in seen:
            continue
        seen.add(rel)
        target = store_dir / rel
        if target.is_symlink() or target.is_file():
            total += target.lstat().st_size
    for root, _dirs, filenames in os.walk(dist_info, followlinks=False):
        root_path = Path(root)
        for filename in filenames:
            path = root_path / filename
            rel = path.relative_to(store_dir).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            total += path.lstat().st_size
    return total


def index_store(store_dir: Path) -> StoreIndex:
    """Index every ``*.dist-info`` directory inside ``store_dir``."""
    if not store_dir.is_dir():
        return StoreIndex(distributions={}, by_import={})

    distributions: dict[str, Distribution] = {}
    import_map: dict[str, set[str]] = {}
    parser = HeaderParser()

    for entry in sorted(store_dir.glob("*.dist-info"), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        metadata_path = entry / "METADATA"
        raw_name = entry.name.removesuffix(".dist-info").rsplit("-", 1)[0]
        requires: set[str] = set()
        if metadata_path.is_file():
            headers = parser.parsestr(
                metadata_path.read_text(encoding="utf-8", errors="replace")
            )
            if headers["Name"]:
                raw_name = headers["Name"].strip()
            for req in headers.get_all("Requires-Dist") or ():
                parsed = _parse_requires_dist(req)
                if parsed is not None:
                    requires.add(parsed)

        name = normalize_dist_name(raw_name)
        files = _read_record(entry / "RECORD")
        top_level = _top_level_imports(files, entry)
        size = _distribution_size(store_dir, entry, files)
        distributions[name] = Distribution(
            name=name,
            dist_info=entry.name,
            files=files,
            top_level=top_level,
            requires=frozenset(requires),
            size=size,
        )
        for module in top_level:
            import_map.setdefault(module, set()).add(name)

    sorted_dists = dict(sorted(distributions.items()))
    by_import = {
        module: tuple(sorted(names)) for module, names in sorted(import_map.items())
    }
    return StoreIndex(distributions=sorted_dists, by_import=by_import)
