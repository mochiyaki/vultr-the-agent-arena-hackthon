"""Blast Radius Zero control plane - FastAPI app served from the Vultr VM."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .agent import Agent
from .config import settings
from .llm import VultrInference
from .sandbox import describe_driver
from .store import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("brz")

app = FastAPI(title="Blast Radius Zero", version="0.1.0")
STATIC = Path(__file__).parent / "static"
_llm = VultrInference()
_agent = Agent(_llm)
_vultr_meta: dict = {}
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))
_sem = asyncio.Semaphore(MAX_CONCURRENT)


class TaskIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)


@app.on_event("startup")
async def _startup() -> None:
    global _vultr_meta
    # Vultr instance metadata service (only reachable from a Vultr VM). Best-effort.
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            r = await c.get("http://169.254.169.254/v1.json")
            if r.status_code == 200:
                j = r.json()
                _vultr_meta = {"instance_id": j.get("instance-v2-id") or j.get("instanceid"),
                               "region": (j.get("region") or {}).get("regioncode"),
                               "hostname": j.get("hostname")}
    except Exception:
        _vultr_meta = {}
    log.info("sandbox driver: %s", json.dumps(describe_driver(), default=str))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/system")
async def system() -> dict:
    return {
        "inference": {"provider": "Vultr Serverless Inference", "base_url": settings.vultr_inference_base_url,
                      "model": settings.vultr_inference_model, "configured": bool(settings.vultr_inference_api_key)},
        "sandbox": describe_driver(),
        "vultr_instance": _vultr_meta or {"note": "metadata service unavailable (not on a Vultr VM?)"},
        "limits": {"max_steps": settings.agent_max_steps, "max_actions_per_step": settings.agent_max_actions_per_step,
                   "max_concurrent_tasks": MAX_CONCURRENT},
        "time": time.time(),
    }


@app.get("/api/models")
async def models() -> dict:
    """Live list of text models available on Vultr Serverless Inference (public endpoint)."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{settings.vultr_inference_base_url}/models")
            r.raise_for_status()
            data = r.json().get("data", [])
    except Exception as exc:
        raise HTTPException(502, f"could not list models: {exc}")
    out = []
    for m in data:
        text_out = [o for o in m.get("output_modalities", []) if o.get("type") == "text"]
        if text_out and "tools" in text_out[0].get("supported_parameters", {}):
            out.append({"id": m["id"], "name": m.get("name")})
    return {"models": out, "current": settings.vultr_inference_model}


@app.post("/api/tasks", status_code=202)
async def create_task(body: TaskIn) -> dict:
    task = store.create(body.prompt.strip())
    task.emit("status", status="queued", message="Task accepted by control plane")

    async def _run():
        async with _sem:
            await _agent.run_task(task)

    asyncio.create_task(_run())
    return {"id": task.id, "status": task.status}


@app.get("/api/tasks")
async def list_tasks() -> dict:
    return {"tasks": [t.summary() for t in store.all()]}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return task.report()


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str):
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    queue = task.subscribe()

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": ev["type"], "id": str(ev["seq"]), "data": json.dumps(ev, default=str)}
                if ev["type"] == "status" and ev.get("message") == "finished":
                    break
        finally:
            task.unsubscribe(queue)

    return EventSourceResponse(gen())


@app.get("/api/tasks/{task_id}/workspace.tar")
async def download_workspace(task_id: str):
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if not task.workspace_tar:
        raise HTTPException(404, "workspace not available (task still running or export failed)")
    return StreamingResponse(io.BytesIO(task.workspace_tar), media_type="application/x-tar",
                             headers={"Content-Disposition": f'attachment; filename="{task_id}-workspace.tar"'})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})
