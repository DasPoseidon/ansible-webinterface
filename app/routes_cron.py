"""Verwaltung der Ansible-Cron-Jobs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .auth import Principal
from .crontab import CronError, CronJob, describe_schedule, next_runs, validate_schedule
from .deps import client_ip, get_services, require

router = APIRouter(prefix="/api/cron", tags=["Cron"])


class JobBody(BaseModel):
    name: str = Field(default="", max_length=120)
    schedule: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    playbook: str = Field(min_length=1, max_length=1024)
    limit: str = Field(default="", max_length=4096)
    tags: List[str] = Field(default_factory=list, max_length=100)
    skip_tags: List[str] = Field(default_factory=list, max_length=100)
    check: bool = False
    diff: bool = False
    extra_vars: str = Field(default="", max_length=100_000)
    forks: Optional[int] = Field(default=None, ge=1, le=500)
    verbosity: int = Field(default=0, ge=0, le=4)

    def to_job(self, job_id: str = "") -> CronJob:
        return CronJob(id=job_id, **self.model_dump())


class EnabledBody(BaseModel):
    enabled: bool


class ImportBody(BaseModel):
    raw: str = Field(min_length=1, max_length=4096)


class RawBody(BaseModel):
    content: str = Field(max_length=500_000)


class ScheduleBody(BaseModel):
    schedule: str = Field(min_length=1, max_length=200)


def _fail(exc: CronError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("")
async def list_jobs(found: Principal = Depends(require("cron.view"))) -> Dict[str, Any]:
    services = get_services()
    try:
        data = await services.cron.list_jobs()
    except CronError as exc:
        return {"jobs": [], "unmanaged": [], "error": str(exc),
                "cli": str(services.cfg.cron_cli), "log_file": str(services.cfg.cron_log)}
    data["error"] = None
    data["can_edit"] = found.can("cron.write")
    return data


@router.post("/validate")
async def check_schedule(body: ScheduleBody,
                         found: Principal = Depends(require("cron.view"))) -> Dict[str, Any]:
    ok, error = validate_schedule(body.schedule)
    return {"ok": ok, "error": error,
            "description": describe_schedule(body.schedule) if ok else "",
            "next_runs": next_runs(body.schedule, 5) if ok else []}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_job(body: JobBody, request: Request,
                     found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        result = await services.cron.create_job(body.to_job(), user=found.name)
    except CronError as exc:
        raise _fail(exc)
    services.audit.write(found.name, "cron.angelegt", body.playbook,
                         detail={"id": result.get("id"), "schedule": body.schedule,
                                 "name": body.name}, ip=client_ip(request))
    return result


@router.put("/{job_id}")
async def update_job(job_id: str, body: JobBody, request: Request,
                     found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        result = await services.cron.update_job(job_id, body.to_job(job_id), user=found.name)
    except CronError as exc:
        raise _fail(exc)
    services.audit.write(found.name, "cron.geaendert", body.playbook,
                         detail={"id": job_id, "schedule": body.schedule},
                         ip=client_ip(request))
    return result


@router.delete("/{job_id}")
async def delete_job(job_id: str, request: Request,
                     found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        result = await services.cron.delete_job(job_id, user=found.name)
    except CronError as exc:
        raise _fail(exc)
    services.audit.write(found.name, "cron.geloescht", job_id, ip=client_ip(request))
    return result


@router.post("/{job_id}/enabled")
async def set_enabled(job_id: str, body: EnabledBody, request: Request,
                      found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        result = await services.cron.set_enabled(job_id, body.enabled, user=found.name)
    except CronError as exc:
        raise _fail(exc)
    services.audit.write(found.name,
                         "cron.aktiviert" if body.enabled else "cron.deaktiviert",
                         job_id, ip=client_ip(request))
    return result


@router.post("/import")
async def import_line(body: ImportBody, request: Request,
                      found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        result = await services.cron.import_line(body.raw, user=found.name)
    except CronError as exc:
        raise _fail(exc)
    services.audit.write(found.name, "cron.uebernommen", body.raw[:200],
                         detail={"id": result.get("id")}, ip=client_ip(request))
    return result


@router.get("/raw")
async def read_raw(found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        return {"content": await services.cron.read_raw()}
    except CronError as exc:
        raise _fail(exc)


@router.put("/raw")
async def write_raw(body: RawBody, request: Request,
                    found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    """Die komplette crontab als Text ersetzen - mit Sicherungskopie."""
    services = get_services()
    problems: List[str] = []
    for number, line in enumerate(body.content.splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#") or "=" in text.split()[0]:
            continue
        parsed = services.cron._parse_job_line(line)
        if parsed is None:
            problems.append("Zeile %d sieht nicht wie ein Cron-Eintrag aus: %s"
                            % (number, text[:60]))
            continue
        ok, error = validate_schedule(parsed[0])
        if not ok:
            problems.append("Zeile %d: %s" % (number, error))
    if problems:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="; ".join(problems[:6]))
    try:
        result = await services.cron.write_raw(body.content, user=found.name)
    except CronError as exc:
        raise _fail(exc)
    services.audit.write(found.name, "cron.rohtext.gespeichert", "crontab",
                         detail={"backup": result.get("backup")}, ip=client_ip(request))
    return result


@router.get("/backups")
async def list_backups(found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    services = get_services()
    return {"backups": await services.cron.list_backups()}


@router.get("/backups/{name}")
async def read_backup(name: str,
                      found: Principal = Depends(require("cron.write"))) -> Dict[str, Any]:
    services = get_services()
    try:
        return {"name": name, "content": await services.cron.read_backup(name)}
    except CronError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
