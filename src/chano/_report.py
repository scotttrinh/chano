"""Bundle size reporting, size-limit enforcement, and ``--explain`` output."""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from chano._store import StoreIndex

# Vercel's maximum uncompressed Python function size is 500 MB.
SIZE_WARN_BYTES = 250 * 1024 * 1024
SIZE_LIMIT_BYTES = 500 * 1024 * 1024


@dataclass(frozen=True)
class FunctionReport:
    """Measured uncompressed size and bundled distributions for one function."""

    name: str
    size: int
    distributions: tuple[str, ...]


@dataclass(frozen=True)
class FunctionExplanation:
    """Explanation of why distributions were included or excluded for one function."""

    name: str
    included: Mapping[str, str]
    """Sorted included distribution name -> human-readable reason chain."""
    excluded: tuple[str, ...]
    """Sorted distribution names present in the store but not bundled."""


def _dir_size(directory: Path) -> int:
    """Return the total uncompressed byte size of files under ``directory``."""
    total = 0
    for root, _dirs, files in os.walk(directory, followlinks=False):
        root_path = Path(root)
        for name in files:
            total += (root_path / name).lstat().st_size
    return total


def _format_size(num_bytes: int) -> str:
    """Format a byte count as B, KB, or MB."""
    mb = 1024 * 1024
    if num_bytes >= mb:
        return f"{num_bytes / mb:.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def _format_largest_distributions(
    names: tuple[str, ...], store: StoreIndex, limit: int = 5
) -> str:
    """Format up to ``limit`` largest distributions from ``names``."""
    dists = [store.distributions[name] for name in names if name in store.distributions]
    dists.sort(key=lambda dist: (-dist.size, dist.name))
    return ", ".join(
        f"{dist.name} ({_format_size(dist.size)})" for dist in dists[:limit]
    )


def _print_distribution_section(
    heading: str,
    rows: Sequence[tuple[str, str | None]],
    store: StoreIndex,
) -> None:
    count = len(rows)
    unit = "distribution" if count == 1 else "distributions"
    print(f"  {heading} ({count} {unit}):")
    if not rows:
        print("    (none)")
        return
    name_width = max(len(name) for name, _ in rows)
    sizes = {name: _format_size(store.distributions[name].size) for name, _ in rows}
    size_width = max(len(size) for size in sizes.values())
    for name, reason in rows:
        suffix = f"  {reason}" if reason is not None else ""
        print(f"    {name:<{name_width}}  {sizes[name]:>{size_width}}{suffix}")


def _print_explanation(explanation: FunctionExplanation, store: StoreIndex) -> None:
    """Print why each distribution was included or excluded for one function."""
    print(f"\nExplain {explanation.name}:")
    _print_distribution_section("Included", list(explanation.included.items()), store)
    _print_distribution_section(
        "Excluded", [(name, None) for name in explanation.excluded], store
    )


def _report_functions(
    reports: list[FunctionReport],
    store: StoreIndex,
    *,
    explanation: FunctionExplanation | None = None,
) -> None:
    """Print per-function sizes, optional explanation, warn above
    ``SIZE_WARN_BYTES``, and fail above ``SIZE_LIMIT_BYTES``."""
    if reports:
        width = max(len(report.name) for report in reports)
        for report in reports:
            count = len(report.distributions)
            unit = "distribution" if count == 1 else "distributions"
            size = _format_size(report.size)
            print(f"  {report.name:<{width}}  {size} ({count} {unit})")

    if explanation is not None:
        _print_explanation(explanation, store)

    oversized: list[str] = []
    for report in reports:
        largest = _format_largest_distributions(report.distributions, store)
        suffix = f" Largest distributions: {largest}" if largest else ""
        if report.size > SIZE_LIMIT_BYTES:
            oversized.append(
                f"Function {report.name!r} bundle is {_format_size(report.size)}, "
                f"exceeding the {_format_size(SIZE_LIMIT_BYTES)} limit.{suffix}"
            )
        elif report.size > SIZE_WARN_BYTES:
            warnings.warn(
                f"Function {report.name!r} bundle is {_format_size(report.size)}, "
                f"exceeding the {_format_size(SIZE_WARN_BYTES)} warning threshold."
                f"{suffix}",
                stacklevel=3,
            )
    if oversized:
        raise RuntimeError("\n".join(oversized))
