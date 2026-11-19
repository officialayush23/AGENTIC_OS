"""Credential storage.

API keys live in the operating-system credential store via `keyring`
(Windows Credential Manager, macOS Keychain, Secret Service on Linux).
They are never written to SQLite, logs, events, prompts or API responses.

If no secure backend is available the store reports itself unavailable and
every write fails with `SecretStoreError` - there is deliberately no plaintext
fallback. `MemorySecretStore` exists for automated tests only.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Dict, Optional

from .redaction import redactor

log = logging.getLogger(__name__)

# Windows Credential Manager stores at most 2560 bytes per credential blob and
# keyring encodes as UTF-16, so long values are split into chunks.
_CHUNK_CHARS = 600
_CHUNK_MARKER = "aos-chunked:v1:"


class SecretStoreError(Exception):
    """Raised when the secure credential store cannot be used."""

    code = "SECRET_STORE_UNAVAILABLE"

    def __init__(self, message: str):
        super().__init__(message)
        self.safe_message = message


class SecretStore(ABC):
    backend_name = "abstract"

    @abstractmethod
    def status(self) -> dict:
        """{'available': bool, 'backend': str, 'reason': str | None}"""

    @abstractmethod
    def get(self, name: str) -> Optional[str]:
        ...

    @abstractmethod
    def set(self, name: str, value: str) -> None:  # noqa: A003
        ...

    @abstractmethod
    def delete(self, name: str) -> bool:
        ...

    def has(self, name: str) -> bool:
        try:
            return self.get(name) is not None
        except SecretStoreError:
            return False

    def require_available(self) -> None:
        st = self.status()
        if not st.get("available"):
            raise SecretStoreError(
                "Secure credential storage is unavailable: "
                f"{st.get('reason') or 'unknown reason'}. Credentials were not saved."
            )


class MemorySecretStore(SecretStore):
    """In-process store for tests. Never used by the real application."""

    backend_name = "memory (tests only)"

    def __init__(self, available: bool = True):
        self._data: Dict[str, str] = {}
        self._available = available
        self._lock = threading.Lock()

    def status(self) -> dict:
        return {
            "available": self._available,
            "backend": self.backend_name,
            "reason": None if self._available else "memory store disabled for this test",
        }

    def get(self, name: str) -> Optional[str]:
        if not self._available:
            raise SecretStoreError("memory store disabled")
        with self._lock:
            value = self._data.get(name)
        if value:
            redactor.register_secret(value)
        return value

    def set(self, name: str, value: str) -> None:  # noqa: A003
        self.require_available()
        redactor.register_secret(value)
        with self._lock:
            self._data[name] = value

    def delete(self, name: str) -> bool:
        self.require_available()
        with self._lock:
            return self._data.pop(name, None) is not None


class KeyringSecretStore(SecretStore):
    """OS credential store through the `keyring` package."""

    def __init__(self, service: str = "AgenticOS", allow_insecure: bool = False):
        self.service = service
        self._allow_insecure = allow_insecure
        self._lock = threading.RLock()
        self._status: Optional[dict] = None
        self._keyring = None

    # ----------------------------------------------------------- availability
    def _load(self):
        if self._keyring is not None:
            return self._keyring
        try:
            import keyring  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency missing
            raise SecretStoreError(f"the 'keyring' package is not installed ({type(exc).__name__})")
        self._keyring = keyring
        return keyring

    def _evaluate(self) -> dict:
        try:
            keyring = self._load()
            backend = keyring.get_keyring()
        except SecretStoreError as exc:
            return {"available": False, "backend": "none", "reason": exc.safe_message}
        except Exception as exc:  # pragma: no cover - backend init failure
            return {"available": False, "backend": "none", "reason": f"keyring initialisation failed ({type(exc).__name__})"}

        cls = type(backend)
        name = f"{cls.__module__}.{cls.__name__}"
        lowered = name.lower()
        if "fail" in lowered and "keyring" in lowered and cls.__name__ == "Keyring" and ".fail" in cls.__module__:
            return {"available": False, "backend": name, "reason": "no secure keyring backend was found on this system"}
        if "plaintext" in lowered or "keyrings.alt" in lowered or ".null" in lowered:
            if not self._allow_insecure:
                return {
                    "available": False,
                    "backend": name,
                    "reason": "the only keyring backend available stores secrets insecurely; refusing to use it",
                }
        chained = getattr(backend, "backends", None)
        if chained is not None and len(list(chained)) == 0:
            return {"available": False, "backend": name, "reason": "no keyring backends are available"}

        # Probe: write, read and delete a harmless value.
        probe_name = "__agentic_os_probe__"
        try:
            backend.set_password(self.service, probe_name, "probe-ok")
            ok = backend.get_password(self.service, probe_name) == "probe-ok"
            try:
                backend.delete_password(self.service, probe_name)
            except Exception:
                pass
        except Exception as exc:
            return {"available": False, "backend": name, "reason": f"credential store probe failed ({type(exc).__name__})"}
        if not ok:
            return {"available": False, "backend": name, "reason": "credential store probe returned an unexpected value"}
        return {"available": True, "backend": name, "reason": None}

    def status(self) -> dict:
        with self._lock:
            if self._status is None or not self._status.get("available"):
                self._status = self._evaluate()
            return dict(self._status)

    @property
    def backend_name(self) -> str:  # type: ignore[override]
        return self.status().get("backend", "keyring")

    # --------------------------------------------------------------- helpers
    def _backend(self):
        self.require_available()
        return self._load().get_keyring()

    def _raw_get(self, backend, key: str) -> Optional[str]:
        return backend.get_password(self.service, key)

    def _raw_delete(self, backend, key: str) -> bool:
        try:
            backend.delete_password(self.service, key)
            return True
        except Exception:
            return False

    # -------------------------------------------------------------- interface
    def get(self, name: str) -> Optional[str]:
        with self._lock:
            backend = self._backend()
            try:
                head = self._raw_get(backend, name)
                if head is None:
                    return None
                if head.startswith(_CHUNK_MARKER):
                    count = int(head[len(_CHUNK_MARKER):])
                    parts = []
                    for i in range(count):
                        part = self._raw_get(backend, f"{name}#{i}")
                        if part is None:
                            raise SecretStoreError("a stored credential is incomplete; please save it again")
                        parts.append(part)
                    value = "".join(parts)
                else:
                    value = head
            except SecretStoreError:
                raise
            except Exception as exc:
                raise SecretStoreError(f"could not read from the credential store ({type(exc).__name__})")
        redactor.register_secret(value)
        return value

    def set(self, name: str, value: str) -> None:  # noqa: A003
        if not isinstance(value, str) or not value:
            raise ValueError("secret value must be a non-empty string")
        redactor.register_secret(value)
        with self._lock:
            backend = self._backend()
            try:
                self._delete_locked(backend, name)
                if len(value) <= _CHUNK_CHARS:
                    backend.set_password(self.service, name, value)
                else:
                    chunks = [value[i:i + _CHUNK_CHARS] for i in range(0, len(value), _CHUNK_CHARS)]
                    for i, chunk in enumerate(chunks):
                        backend.set_password(self.service, f"{name}#{i}", chunk)
                    backend.set_password(self.service, name, f"{_CHUNK_MARKER}{len(chunks)}")
            except SecretStoreError:
                raise
            except Exception as exc:
                raise SecretStoreError(f"could not write to the credential store ({type(exc).__name__})")
        log.info("credential stored in OS credential store (name=%s)", name)

    def _delete_locked(self, backend, name: str) -> bool:
        head = None
        try:
            head = self._raw_get(backend, name)
        except Exception:
            head = None
        existed = head is not None
        if head and head.startswith(_CHUNK_MARKER):
            try:
                count = int(head[len(_CHUNK_MARKER):])
            except ValueError:
                count = 0
            for i in range(count):
                self._raw_delete(backend, f"{name}#{i}")
        if existed:
            self._raw_delete(backend, name)
        return existed

    def delete(self, name: str) -> bool:
        with self._lock:
            backend = self._backend()
            existed = self._delete_locked(backend, name)
        if existed:
            log.info("credential deleted from OS credential store (name=%s)", name)
        return existed


def provider_secret_name(provider_id: str) -> str:
    return f"provider:{provider_id}:api_key"


def build_secret_store(backend: str, service: str, allow_insecure: bool = False) -> SecretStore:
    if backend == "memory":
        return MemorySecretStore()
    return KeyringSecretStore(service=service, allow_insecure=allow_insecure)
