"""Playbooks, Inventory und Laeufe - inklusive Live-Uebertragung."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, HTTPException, Query, Request, WebSocket,
                     WebSocketDisconnect, status)
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import Principal
from .deps import client_ip, get_services, require, websocket_principal
from .runner import RunRequest, RunError

router = APIRouter(tags=["Laeufe"])


class RunBody(BaseModel):
    playbook: str = Field(min_length=1, max_length=1024)
    limit: str = Field(default="", max_length=4096)
    tags: List[str] = Field(default_factory=list, max_length=100)
    skip_tags: List[str] = Field(default_factory=list, max_length=100)
    check: bool = False
    diff: bool = False
    extra_vars: str = Field(default="", max_length=200_000)
    verbosity: int = Field(default=0, ge=0, le=4)
    forks: Optional[int] = Field(default=None, ge=1, le=500)
    source: str = Field(default="web", max_length=32)
    label: str = Field(default="", max_length=200)


# -- Playbooks -------------------------------------------------------------


@router.get("/api/playbooks")
async def list_playbooks(refresh: bool = False,
                         found: Principal = Depends(require("runs.view"))) -> Dict[str, Any]:
    services = get_services()
    return {"playbooks": services.playbooks.discover(force=refresh),
            "ansible_dir": str(services.cfg.ansible_dir),
            "globs": services.cfg.discovery_cfg.get("playbook_globs") or [],
            "excluded": services.cfg.discovery_cfg.get("exclude_dirs") or []}


@router.get("/api/playbooks/preview")
async def preview_playbook(playbook: str, limit: str = "", tags: str = "", skip_tags: str = "",
                           found: Principal = Depends(require("runs.view"))) -> Dict[str, Any]:
    """Zeigt vor dem Start, welche Hosts und Schritte betroffen waeren."""
    services = get_services()
    try:
        services.runs.resolve_playbook(playbook)
    except RunError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    tasks, hosts = await asyncio.gather(
        services.playbooks.list_tasks(
            playbook, limit=limit,
            tags=[t for t in tags.split(",") if t],
            skip_tags=[t for t in skip_tags.split(",") if t]),
        services.playbooks.list_hosts(playbook, limit=limit),
    )
    return {"tasks": tasks, "hosts": hosts}


@router.get("/api/playbooks/tags")
async def playbook_tags(playbook: str,
                        found: Principal = Depends(require("runs.view"))) -> Dict[str, Any]:
    services = get_services()
    return await services.playbooks.list_tags(playbook)


@router.post("/api/playbooks/syntax-check")
async def playbook_syntax(body: Dict[str, str],
                          found: Principal = Depends(require("runs.view"))) -> Dict[str, Any]:
    services = get_services()
    playbook = (body or {}).get("playbook", "")
    try:
        services.runs.resolve_playbook(playbook)
    except RunError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return await services.playbooks.syntax_check(playbook)


# -- Inventory -------------------------------------------------------------


@router.get("/api/inventory")
async def inventory(refresh: bool = False,
                    found: Principal = Depends(require("inventory.view"))) -> Dict[str, Any]:
    services = get_services()
    return await services.inventory.get(force=refresh)


@router.get("/api/inventory/resolve")
async def resolve_limit(limit: str = "",
                        found: Principal = Depends(require("inventory.view"))) -> Dict[str, Any]:
    services = get_services()
    return await services.inventory.resolve_limit(limit)


# -- Laeufe ----------------------------------------------------------------


@router.get("/api/runs")
async def list_runs(limit: int = Query(default=100, ge=1, le=1000),
                    status_filter: Optional[str] = Query(default=None, alias="status"),
                    playbook: Optional[str] = None, source: Optional[str] = None,
                    found: Principal = Depends(require("runs.view"))) -> Dict[str, Any]:
    services = get_services()
    return {"runs": services.runs.list_runs(limit, status=status_filter,
                                            playbook=playbook, source=source),
            "active": len(services.runs.active_runs())}


@router.post("/api/runs", status_code=status.HTTP_201_CREATED)
async def start_run(body: RunBody, request: Request,
                    found: Principal = Depends(require("runs.start"))) -> Dict[str, Any]:
    services = get_services()
    data = body.model_dump()
    data["user"] = found.name
    data["source"] = "cron" if found.kind == "token" and body.source == "cron" else (
        "api" if found.kind == "token" else "web")
    try:
        run = await services.runs.submit(RunRequest(**data))
    except RunError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    services.audit.write(found.name, "lauf.gestartet", run.request.playbook,
                         detail={"run_id": run.id, "limit": run.request.limit,
                                 "check": run.request.check, "tags": run.request.tags,
                                 "quelle": run.request.source},
                         ip=client_ip(request))
    return {"id": run.id, "meta": run.meta()}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str,
                  found: Principal = Depends(require("runs.view"))) -> Dict[str, Any]:
    services = get_services()
    run = services.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lauf nicht gefunden")
    return run.snapshot()


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request,
                     found: Principal = Depends(require("runs.cancel"))) -> Dict[str, Any]:
    services = get_services()
    run = services.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lauf nicht gefunden")
    ok = await services.runs.cancel(run_id, by=found.name)
    if not ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Dieser Lauf ist nicht mehr aktiv")
    services.audit.write(found.name, "lauf.abgebrochen", run.request.playbook,
                         detail={"run_id": run_id}, ip=client_ip(request))
    return {"ok": True, "meta": run.meta()}


@router.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, request: Request,
                     found: Principal = Depends(require("runs.cancel"))) -> Dict[str, Any]:
    services = get_services()
    if not services.runs.delete(run_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Lauf nicht gefunden oder noch aktiv")
    services.audit.write(found.name, "lauf.geloescht", run_id, ip=client_ip(request))
    return {"ok": True}


@router.get("/api/runs/{run_id}/log")
async def run_log(run_id: str, download: bool = False,
                  found: Principal = Depends(require("runs.view"))):
    services = get_services()
    run = services.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lauf nicht gefunden")
    if not run.log_file.is_file():
        return PlainTextResponse("", media_type="text/plain; charset=utf-8")
    if download:
        return FileResponse(str(run.log_file), media_type="text/plain; charset=utf-8",
                            filename="ansible-%s.log" % run_id)
    return FileResponse(str(run.log_file), media_type="text/plain; charset=utf-8")


@router.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, task: Optional[str] = None, host: Optional[str] = None,
                     limit: int = Query(default=2000, ge=1, le=100000),
                     found: Principal = Depends(require("runs.view"))) -> Dict[str, Any]:
    """Rohereignisse eines Laufs, optional nach Task oder Host gefiltert."""
    services = get_services()
    run = services.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lauf nicht gefunden")
    events: List[Dict[str, Any]] = []
    try:
        with run.event_file.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if task and event.get("task_uuid") != task:
                    continue
                if host and event.get("host") != host:
                    continue
                events.append(event)
                if len(events) >= limit:
                    break
    except OSError:
        pass
    return {"events": events, "count": len(events)}


@router.get("/api/runs/{run_id}/follow")
async def follow_run(run_id: str, found: Principal = Depends(require("runs.view"))):
    """Textstrom der Logausgabe bis zum Ende des Laufs (fuer den Cron-Client)."""
    services = get_services()
    run = services.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lauf nicht gefunden")

    async def generate():
        topic = "run:%s" % run_id
        queue = services.bus.subscribe(topic)
        try:
            for line in list(run.log_tail):
                yield (line + "\n").encode("utf-8")
            if not run.is_active:
                return
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    if not run.is_active:
                        return
                    continue
                kind = message.get("type")
                if kind == "log":
                    for line in message.get("lines", []):
                        yield (line + "\n").encode("utf-8")
                elif kind == "status":
                    if (message.get("meta") or {}).get("status") not in ("queued", "running"):
                        return
                elif kind == "closed":
                    return
        finally:
            services.bus.unsubscribe(topic, queue)

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


# -- Live-Verbindungen -----------------------------------------------------


async def _pump(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Schickt Nachrichten an den Client und erkennt dabei Verbindungsabbruch."""
    receiver = asyncio.create_task(websocket.receive_text())
    sender: Optional[asyncio.Task] = None
    try:
        while True:
            if sender is None:
                sender = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({receiver, sender},
                                         return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                return
            if sender in done:
                message = sender.result()
                sender = None
                await websocket.send_text(json.dumps(message, ensure_ascii=False, default=str))
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        return
    finally:
        for task in (receiver, sender):
            if task is not None and not task.done():
                task.cancel()


@router.websocket("/ws/runs")
async def websocket_runs(websocket: WebSocket) -> None:
    found = await websocket_principal(websocket, "runs.view")
    if found is None:
        return
    services = get_services()
    await websocket.accept()
    queue = services.bus.subscribe("runs")
    try:
        await websocket.send_text(json.dumps({
            "type": "init",
            "runs": services.runs.list_runs(100),
        }, ensure_ascii=False, default=str))
        await _pump(websocket, queue)
    finally:
        services.bus.unsubscribe("runs", queue)


@router.websocket("/ws/runs/{run_id}")
async def websocket_run(websocket: WebSocket, run_id: str) -> None:
    found = await websocket_principal(websocket, "runs.view")
    if found is None:
        return
    services = get_services()
    run = services.runs.get(run_id)
    await websocket.accept()
    if run is None:
        await websocket.send_text(json.dumps({"type": "error",
                                              "message": "Lauf nicht gefunden"}))
        await websocket.close()
        return
    topic = "run:%s" % run_id
    queue = services.bus.subscribe(topic)
    try:
        snapshot = run.snapshot()
        snapshot["type"] = "init"
        await websocket.send_text(json.dumps(snapshot, ensure_ascii=False, default=str))
        await _pump(websocket, queue)
    finally:
        services.bus.unsubscribe(topic, queue)
