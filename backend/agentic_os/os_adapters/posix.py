"""Linux / macOS adapter (used for development and the automated tests)."""

from __future__ import annotations

from typing import Dict

from .base import OSAdapter

_DENIED = frozenset({
    "sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh", "busybox", "env", "xargs", "sudo", "su", "doas",
    "pkexec", "python", "python3", "perl", "ruby", "node", "osascript", "open", "xdg-open", "nohup", "setsid",
    "nc", "ncat", "socat", "ssh", "scp", "curl", "wget", "rm", "dd", "mkfs", "chmod", "chown", "systemctl",
    "launchctl", "crontab", "at", "git",
})


class PosixAdapter(OSAdapter):
    platform_name = "posix"
    denied_executables = _DENIED
    allowed_extensions = ("", ".app", ".appimage")

    def validate_executable(self, path: str) -> str:
        real = super().validate_executable(path)
        stem = real.rsplit("/", 1)[-1].lower()
        if stem.startswith("python") or stem.startswith("perl") or stem.startswith("ruby"):
            from ..tools.base import ToolError

            raise ToolError("APP_DENIED", "Interpreters cannot be allowlisted.")
        return real

    def _popen_kwargs(self) -> Dict:
        return {"start_new_session": True}
