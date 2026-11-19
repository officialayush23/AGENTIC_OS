"""Windows adapter."""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List

from .base import OSAdapter

_DENIED = frozenset({
    "cmd", "cmd.exe", "powershell", "powershell.exe", "powershell_ise.exe", "pwsh", "pwsh.exe",
    "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe", "reg.exe", "regedit.exe",
    "regedt32.exe", "schtasks.exe", "at.exe", "sc.exe", "net.exe", "net1.exe", "netsh.exe", "bitsadmin.exe",
    "certutil.exe", "wmic.exe", "msiexec.exe", "bash.exe", "wsl.exe", "wslhost.exe", "python.exe",
    "pythonw.exe", "py.exe", "pyw.exe", "node.exe", "conhost.exe", "taskkill.exe", "runas.exe",
    "msbuild.exe", "installutil.exe", "cmstp.exe", "forfiles.exe", "bcdedit.exe", "diskpart.exe",
    "format.com", "takeown.exe", "icacls.exe", "cacls.exe", "vssadmin.exe", "wevtutil.exe",
    "msdt.exe", "control.exe", "mmc.exe", "explorer++.exe", "wt.exe", "windowsterminal.exe",
    "ssh.exe", "scp.exe", "curl.exe", "ftp.exe", "tftp.exe", "git.exe", "git-bash.exe", "sh.exe",
})


class WindowsAdapter(OSAdapter):
    platform_name = "windows"
    denied_executables = _DENIED
    allowed_extensions = (".exe",)

    def _popen_kwargs(self) -> Dict:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        return {"creationflags": flags}

    def default_apps(self) -> List[Dict]:
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        candidates = [
            ("notepad", "Notepad", os.path.join(system_root, "System32", "notepad.exe"), "workspace_file"),
            ("calculator", "Calculator", os.path.join(system_root, "System32", "calc.exe"), "none"),
            ("paint", "Paint", os.path.join(system_root, "System32", "mspaint.exe"), "none"),
            ("explorer", "File Explorer", os.path.join(system_root, "explorer.exe"), "workspace_path"),
        ]
        return [
            {"id": app_id, "name": name, "path": path, "args_policy": policy, "requires_approval": False, "builtin": True}
            for app_id, name, path, policy in candidates
            if os.path.isfile(path)
        ]
