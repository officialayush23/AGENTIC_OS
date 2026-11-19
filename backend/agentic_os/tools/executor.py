"""Tool executor: idempotency, timing, normalized results and evidence.

The executor never decides *whether* an action is allowed - that is the
policy engine's job, and the orchestration graph only calls `execute()` after
policy (and, when required, approval) has passed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ValidationError

from ..persistence.repositories import ExecutionRepo
from ..security.redaction import redactor
from ..util import bounded_json, sha256_hex, utc_now_iso
from .base import ToolContext, ToolError, ToolSpec

log = logging.getLogger(__name__)


def idempotency_key(task_id: str, spec: ToolSpec, args: Dict[str, Any]) -> str:
    return "idem_" + sha256_hex({"task": task_id, "tool": spec.name, "version": spec.version, "args": args})[:40]


def _evidence(spec: ToolSpec, output: Optional[dict]) -> List[dict]:
    if not output:
        return []
    ev: List[dict] = []
    if output.get("path") and output.get("sha256"):
        ev.append({"type": "file", "path": output["path"], "sha256": output["sha256"], "size": output.get("size")})
    elif output.get("path") and output.get("after_sha256"):
        ev.append({"type": "file", "path": output["path"], "sha256": output["after_sha256"], "size": output.get("size_after"),
                   "before_sha256": output.get("before_sha256")})
    if isinstance(output.get("entries"), list):
        ev.append({"type": "listing", "path": output.get("path", "."), "files": output.get("file_count"),
                   "folders": output.get("dir_count")})
    if isinstance(output.get("matches"), list):
        ev.append({"type": "search", "matches": len(output["matches"])})
    if isinstance(output.get("config_files"), list):
        ev.append({"type": "classification", "config_files": len(output["config_files"]),
                   "ecosystems": output.get("ecosystems", [])[:10]})
    if output.get("markdown") and output.get("sha256"):
        ev.append({"type": "document", "sha256": output["sha256"], "sections": output.get("sections", [])})
    if output.get("pid"):
        ev.append({"type": "process", "pid": output["pid"], "name": output.get("app_name")})
    if output.get("backup"):
        ev.append({"type": "backup", "path": output["backup"]})
    return ev


def _state_change(spec: ToolSpec, output: Optional[dict]) -> dict:
    if not spec.mutates or not output:
        return {"mutated": False, "resources": []}
    resources = []
    action = output.get("action") or output.get("operation")
    if output.get("path"):
        changed = action in ("created", "overwritten") or output.get("changed") is True
        resources.append({
            "type": "file", "path": output["path"], "action": action,
            "before_sha256": output.get("previous_sha256") or output.get("before_sha256"),
            "after_sha256": output.get("sha256") or output.get("after_sha256"),
            "backup": output.get("backup"), "changed": changed,
        })
        return {"mutated": changed, "resources": resources}
    if output.get("pid"):
        resources.append({"type": "process", "pid": output["pid"], "action": "started", "name": output.get("app_name")})
        return {"mutated": True, "resources": resources}
    return {"mutated": True, "resources": resources}


class ToolExecutor:
    def __init__(self, executions: ExecutionRepo):
        self.executions = executions

    def _result(self, spec: ToolSpec, key: str, attempt: int, started_at: str, started: float, *, success: bool,
                status: str, output: Optional[dict] = None, error_code: Optional[str] = None,
                error_message: Optional[str] = None, error_details: Optional[dict] = None,
                retryable: bool = False, note: Optional[str] = None) -> dict:
        safe_output = redactor.redact(output) if output is not None else None
        return {
            "success": success,
            "status": status,
            "tool": spec.name,
            "tool_version": spec.version,
            "idempotency_key": key,
            "attempt": attempt,
            "output": safe_output,
            "error_code": error_code,
            "error_message": redactor.redact_text(error_message) if error_message else None,
            "error_details": redactor.redact(error_details) if error_details else None,
            "retryable": bool(retryable),
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "evidence": _evidence(spec, safe_output),
            "state_change": _state_change(spec, safe_output) if success else {"mutated": False, "resources": []},
            "note": note,
        }

    def execute(self, spec: ToolSpec, args_model: BaseModel, ctx: ToolContext, *, attempt: int = 1) -> dict:
        args = args_model.model_dump(mode="json")
        key = idempotency_key(ctx.task_id, spec, args)
        started_at, started = utc_now_iso(), time.monotonic()
        record = self.executions.get(key) if spec.mutates else None

        if record is not None and record["status"] == "succeeded" and record.get("result"):
            prior = record["result"]
            return self._result(
                spec, key, attempt, started_at, started, success=True, status="reused",
                output=prior.get("output"),
                note="This exact action already completed earlier in this task, so it was not repeated.",
            )
        if record is not None and record["status"] != "succeeded" and spec.reconcile is not None:
            # A previous attempt was interrupted ("started") or failed: find out whether its effect exists.
            try:
                rec = spec.reconcile(ctx, args_model)
            except ToolError as exc:
                rec = None
                log.warning("reconcile failed for %s: %s", spec.name, exc.message)
            if rec is not None and rec.state == "done":
                out = rec.output or {}
                result = self._result(spec, key, attempt, started_at, started, success=True, status="reconciled",
                                      output=out, note=f"An earlier attempt had already produced this result ({rec.detail}).")
                self.executions.finish(key, "succeeded", bounded_json(result))
                return result
            if record["status"] == "started" and (rec is None or rec.state == "unknown") and not spec.idempotent:
                return self._result(
                    spec, key, attempt, started_at, started, success=False, status="failed",
                    error_code="UNCERTAIN_PRIOR_EXECUTION",
                    error_message="An earlier attempt of this action was interrupted and its outcome cannot be "
                                  "confirmed, so it was not repeated automatically. "
                                  + (rec.detail if rec else ""),
                    retryable=False,
                )

        if spec.mutates:
            self.executions.start(key, ctx.task_id, ctx.step_id, spec.name, spec.version, sha256_hex(args))
        step_deadline = time.monotonic() + float(spec.timeout_s)
        ctx.deadline = min(ctx.deadline, step_deadline) if ctx.deadline else step_deadline
        try:
            ctx.check()
            raw = spec.handler(ctx, args_model)
            if not isinstance(raw, dict):
                raise ToolError("TOOL_OUTPUT_INVALID", "the tool returned an unexpected result type")
            output = spec.output_model.model_validate(raw).model_dump(mode="json")
            result = self._result(spec, key, attempt, started_at, started, success=True, status="succeeded", output=output)
        except ToolError as exc:
            result = self._result(
                spec, key, attempt, started_at, started, success=False, status="failed", error_code=exc.code,
                error_message=exc.message, error_details=exc.details, retryable=exc.retryable,
            )
        except ValidationError:
            log.exception("tool %s produced output that does not match its schema", spec.name)
            result = self._result(spec, key, attempt, started_at, started, success=False, status="failed",
                                  error_code="TOOL_OUTPUT_INVALID",
                                  error_message="The tool produced output that does not match its declared schema.")
        except Exception as exc:  # noqa: BLE001 - normalized, never leaked raw
            log.exception("tool %s crashed", spec.name)
            result = self._result(
                spec, key, attempt, started_at, started, success=False, status="failed", error_code="TOOL_INTERNAL_ERROR",
                error_message=f"Unexpected {type(exc).__name__} inside the tool.", retryable=not spec.mutates,
            )
        if spec.mutates:
            self.executions.finish(key, "succeeded" if result["success"] else "failed", bounded_json(result))
        return result
