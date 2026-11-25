"""Anmeldung, Abmeldung und Selbstauskunft."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .auth import (CSRF_COOKIE, ROLE_LABELS, ROLES, SESSION_COOKIE, AuthError,
                   Principal, verify_password)
from .deps import client_ip, get_services, principal

router = APIRouter(prefix="/api", tags=["Anmeldung"])


class LoginBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class SetupBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=10, max_length=512)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=10, max_length=512)


def _set_session_cookies(response: Response, found: Principal, secure: bool) -> None:
    response.set_cookie(SESSION_COOKIE, found.session_token or "", httponly=True,
                        samesite="lax", secure=secure, path="/")
    response.set_cookie(CSRF_COOKIE, found.csrf_token or "", httponly=False,
                        samesite="lax", secure=secure, path="/")


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.get("/me")
async def me(request: Request) -> Dict[str, Any]:
    """Wer ist angemeldet - und was darf er? Ohne Anmeldung kein Fehler."""
    services = get_services()
    from .deps import _principal_from_request
    found = _principal_from_request(request)
    payload: Dict[str, Any] = {
        "authenticated": found is not None,
        "needs_setup": not services.auth.has_users(),
        "config": services.cfg.to_public_dict(),
        "roles": [{"name": name, "label": ROLE_LABELS[name]} for name in ROLES],
    }
    if found is not None:
        payload["user"] = found.to_public()
        payload["csrf_token"] = found.csrf_token
    return payload


@router.post("/setup")
async def setup(body: SetupBody, request: Request, response: Response) -> Dict[str, Any]:
    """Legt den ersten Administrator an - nur solange es keinen Benutzer gibt."""
    services = get_services()
    if services.auth.has_users():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Es gibt bereits Benutzer. Die Ersteinrichtung ist "
                                   "abgeschlossen.")
    try:
        services.auth.add_user(body.name, body.password, "admin")
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    found = services.auth.authenticate(body.name, body.password, ip=client_ip(request))
    found = services.auth.create_session(found, ip=client_ip(request),
                                         user_agent=request.headers.get("user-agent", ""))
    _set_session_cookies(response, found, bool(services.cfg.server.get("secure_cookies")))
    services.audit.write(body.name, "einrichtung.admin", body.name, ip=client_ip(request))
    return {"user": found.to_public(), "csrf_token": found.csrf_token}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> Dict[str, Any]:
    services = get_services()
    ip = client_ip(request)
    try:
        found = services.auth.authenticate(body.name, body.password, ip=ip)
    except AuthError as exc:
        services.audit.write(body.name, "anmeldung.fehlgeschlagen", "", ok=False, ip=ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    found = services.auth.create_session(found, ip=ip,
                                         user_agent=request.headers.get("user-agent", ""))
    _set_session_cookies(response, found, bool(services.cfg.server.get("secure_cookies")))
    services.audit.write(found.name, "anmeldung", "", ip=ip)
    return {"user": found.to_public(), "csrf_token": found.csrf_token}


@router.post("/logout")
async def logout(request: Request, response: Response,
                 found: Principal = Depends(principal)) -> Dict[str, Any]:
    services = get_services()
    services.auth.destroy_session(found.session_token)
    _clear_session_cookies(response)
    services.audit.write(found.name, "abmeldung", "", ip=client_ip(request))
    return {"ok": True}


@router.post("/password")
async def change_password(body: PasswordBody, request: Request,
                          found: Principal = Depends(principal)) -> Dict[str, Any]:
    """Eigenes Passwort aendern. Alle anderen Sitzungen werden beendet."""
    services = get_services()
    if found.kind != "session":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Das geht nur fuer angemeldete Benutzer, nicht fuer Tokens")
    try:
        services.auth.authenticate(found.name, body.current_password, ip=client_ip(request))
    except AuthError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Das aktuelle Passwort ist falsch")
    try:
        services.auth.update_user(found.name, password=body.new_password)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    services.audit.write(found.name, "passwort.geaendert", found.name, ip=client_ip(request))
    return {"ok": True, "note": "Bitte neu anmelden - alle Sitzungen wurden beendet."}
