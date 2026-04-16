"""Open the canonical Rick Astley video in a browser."""

from __future__ import annotations

import sys
import webbrowser
from types import ModuleType

VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def play() -> bool:
    """Open the video in the user's default browser."""
    return webbrowser.open(VIDEO_URL, new=2, autoraise=True)


def chano() -> bool:
    """Alias for :func:`play`."""
    return play()


def main() -> int:
    """CLI entry point."""
    return 0 if chano() else 1


class _CallableModule(ModuleType):
    def __call__(self) -> bool:
        return chano()


sys.modules[__name__].__class__ = _CallableModule

__all__ = ["VIDEO_URL", "chano", "main", "play"]
