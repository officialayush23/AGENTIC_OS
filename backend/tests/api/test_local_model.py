"""Local model: install flow (download + register + self-test) and fallback from an unavailable online model."""

import io
import json
import os
import time

from tests.api.test_api import client  # noqa: F401 - fixture

from agentic_os.providers.base import ErrorCode, LLMResult, ProviderError
from agentic_os.providers.chain import ChainProvider
from agentic_os.services import local_model as lm


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_hub(files):
    def opener(req, timeout=0):
        url = req.full_url
        if "/api/models/" in url:
            return _Resp(json.dumps([{"type": "file", "path": k, "size": len(v)} for k, v in files.items()]).encode())
        name = url.rsplit("/", 1)[-1]
        data = files[name]
        rng = req.headers.get("Range")
        if rng:
            r = _Resp(data[int(rng.split("=")[1].rstrip("-")):])
            r.status = 206
            return r
        return _Resp(data)
    return opener


def test_install_downloads_resumes_registers_and_tests(tmp_path):
    files = {"genai_config.json": b'{"model": {"context_length": 4096}}', "model.onnx": b"x" * 1000,
             "model.onnx.data": b"y" * 5000, "tokenizer.json": b"{}", "README.md": b"skip me"}
    registered, pip_calls = [], []
    svc = lm.LocalModelService(str(tmp_path), register=registered.append, self_test=lambda: {"ok": True},
                               opener=_fake_hub(files), pip=pip_calls.append)
    dest = tmp_path / "qwen2.5-1.5b-cpu"
    dest.mkdir()
    (dest / "model.onnx.data.part").write_bytes(b"y" * 2000)  # an interrupted earlier download
    svc.install("qwen2.5-1.5b-cpu")
    while svc.job.running:
        time.sleep(0.02)
    assert svc.job.error is None, svc.job.error
    assert (dest / "model.onnx.data").read_bytes() == files["model.onnx.data"]
    assert not (dest / "README.md").exists() and (dest / lm.READY_MARK).exists()
    assert registered == ["qwen2.5-1.5b-cpu"] and svc.installed() == ["qwen2.5-1.5b-cpu"]
    assert svc.job.public()["percent"] == 100


def test_chain_falls_back_on_quota_but_not_on_bad_request():
    class P:
        def __init__(self, kind, err=None):
            self.kind, self.err, self.config, self.seen = kind, err, None, []

        def generate_structured(self, system, user, schema, name, **kw):
            self.seen.append(user)
            if self.err:
                raise ProviderError(self.err, "x")
            return LLMResult(text="{}", data={"ok": True})

    online, local = P("gemini", ErrorCode.QUOTA_EXCEEDED), P("local")
    chain = ChainProvider([online, local])
    assert chain.generate_structured("s", "full", {}, "plan", local_variant=("compact", {})).data == {"ok": True}
    assert online.seen == ["full"] and local.seen == ["compact"]
    bad = ChainProvider([P("gemini", ErrorCode.BAD_REQUEST), P("local")])
    try:
        bad.generate_structured("s", "u", {}, "plan")
        assert False
    except ProviderError as exc:
        assert exc.code == ErrorCode.BAD_REQUEST


def test_local_model_api(client):
    s = client.get("/api/local-model").json()
    assert {c["id"] for c in s["catalog"]} >= {"qwen2.5-1.5b-gpu", "qwen2.5-1.5b-cpu"} and s["use"] == "off"
    assert client.post("/api/local-model/install", json={"model": "nope"}).status_code == 404
    assert client.put("/api/local-model/use", json={"use": "main"}).status_code == 400
    # register as the install would, then switch how it is used
    st = client.app.state.aos
    st._register_local("qwen2.5-1.5b-cpu")
    assert client.get("/api/local-model").json()["use"] in ("backup", "main")
    assert client.put("/api/local-model/use", json={"use": "main"}).json()["use"] == "main"
    assert client.put("/api/local-model/use", json={"use": "off"}).json()["use"] == "off"
    # with no other provider, the backup is also the main model
    assert client.put("/api/local-model/use", json={"use": "backup"}).json()["use"] in ("backup", "main")
    # the chain: default provider first, local as the backup
    chain = st.llm_provider()
    kinds = [p.kind for p in getattr(chain, "providers", [chain])]
    assert kinds[-1] == "local"
