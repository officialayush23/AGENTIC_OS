"""PyInstaller entry point for the desktop sidecar (see build.bat)."""

from agentic_os.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
