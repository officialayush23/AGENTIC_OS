"""Application tools: list and launch explicitly allowlisted applications.

No shell, no arbitrary executables, no free-form arguments, no elevation.
Each allowlisted app has an argument policy:

* ``none``            - launched without arguments;
* ``workspace_file``  - optionally one existing, non-sensitive workspace file;
* ``workspace_path``  - optionally one existing workspace file or folder.
"""

from __future__ import annotations

import os
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .base import Assessment, Check, ReconcileResult, RiskLevel, ToolContext, ToolError, ToolSpec

ARGS_POLICIES = ("none", "workspace_file", "workspace_path")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ListAppsInput(_In):
    pass


class AppInfo(_Out):
    id: str
    name: str
    args_policy: str
    requires_approval: bool
    available: bool
    executable: str


class ListAppsOutput(_Out):
    apps: List[AppInfo]
    launch_enabled: bool


class LaunchAppInput(_In):
    app_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_\-]{0,39}$",
                        description="Id of an allowlisted application (see list_allowlisted_applications).")
    file: Optional[str] = Field(None, max_length=1024,
                                description="Optional workspace file/folder to open, if the app's argument policy allows it.")


class LaunchAppOutput(_Out):
    app_id: str
    app_name: str
    pid: int
    launched_at: str
    opened: Optional[str] = None
    still_running: Optional[bool] = None
    exit_code: Optional[int] = None


def _list_apps_handler(ctx: ToolContext, args: ListAppsInput) -> dict:
    apps = []
    for app in ctx.apps.list():
        if not app.get("enabled", True):
            continue
        try:
            ctx.os.validate_executable(app["path"])
            available = True
        except ToolError:
            available = False
        apps.append({
            "id": app["id"], "name": app["name"], "args_policy": app.get("args_policy", "none"),
            "requires_approval": bool(app.get("requires_approval")), "available": available,
            "executable": os.path.basename(app["path"]),
        })
    return {"apps": apps, "launch_enabled": bool(ctx.setting("allow_app_launch", True))}


def _resolve_launch(ctx: ToolContext, args: LaunchAppInput):
    app = ctx.apps.get(args.app_id)
    if app is None or not app.get("enabled", True):
        raise ToolError("APP_NOT_ALLOWLISTED", f"'{args.app_id}' is not an allowlisted application.")
    exe = ctx.os.validate_executable(app["path"])
    policy = app.get("args_policy", "none")
    if policy not in ARGS_POLICIES:
        raise ToolError("APP_POLICY_INVALID", "This application has an invalid argument policy.")
    argv: List[str] = []
    opened = None
    if args.file:
        if policy == "none":
            raise ToolError("APP_ARGS_NOT_ALLOWED", f"{app['name']} is allowlisted without arguments; it cannot open files.")
        target = ctx.workspace.resolve(args.file)
        if not target.exists:
            raise ToolError("FILE_NOT_FOUND", f"'{target.rel}' does not exist in the workspace.")
        if target.sensitive:
            raise ToolError("PATH_SENSITIVE", f"'{target.rel}' is a protected file and will not be opened.")
        if policy == "workspace_file" and not target.is_file:
            raise ToolError("APP_ARGS_NOT_ALLOWED", f"{app['name']} can only open files, not folders.")
        argv = [target.abs_path]
        opened = target.rel
    return app, exe, argv, opened


def _launch_assess(ctx: ToolContext, args: LaunchAppInput) -> Assessment:
    app, exe, argv, opened = _resolve_launch(ctx, args)
    reasons = []
    if app.get("requires_approval"):
        reasons.append(f"{app['name']} is configured to require approval")
    return Assessment(
        risk=RiskLevel.REVERSIBLE_WRITE,
        reasons=reasons,
        effect=f"Start {app['name']} ({os.path.basename(exe)})" + (f" with '{opened}'" if opened else "") + ".",
        target_state={"app_id": app["id"], "executable": exe, "opened": opened},
        requires_approval=bool(app.get("requires_approval")),
    )


def _launch_handler(ctx: ToolContext, args: LaunchAppInput) -> dict:
    if not ctx.setting("allow_app_launch", True):
        raise ToolError("APP_LAUNCH_DISABLED", "Launching applications is disabled in Settings.")
    app, exe, argv, opened = _resolve_launch(ctx, args)
    expected = ctx.expected_state or {}
    if expected.get("executable") and os.path.normcase(expected["executable"]) != os.path.normcase(exe):
        raise ToolError("APP_CHANGED_SINCE_APPROVAL", "The application's executable changed after approval.")
    ctx.check()
    result = ctx.os.launch(exe, argv, cwd=ctx.workspace.root)
    return {"app_id": app["id"], "app_name": app["name"], "opened": opened, **result}


def _launch_verify(ctx: ToolContext, args: LaunchAppInput, out: dict) -> List[Check]:
    pid_ok = int(out.get("pid") or 0) > 0
    running = out.get("still_running")
    code = out.get("exit_code")
    exe_name = os.path.basename(str((ctx.apps.get(args.app_id) or {}).get("path", ""))).lower()
    # Some Windows apps (calc.exe, explorer.exe, Store-app stubs) hand off to another process and exit.
    benign_exit = code in (0,) or (exe_name == "explorer.exe" and code == 1)
    return [
        Check("process_started", pid_ok, f"pid {out.get('pid')}"),
        Check("no_startup_failure", bool(running) or benign_exit,
              "still running" if running else f"exited with code {code} (some apps hand off to another process)"),
    ]


def _launch_reconcile(ctx: ToolContext, args: LaunchAppInput) -> ReconcileResult:
    return ReconcileResult("unknown", detail="cannot tell whether the earlier launch happened")


APP_TOOLS = [
    ToolSpec(
        name="list_allowlisted_applications", version="1.0",
        description="List the applications the user has allowlisted for launching, with their argument policy.",
        input_model=ListAppsInput, output_model=ListAppsOutput, capabilities=("apps.read",),
        risk=RiskLevel.READ_ONLY, timeout_s=10, mutates=False, idempotent=True, retryable=True, approval="never",
        verification="Returned apps are enabled allowlist entries.", handler=_list_apps_handler,
        verify=lambda ctx, a, out: [Check("returned", isinstance(out.get("apps"), list), f"{len(out.get('apps', []))} apps")],
        category="apps",
    ),
    ToolSpec(
        name="launch_allowlisted_application", version="1.0",
        description="Start an allowlisted application by id, optionally opening one workspace file/folder if its "
                    "argument policy allows. No other arguments, no shell, no elevation.",
        input_model=LaunchAppInput, output_model=LaunchAppOutput, capabilities=("apps.launch",),
        risk=RiskLevel.REVERSIBLE_WRITE, timeout_s=15, mutates=True, idempotent=False, retryable=False,
        approval="policy", verification="A process id was returned and the process did not fail at startup.",
        handler=_launch_handler, assess=_launch_assess, verify=_launch_verify, reconcile=_launch_reconcile,
        audit_fields=("app_id", "file"), category="apps",
        examples=({"app_id": "notepad", "file": "project_report.md"},),
    ),
]
