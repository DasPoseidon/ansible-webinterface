"""Einstiegspunkt des Webinterfaces."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigError, load_config
from .deps import Services, get_services, set_services
from . import routes_admin, routes_auth, routes_cron, routes_files, routes_runs

logger = logging.getLogger("ansible-web")

BANNER = """
Ansible-Webinterface
  Ansible-Verzeichnis : %s
  Inventory           : %s
  Daten-/Konfigordner : %s
  Zustandsverzeichnis : %s
  Adresse             : http://%s:%s
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(Path(os.environ["ANSIBLE_WEB_CONFIG"])
                      if os.environ.get("ANSIBLE_WEB_CONFIG") else None)
    services = Services(cfg)
    set_services(services)

    logger.info(BANNER, cfg.ansible_dir, cfg.inventory, cfg.data_dir, cfg.state_dir,
                cfg.server["host"], cfg.server["port"])
    for problem in cfg.validate():
        logger.warning("Konfiguration: %s", problem)
    if not services.auth.has_users():
        logger.warning("Es gibt noch keinen Benutzer - die Oberflaeche fuehrt durch "
                       "die Ersteinrichtung.")

    await services.runs.start()
    client_file = services.write_client_file()
    if client_file:
        logger.info("Angaben fuer den Cron-Client liegen in %s", client_file)
    else:
        logger.warning("Die Datei client.json konnte nicht geschrieben werden - "
                       "Cron-Jobs koennen den Dienst dann nicht ansprechen.")
    try:
        yield
    finally:
        await services.runs.stop()
        set_services(None)


app = FastAPI(title="Ansible-Webinterface", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

app.include_router(routes_auth.router)
app.include_router(routes_admin.router)
app.include_router(routes_runs.router)
app.include_router(routes_files.router)
app.include_router(routes_cron.router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; img-src 'self' data:; connect-src 'self' ws: wss:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    """Einfacher Lebenszeichen-Endpunkt, ohne Anmeldung."""
    return {"ok": True}


@app.get("/")
async def index():
    target = STATIC_DIR / "index.html"
    if not target.is_file():
        return JSONResponse({"detail": "Oberflaeche nicht gefunden (static/index.html fehlt)"},
                            status_code=500)
    return FileResponse(str(target), media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
async def favicon():
    target = STATIC_DIR / "favicon.svg"
    if target.is_file():
        return FileResponse(str(target), media_type="image/svg+xml")
    return JSONResponse({}, status_code=404)


def main() -> None:
    """Startet den Dienst mit uvicorn (fuer 'python -m app.main')."""
    import uvicorn

    try:
        cfg = load_config(Path(os.environ["ANSIBLE_WEB_CONFIG"])
                          if os.environ.get("ANSIBLE_WEB_CONFIG") else None)
    except ConfigError as exc:
        raise SystemExit("Konfiguration fehlerhaft: %s" % exc)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    uvicorn.run("app.main:app", host=str(cfg.server["host"]), port=int(cfg.server["port"]),
                log_level="info", access_log=False, ws_ping_interval=20,
                ws_ping_timeout=20, proxy_headers=bool(cfg.server.get("behind_proxy")),
                forwarded_allow_ips="*" if cfg.server.get("behind_proxy") else None)


if __name__ == "__main__":
    main()
