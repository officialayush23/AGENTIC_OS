"""Core tool types: risk levels, tool specification, context, errors.

Every tool is a `ToolSpec` with a Pydantic input model, a Pydantic output
model, declared capabilities, a base risk level, a timeout, idempotency and
retry metadata, a handler, and (optionally) a deterministic `assess`
function used by the policy engine, a `verify` function used after
execution, and a `reconcile` function used after a crash to find out whether
a side effect already happened.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel


class RiskLevel(str, Enum):
    READ_ONLY = "READ_ONLY"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    EXTERNAL_COMMUNICATION = "EXTERNAL_COMMUNICATION"
    SENSITIVE_WRITE = "SENSITIVE_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"
    PRIVILEGED_OR_UNSUPPORTED = "PRIVILEGED_OR_UNSUPPORTED"


RISK_ORDER: List[RiskLevel] = [
    RiskLevel.READ_ONLY,
    RiskLevel.REVERSIBLE_WRITE,
    RiskLevel.EXTERNAL_COMMUNICATION,
    RiskLevel.SENSITIVE_WRITE,
    RiskLevel.DESTRUCTIVE,
    RiskLevel.PRIVILEGED_OR_UNSUPPORTED,
]

RISK_DESCRIPTIONS = {
    RiskLevel.READ_ONLY: "Reads information without changing anything.",
    RiskLevel.REVERSIBLE_WRITE: "Creates something new or starts an app; easy to undo.",
    RiskLevel.EXTERNAL_COMMUNICATION: "Sends information outside this computer.",
    RiskLevel.SENSITIVE_WRITE: "Changes or overwrites existing data.",
    RiskLevel.DESTRUCTIVE: "Deletes data or makes hard-to-reverse changes.",
    RiskLevel.PRIVILEGED_OR_UNSUPPORTED: "Not allowed in this version (elevation, shell, security changes).",
}


def parse_risk(value: Any) -> Optional[RiskLevel]:
    if value is None:
        return None
    if isinstance(value, RiskLevel):
        return value
    text = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {"DESTRUCTIVE_OR_PRIVILEGED": RiskLevel.DESTRUCTIVE, "SENSITIVE": RiskLevel.SENSITIVE_WRITE,
               "PRIVILEGED": RiskLevel.PRIVILEGED_OR_UNSUPPORTED, "UNSUPPORTED": RiskLevel.PRIVILEGED_OR_UNSUPPORTED}
    if text in aliases:
        return aliases[text]
    try:
        return RiskLevel(text)
    except ValueError:
        return None


def max_risk(*levels: Optional[RiskLevel]) -> RiskLevel:
    present = [lvl for lvl in levels if lvl is not None]
    if not present:
        return RiskLevel.READ_ONLY
    return max(present, key=RISK_ORDER.index)


# ---------------------------------------------------------------------------
class ToolError(Exception):
    """Expected, user-explainable tool failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


class ToolTimeout(ToolError):
    def __init__(self, message: str = "the tool exceeded its time limit"):
        super().__init__("TOOL_TIMEOUT", message, retryable=True)


class ToolCancelled(ToolError):
    def __init__(self, message: str = "cancelled before completion"):
        super().__init__("TOOL_CANCELLED", message, retryable=False)


# ---------------------------------------------------------------------------
@dataclass
class Assessment:
    """Deterministic, tool-specific view of what an action will do."""

    risk: Optional[RiskLevel] = None
    reasons: List[str] = field(default_factory=list)
    effect: str = ""
    preview: Optional[dict] = None
    target_state: Optional[dict] = None
    deny: Optional[str] = None
    deny_code: Optional[str] = None
    requires_approval: bool = False
    # A failed precondition (e.g. text to replace not found). Not a policy
    # violation: the step fails without asking for approval.
    precondition_error: Optional[Tuple[str, str]] = None


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": bool(self.passed), "detail": self.detail}


@dataclass
class ReconcileResult:
    """Outcome of checking whether an interrupted mutating call already took effect."""

    state: str  # "done" | "not_done" | "unknown"
    output: Optional[dict] = None
    detail: str = ""


