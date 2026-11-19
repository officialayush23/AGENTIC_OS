"""System inspection tools: minimal, read-only, privacy-preserving."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .base import Check, RiskLevel, ToolContext, ToolSpec


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SystemInfoInput(_In):
    pass


class SystemInfoOutput(_Out):
    os: str
    os_release: str
    os_version: str
    machine: str
    python_version: str
    cpu_count: Optional[int] = None
    memory_total_mb: Optional[int] = None
    memory_available_mb: Optional[int] = None
    memory_percent: Optional[float] = None
    boot_time: Optional[str] = None
    workspace_disk_total_gb: Optional[float] = None
    workspace_disk_free_gb: Optional[float] = None
    collected_at: str
    note: Optional[str] = None


class ProcessSummaryInput(_In):
    limit: int = Field(15, ge=1, le=50, description="How many processes to return.")
    sort_by: str = Field("memory", pattern="^(memory|name)$")


class ProcessInfo(_Out):
    pid: int
    name: str
    memory_mb: Optional[float] = None


class ProcessSummaryOutput(_Out):
    processes: List[ProcessInfo]
    total_processes: int
    collected_at: str


def _sysinfo_handler(ctx: ToolContext, args: SystemInfoInput) -> dict:
    return ctx.os.system_info(ctx.workspace.root if ctx.workspace else None)


def _sysinfo_verify(ctx: ToolContext, args: SystemInfoInput, out: dict) -> List[Check]:
    return [Check("has_os", bool(out.get("os")), f"{out.get('os')} {out.get('os_release')}"),
            Check("has_cpu_count", (out.get("cpu_count") or 0) > 0, f"{out.get('cpu_count')} logical CPUs")]


def _proc_handler(ctx: ToolContext, args: ProcessSummaryInput) -> dict:
    return ctx.os.list_processes(limit=args.limit, sort_by=args.sort_by)


def _proc_verify(ctx: ToolContext, args: ProcessSummaryInput, out: dict) -> List[Check]:
    procs = out.get("processes", [])
    return [Check("within_limit", len(procs) <= args.limit, f"{len(procs)} processes returned"),
            Check("fields_minimal", all(set(p.keys()) <= {"pid", "name", "memory_mb"} for p in procs),
                  "only pid, name and memory are reported")]


SYSTEM_TOOLS = [
    ToolSpec(
        name="get_system_info", version="1.0",
        description="Basic system information: OS, CPU count, memory, free disk space of the workspace drive. "
                    "No user names, host names or network details.",
        input_model=SystemInfoInput, output_model=SystemInfoOutput, capabilities=("system.read",),
        risk=RiskLevel.READ_ONLY, timeout_s=10, mutates=False, idempotent=True, retryable=True, approval="never",
        verification="Required fields present.", handler=_sysinfo_handler, verify=_sysinfo_verify, category="system",
    ),
    ToolSpec(
        name="list_processes_summary", version="1.0",
        description="Top running processes by memory or name: only pid, name and memory (no command lines, users "
                    "or paths).",
        input_model=ProcessSummaryInput, output_model=ProcessSummaryOutput, capabilities=("system.read",),
        risk=RiskLevel.READ_ONLY, timeout_s=15, mutates=False, idempotent=True, retryable=True, approval="never",
        verification="Within limit; only minimal fields reported.", handler=_proc_handler, verify=_proc_verify,
        category="system",
    ),
]
