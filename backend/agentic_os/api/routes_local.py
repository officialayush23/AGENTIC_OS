"""Local AI model: install a quantized Qwen2.5 (ONNX Runtime GenAI) and choose how it is used.

GET    /api/local-model              runtime, installed models, catalog, install progress, how it is used
POST   /api/local-model/install      {"model": id}  install runtime + download + register + self-test
POST   /api/local-model/cancel       stop a download
POST   /api/local-model/test         run the connection test again
PUT    /api/local-model/use          {"use": "backup" | "main" | "off"}
DELETE /api/local-model/{model_id}   delete the files
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel


class InstallIn(BaseModel):
    model: str


class UseIn(BaseModel):
    use: Literal["backup", "main", "off"]


def register(api: APIRouter, app: FastAPI, st, ApiError) -> None:
    def usage() -> dict:
        rec = st.providers.repo.get("local")
        default = st.providers.resolve_record(None)
        if not rec or not rec.get("enabled", True):
            use = "off"
        elif default and default.get("id") == "local":
            use = "main"
        else:
            use = "backup" if st.user_settings().get("local_fallback", True) else "off"
        return {"provider": st.providers.public(rec) if rec else None, "use": use}

    @api.get("/local-model")
    async def local_status():
        return {**st.local_model.status(), **usage()}

    @api.post("/local-model/install")
    async def local_install(body: InstallIn):
        try:
            return st.local_model.install(body.model)
        except KeyError:
            raise ApiError(404, "MODEL_UNKNOWN", f"Unknown local model '{body.model}'.")

    @api.post("/local-model/cancel")
    async def local_cancel():
        st.local_model.cancel()
        return {"ok": True}

    @api.post("/local-model/test")
    async def local_test():
        if not st.providers.repo.get("local"):
            raise ApiError(400, "LOCAL_NOT_INSTALLED", "Install a local model first.")
        return await asyncio.to_thread(st.providers.test, "local")

    @api.put("/local-model/use")
    async def local_use(body: UseIn):
        rec = st.providers.repo.get("local")
        if rec is None:
            raise ApiError(400, "LOCAL_NOT_INSTALLED", "Install a local model first.")
        st.repos.settings.set("local_fallback", body.use != "off")
        if rec.get("enabled", True) != (body.use != "off"):
            st.providers.save("local", {"enabled": body.use != "off"})
        if body.use == "main":
            st.providers.set_default("local")
        else:
            default = st.providers.resolve_record(None)
            if default and default.get("id") == "local":
                other = next((r for r in st.providers.repo.list() if r["id"] != "local" and r.get("enabled", True)), None)
                if other:
                    st.providers.set_default(other["id"])
        return usage()

    @api.delete("/local-model/{model_id}")
    async def local_delete(model_id: str):
        rec = st.providers.repo.get("local")
        if rec and rec.get("model") == model_id:
            default = st.providers.resolve_record(None)
            if default and default.get("id") == "local":
                other = next((r for r in st.providers.repo.list() if r["id"] != "local"), None)
                if other:
                    st.providers.set_default(other["id"])
            st.providers.delete("local")
        st.local_model.remove(model_id)
        return {"ok": True}
