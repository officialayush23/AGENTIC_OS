"""Process configuration.

`Settings` holds process-level configuration that is fixed for the lifetime of
the backend (where data lives, which port to bind, which origins are allowed).
User-editable preferences (workspace root, limits, policy toggles) live in the
SQLite `settings` table and are managed by `agentic_os.services.settings`.

Only the standard library is used here so the module is importable anywhere.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional, Tuple

from . import APP_NAME

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 8765
DEV_UI_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")
TAURI_ORIGINS = ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def default_data_dir() -> Path:
    """Per-user application data directory (never inside the repository)."""
    env = os.environ.get("AGENTIC_OS_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "agentic-os"


def repo_root() -> Path:
    """Repository root when running from a source checkout."""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Limits:
    """Hard execution limits. Enforced in code, never only in prompts."""

    max_plan_steps: int = 12
    max_step_executions: int = 20
    max_retries_per_step: int = 2
    max_replans: int = 2
    max_clarifications: int = 2
    max_graph_transitions: int = 150
    task_timeout_s: int = 600
    max_llm_calls: int = 8
    max_llm_tokens: int = 60000

    def as_dict(self) -> dict:
        return {
            "max_plan_steps": self.max_plan_steps,
            "max_step_executions": self.max_step_executions,
            "max_retries_per_step": self.max_retries_per_step,
            "max_replans": self.max_replans,
            "max_clarifications": self.max_clarifications,
            "max_graph_transitions": self.max_graph_transitions,
            "task_timeout_s": self.task_timeout_s,
            "max_llm_calls": self.max_llm_calls,
            "max_llm_tokens": self.max_llm_tokens,
        }


# Bounds the UI is allowed to set. Values outside are rejected (not clamped).
LIMIT_BOUNDS = {
    "max_plan_steps": (1, 12),
    "max_step_executions": (1, 40),
    "max_retries_per_step": (0, 3),
    "max_replans": (0, 3),
    "max_clarifications": (0, 3),
    "max_graph_transitions": (20, 300),
    "task_timeout_s": (30, 3600),
    "max_llm_calls": (0, 30),
    "max_llm_tokens": (1000, 500000),
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    ui_dist_dir: Optional[Path] = None
    extra_allowed_origins: Tuple[str, ...] = ()
    ui_url: Optional[str] = None
    open_browser: bool = True
    bearer_token: Optional[str] = None
    enable_fake_provider: bool = False
    dev_mode: bool = False
    log_level: str = "INFO"
    max_concurrent_tasks: int = 2
    session_ttl_days: int = 7
    pairing_code_ttl_s: int = 600
    limits: Limits = field(default_factory=Limits)
    secret_store_backend: str = "keyring"  # "keyring" | "memory" (tests only)
    keyring_service: str = APP_NAME
    allow_insecure_keyring: bool = False

    # ------------------------------------------------------------------ paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "agentic_os.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def default_demo_workspace(self) -> Path:
        return self.data_dir / "demo_workspace"

    # ---------------------------------------------------------------- network
    @property
    def self_origins(self) -> Tuple[str, ...]:
        p = self.port
        return (f"http://127.0.0.1:{p}", f"http://localhost:{p}", f"http://[::1]:{p}")

    @property
    def allowed_origins(self) -> Tuple[str, ...]:
        origins = list(self.self_origins)
        if self.dev_mode:
            origins.extend(DEV_UI_ORIGINS)
        if self.bearer_token:
            origins.extend(TAURI_ORIGINS)
        origins.extend(self.extra_allowed_origins)
        seen = []
        for o in origins:
            o = o.rstrip("/")
            if o and o not in seen:
                seen.append(o)
        return tuple(seen)

    @property
    def allowed_hosts(self) -> Tuple[str, ...]:
        p = self.port
        return (f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}")

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.logs_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        host = os.environ.get("AGENTIC_OS_HOST", "127.0.0.1").strip()
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                "Agentic OS v0.1 only binds to a loopback address (127.0.0.1, ::1 or localhost). "
                f"Refusing AGENTIC_OS_HOST={host!r}."
            )
        ui_dist_env = os.environ.get("AGENTIC_OS_UI_DIST")
        ui_dist = Path(ui_dist_env).expanduser() if ui_dist_env else repo_root() / "frontend" / "dist"
        extra = tuple(
            o.strip().rstrip("/")
            for o in os.environ.get("AGENTIC_OS_ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        )
        values = dict(
            data_dir=default_data_dir(),
            host=host,
            port=_env_int("AGENTIC_OS_PORT", DEFAULT_PORT),
            ui_dist_dir=ui_dist,
            extra_allowed_origins=extra,
            ui_url=os.environ.get("AGENTIC_OS_UI_URL") or None,
            open_browser=_env_bool("AGENTIC_OS_OPEN_BROWSER", True),
            enable_fake_provider=_env_bool("AGENTIC_OS_ENABLE_FAKE_PROVIDER", False),
            dev_mode=_env_bool("AGENTIC_OS_DEV", False),
            log_level=os.environ.get("AGENTIC_OS_LOG_LEVEL", "INFO").upper(),
            max_concurrent_tasks=_env_int("AGENTIC_OS_MAX_CONCURRENT_TASKS", 2),
            allow_insecure_keyring=_env_bool("AGENTIC_OS_ALLOW_INSECURE_KEYRING", False),
        )
        values.update(overrides)
        settings = cls(**values)
        if not (1 <= settings.port <= 65535):
            raise ValueError("AGENTIC_OS_PORT must be between 1 and 65535")
        return settings
