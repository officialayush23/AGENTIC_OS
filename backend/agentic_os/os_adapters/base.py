"""Base OS adapter and the catalogue of future (not implemented) capabilities."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..tools.base import ToolError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class OSAdapter(ABC):
    """Safe, minimal OS capabilities used by v0.1 tools."""

    platform_name = "generic"
    #: Executable names that may never be allowlisted (shells, script hosts, system tools).
    denied_executables: frozenset = frozenset()
    #: Allowed executable extensions ('' means "no extension required").
    allowed_extensions: tuple = ("",)

    # ------------------------------------------------------------ system info
    def system_info(self, workspace_root: Optional[str] = None) -> Dict:
        info: Dict = {
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version()[:120],
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "collected_at": _now(),
        }
        try:
            import psutil  # type: ignore

            vm = psutil.virtual_memory()
            info["memory_total_mb"] = int(vm.total / 1048576)
            info["memory_available_mb"] = int(vm.available / 1048576)
            info["memory_percent"] = float(vm.percent)
            info["boot_time"] = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except Exception:  # noqa: BLE001 - psutil optional
            info["note"] = "Memory details unavailable (psutil not installed)."
        if workspace_root and os.path.isdir(workspace_root):
            usage = shutil.disk_usage(workspace_root)
            info["workspace_disk_total_gb"] = round(usage.total / 1073741824, 1)
            info["workspace_disk_free_gb"] = round(usage.free / 1073741824, 1)
        return info

    def list_processes(self, limit: int = 15, sort_by: str = "memory") -> Dict:
        try:
            import psutil  # type: ignore
        except ImportError:
            raise ToolError("UNSUPPORTED", "Process inspection needs the 'psutil' package.")
        rows: List[Dict] = []
        total = 0
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            total += 1
            try:
                mem = proc.info.get("memory_info")
                rows.append({
                    "pid": int(proc.info["pid"]),
                    "name": str(proc.info.get("name") or "?")[:80],
                    "memory_mb": round(mem.rss / 1048576, 1) if mem else None,
                })
            except Exception:  # noqa: BLE001 - process vanished / access denied
                continue
        if sort_by == "name":
            rows.sort(key=lambda r: r["name"].lower())
        else:
            rows.sort(key=lambda r: r["memory_mb"] or 0, reverse=True)
        return {"processes": rows[:limit], "total_processes": total, "collected_at": _now()}

    # --------------------------------------------------------------- launching
    def validate_executable(self, path: str) -> str:
        if not path or not isinstance(path, str):
            raise ToolError("APP_PATH_INVALID", "No executable path configured.")
        if not os.path.isabs(path):
            raise ToolError("APP_PATH_INVALID", "Executable paths must be absolute.")
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            raise ToolError("APP_NOT_FOUND", f"Executable not found: {os.path.basename(path)}")
        base = os.path.basename(real).lower()
        stem, ext = os.path.splitext(base)
        if base in self.denied_executables or stem in self.denied_executables:
            raise ToolError("APP_DENIED", f"'{base}' is a shell or system tool and can never be launched by the agent.")
        if ext not in self.allowed_extensions:
            raise ToolError("APP_DENIED", f"Only {', '.join(e or 'extension-less' for e in self.allowed_extensions)} executables can be allowlisted.")
        if not os.access(real, os.X_OK) and os.name != "nt":
            raise ToolError("APP_DENIED", "The file is not executable.")
        return real

    def _popen_kwargs(self) -> Dict:
        return {}

    def launch(self, exe: str, args: List[str], cwd: Optional[str] = None) -> Dict:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("AGENTIC_OS_")}
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False, allowlisted executable
                [exe, *args], cwd=cwd, shell=False, close_fds=True, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **self._popen_kwargs(),
            )
        except PermissionError:
            raise ToolError("APP_LAUNCH_DENIED", "The operating system refused to start the application.")
        except OSError as exc:
            raise ToolError("APP_LAUNCH_FAILED", f"The application could not be started ({type(exc).__name__}).", retryable=True)
        launched_at = _now()
        time.sleep(0.8)
        code = proc.poll()
        return {"pid": proc.pid, "launched_at": launched_at, "still_running": code is None, "exit_code": code}

    def default_apps(self) -> List[Dict]:
        return []

    # ------------------------------------------------------- future capabilities
    # These are extension points only. They raise so nothing can pretend they work.
    def ui_automation(self):  # pragma: no cover - not implemented in v0.1
        raise NotImplementedError("UI Automation is planned, not implemented in v0.1")

    def clipboard(self):  # pragma: no cover
        raise NotImplementedError("Clipboard access is planned, not implemented in v0.1")

    def notifications(self):  # pragma: no cover
        raise NotImplementedError("Notifications are planned, not implemented in v0.1")

    def screenshots(self):  # pragma: no cover
        raise NotImplementedError("Screenshots are planned, not implemented in v0.1")


FUTURE_CAPABILITIES = [
    {"id": "browser_automation", "name": "Browser automation (Playwright)", "status": "planned",
     "detail": "Navigate, inspect and fill forms in an isolated browser context with per-action approval."},
    {"id": "web_search", "name": "Web search and cited research", "status": "planned",
     "detail": "Search-API provider interface, bounded crawling, robots.txt, untrusted-content handling."},
    {"id": "gmail", "name": "Gmail", "status": "planned",
     "detail": "OAuth (minimum scopes): search, read, draft; send only after explicit approval."},
    {"id": "calendar", "name": "Google Calendar / Meet links", "status": "planned",
     "detail": "List events, check availability, create events after confirmation."},
    {"id": "notion", "name": "Notion", "status": "planned",
     "detail": "Search, create and append pages with preview and verification."},
    {"id": "ui_automation", "name": "Windows UI Automation", "status": "planned",
     "detail": "Accessibility-tree based interaction with desktop apps."},
    {"id": "clipboard", "name": "Clipboard", "status": "planned", "detail": "Read/write with explicit approval."},
    {"id": "screenshots", "name": "Screenshots", "status": "planned", "detail": "Task-scoped captures."},
    {"id": "notifications", "name": "Desktop notifications", "status": "planned", "detail": "Approval and completion alerts."},
    {"id": "scheduling", "name": "Scheduled tasks", "status": "planned", "detail": "Run approved tasks on a schedule."},
    {"id": "local_models", "name": "Local models (Ollama / llama.cpp)", "status": "planned",
     "detail": "Provider interface is ready; local inference is out of scope for v0.1."},
]
