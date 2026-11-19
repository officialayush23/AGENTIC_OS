"""Find the programs installed on this computer, so the user can allowlist one with a click.

Windows: Start Menu shortcuts (all users + current user), resolved to their .exe target in one
PowerShell call, plus the "App Paths" registry keys. Linux: .desktop files. macOS: /Applications.
Only real executables are returned; nothing is launched here.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Dict, List

_CACHE: Dict[str, object] = {"at": 0.0, "apps": []}
SKIP = re.compile(r"(uninstall|setup|install|readme|help|license|release notes|documentation|website|repair|"
                  r"update|feedback|support|manual|changelog|\.url$)", re.I)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s[:40] or "app")


def _windows() -> List[dict]:
    roots = [os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")]
    links = []
    for r in roots:
        if r and os.path.isdir(r):
            links += glob.glob(os.path.join(r, "**", "*.lnk"), recursive=True)
    links = [l for l in links if not SKIP.search(os.path.basename(l))][:600]
    out: List[dict] = []
    if links:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write("\n".join(links))
            listfile = fh.name
        script = (
            "$s = New-Object -ComObject WScript.Shell; $r = @(); "
            f"foreach ($p in Get-Content -Encoding UTF8 -LiteralPath '{listfile}') {{ try {{ $l = $s.CreateShortcut($p); "
            "$r += [pscustomobject]@{ lnk = $p; target = $l.TargetPath } } catch {} }; "
            "$r | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                  capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = json.loads(proc.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
        except Exception:  # noqa: BLE001
            data = []
        finally:
            try:
                os.unlink(listfile)
            except OSError:
                pass
        for d in data:
            target = str(d.get("target") or "")
            if target.lower().endswith(".exe") and os.path.isfile(target):
                name = os.path.splitext(os.path.basename(d["lnk"]))[0]
                out.append({"name": name, "path": target, "source": "Start menu"})
    try:
        import winreg  # type: ignore
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths")
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, sub) as k:
                        val, _ = winreg.QueryValueEx(k, None)
                    val = os.path.expandvars(str(val).strip('"'))
                    if val.lower().endswith(".exe") and os.path.isfile(val):
                        out.append({"name": os.path.splitext(sub)[0], "path": val, "source": "Registry"})
                except OSError:
                    continue
    except ImportError:
        pass
    for exe, name in ((r"C:\Windows\System32\notepad.exe", "Notepad"), (r"C:\Windows\System32\mspaint.exe", "Paint"),
                      (r"C:\Windows\System32\calc.exe", "Calculator"), (r"C:\Windows\explorer.exe", "File Explorer")):
        if os.path.isfile(exe):
            out.append({"name": name, "path": exe, "source": "Windows"})
    return out


def _linux() -> List[dict]:
    out = []
    dirs = ["/usr/share/applications", "/usr/local/share/applications", os.path.expanduser("~/.local/share/applications")]
    for d in dirs:
        for f in glob.glob(os.path.join(d, "*.desktop")):
            try:
                text = open(f, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if re.search(r"^NoDisplay=true", text, re.M):
                continue
            name = re.search(r"^Name=(.+)$", text, re.M)
            exe = re.search(r"^Exec=(.+)$", text, re.M)
            if not (name and exe):
                continue
            try:
                cmd = shlex.split(re.sub(r"%[a-zA-Z]", "", exe.group(1)))[0]
            except ValueError:
                continue
            path = cmd if os.path.isabs(cmd) else next((os.path.join(p, cmd) for p in os.environ.get("PATH", "").split(":")
                                                         if os.path.isfile(os.path.join(p, cmd))), None)
            if path and os.access(path, os.X_OK):
                out.append({"name": name.group(1).strip(), "path": path, "source": "Applications"})
    return out


def _mac() -> List[dict]:
    out = []
    for app in glob.glob("/Applications/*.app") + glob.glob(os.path.expanduser("~/Applications/*.app")):
        name = os.path.splitext(os.path.basename(app))[0]
        macos = os.path.join(app, "Contents", "MacOS")
        exes = [os.path.join(macos, f) for f in os.listdir(macos)] if os.path.isdir(macos) else []
        exe = next((e for e in exes if os.path.basename(e) == name), exes[0] if exes else None)
        if exe:
            out.append({"name": name, "path": exe, "source": "Applications"})
    return out


def discover(force: bool = False) -> List[dict]:
    if not force and _CACHE["apps"] and time.time() - float(_CACHE["at"]) < 300:
        return list(_CACHE["apps"])  # type: ignore[arg-type]
    raw = _windows() if sys.platform == "win32" else _mac() if sys.platform == "darwin" else _linux()
    seen, apps = set(), []
    for a in sorted(raw, key=lambda a: a["name"].lower()):
        key = os.path.normcase(a["path"])
        if key in seen:
            continue
        seen.add(key)
        apps.append({**a, "id": _slug(a["name"])})
    _CACHE.update(at=time.time(), apps=apps)
    return apps
