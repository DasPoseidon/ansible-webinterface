"""Benutzerverwaltung, API-Tokens, Audit-Log und Systemzustand."""

from __future__ import annotations

import shutil
import sys
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .auth import ROLES, AuthError, Principal
from .deps import client_ip, get_services, require

router = APIRouter(prefix="/api", tags=["Verwaltung"])


class UserCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=10, max_length=512)
    role: str = "viewer"


class UserUpdateBody(BaseModel):
    password: Optional[str] = Field(default=None, min_length=10, max_length=512)
    role: Optional[str] = None
    disabled: Optional[bool] = None


class TokenCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: str = "operator"


@router.get("/status")
async def status_overview(found: Principal = Depends(require("system.view"))) -> Dict[str, Any]:
    services = get_services()
    cfg = services.cfg
    active = services.runs.active_runs()
    git_info: Dict[str, Any] = {"repo": False}
    try:
        git_info = await services.git.info()
    except Exception as exc:                                  # pragma: no cover
        git_info = {"repo": False, "error": str(exc)}
    return {
        "config": cfg.to_public_dict(),
        "problems": cfg.validate(),
        "config_file": str(cfg.source) if cfg.source else None,
        "state_dir": str(cfg.state_dir),
        "ansible_playbook": cfg.ansible_playbook,
        "python": sys.version.split()[0],
        "crontab_available": bool(shutil.which(cfg.cron_cfg.get("crontab_command") or "crontab")),
        "tls": services.tls_info,
        "base_url": cfg.base_url,
        "git": git_info,
        "runs": {
            "active": [run.meta() for run in active],
            "total": len(services.runs.runs),
            "max_concurrent": cfg.max_concurrent,
            "keep": cfg.keep_runs,
        },
    }


@router.get("/users")
async def list_users(found: Principal = Depends(require("users.manage"))) -> Dict[str, Any]:
    services = get_services()
    return {"users": services.auth.list_users(), "roles": list(ROLES)}


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreateBody, request: Request,
                      found: Principal = Depends(require("users.manage"))) -> Dict[str, Any]:
    services = get_services()
    try:
        created = services.auth.add_user(body.name, body.password, body.role)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    services.audit.write(found.name, "benutzer.angelegt", body.name,
                         detail={"role": body.role}, ip=client_ip(request))
    return created


@router.put("/users/{name}")
async def update_user(name: str, body: UserUpdateBody, request: Request,
                      found: Principal = Depends(require("users.manage"))) -> Dict[str, Any]:
    services = get_services()
    try:
        services.auth.update_user(name, password=body.password, role=body.role,
                                  disabled=body.disabled)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    changed = [key for key, value in body.model_dump().items() if value is not None]
    services.audit.write(found.name, "benutzer.geaendert", name,
                         detail={"felder": changed}, ip=client_ip(request))
    return {"ok": True}


@router.delete("/users/{name}")
async def delete_user(name: str, request: Request,
                      found: Principal = Depends(require("users.manage"))) -> Dict[str, Any]:
    services = get_services()
    try:
        services.auth.delete_user(name)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    services.audit.write(found.name, "benutzer.geloescht", name, ip=client_ip(request))
    return {"ok": True}


@router.post("/users/{name}/sessions/clear")
async def clear_sessions(name: str, request: Request,
                         found: Principal = Depends(require("users.manage"))) -> Dict[str, Any]:
    services = get_services()
    count = services.auth.destroy_all_sessions(name)
    services.audit.write(found.name, "benutzer.sitzungen.beendet", name,
                         detail={"anzahl": count}, ip=client_ip(request))
    return {"ok": True, "beendet": count}


@router.get("/tokens")
async def list_tokens(found: Principal = Depends(require("users.manage"))) -> Dict[str, Any]:
    services = get_services()
    return {"tokens": services.auth.list_api_tokens()}


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_token(body: TokenCreateBody, request: Request,
                       found: Principal = Depends(require("users.manage"))) -> Dict[str, Any]:
    services = get_services()
    try:
        token = services.auth.create_api_token(body.name, body.role)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    services.audit.write(found.name, "token.angelegt", body.name,
                         detail={"role": body.role}, ip=client_ip(request))
    return {"token": token,
            "note": "Dieses Token wird nur jetzt angezeigt. Bitte sicher notieren."}


@router.delete("/tokens/{token_id}")
async def delete_token(token_id: str, request: Request,
                       found: Principal = Depends(require("users.manage"))) -> Dict[str, Any]:
    services = get_services()
    try:
        services.auth.delete_api_token(token_id)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    services.audit.write(found.name, "token.geloescht", token_id, ip=client_ip(request))
    return {"ok": True}


@router.get("/audit")
async def audit_log(limit: int = 300, user: Optional[str] = None, action: Optional[str] = None,
                    found: Principal = Depends(require("audit.view"))) -> Dict[str, Any]:
    services = get_services()
    return {"entries": services.audit.tail(limit, user=user, action=action)}
