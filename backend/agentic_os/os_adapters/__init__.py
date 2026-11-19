"""OS-neutral adapter interface. Platform-specific imports stay inside the adapters."""

from __future__ import annotations

import sys


def get_os_adapter():
    if sys.platform == "win32":
        from .windows import WindowsAdapter

        return WindowsAdapter()
    from .posix import PosixAdapter

    return PosixAdapter()
