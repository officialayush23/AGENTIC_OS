"""Capability table.

A capability is an unforgeable, kernel-held grant of authority. Processes
only hold opaque ids; the model never sees them. Capabilities can be
attenuated (never amplified) when delegated to a child, can expire, can be
one-shot, and can be revoked.

Kinds:
    fs       scope = workspace-relative path prefix ("" = whole workspace); rights read, write
    app      scope = allowlisted app id or "*";                              rights launch
    sys      scope = "*";                                                    rights read
    compute  scope = "*";                                                    rights use
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..util import utc_now_iso

KINDS = {"fs": {"read", "write"}, "app": {"launch"}, "sys": {"read"}, "compute": {"use"},
         "mail": {"read", "send"}, "web": {"read", "act"}}

# Tool-level capability names (ToolSpec.capabilities) -> (kind, right)
TOOL_CAP_MAP: Dict[str, Tuple[str, str]] = {
    "workspace.read": ("fs", "read"),
    "workspace.write": ("fs", "write"),
    "apps.read": ("sys", "read"),
    "system.read": ("sys", "read"),
    "apps.launch": ("app", "launch"),
    "compute": ("compute", "use"),
    "mail.read": ("mail", "read"),
    "mail.send": ("mail", "send"),
    "web.read": ("web", "read"),
    "web.act": ("web", "act"),
}

# Capabilities whose actions cannot be staged in the overlay (they leave the computer or launch things).
IRREVERSIBLE_TOOL_CAPS = {"apps.launch", "mail.send", "web.act"}
# Capabilities a process does not hold at spawn but may be granted by the user on request.
GRANTABLE_TOOL_CAPS = IRREVERSIBLE_TOOL_CAPS | {"web.read"}


def url_domain(url: str) -> str:
    from urllib.parse import urlsplit
    text = str(url or "").strip()
    if "://" not in text:
        text = "https://" + text
    host = (urlsplit(text).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def cap_resource(kind: str, args: dict) -> str:
    """The resource an action touches, for capability checks."""
    if kind == "app":
        return str(args.get("app_id") or "*")
    if kind == "web":
        return url_domain(args.get("url") or args.get("domain") or "") or "*"
    return "*"


class CapabilityError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Capability:
    cap_id: str
    pid: str
    kind: str
    scope: str
    rights: Tuple[str, ...]
    expires_at: Optional[float] = None
    uses_left: Optional[int] = None
    parent_cap: Optional[str] = None
    origin: str = "spawn"        # spawn | delegate | approval
    revoked: bool = False

    def as_dict(self) -> dict:
        d = asdict(self)
        d["rights"] = list(self.rights)
        return d


def norm_scope(scope: str) -> str:
    s = (scope or "").replace("\\", "/").strip("/")
    return "" if s in ("", ".") else s


def scope_covers(scope: str, rel: str, kind: str) -> bool:
    """Does capability `scope` cover resource `rel`?"""
    if kind == "web":
        return scope == "*" or rel == scope or rel.endswith("." + scope)
    if kind != "fs":
        return scope == "*" or scope == rel
    scope = norm_scope(scope)
    rel = norm_scope(rel)
    if scope == "":
        return True
    return rel == scope or rel.startswith(scope + "/")


def scope_within(child: str, parent: str, kind: str) -> bool:
    if kind == "web":
        return scope_covers(parent, child, "web")
    if kind != "fs":
        return parent == "*" or child == parent
    return scope_covers(parent, child, "fs")


class CapabilityTable:
    """In-memory table owned by the kernel loop; persisted through `store`."""

    def __init__(self, store, clock: Callable[[], float]):
        self._caps: Dict[str, Capability] = {}
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------ lifecycle
    def load(self) -> None:
        for rec in self.store.load_capabilities():
            cap = Capability(
                cap_id=rec["cap_id"], pid=rec["pid"], kind=rec["kind"], scope=rec["scope"],
                rights=tuple(rec["rights"].split(",")) if rec["rights"] else (), expires_at=rec["expires_at"],
                uses_left=rec["uses_left"], parent_cap=rec["parent_cap"], origin=rec["origin"],
                revoked=bool(rec["revoked"]),
            )
            self._caps[cap.cap_id] = cap

    def _persist(self, cap: Capability) -> None:
        self.store.save_capability(cap.as_dict(), utc_now_iso())

    def grant(self, pid: str, kind: str, scope: str, rights: Iterable[str], *, ttl_s: Optional[float] = None,
              uses: Optional[int] = None, origin: str = "spawn", parent_cap: Optional[str] = None) -> Capability:
        if kind not in KINDS:
            raise CapabilityError("CAP_KIND_UNKNOWN", f"unknown capability kind {kind!r}")
        rights_t = tuple(sorted(set(rights)))
        if not rights_t or not set(rights_t) <= KINDS[kind]:
            raise CapabilityError("CAP_RIGHTS_INVALID", f"invalid rights {rights_t} for {kind}")
        cap = Capability(
            cap_id="cap_" + secrets.token_hex(12), pid=pid, kind=kind,
            scope=norm_scope(scope) if kind == "fs" else (scope or "*"), rights=rights_t,
            expires_at=(self.clock() + ttl_s) if ttl_s else None, uses_left=uses, parent_cap=parent_cap,
            origin=origin,
        )
        self._caps[cap.cap_id] = cap
        self._persist(cap)
        return cap

    def attenuate(self, parent_cap_id: str, child_pid: str, *, scope: Optional[str] = None,
                  rights: Optional[Iterable[str]] = None, ttl_s: Optional[float] = None) -> Capability:
        """Delegate a *weaker or equal* copy of a capability to another process."""
        parent = self.get_valid(parent_cap_id)
        if parent is None:
            raise CapabilityError("CAP_INVALID", "cannot delegate an invalid, expired or revoked capability")
        new_scope = parent.scope if scope is None else (norm_scope(scope) if parent.kind == "fs" else scope)
        if not scope_within(new_scope, parent.scope, parent.kind):
            raise CapabilityError("CAP_AMPLIFICATION", f"scope {new_scope!r} is wider than parent scope {parent.scope!r}")
        new_rights = set(parent.rights if rights is None else rights)
        if not new_rights <= set(parent.rights):
            raise CapabilityError("CAP_AMPLIFICATION", f"rights {sorted(new_rights)} exceed parent {list(parent.rights)}")
        expires = parent.expires_at
        if ttl_s is not None:
            cand = self.clock() + ttl_s
            expires = cand if expires is None else min(expires, cand)
        cap = Capability(
            cap_id="cap_" + secrets.token_hex(12), pid=child_pid, kind=parent.kind, scope=new_scope,
            rights=tuple(sorted(new_rights)), expires_at=expires, uses_left=parent.uses_left,
            parent_cap=parent.cap_id, origin="delegate",
        )
        self._caps[cap.cap_id] = cap
        self._persist(cap)
        return cap

    def revoke(self, cap_id: str) -> None:
        """Revoke a capability and everything delegated from it."""
        cap = self._caps.get(cap_id)
        if cap is None or cap.revoked:
            return
        cap.revoked = True
        self._persist(cap)
        for other in list(self._caps.values()):
            if other.parent_cap == cap_id:
                self.revoke(other.cap_id)

    def revoke_all(self, pid: str) -> None:
        for cap in list(self._caps.values()):
            if cap.pid == pid:
                self.revoke(cap.cap_id)

    # --------------------------------------------------------------- query
    def get_valid(self, cap_id: str) -> Optional[Capability]:
        cap = self._caps.get(cap_id)
        if cap is None or cap.revoked:
            return None
        if cap.expires_at is not None and self.clock() >= cap.expires_at:
            return None
        if cap.uses_left is not None and cap.uses_left <= 0:
            return None
        if cap.parent_cap and self.get_valid(cap.parent_cap) is None:
            return None  # parent revoked/expired -> delegated copy dies with it
        return cap

    def held(self, pid: str) -> List[Capability]:
        return [c for c in self._caps.values() if c.pid == pid and self.get_valid(c.cap_id) is not None]

    def find(self, pid: str, kind: str, right: str, resource: str) -> Optional[Capability]:
        for cap in self.held(pid):
            if cap.kind == kind and right in cap.rights and scope_covers(cap.scope, resource, kind):
                return cap
        return None

    def check(self, pid: str, kind: str, right: str, resource: str) -> Capability:
        cap = self.find(pid, kind, right, resource)
        if cap is None:
            raise CapabilityError(
                "CAP_DENIED", f"process holds no '{kind}:{right}' capability covering {resource!r}")
        return cap

    def consume(self, cap: Capability) -> None:
        if cap.uses_left is not None:
            cap.uses_left -= 1
            self._persist(cap)

    def public(self, pid: str) -> List[dict]:
        out = []
        for c in self.held(pid):
            d = c.as_dict()
            d.pop("cap_id", None)
            d.pop("parent_cap", None)
            out.append(d)
        return out
