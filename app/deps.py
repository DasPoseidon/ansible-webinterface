"""Gemeinsame Dienste und Zugriffspruefungen fuer alle Routen."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, WebSocket, status

from .audit import AuditLog
from .auth import CSRF_HEADER, SESSION_COOKIE, AuthStore, Principal
from .bus import Broadcaster
from .config import Config
from .crontab import CrontabService
from .files import FileService
from .gitrepo import GitRepo
from .inventory import InventoryService
from .playbooks import PlaybookService
from .runner import RunManager
from .validate import Validator

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CRON_TOKEN_NAME = "cron-client"


class Services:
    """Alle Dienste der Anwendung, einmal angelegt beim Start."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.auth = AuthStore(cfg.auth_file, cfg.sessions_file,
                              ttl_hours=cfg.server.get("session_ttl_hours", 12),
                              idle_hours=cfg.server.get("session_idle_hours", 8))
        self.audit = AuditLog(cfg.audit_file)
        self.bus = Broadcaster()
        self.runs = RunManager(cfg, self.bus, audit=self.audit)
        self.inventory = InventoryService(cfg)
        self.playbooks = PlaybookService(cfg)
        self.validator = Validator(cfg)
        self.git = GitRepo(cfg)
        self.files = FileService(cfg, self.validator, self.git)
        self.cron = CrontabService(cfg)

    # -- Datei fuer den Cron-Client ---------------------------------------

    def write_client_file(self) -> Optional[Path]:
        """Legt die Angaben ab, die ``bin/ansible-web-run`` braucht.

        Darin steht auch ein eigenes API-Token mit der Rolle 'operator' -
        gerade genug, um Laeufe zu starten, aber nichts zu veraendern.
        """
        cfg = self.cfg
        token: Optional[str] = None
        target = cfg.state_dir / "client.json"
        existing: Dict[str, Any] = {}
        if target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
        known = {entry["name"] for entry in self.auth.list_api_tokens()}
        if CRON_TOKEN_NAME in known and existing.get("token"):
            token = existing["token"]
        else:
            for entry in self.auth.list_api_tokens():
                if entry["name"] == CRON_TOKEN_NAME:
                    self.auth.delete_api_token(entry["id"])
            token = self.auth.create_api_token(CRON_TOKEN_NAME, "operator")

        payload = {
            "api_url": cfg.cron_api_url,
            "token": token,
            "ansible_dir": str(cfg.ansible_dir),
            "inventory": str(cfg.inventory),
            "ansible_playbook": cfg.ansible_playbook,
            "callback_dir": str(cfg.callback_dir),
            "runs_dir": str(cfg.runs_dir),
            "vault_password_file": cfg.run_cfg.get("vault_password_file") or "",
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
            finally:
                os.close(fd)
            pointer = cfg.package_dir / "bin" / ".client-path"
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(str(target) + "\n", encoding="utf-8")
            return target
        except OSError:
            return None


_services: Optional[Services] = None


def set_services(value: Optional[Services]) -> None:
    global _services
    _services = value


def get_services() -> Services:
    if _services is None:                                   # pragma: no cover
        raise HTTPException(status_code=503, detail="Dienst startet noch")
    return _services


def client_ip(request: Request) -> str:
    services = get_services()
    if services.cfg.server.get("behind_proxy"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


# -- Anmeldung pruefen -----------------------------------------------------


def _principal_from_request(request: Request) -> Optional[Principal]:
    services = get_services()
    header = request.headers.get("x-api-token") or ""
    if not header:
        authorization = request.headers.get("authorization") or ""
        if authorization.lower().startswith("bearer "):
            header = authorization[7:].strip()
    if header:
        principal = services.auth.resolve_api_token(header)
        if principal is not None:
            return principal
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="API-Token ist unbekannt oder abgelaufen")
    return services.auth.resolve_session(request.cookies.get(SESSION_COOKIE))


async def principal(request: Request) -> Principal:
    found = _principal_from_request(request)
    if found is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Nicht angemeldet")
    if found.kind == "session" and request.method not in SAFE_METHODS:
        # Ausschliesslich der Header zaehlt. Den Cookie-Wert hier zu
        # akzeptieren wuerde den Schutz aufheben, denn Cookies sendet der
        # Browser auch bei Anfragen von fremden Seiten automatisch mit.
        sent = request.headers.get(CSRF_HEADER) or ""
        if not found.csrf_token or not secrets.compare_digest(sent, found.csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Sicherheitsmerkmal (CSRF) fehlt oder passt nicht. "
                                       "Bitte Seite neu laden.")
    request.state.principal = found
    return found


def require(capability: str) -> Callable:
    """Erzeugt eine Abhaengigkeit, die eine bestimmte Berechtigung verlangt."""

    async def dependency(request: Request) -> Principal:
        found = await principal(request)
        if not found.can(capability):
            get_services().audit.write(found.name, "zugriff.verweigert", capability,
                                       ok=False, ip=client_ip(request))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Dafuer fehlt die Berechtigung (Rolle: %s)" % found.role)
        return found

    return dependency


async def websocket_principal(websocket: WebSocket, capability: str = "runs.view") -> Optional[Principal]:
    """Prueft Anmeldung und Herkunft einer WebSocket-Verbindung."""
    services = get_services()
    origin = websocket.headers.get("origin")
    if origin:
        host = websocket.headers.get("host", "")
        try:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != host:
                await websocket.close(code=4403)
                return None
        except ValueError:
            await websocket.close(code=4403)
            return None
    found = services.auth.resolve_session(websocket.cookies.get(SESSION_COOKIE))
    if found is None:
        token = websocket.query_params.get("token") or ""
        found = services.auth.resolve_api_token(token) if token else None
    if found is None or not found.can(capability):
        await websocket.close(code=4401)
        return None
    return found
