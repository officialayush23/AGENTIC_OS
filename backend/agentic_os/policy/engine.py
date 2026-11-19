"""Policy engine.

The policy engine is the single authority on whether an action may run. It is
pure Python with no LLM involvement. Model-provided hints (a plan step's
`risk` or `requires_approval`) are only ever used to make a decision *stricter*.

Decisions:
* ``allow``               - execute now;
* ``require_approval``    - pause for the user, bound to `action_hash`;
* ``deny``                - never execute this action;
* ``precondition_failed`` - action is allowed in principle but cannot succeed
                            as specified (e.g. file already exists). No
                            approval is requested for doomed actions.

`action_hash` binds task, plan version, step, tool, tool version, the exact
resolved arguments and the observed target state. If any of those change,
the hash changes and a previous approval no longer applies.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..tools.base import RISK_ORDER, RiskLevel, ToolContext, ToolError, max_risk, parse_risk
from ..tools.registry import ArgsValidationError, ToolRegistry
from ..util import bounded_json, sha256_hex

APPROVAL_THRESHOLD = RiskLevel.EXTERNAL_COMMUNICATION

CAPABILITY_SETTINGS = {
    "workspace.write": ("allow_workspace_writes", "Workspace writes are disabled in Settings (read-only mode)."),
    "apps.launch": ("allow_app_launch", "Launching applications is disabled in Settings."),
}


@dataclass
class PolicyDecision:
    decision: str
    risk: str
    base_risk: str
    reasons: List[str] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)
    effect: str = ""
    preview: Optional[dict] = None
    target_state: Optional[dict] = None
    action_hash: str = ""
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    tool: str = ""
    tool_version: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["preview"] = bounded_json(self.preview, max_chars=60000, max_str=50000) if self.preview else None
        return d


def compute_action_hash(task_id: str, plan_version: int, step_id: str, tool: str, tool_version: str,
                        args: Dict[str, Any], target_state: Optional[dict]) -> str:
    return sha256_hex({
        "task_id": task_id, "plan_version": int(plan_version), "step_id": step_id, "tool": tool,
        "tool_version": tool_version, "args": args, "target_state": target_state or {},
    })


class PolicyEngine:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def evaluate(
        self,
        *,
        task_id: str,
        plan_version: int,
        step: Dict[str, Any],
        args: Dict[str, Any],
        ctx: ToolContext,
        user_settings: Dict[str, Any],
        platform: str = sys.platform,
        deferred_capabilities: Optional[set] = None,
    ) -> PolicyDecision:
        """`deferred_capabilities`: tool capabilities whose effects are staged in a kernel
        transaction (e.g. {"workspace.write"}). Risk-based approval for such steps is
        deferred to the commit review (rule P016). It never relaxes a deny, the tool's
        own approval="always", the user's require-approval setting, or a plan request."""
        tool_name = str(step.get("tool") or "")
        step_id = str(step.get("id") or "")
        spec = self.registry.get(tool_name)

        def deny(code: str, message: str, rule: str, risk: RiskLevel = RiskLevel.PRIVILEGED_OR_UNSUPPORTED,
                 base: Optional[RiskLevel] = None) -> PolicyDecision:
            return PolicyDecision(
                decision="deny", risk=risk.value, base_risk=(base or risk).value, reasons=[message], rules=[rule],
                error_code=code, error_message=message, tool=tool_name, tool_version=spec.version if spec else "",
                action_hash=compute_action_hash(task_id, plan_version, step_id, tool_name, spec.version if spec else "", args, None),
            )

        # P001 - only registered tools.
        if spec is None:
            hint = self.registry.unsupported_hint(tool_name) or f"'{tool_name}' is not a registered tool."
            return deny("TOOL_UNKNOWN", hint, "P001_unknown_tool")
        base = spec.risk
        # P002 - platform support.
        if not self.registry.is_available(spec, platform):
            return deny("TOOL_UNAVAILABLE", f"{tool_name} is not available on this operating system.", "P002_platform", base, base)
        # P003 - privileged / unsupported risk class is never executed.
        if base == RiskLevel.PRIVILEGED_OR_UNSUPPORTED:
            return deny("ACTION_PRIVILEGED", "Privileged or unsupported actions are not allowed.", "P003_privileged", base, base)
        # P004 - capabilities the user has switched off.
        for cap in spec.capabilities:
            setting = CAPABILITY_SETTINGS.get(cap)
            if setting and not user_settings.get(setting[0], True):
                return deny("CAPABILITY_DISABLED", setting[1], "P004_capability_disabled", base, base)
        # P005 - arguments must validate against the tool's schema.
        try:
            args_model = self.registry.validate_args(tool_name, args)
        except ArgsValidationError as exc:
            msg = "; ".join(f"{e['field']}: {e['message']}" for e in exc.errors[:5])
            return PolicyDecision(
                decision="precondition_failed", risk=base.value, base_risk=base.value,
                reasons=[f"Invalid arguments: {msg}"], rules=["P005_args_invalid"], error_code="ARGS_INVALID",
                error_message=f"Invalid arguments for {tool_name}: {msg}", tool=tool_name, tool_version=spec.version,
                action_hash=compute_action_hash(task_id, plan_version, step_id, tool_name, spec.version, args, None),
            )
        canonical_args = args_model.model_dump(mode="json")

        # P006 - deterministic, tool-specific assessment (paths, sensitivity, overwrite, app allowlist...).
        assessment = None
        if spec.assess is not None:
            try:
                assessment = spec.assess(ctx, args_model)
            except ToolError as exc:
                code = exc.code
                if code.startswith("PATH_") or code.startswith("APP_") or code in ("CONTENT_TOO_LARGE", "WORKSPACE_MISSING"):
                    return deny(code, exc.message, "P006_assessment_denied", max_risk(base, RiskLevel.SENSITIVE_WRITE), base)
                return PolicyDecision(
                    decision="precondition_failed", risk=base.value, base_risk=base.value, reasons=[exc.message],
                    rules=["P007_precondition"], error_code=code, error_message=exc.message, tool=tool_name,
                    tool_version=spec.version,
                    action_hash=compute_action_hash(task_id, plan_version, step_id, tool_name, spec.version, canonical_args, None),
                )
        if assessment is not None and assessment.deny:
            return deny(assessment.deny_code or "POLICY_DENIED", assessment.deny, "P006_assessment_denied", base, base)

        target_state = assessment.target_state if assessment else None
        action_hash = compute_action_hash(task_id, plan_version, step_id, tool_name, spec.version, canonical_args, target_state)
        reasons: List[str] = list(assessment.reasons) if assessment else []
        rules: List[str] = []

        if assessment is not None and assessment.precondition_error:
            code, message = assessment.precondition_error
            return PolicyDecision(
                decision="precondition_failed", risk=base.value, base_risk=base.value, reasons=[message],
                rules=["P007_precondition"], effect=assessment.effect, error_code=code, error_message=message,
                tool=tool_name, tool_version=spec.version, action_hash=action_hash, target_state=target_state,
            )

        # P010 - effective risk: the maximum of tool, assessment and model hint (model can only escalate).
        model_risk = parse_risk(step.get("risk"))
        effective = max_risk(base, assessment.risk if assessment else None, model_risk)
        if model_risk is not None and RISK_ORDER.index(model_risk) > RISK_ORDER.index(max_risk(base, assessment.risk if assessment else None)):
            reasons.append(f"Plan marked this step as {model_risk.value}; using the stricter level.")
            rules.append("P010_model_escalation")
        if effective == RiskLevel.PRIVILEGED_OR_UNSUPPORTED:
            return deny("ACTION_PRIVILEGED", "This action is classified as privileged or unsupported.", "P003_privileged", effective, base)

        needs_approval = False
        staged = bool(deferred_capabilities) and bool(spec.capabilities) and set(spec.capabilities) <= set(deferred_capabilities)
        if staged and RISK_ORDER.index(effective) < RISK_ORDER.index(RiskLevel.DESTRUCTIVE):
            rules.append("P016_deferred_to_commit")
            reasons.append("The change is staged in this task's transaction; you review it before anything is written.")
        elif RISK_ORDER.index(effective) >= RISK_ORDER.index(APPROVAL_THRESHOLD):
            needs_approval = True
            rules.append("P011_risk_threshold")
            reasons.append(f"Risk level {effective.value} always requires approval.")
        if spec.approval == "always":
            needs_approval = True
            rules.append("P012_tool_requires_approval")
            if not any("always requires approval" in r for r in reasons):
                reasons.append(f"{tool_name} always requires approval.")
        if assessment is not None and assessment.requires_approval and not staged:
            needs_approval = True
            rules.append("P013_assessment_requires_approval")
        if spec.mutates and user_settings.get("require_approval_for_writes"):
            needs_approval = True
            rules.append("P014_settings_require_approval_for_writes")
            reasons.append("Settings require approval for every change.")
        if step.get("requires_approval") is True:
            needs_approval = True
            rules.append("P015_plan_requested_approval")
            reasons.append("The plan asked for confirmation before this step.")
        if not needs_approval:
            rules.append("P020_allowed_by_risk")

        return PolicyDecision(
            decision="require_approval" if needs_approval else "allow",
            risk=effective.value, base_risk=base.value, reasons=reasons, rules=rules,
            effect=assessment.effect if assessment else spec.description, preview=assessment.preview if assessment else None,
            target_state=target_state, action_hash=action_hash, tool=tool_name, tool_version=spec.version,
        )
