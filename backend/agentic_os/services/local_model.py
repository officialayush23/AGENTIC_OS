"""Local AI model: a quantized Qwen2.5 running on this PC with ONNX Runtime GenAI.

Nothing here is required. When the user clicks Install in Settings, the service:
  1. installs the runtime into the app's own Python environment
     (Windows GPU: onnxruntime-genai-directml, which drives any DirectX 12 GPU; otherwise onnxruntime-genai),
  2. downloads a pinned, pre-built int4 model from Hugging Face (resumable, size-checked),
  3. registers a provider of kind "local" and runs a short self-test.

The local provider is then used as a backup whenever the online model fails (busy, quota, offline),
or as the main model if the user picks it.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

HF = "https://huggingface.co"

# Pinned builds (repo @ revision). GPU builds use DirectML (Windows, any DX12 GPU: NVIDIA, AMD, Intel).
CATALOG: Dict[str, dict] = {
    "qwen2.5-1.5b-gpu": {
        "label": "Qwen2.5 1.5B Instruct · GPU (DirectML, int4)", "params": "1.5B", "device": "dml",
        "repo": "amd/OGA_DML_Qwen_Qwen2.5-1.5B-Instruct", "revision": "eb169a108813f01b3d14519fd2d90b7408ae76cb",
        "size_mb": 1300, "vram_gb": 2, "license": "MIT (AMD build) + Qwen license",
        "note": "Fast on most GPUs. Good for notes, short answers and simple plans.",
    },
    "qwen2.5-3b-gpu": {
        "label": "Qwen2.5 3B Instruct · GPU (DirectML, int4)", "params": "3B", "device": "dml",
        "repo": "amd/OGA_DML_Qwen_Qwen2.5-3B-Instruct", "revision": "b5b498a98ade72b0f1e0bafb645fcf89e45e626d",
        "size_mb": 2250, "vram_gb": 4, "license": "MIT (AMD build) + Qwen license",
        "note": "Better planning and writing. Needs about 4 GB of GPU memory.",
    },
    "qwen2.5-1.5b-cpu": {
        "label": "Qwen2.5 1.5B Instruct · CPU (int4)", "params": "1.5B", "device": "cpu",
        "repo": "elbruno/Qwen2.5-1.5B-Instruct-onnx", "revision": "80a33348682e2b45ef7efd19b1db0bbcdbe7cc15",
        "size_mb": 1950, "vram_gb": 0, "license": "Apache-2.0 (Qwen2.5)",
        "note": "Works without a GPU. Slower (several tokens per second).",
    },
}
MODEL_FILES = ("genai_config.json", "model.onnx", "model.onnx.data", "tokenizer.json", "tokenizer_config.json",
               "special_tokens_map.json", "added_tokens.json", "vocab.json", "merges.txt", "chat_template.jinja")
REQUIRED_FILES = ("genai_config.json", "model.onnx", "tokenizer.json")
READY_MARK = ".agentic-os-ready"


def runtime_package(device: str) -> str:
    # On Windows always the DirectML build: it also runs CPU models, and avoids two onnxruntime packages clashing.
    return "onnxruntime-genai-directml" if sys.platform == "win32" else "onnxruntime-genai"


def runtime_status() -> dict:
    spec = importlib.util.find_spec("onnxruntime_genai")
    if spec is None:
        return {"installed": False, "package": None, "version": None}
    try:
        from importlib.metadata import version, PackageNotFoundError
        for pkg in ("onnxruntime-genai-directml", "onnxruntime-genai-cuda", "onnxruntime-genai"):
            try:
                return {"installed": True, "package": pkg, "version": version(pkg)}
            except PackageNotFoundError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return {"installed": True, "package": "onnxruntime-genai", "version": None}


def default_model_for_platform() -> str:
    return "qwen2.5-1.5b-gpu" if sys.platform == "win32" else "qwen2.5-1.5b-cpu"


@dataclass
class InstallJob:
    model: str
    running: bool = True
    stage: str = "starting"
    message: str = ""
    done_bytes: int = 0
    total_bytes: int = 0
    error: Optional[str] = None
    result: Optional[dict] = None
    started: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        pct = int(self.done_bytes * 100 / self.total_bytes) if self.total_bytes else None
        return {"model": self.model, "running": self.running, "stage": self.stage, "message": self.message,
                "done_mb": round(self.done_bytes / 2**20), "total_mb": round(self.total_bytes / 2**20), "percent": pct,
                "error": self.error, "result": self.result, "elapsed_s": int(time.time() - self.started)}


class LocalModelService:
    def __init__(self, models_dir: str, *, register: Callable[[str], None], self_test: Callable[[], dict],
                 opener: Callable = urllib.request.urlopen, pip: Optional[Callable[[List[str]], None]] = None):
        self.models_dir = models_dir
        self._register = register
        self._self_test = self_test
        self._open = opener
        self._pip = pip or self._pip_install
        self.job: Optional[InstallJob] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ status
    def path(self, model_id: str) -> str:
        return os.path.join(self.models_dir, model_id)

    def installed(self) -> List[str]:
        return [m for m in CATALOG if os.path.exists(os.path.join(self.path(m), READY_MARK))]

    def disk(self) -> dict:
        probe = self.models_dir
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            free = shutil.disk_usage(probe or ".").free
        except OSError:
            free = None
        return {"folder": self.models_dir, "free_gb": round(free / 2**30, 1) if free is not None else None}

    def status(self) -> dict:
        return {"runtime": runtime_status(), "storage": self.disk(), "installed": self.installed(), "recommended": default_model_for_platform(),
                "platform": sys.platform, "python": ".".join(map(str, sys.version_info[:3])),
                "catalog": [{"id": k, **{x: v[x] for x in ("label", "params", "device", "size_mb", "vram_gb", "note", "license")},
                             "repo": v["repo"], "installed": k in self.installed()} for k, v in CATALOG.items()],
                "job": self.job.public() if self.job else None}

    # ------------------------------------------------------------ install
    def install(self, model_id: str) -> dict:
        if model_id not in CATALOG:
            raise KeyError(model_id)
        free = self.disk()["free_gb"]
        if free is not None and free * 1024 < CATALOG[model_id]["size_mb"] * 1.1 and model_id not in self.installed():
            raise OSError(f"not enough free space in {self.models_dir}: {free} GB free, "
                          f"{CATALOG[model_id]['size_mb'] / 1024:.1f} GB needed")
        with self._lock:
            if self.job and self.job.running:
                return self.job.public()
            self.job = InstallJob(model=model_id)
            job = self.job
        threading.Thread(target=self._run, args=(job,), name="aos-local-model", daemon=True).start()
        return job.public()

    def cancel(self) -> None:
        if self.job and self.job.running:
            self.job.cancel.set()

    def remove(self, model_id: str) -> None:
        p = self.path(model_id)
        if os.path.isdir(p) and os.path.abspath(p).startswith(os.path.abspath(self.models_dir)):
            shutil.rmtree(p, ignore_errors=True)

    def _run(self, job: InstallJob) -> None:
        spec = CATALOG[job.model]
        try:
            pkg = runtime_package(spec["device"])
            rt = runtime_status()
            if not rt["installed"] or rt["package"] != pkg:
                job.stage, job.message = "runtime", f"Installing {pkg} (one time)"
                if rt["installed"] and rt["package"] != pkg:
                    self._pip(["uninstall", "-y", rt["package"], "onnxruntime"])
                self._pip(["install", "--disable-pip-version-check", "--no-input", "--no-cache-dir", pkg])
                importlib.invalidate_caches()
            job.stage, job.message = "download", f"Downloading {spec['repo']}"
            self._download(job, spec)
            job.stage, job.message = "register", "Adding the local provider"
            self._register(job.model)
            job.stage, job.message = "test", "Loading the model and running a short test"
            job.result = self._self_test()
            job.stage = "done"
            job.message = "Ready" if (job.result or {}).get("ok") else "Installed, but the test failed"
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            log.warning("local model install failed: %s", exc)
            job.error = getattr(exc, "message", None) or str(exc)
            job.stage = "failed"
        finally:
            job.running = False

    @staticmethod
    def _pip_install(args: List[str]) -> None:
        proc = subprocess.run([sys.executable, "-m", "pip", *args], capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError("pip failed: " + " | ".join(tail))

    def _download(self, job: InstallJob, spec: dict) -> None:
        dest = self.path(job.model)
        os.makedirs(dest, exist_ok=True)
        tree = self._json(f"{HF}/api/models/{spec['repo']}/tree/{spec['revision']}")
        sizes = {f["path"]: int(f.get("size") or 0) for f in tree if f.get("type") == "file"}
        files = [f for f in MODEL_FILES if f in sizes]
        missing = [f for f in REQUIRED_FILES if f not in sizes]
        if missing:
            raise RuntimeError(f"the model repository is missing {', '.join(missing)}")
        job.total_bytes = sum(sizes[f] for f in files)
        done = lambda f: os.path.exists(os.path.join(dest, f)) and os.path.getsize(os.path.join(dest, f)) == sizes[f]
        job.done_bytes = sum(sizes[f] for f in files if done(f))
        for name in files:
            target = os.path.join(dest, name)
            if done(name):
                continue
            if os.path.exists(target):
                os.remove(target)
            self._fetch(job, f"{HF}/{spec['repo']}/resolve/{spec['revision']}/{name}", target, sizes[name])
        with open(os.path.join(dest, READY_MARK), "w", encoding="utf-8") as fh:
            json.dump({"repo": spec["repo"], "revision": spec["revision"], "device": spec["device"],
                       "installed_at": time.time()}, fh)

    def _json(self, url: str):
        with self._open(urllib.request.Request(url, headers={"User-Agent": "agentic-os"}), timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    def _fetch(self, job: InstallJob, url: str, target: str, size: int) -> None:
        part = target + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if size and have > size:
            os.remove(part)
            have = 0
        job.done_bytes += have
        if size and have == size:
            os.replace(part, target)
            return
        headers = {"User-Agent": "agentic-os"}
        if have:
            headers["Range"] = f"bytes={have}-"
        with self._open(urllib.request.Request(url, headers=headers), timeout=120) as r:
            if have and getattr(r, "status", 200) != 206:  # server ignored the range: start over
                job.done_bytes -= have
                have = 0
            with open(part, "ab" if have else "wb") as fh:
                while True:
                    if job.cancel.is_set():
                        raise RuntimeError("cancelled")
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    job.done_bytes += len(chunk)
        got = os.path.getsize(part)
        if size and got != size:
            raise RuntimeError(f"{os.path.basename(target)}: downloaded {got} bytes, expected {size}")
        os.replace(part, target)