@dataclass
class ToolContext:
    task_id: str
    step_id: str
    workspace: Any  # WorkspaceGuard
    os: Any  # OSAdapter
    apps: Any  # AppRepo-like: get(app_id) / list()
    user_settings: Dict[str, Any]
    data_dir: Any = None
    deadline: float = 0.0  # time.monotonic() deadline; 0 = none
    cancel_check: Callable[[], bool] = lambda: False
    # Target state captured by the policy engine (and bound into any approval).
    # Mutating tools compare it with the live state right before writing.
    expected_state: Optional[dict] = None
    task_created_at: Optional[str] = None
    # Kernel-provided services (e.g. "rag": the knowledge base). Tools must tolerate absence.
    services: Dict[str, Any] = field(default_factory=dict)

    def setting(self, key: str, default: Any) -> Any:
        value = self.user_settings.get(key, default) if self.user_settings else default
        return default if value is None else value

    def check(self) -> None:
        """Cooperative cancellation / timeout check for long-running loops."""
        if self.deadline and time.monotonic() > self.deadline:
            raise ToolTimeout()
        if self.cancel_check():
            raise ToolCancelled()


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


@dataclass
class ToolSpec:
    name: str
    version: str
    description: str
    input_model: Type[BaseModel]
    output_model: Type[BaseModel]
    capabilities: Tuple[str, ...]
    risk: RiskLevel
    timeout_s: float
    mutates: bool
    idempotent: bool
    retryable: bool
    approval: str  # "never" | "policy" | "always"
    verification: str
    handler: Callable[[ToolContext, BaseModel], dict]
    assess: Optional[Callable[[ToolContext, BaseModel], Assessment]] = None
    verify: Optional[Callable[[ToolContext, BaseModel, dict], List[Check]]] = None
    reconcile: Optional[Callable[[ToolContext, BaseModel], ReconcileResult]] = None
    audit_fields: Tuple[str, ...] = ()
    platforms: Tuple[str, ...] = ("win32", "linux", "darwin")
    category: str = "workspace"
    examples: Tuple[dict, ...] = ()

    def validate_spec(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r}")
        if not _VERSION_RE.match(self.version):
            raise ValueError(f"tool {self.name}: invalid version {self.version!r}")
        if not (isinstance(self.input_model, type) and issubclass(self.input_model, BaseModel)):
            raise ValueError(f"tool {self.name}: input_model must be a Pydantic model")
        if not (isinstance(self.output_model, type) and issubclass(self.output_model, BaseModel)):
            raise ValueError(f"tool {self.name}: output_model must be a Pydantic model")
        if not isinstance(self.risk, RiskLevel):
            raise ValueError(f"tool {self.name}: risk must be a RiskLevel")
        if not (0 < float(self.timeout_s) <= 600):
            raise ValueError(f"tool {self.name}: timeout must be within (0, 600] seconds")
        if self.approval not in ("never", "policy", "always"):
            raise ValueError(f"tool {self.name}: approval must be never|policy|always")
        if not callable(self.handler):
            raise ValueError(f"tool {self.name}: handler must be callable")
        if not self.capabilities:
            raise ValueError(f"tool {self.name}: at least one capability must be declared")
        if not self.verification:
            raise ValueError(f"tool {self.name}: a verification method must be described")
        if self.mutates and self.risk == RiskLevel.READ_ONLY:
            raise ValueError(f"tool {self.name}: a mutating tool cannot be READ_ONLY")

    def input_schema(self) -> dict:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict:
        return self.output_model.model_json_schema()

    def public(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "category": self.category,
            "capabilities": list(self.capabilities),
            "risk": self.risk.value,
            "risk_description": RISK_DESCRIPTIONS[self.risk],
            "timeout_s": self.timeout_s,
            "mutates": self.mutates,
            "idempotent": self.idempotent,
            "retryable": self.retryable,
            "approval": self.approval,
            "verification": self.verification,
            "platforms": list(self.platforms),
            "input_schema": self.input_schema(),
            "output_schema": self.output_schema(),
        }
